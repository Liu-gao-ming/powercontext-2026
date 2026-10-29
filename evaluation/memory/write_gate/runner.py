# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Evaluate exact Memory write inputs without changing authoritative state."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from time import perf_counter

from powercontext.builtin.artifacts.memory.protocols import MemoryWriteGateRequest, MemoryWriteVerdict
from powercontext.builtin.decision_observations import (
    DecisionCoverage,
    DecisionObservation,
    DecisionPolicyMode,
    DecisionPrivacyBoundary,
)
from powercontext.builtin.inference import InferenceUsage
from powercontext.builtin.runtime.decision_model import (
    DecisionModel,
    DecisionOutcome,
    DecisionRequest,
    DecisionResult,
    FailOpenDecisionModel,
)
from powercontext.builtin.runtime.memory_write_gate import DecisionMemoryWriteGate, memory_write_policy

from .dataset import Case, Dataset, Label
from .prepare_public import ARCHIVE_SHA256
from .resolver import CaseResolver, MissingInput, PublicDatasetResolver, Unauthorized, UnsupportedReference

THRESHOLDS: tuple[float | None, ...] = (None, 0.5, 0.7, 0.9)
HOLD_DIRECTIONS = (DecisionOutcome.YES, DecisionOutcome.NO)


class EvaluationStatus(StrEnum):
    EVALUATED = "evaluated"
    MISSING_INPUT = "missing_input"
    UNAUTHORIZED = "unauthorized"
    UNSUPPORTED_REFERENCE = "unsupported_reference"
    EVALUATION_FALLBACK = "evaluation_fallback"
    NOT_ATTEMPTED = "not_attempted"


@dataclass(slots=True)
class _Collector:
    observation: DecisionObservation | None = None

    async def record(self, observation: DecisionObservation, /) -> None:
        self.observation = observation


class _CapturingModel:
    def __init__(self, delegate: DecisionModel) -> None:
        self._delegate = FailOpenDecisionModel(delegate)
        self.policy_id = self._delegate.policy_id
        self.is_local_only = bool(getattr(self._delegate, "is_local_only", False))
        self.provider_id = getattr(self._delegate, "provider_id", None)
        self.backend_model_id = getattr(self._delegate, "backend_model_id", None)
        self.result: DecisionResult | None = None

    async def evaluate(self, request: DecisionRequest, /) -> DecisionResult:
        self.result = await self._delegate.evaluate(request)
        return self.result


@dataclass(frozen=True, slots=True)
class CaseResult:
    case_id: str
    group_id: str
    language: str
    label: Label
    status: EvaluationStatus
    original_label: str | None = None
    coverage: DecisionCoverage | None = None
    outcome: DecisionOutcome | None = None
    confidence: float | None = None
    used_fallback: bool = False
    fallback_reason: str | None = None
    latency_ms: float | None = None
    requests: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    currency_cost_usd: float | None = None
    actions: dict[str, MemoryWriteVerdict] | None = None


def configuration_key(hold_on: DecisionOutcome, threshold: float | None) -> str:
    return f"hold_on={hold_on.value};threshold={'none' if threshold is None else format(threshold, '.1f')}"


async def evaluate_dataset(
    dataset: Dataset,
    resolver: CaseResolver,
    *,
    model: DecisionModel | None = None,
) -> tuple[CaseResult, ...]:
    """Run each case once and apply candidate policies to its ephemeral model result."""

    return tuple([await _evaluate_case(case, resolver, model=model) for case in dataset.cases])


async def evaluate_public_dataset(
    dataset: Dataset,
    resolver: PublicDatasetResolver,
    *,
    model: DecisionModel,
    on_result: Callable[[CaseResult], None] | None = None,
) -> tuple[CaseResult, ...]:
    """Call a hosted model only on pinned public data, then rescore its answer locally.

    This measures a candidate backend; it does not exercise hosted Runtime egress.
    Private references and arbitrary archives cannot enter this path.
    """

    if (
        not isinstance(resolver, PublicDatasetResolver)
        or resolver.archive_sha256 != ARCHIVE_SHA256
        or dataset.public_source is None
        or dataset.public_source.archive_sha256 != ARCHIVE_SHA256
    ):
        raise ValueError("hosted evaluation requires the pinned official public archive and manifests")  # noqa: TRY003
    results = []
    stop_reason = None
    for case in dataset.cases:
        if stop_reason:
            result = replace(_incomplete(case, EvaluationStatus.NOT_ATTEMPTED), fallback_reason=stop_reason)
        else:
            result = await _evaluate_case(case, resolver, model=None, public_model=model)
            if result.fallback_reason in {"insufficient_credit", "credential_rejected", "rate_limited"}:
                stop_reason = result.fallback_reason
        results.append(result)
        if on_result is not None:
            on_result(result)
    return tuple(results)


async def _evaluate_case(
    case: Case,
    resolver: CaseResolver,
    *,
    model: DecisionModel | None,
    public_model: DecisionModel | None = None,
) -> CaseResult:
    try:
        resolved = await resolver.resolve(case)
    except MissingInput:
        return _incomplete(case, EvaluationStatus.MISSING_INPUT)
    except Unauthorized:
        return _incomplete(case, EvaluationStatus.UNAUTHORIZED)
    except UnsupportedReference:
        return _incomplete(case, EvaluationStatus.UNSUPPORTED_REFERENCE)

    chosen_model = model
    request_capture = _RequestCapture() if public_model is not None else None
    if request_capture is not None:
        chosen_model = request_capture
    capturing = None if chosen_model is None else _CapturingModel(chosen_model)
    privacy = DecisionPrivacyBoundary.LOCAL_ONLY if capturing is not None else DecisionPrivacyBoundary.NO_EXTERNAL_CALL
    collector = _Collector()
    request = MemoryWriteGateRequest(
        candidates=(resolved.candidate,),
        evidence=resolved.evidence,
        scope_id=case.scope_id,
        operation_id=f"offline-evaluation:{case.case_id}",
        observation_sink=collector,
        defer_observation_emission=True,
    )
    gate = DecisionMemoryWriteGate(
        capturing,
        hold_on=DecisionOutcome.YES,
        policy=memory_write_policy(mode=DecisionPolicyMode.ENFORCING, privacy_boundary=privacy),
    )
    await gate.assess(request)
    public_latency_ms = None
    public_result = None
    if request_capture is not None and request_capture.request is not None and public_model is not None:
        started = perf_counter()
        public_result = await FailOpenDecisionModel(public_model).evaluate(request_capture.request)
        public_latency_ms = (perf_counter() - started) * 1000
        # Only an already obtained answer enters the gate. No hosted model is
        # marked local or invoked by the Runtime's local_only path.
        capturing = _CapturingModel(_ResultModel(public_result))
        await DecisionMemoryWriteGate(capturing).assess(request)
    observation = collector.observation
    if observation is None:
        raise RuntimeError("write gate produced no observation")  # noqa: TRY003
    assessment = observation.assessment
    status = (
        EvaluationStatus.EVALUATED
        if assessment.coverage is DecisionCoverage.ADJUDICATED
        else EvaluationStatus.EVALUATION_FALLBACK
    )
    result = None if capturing is None else capturing.result
    actions: dict[str, MemoryWriteVerdict] = {}
    rescore_request = replace(request, observation_sink=None)
    for hold_on in HOLD_DIRECTIONS:
        for threshold in THRESHOLDS:
            mapped = DecisionMemoryWriteGate(
                _ResultModel(result) if result is not None else None,
                hold_on=hold_on,
                threshold=threshold,
                policy=memory_write_policy(
                    hold_on=hold_on,
                    threshold=threshold,
                    mode=DecisionPolicyMode.ENFORCING,
                    privacy_boundary=DecisionPrivacyBoundary.LOCAL_ONLY
                    if result is not None
                    else DecisionPrivacyBoundary.NO_EXTERNAL_CALL,
                ),
            )
            mapped_assessment = await mapped.assess(rescore_request)
            actions[configuration_key(hold_on, threshold)] = mapped_assessment.verdict
    return CaseResult(
        case_id=case.case_id,
        group_id=case.group_id,
        language=case.language,
        label=case.label,
        status=status,
        original_label=case.original_label,
        coverage=assessment.coverage,
        outcome=None if result is None else result.outcome,
        confidence=None if result is None else result.confidence,
        used_fallback=assessment.used_fallback,
        fallback_reason=(getattr(public_model, "failure_reason", None) if public_result is not None else None)
        or observation.fallback_reason
        or (assessment.reason if status is EvaluationStatus.EVALUATION_FALLBACK else None),
        latency_ms=public_latency_ms if public_latency_ms is not None else assessment.latency_ms,
        requests=assessment.usage.requests,
        input_tokens=assessment.usage.input_tokens,
        output_tokens=assessment.usage.output_tokens,
        currency_cost_usd=getattr(public_model, "last_cost_usd", None) if public_result is not None else None,
        actions=actions,
    )


class _RequestCapture:
    """Capture the gate's exact question/projection without performing inference.

    Runtime preflight can reject an input without calling this local recorder.
    Its placeholder abstention is never scored as a hosted model response.
    """

    policy_id = "evaluation.capture.v1"
    is_local_only = True

    def __init__(self) -> None:
        self.request: DecisionRequest | None = None

    async def evaluate(self, request: DecisionRequest, /) -> DecisionResult:
        self.request = request
        return DecisionResult(
            outcome=DecisionOutcome.ABSTAIN,
            policy_id=self.policy_id,
            usage=InferenceUsage(requests=0),
            used_fallback=True,
        )


class _ResultModel:
    policy_id = "evaluation.rescore.v1"
    is_local_only = True

    def __init__(self, result: DecisionResult) -> None:
        self._result = result

    async def evaluate(self, request: DecisionRequest, /) -> DecisionResult:
        return self._result


def _incomplete(case: Case, status: EvaluationStatus) -> CaseResult:
    return CaseResult(
        case_id=case.case_id,
        group_id=case.group_id,
        language=case.language,
        label=case.label,
        status=status,
        original_label=case.original_label,
    )
