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

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import pytest

from powercontext.builtin.inference import InferenceUsage
from powercontext.builtin.runtime.decision_model import (
    DecisionKind,
    DecisionOutcome,
    DecisionRequest,
    DecisionResult,
    FailOpenDecisionModel,
)
from powercontext.builtin.runtime.decision_policy import (
    DecisionCoverage,
    DecisionObservation,
    DecisionPolicyMode,
    DecisionPrivacyBoundary,
    DecisionVerdict,
)
from powercontext.builtin.runtime.decision_projection import (
    DecisionRequestProjection,
    KnownSecretDecisionRequestSanitizer,
)
from powercontext.builtin.runtime.memory_write_gate import (
    DecisionMemoryWriteGate,
    MemoryWriteGateRequest,
    MemoryWriteRejectionCode,
    MemoryWriteVerdict,
    _assess_memory_write_decision,
    _candidate_subject,
    build_memory_write_gate,
    memory_write_policy,
)

# A known-answer pair used to orient the hold direction. A durable preference should pass an
# evidence check; filler should not. The direction must be established from this pair before the
# gate is enabled, so a backend with the opposite polarity is caught instead of silently holding
# the wrong writes.
_PROBE_PREFERENCE = "Always run the full test suite before committing."
_PROBE_FILLER = "ok sounds good sure"


class _StaticDecisionModel:
    """A backend that always returns one prepared verdict."""

    policy_id = "powercontext.decision.static.v1"
    is_local_only = True

    def __init__(self, result: DecisionResult) -> None:
        self._result = result

    async def evaluate(self, request: DecisionRequest, /) -> DecisionResult:
        return self._result


class _RecordingDecisionModel(_StaticDecisionModel):
    def __init__(self, result: DecisionResult) -> None:
        super().__init__(result)
        self.requests: list[DecisionRequest] = []

    async def evaluate(self, request: DecisionRequest, /) -> DecisionResult:
        self.requests.append(request)
        return await super().evaluate(request)


class _RemoteRecordingDecisionModel(_RecordingDecisionModel):
    is_local_only = False


class _IdentifiedDecisionModel(_StaticDecisionModel):
    provider_id = "test-provider"
    backend_model_id = "test-model"


class _FailingDecisionModel:
    """A backend whose every evaluation raises."""

    policy_id = "powercontext.decision.failing.v1"
    is_local_only = True

    async def evaluate(self, request: DecisionRequest, /) -> DecisionResult:
        raise ValueError("backend unavailable")  # noqa: TRY003


class _RecordingObservationSink:
    """Collect the safe sidecar a gate offers to durable runtime storage."""

    def __init__(self) -> None:
        self.observations: list[DecisionObservation] = []

    async def record(self, observation: DecisionObservation, /) -> None:
        self.observations.append(observation)


class _PolarityBackend:
    """A backend that calls one known answer insufficient and the other sufficient."""

    policy_id = "powercontext.decision.polarity.v1"
    is_local_only = True

    def __init__(self, *, insufficient_for: frozenset[str]) -> None:
        self._insufficient_for = frozenset(_candidate_subject((value,)) for value in insufficient_for)

    async def evaluate(self, request: DecisionRequest, /) -> DecisionResult:
        outcome = DecisionOutcome.YES if request.subject in self._insufficient_for else DecisionOutcome.NO
        return DecisionResult(outcome, self.policy_id, InferenceUsage(requests=1))


def _verdict(
    outcome: DecisionOutcome,
    *,
    confidence: float | None = None,
    rationale: str | None = None,
    used_fallback: bool = False,
) -> DecisionResult:
    return DecisionResult(
        outcome,
        "powercontext.decision.static.v1",
        InferenceUsage(requests=1),
        rationale=rationale,
        confidence=confidence,
        used_fallback=used_fallback,
    )


def test_decision_kind_values_are_stable() -> None:
    assert DecisionKind.MEMORY_WRITE_GATE.value == "memory.write-gate"


def test_gate_vocabulary_is_complete() -> None:
    assert {verdict.value for verdict in MemoryWriteVerdict} == {"accept", "flag", "hold"}
    assert {code.value for code in MemoryWriteRejectionCode} == {
        "needs_evidence",
        "evidence_limit_exceeded",
        "insufficient_coverage",
    }


def test_a_supporting_verdict_accepts_the_write() -> None:
    async def scenario() -> None:
        gate = DecisionMemoryWriteGate(_StaticDecisionModel(_verdict(DecisionOutcome.NO)), hold_on=DecisionOutcome.YES)

        assessment = await gate.assess(_request(evidence=("source:task:1",)))

        assert assessment.verdict is MemoryWriteVerdict.ACCEPT
        assert assessment.code is None
        assert assessment.reason is None

    asyncio.run(scenario())


def test_the_hold_direction_is_read_from_configuration() -> None:
    async def scenario() -> None:
        gate = DecisionMemoryWriteGate(_StaticDecisionModel(_verdict(DecisionOutcome.NO)), hold_on=DecisionOutcome.NO)

        assessment = await gate.assess(_request(evidence=("source:task:1",)))

        assert assessment.verdict is MemoryWriteVerdict.HOLD

    asyncio.run(scenario())


def test_direct_gate_binds_its_effective_hold_direction_into_the_observation_version() -> None:
    async def scenario() -> None:
        sink = _RecordingObservationSink()
        gate = DecisionMemoryWriteGate(_StaticDecisionModel(_verdict(DecisionOutcome.YES)), hold_on=DecisionOutcome.NO)

        await gate.assess(_request(evidence=("source:task:1",), observation_sink=sink))

        assert sink.observations[0].policy_version == "1;hold_on=no;threshold=none"

    asyncio.run(scenario())


def test_direct_gate_rejects_a_disabled_or_conflicting_policy() -> None:
    disabled = memory_write_policy(mode=DecisionPolicyMode.DISABLED)
    with pytest.raises(ValueError, match="disabled policy"):
        DecisionMemoryWriteGate(_StaticDecisionModel(_verdict(DecisionOutcome.YES)), policy=disabled)

    policy = memory_write_policy(hold_on=DecisionOutcome.NO)
    with pytest.raises(ValueError, match="hold direction"):
        DecisionMemoryWriteGate(
            _StaticDecisionModel(_verdict(DecisionOutcome.YES)),
            hold_on=DecisionOutcome.YES,
            policy=policy,
        )

    threshold_policy = memory_write_policy(threshold=0.4)
    with pytest.raises(ValueError, match="operational parameters"):
        DecisionMemoryWriteGate(_StaticDecisionModel(_verdict(DecisionOutcome.YES)), policy=threshold_policy)


def test_direct_gate_rejects_policy_fields_it_does_not_execute() -> None:
    model = _StaticDecisionModel(_verdict(DecisionOutcome.YES))
    question_policy = memory_write_policy().model_copy(update={"question": "Does this unrelated question hold?"})
    with pytest.raises(ValueError, match="question"):
        DecisionMemoryWriteGate(model, policy=question_policy)

    mapping_policy = memory_write_policy().model_copy(
        update={
            "outcome_mapping": {
                DecisionOutcome.YES.value: DecisionVerdict.DENY.value,
                DecisionOutcome.NO.value: "review",
                DecisionOutcome.ABSTAIN.value: DecisionVerdict.UNKNOWN.value,
            }
        }
    )
    with pytest.raises(ValueError, match="outcome mapping"):
        DecisionMemoryWriteGate(model, policy=mapping_policy)


def test_observation_records_backend_identity_and_evaluation_latency() -> None:
    async def scenario() -> None:
        sink = _RecordingObservationSink()
        gate = DecisionMemoryWriteGate(_IdentifiedDecisionModel(_verdict(DecisionOutcome.NO)))

        await gate.assess(_request(evidence=("source:task:1",), observation_sink=sink))

        observation = sink.observations[0]
        assert observation.provider_id == "test-provider"
        assert observation.backend_model_id == "test-model"
        assert observation.assessment.latency_ms is not None
        assert observation.assessment.latency_ms >= 0

    asyncio.run(scenario())


def test_a_hold_carries_a_structured_code_and_reason() -> None:
    async def scenario() -> None:
        gate = DecisionMemoryWriteGate(
            _StaticDecisionModel(_verdict(DecisionOutcome.YES, rationale="the citation is thin")),
            hold_on=DecisionOutcome.YES,
        )

        assessment = await gate.assess(_request(evidence=("source:task:1",)))

        assert assessment.verdict is MemoryWriteVerdict.HOLD
        assert assessment.code is MemoryWriteRejectionCode.INSUFFICIENT_COVERAGE
        assert assessment.reason == "the citation is thin"

    asyncio.run(scenario())


def test_a_hold_without_citations_reports_needs_evidence() -> None:
    async def scenario() -> None:
        gate = DecisionMemoryWriteGate(_StaticDecisionModel(_verdict(DecisionOutcome.YES)), hold_on=DecisionOutcome.YES)

        assessment = await gate.assess(_request(evidence=()))

        assert assessment.verdict is MemoryWriteVerdict.HOLD
        assert assessment.code is MemoryWriteRejectionCode.NEEDS_EVIDENCE
        assert assessment.reason is not None

    asyncio.run(scenario())


def test_a_hold_beyond_the_evidence_ceiling_reports_limit_exceeded() -> None:
    async def scenario() -> None:
        gate = DecisionMemoryWriteGate(_StaticDecisionModel(_verdict(DecisionOutcome.YES)), hold_on=DecisionOutcome.YES)

        assessment = await gate.assess(_request(evidence=tuple(f"source:task:{index}" for index in range(40))))

        assert assessment.verdict is MemoryWriteVerdict.HOLD
        assert assessment.code is MemoryWriteRejectionCode.EVIDENCE_LIMIT_EXCEEDED

    asyncio.run(scenario())


def test_an_oversized_candidate_batch_is_held_before_backend_assessment() -> None:
    async def scenario() -> None:
        backend = _RecordingDecisionModel(_verdict(DecisionOutcome.NO))
        gate = DecisionMemoryWriteGate(backend, hold_on=DecisionOutcome.YES)

        assessment = await gate.assess(
            _request(
                candidates=("x" * 4000, "UNASSESSED_TAIL"),
                evidence=("source:task:1\nsupporting text",),
            )
        )

        assert assessment.verdict is MemoryWriteVerdict.HOLD
        assert assessment.code is MemoryWriteRejectionCode.INSUFFICIENT_COVERAGE
        assert assessment.reason == "the candidate batch exceeds the gate assessment budget"
        assert backend.requests == []

    asyncio.run(scenario())


def test_shadow_mode_passes_an_oversized_candidate_batch_without_calling_the_backend() -> None:
    async def scenario() -> None:
        backend = _RecordingDecisionModel(_verdict(DecisionOutcome.YES))
        gate = DecisionMemoryWriteGate(
            backend,
            policy=memory_write_policy(mode=DecisionPolicyMode.SHADOW),
        )

        assessment = await gate.assess(
            _request(candidates=("x" * 4000, "UNASSESSED_TAIL"), evidence=("source:task:1",))
        )

        assert assessment.verdict is MemoryWriteVerdict.ACCEPT
        assert assessment.used_fallback is False
        assert backend.requests == []

    asyncio.run(scenario())


def test_advisory_mode_flags_an_insufficient_write_without_holding_it() -> None:
    async def scenario() -> None:
        gate = DecisionMemoryWriteGate(
            _StaticDecisionModel(_verdict(DecisionOutcome.YES, rationale="evidence is insufficient")),
            policy=memory_write_policy(mode=DecisionPolicyMode.ADVISORY),
        )

        assessment = await gate.assess(_request(evidence=("source:task:1",)))

        assert assessment.verdict is MemoryWriteVerdict.FLAG
        assert assessment.reason == "evidence is insufficient"
        assert assessment.code is None

    asyncio.run(scenario())


def test_a_low_confidence_hold_is_written_but_flagged() -> None:
    async def scenario() -> None:
        gate = DecisionMemoryWriteGate(
            _StaticDecisionModel(_verdict(DecisionOutcome.YES, confidence=0.2, rationale="uncertain")),
            hold_on=DecisionOutcome.YES,
            threshold=0.5,
        )

        assessment = await gate.assess(_request(evidence=("source:task:1",)))

        assert assessment.verdict is MemoryWriteVerdict.FLAG
        assert assessment.code is None
        assert assessment.reason == "uncertain"

    asyncio.run(scenario())


def test_a_confident_hold_still_holds_with_a_threshold() -> None:
    async def scenario() -> None:
        gate = DecisionMemoryWriteGate(
            _StaticDecisionModel(_verdict(DecisionOutcome.YES, confidence=0.9)),
            hold_on=DecisionOutcome.YES,
            threshold=0.5,
        )

        assessment = await gate.assess(_request(evidence=("source:task:1",)))

        assert assessment.verdict is MemoryWriteVerdict.HOLD

    asyncio.run(scenario())


def test_a_deliberate_abstention_passes_the_write_through() -> None:
    async def scenario() -> None:
        gate = DecisionMemoryWriteGate(
            _StaticDecisionModel(_verdict(DecisionOutcome.ABSTAIN)), hold_on=DecisionOutcome.YES
        )

        assessment = await gate.assess(_request(evidence=("source:task:1",)))

        assert assessment.verdict is MemoryWriteVerdict.ACCEPT
        assert assessment.used_fallback is False

    asyncio.run(scenario())


def test_a_failing_backend_is_fail_open() -> None:
    async def scenario() -> None:
        gate = DecisionMemoryWriteGate(FailOpenDecisionModel(_FailingDecisionModel()), hold_on=DecisionOutcome.YES)

        assessment = await gate.assess(_request(evidence=("source:task:1",)))

        assert assessment.verdict is MemoryWriteVerdict.ACCEPT
        assert assessment.code is None
        assert assessment.reason is None
        assert assessment.used_fallback is True

    asyncio.run(scenario())


def test_memory_write_decision_policy_keeps_fallback_unadjudicated() -> None:
    assessment = _assess_memory_write_decision(
        _verdict(DecisionOutcome.ABSTAIN, used_fallback=True),
        hold_on=DecisionOutcome.YES,
    )

    assert assessment.coverage is DecisionCoverage.UNADJUDICATED
    assert assessment.verdict is DecisionVerdict.UNKNOWN
    assert assessment.used_fallback is True


def test_memory_write_decision_policy_uses_configured_hold_direction() -> None:
    yes_holds = _assess_memory_write_decision(_verdict(DecisionOutcome.YES), hold_on=DecisionOutcome.YES)
    no_holds = _assess_memory_write_decision(_verdict(DecisionOutcome.YES), hold_on=DecisionOutcome.NO)

    assert yes_holds.coverage is DecisionCoverage.ADJUDICATED
    assert yes_holds.verdict is DecisionVerdict.DENY
    assert no_holds.coverage is DecisionCoverage.ADJUDICATED
    assert no_holds.verdict is DecisionVerdict.ALLOW


def test_gate_emits_policy_observation_without_raw_subject_or_evidence(caplog: pytest.LogCaptureFixture) -> None:
    async def scenario() -> None:
        gate = DecisionMemoryWriteGate(
            _StaticDecisionModel(_verdict(DecisionOutcome.YES)),
            policy=memory_write_policy(mode=DecisionPolicyMode.SHADOW),
        )

        assessment = await gate.assess(_request(candidates=("secret claim",), evidence=("secret evidence",)))

        assert assessment.verdict is MemoryWriteVerdict.ACCEPT

    caplog.set_level(logging.INFO, logger="powercontext.builtin.runtime.decision_policy")
    asyncio.run(scenario())

    record = next(record for record in caplog.records if getattr(record, "event", None) == "decision.observation")
    assert record.__dict__["policy_id"] == "memory.write.evidence_sufficiency.v1"
    assert record.__dict__["mode"] == "shadow"
    assert record.__dict__["privacy_boundary"] == "local_only"
    assert record.__dict__["privacy_outcome"] == "local_model_called"
    assert record.__dict__["coverage"] == "adjudicated"
    assert "secret claim" not in str(record.__dict__)
    assert "secret evidence" not in str(record.__dict__)


def test_gate_offers_a_bounded_observation_to_the_request_sink() -> None:
    async def scenario() -> None:
        sink = _RecordingObservationSink()
        gate = DecisionMemoryWriteGate(_StaticDecisionModel(_verdict(DecisionOutcome.YES)))

        assessment = await gate.assess(
            MemoryWriteGateRequest(
                candidates=("secret claim",),
                evidence=("secret evidence",),
                expected_revision=1,
                scope_id="scope-a",
                operation_id="memory-write:memory-a@2",
                subject_refs=("entry:entry-a@version-a",),
                evidence_refs=("source:task:1",),
                observation_sink=sink,
            )
        )

        assert assessment.verdict is MemoryWriteVerdict.HOLD
        assert len(sink.observations) == 1
        observation = sink.observations[0]
        assert observation.scope_id == "scope-a"
        assert observation.operation_id == "memory-write:memory-a@2"
        assert observation.subject_refs == ("entry:entry-a@version-a",)
        assert observation.evidence_refs == ("source:task:1",)
        assert observation.final_action == "memory_write_hold"
        assert observation.privacy_outcome == "local_model_called"
        assert "secret claim" not in observation.model_dump_json()
        assert "secret evidence" not in observation.model_dump_json()

    asyncio.run(scenario())


def test_a_failing_observation_sink_keeps_the_gate_verdict_and_logs_only_its_type(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _FailingObservationSink:
        async def record(self, observation: DecisionObservation, /) -> None:
            raise RuntimeError("secret sink failure detail")  # noqa: TRY003

    async def scenario() -> None:
        gate = DecisionMemoryWriteGate(_StaticDecisionModel(_verdict(DecisionOutcome.YES)))

        assessment = await gate.assess(
            MemoryWriteGateRequest(
                candidates=("secret claim",),
                evidence=("secret evidence",),
                expected_revision=1,
                scope_id="scope-a",
                operation_id="memory-write:memory-a@2",
                observation_sink=_FailingObservationSink(),
            )
        )

        assert assessment.verdict is MemoryWriteVerdict.HOLD

    caplog.set_level(logging.WARNING, logger="powercontext.builtin.runtime.memory_write_gate")
    asyncio.run(scenario())

    record = next(
        record for record in caplog.records if getattr(record, "event", None) == "decision.observation_sink_failed"
    )
    assert record.__dict__["error_type"] == "RuntimeError"
    assert "secret sink failure detail" not in str(record.__dict__)


def test_the_hold_direction_cannot_be_abstain() -> None:
    with pytest.raises(ValueError, match="hold direction"):
        DecisionMemoryWriteGate(_StaticDecisionModel(_verdict(DecisionOutcome.NO)), hold_on=DecisionOutcome.ABSTAIN)


def test_noul_polarity_probe_pairs_a_known_answer_with_the_direction() -> None:
    async def scenario() -> None:
        backend = _PolarityBackend(insufficient_for=frozenset({_PROBE_FILLER}))
        gate = DecisionMemoryWriteGate(backend, hold_on=DecisionOutcome.YES)

        preference = await gate.assess(_request(candidates=(_PROBE_PREFERENCE,)))
        filler = await gate.assess(_request(candidates=(_PROBE_FILLER,)))

        assert preference.verdict is MemoryWriteVerdict.ACCEPT
        assert filler.verdict is MemoryWriteVerdict.HOLD

    asyncio.run(scenario())


def test_noul_polarity_probe_exposes_a_contradictory_backend() -> None:
    async def scenario() -> None:
        # The same pair with the opposite polarity: the probe must fail rather than let a
        # reversed backend enable the gate against real preferences.
        backend = _PolarityBackend(insufficient_for=frozenset({_PROBE_PREFERENCE}))
        gate = DecisionMemoryWriteGate(backend, hold_on=DecisionOutcome.YES)

        preference = await gate.assess(_request(candidates=(_PROBE_PREFERENCE,)))

        assert preference.verdict is MemoryWriteVerdict.HOLD

    asyncio.run(scenario())


def test_candidate_subject_preserves_candidate_boundaries() -> None:
    valid_batch = _candidate_subject(("Alpha uses MySQL.\nBeta uses PostgreSQL.", "Gamma uses Redis."))
    crossed_batch = _candidate_subject(("Alpha uses MySQL.", "Beta uses PostgreSQL.\nGamma uses Redis."))

    assert valid_batch != crossed_batch
    assert '"candidate":1' in valid_batch
    assert '"candidate":2' in valid_batch


def test_the_gate_stays_disabled_without_an_enabled_flag() -> None:
    assert build_memory_write_gate(_StaticDecisionModel(_verdict(DecisionOutcome.YES)), enabled=False) is None


def test_the_gate_stays_disabled_without_a_backend() -> None:
    assert build_memory_write_gate(None, enabled=True) is None


def test_the_gate_is_built_from_configuration() -> None:
    gate = build_memory_write_gate(
        _StaticDecisionModel(_verdict(DecisionOutcome.NO)),
        enabled=True,
        hold_on="no",
        threshold=0.4,
        mode=DecisionPolicyMode.SHADOW,
        privacy_boundary=DecisionPrivacyBoundary.LOCAL_ONLY,
    )

    assert isinstance(gate, DecisionMemoryWriteGate)
    assert gate.policy_id == "powercontext.decision.static.v1"
    assert gate.mode is DecisionPolicyMode.SHADOW


def test_new_memory_write_policies_default_to_shadow_mode() -> None:
    assert memory_write_policy().mode is DecisionPolicyMode.SHADOW


def test_memory_write_policy_version_binds_the_hold_direction_and_threshold() -> None:
    default = memory_write_policy(hold_on=DecisionOutcome.YES, threshold=None)
    different_direction = memory_write_policy(hold_on=DecisionOutcome.NO, threshold=None)
    different_threshold = memory_write_policy(hold_on=DecisionOutcome.YES, threshold=0.4)

    assert len({default.version, different_direction.version, different_threshold.version}) == 3


def test_disabled_mode_does_not_construct_a_memory_write_gate() -> None:
    gate = build_memory_write_gate(
        _StaticDecisionModel(_verdict(DecisionOutcome.YES)),
        enabled=True,
        mode=DecisionPolicyMode.DISABLED,
        privacy_boundary=DecisionPrivacyBoundary.LOCAL_ONLY,
    )

    assert gate is None


def test_no_external_call_boundary_skips_the_backend_and_emits_an_unadjudicated_observation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        backend = _RecordingDecisionModel(_verdict(DecisionOutcome.YES))
        gate = build_memory_write_gate(
            backend,
            enabled=True,
            privacy_boundary=DecisionPrivacyBoundary.NO_EXTERNAL_CALL,
        )

        assert isinstance(gate, DecisionMemoryWriteGate)
        assessment = await gate.assess(_request(evidence=("source:task:1",)))

        assert assessment.verdict is MemoryWriteVerdict.ACCEPT
        assert assessment.used_fallback is True
        assert backend.requests == []

    caplog.set_level(logging.INFO, logger="powercontext.builtin.runtime.decision_policy")
    asyncio.run(scenario())

    record = next(record for record in caplog.records if getattr(record, "event", None) == "decision.observation")
    assert record.__dict__["coverage"] == "unadjudicated"
    assert record.__dict__["verdict"] == "unknown"
    assert record.__dict__["fallback_reason"] == "privacy_boundary"


def test_direct_gate_construction_honors_the_no_external_call_boundary() -> None:
    async def scenario() -> None:
        backend = _RecordingDecisionModel(_verdict(DecisionOutcome.YES))
        gate = DecisionMemoryWriteGate(
            backend,
            policy=memory_write_policy(privacy_boundary=DecisionPrivacyBoundary.NO_EXTERNAL_CALL),
        )

        assessment = await gate.assess(_request(evidence=("source:task:1",)))

        assert assessment.verdict is MemoryWriteVerdict.ACCEPT
        assert assessment.used_fallback is True
        assert backend.requests == []

    asyncio.run(scenario())


def test_local_only_boundary_treats_an_explicit_non_local_model_as_remote() -> None:
    class _RemoteDecisionModel(_RecordingDecisionModel):
        is_local_only = False

    async def scenario() -> None:
        backend = _RemoteDecisionModel(_verdict(DecisionOutcome.YES))
        gate = build_memory_write_gate(
            backend,
            enabled=True,
            privacy_boundary=DecisionPrivacyBoundary.LOCAL_ONLY,
        )

        assert isinstance(gate, DecisionMemoryWriteGate)
        assessment = await gate.assess(_request(evidence=("source:task:1",)))

        assert assessment.verdict is MemoryWriteVerdict.ACCEPT
        assert assessment.used_fallback is True
        assert backend.requests == []

    asyncio.run(scenario())


def test_local_only_boundary_treats_an_unattested_model_as_remote() -> None:
    class _UnattestedDecisionModel:
        policy_id = "powercontext.decision.unattested.v1"

        def __init__(self) -> None:
            self.requests: list[DecisionRequest] = []

        async def evaluate(self, request: DecisionRequest, /) -> DecisionResult:
            self.requests.append(request)
            return _verdict(DecisionOutcome.YES)

    async def scenario() -> None:
        backend = _UnattestedDecisionModel()
        gate = build_memory_write_gate(backend, enabled=True, privacy_boundary=DecisionPrivacyBoundary.LOCAL_ONLY)

        assert isinstance(gate, DecisionMemoryWriteGate)
        assessment = await gate.assess(_request(evidence=("source:task:1",)))

        assert assessment.verdict is MemoryWriteVerdict.ACCEPT
        assert assessment.used_fallback is True
        assert backend.requests == []

    asyncio.run(scenario())


def test_hosted_redacted_direct_gate_refuses_to_call_without_a_sanitizer() -> None:
    async def scenario() -> None:
        backend = _RemoteRecordingDecisionModel(_verdict(DecisionOutcome.YES))
        sink = _RecordingObservationSink()
        gate = DecisionMemoryWriteGate(
            backend,
            policy=memory_write_policy(privacy_boundary=DecisionPrivacyBoundary.HOSTED_REDACTED),
        )

        assessment = await gate.assess(_request(evidence=("source:task:1",), observation_sink=sink))

        assert assessment.verdict is MemoryWriteVerdict.ACCEPT
        assert assessment.used_fallback is True
        assert backend.requests == []
        assert sink.observations[0].fallback_reason == "projection_sanitizer_missing"
        assert sink.observations[0].privacy_outcome == "hosted_redacted_sanitizer_missing"

    asyncio.run(scenario())


def test_hosted_redacted_boundary_sends_only_the_permitted_projection(caplog: pytest.LogCaptureFixture) -> None:
    class _Sanitizer:
        def sanitize(self, request: DecisionRequest, /) -> DecisionRequestProjection:
            assert "RAW_SENTINEL" in request.subject
            assert request.evidence == ("RAW_EVIDENCE",)
            return DecisionRequestProjection.permitted(
                DecisionRequest(
                    request.decision_kind,
                    request.question,
                    "sanitized candidate",
                    ("sanitized evidence",),
                ),
                redaction_facts=("subject_redacted", "evidence_redacted"),
            )

    async def scenario() -> None:
        caplog.set_level(logging.INFO, logger="powercontext.builtin.runtime.decision_policy")
        backend = _RemoteRecordingDecisionModel(_verdict(DecisionOutcome.NO))
        sink = _RecordingObservationSink()
        gate = build_memory_write_gate(
            backend,
            enabled=True,
            privacy_boundary=DecisionPrivacyBoundary.HOSTED_REDACTED,
            decision_request_sanitizer=_Sanitizer(),
        )
        assert isinstance(gate, DecisionMemoryWriteGate)

        assessment = await gate.assess(
            _request(
                candidates=("RAW_SENTINEL",),
                evidence=("RAW_EVIDENCE",),
                observation_sink=sink,
            )
        )

        assert assessment.verdict is MemoryWriteVerdict.ACCEPT
        assert len(backend.requests) == 1
        assert backend.requests[0].subject == "sanitized candidate"
        assert backend.requests[0].evidence == ("sanitized evidence",)
        observation = sink.observations[0]
        assert observation.privacy_outcome == "hosted_redacted_model_called"
        assert observation.metadata["redaction_facts"] == ["subject_redacted", "evidence_redacted"]
        assert "RAW_SENTINEL" not in observation.model_dump_json()
        assert "RAW_EVIDENCE" not in observation.model_dump_json()
        record = next(record for record in caplog.records if getattr(record, "event", None) == "decision.observation")
        assert record.__dict__["privacy_outcome"] == "hosted_redacted_model_called"
        assert "RAW_SENTINEL" not in caplog.text
        assert "RAW_EVIDENCE" not in caplog.text

    asyncio.run(scenario())


def test_hosted_redacted_boundary_keeps_local_model_outcome_local() -> None:
    async def scenario() -> None:
        backend = _RecordingDecisionModel(_verdict(DecisionOutcome.NO))
        sink = _RecordingObservationSink()
        gate = build_memory_write_gate(
            backend,
            enabled=True,
            privacy_boundary=DecisionPrivacyBoundary.HOSTED_REDACTED,
        )

        assert isinstance(gate, DecisionMemoryWriteGate)
        await gate.assess(_request(evidence=("local evidence",), observation_sink=sink))

        assert sink.observations[0].privacy_outcome == "local_model_called"

    asyncio.run(scenario())


def test_default_projection_sanitizer_withholds_a_provider_prefixed_secret() -> None:
    sanitizer = KnownSecretDecisionRequestSanitizer()
    projection = sanitizer.sanitize(
        DecisionRequest(
            DecisionKind.MEMORY_WRITE_GATE.value,
            "Keep this?",
            "sk-provider-secret-1234567890",
        )
    )
    benign = sanitizer.sanitize(
        DecisionRequest(
            DecisionKind.MEMORY_WRITE_GATE.value,
            "Keep this?",
            "risk-assessment-planning",
        )
    )

    assert projection.is_permitted is False
    assert projection.reason_code == "projection_material_removed"
    assert benign.is_permitted is True
    assert benign.request is not None
    assert benign.request.subject == "risk-assessment-planning"


def test_hosted_redacted_sanitizes_candidate_values_before_json_encoding(monkeypatch) -> None:
    secret = 'synthetic-quote"line\nback\\slash-1987'  # noqa: S105 - synthetic redaction sentinel.
    monkeypatch.setenv("REVIEW_PASSWORD", secret)

    async def scenario() -> None:
        backend = _RemoteRecordingDecisionModel(_verdict(DecisionOutcome.NO))
        sink = _RecordingObservationSink()
        gate = build_memory_write_gate(
            backend,
            enabled=True,
            privacy_boundary=DecisionPrivacyBoundary.HOSTED_REDACTED,
            decision_request_sanitizer=KnownSecretDecisionRequestSanitizer(),
        )
        assert isinstance(gate, DecisionMemoryWriteGate)

        assessment = await gate.assess(
            _request(
                candidates=(f"Rotate {secret} before release.",),
                evidence=("The credential rotation policy is current.",),
                observation_sink=sink,
            )
        )

        assert assessment.used_fallback is False
        assert len(backend.requests) == 1
        projected = backend.requests[0]
        assert secret not in projected.subject
        assert json.loads(projected.subject) == [{"candidate": 1, "text": "Rotate [REDACTED] before release."}]
        assert sink.observations[0].metadata["redaction_facts"] == ["subject_redacted"]

    asyncio.run(scenario())


def test_hosted_redacted_sanitizes_source_evidence_values_before_json_encoding(monkeypatch) -> None:
    secret = 'synthetic-quote"line\nback\\slash-evidence-1987'  # noqa: S105 - synthetic redaction sentinel.
    monkeypatch.setenv("REVIEW_PASSWORD", secret)

    async def scenario() -> None:
        backend = _RemoteRecordingDecisionModel(_verdict(DecisionOutcome.NO))
        sink = _RecordingObservationSink()
        gate = build_memory_write_gate(
            backend,
            enabled=True,
            privacy_boundary=DecisionPrivacyBoundary.HOSTED_REDACTED,
            decision_request_sanitizer=KnownSecretDecisionRequestSanitizer(),
        )
        assert isinstance(gate, DecisionMemoryWriteGate)
        serialized_evidence = json.dumps(
            {"text": f"Python 3.11 is required. Password: {secret}"},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

        assessment = await gate.assess(
            _request(
                candidates=("The project uses Python 3.11.",),
                evidence=(serialized_evidence,),
                observation_sink=sink,
            )
        )

        assert assessment.used_fallback is False
        assert len(backend.requests) == 1
        projected = backend.requests[0]
        assert secret not in projected.evidence[0]
        assert json.loads(projected.evidence[0]) == {"text": "Python 3.11 is required. Password: [REDACTED]"}
        assert sink.observations[0].metadata["redaction_facts"] == ["evidence_redacted"]

    asyncio.run(scenario())


def test_hosted_redacted_withholds_when_source_evidence_values_are_entirely_redacted(monkeypatch) -> None:
    secret = 'synthetic-quote"line\nback\\slash-evidence-1987'  # noqa: S105 - synthetic redaction sentinel.
    monkeypatch.setenv("REVIEW_PASSWORD", secret)

    async def scenario() -> None:
        backend = _RemoteRecordingDecisionModel(_verdict(DecisionOutcome.NO))
        sink = _RecordingObservationSink()
        gate = build_memory_write_gate(
            backend,
            enabled=True,
            privacy_boundary=DecisionPrivacyBoundary.HOSTED_REDACTED,
            decision_request_sanitizer=KnownSecretDecisionRequestSanitizer(),
        )
        assert isinstance(gate, DecisionMemoryWriteGate)

        assessment = await gate.assess(
            _request(
                candidates=("The project uses Python 3.11.",),
                evidence=(json.dumps({"text": secret}, ensure_ascii=False, separators=(",", ":")),),
                observation_sink=sink,
            )
        )

        assert assessment.verdict is MemoryWriteVerdict.ACCEPT
        assert assessment.used_fallback is True
        assert backend.requests == []
        observation = sink.observations[0]
        assert observation.assessment.coverage is DecisionCoverage.UNADJUDICATED
        assert observation.fallback_reason == "projection_material_removed"
        assert observation.privacy_outcome == "hosted_redacted_withheld"

    asyncio.run(scenario())


def test_hosted_redacted_sanitizes_numeric_source_evidence_values(monkeypatch) -> None:
    secret = "87654321"  # noqa: S105 - synthetic redaction sentinel.
    monkeypatch.setenv("REVIEW_PASSWORD", secret)

    async def scenario() -> None:
        backend = _RemoteRecordingDecisionModel(_verdict(DecisionOutcome.NO))
        sink = _RecordingObservationSink()
        gate = build_memory_write_gate(
            backend,
            enabled=True,
            privacy_boundary=DecisionPrivacyBoundary.HOSTED_REDACTED,
            decision_request_sanitizer=KnownSecretDecisionRequestSanitizer(),
        )
        assert isinstance(gate, DecisionMemoryWriteGate)

        assessment = await gate.assess(
            _request(
                candidates=("The project uses Python 3.11.",),
                evidence=(
                    json.dumps(
                        {"text": "Python 3.11 is required.", "credential": int(secret)},
                        separators=(",", ":"),
                    ),
                ),
                observation_sink=sink,
            )
        )

        assert assessment.used_fallback is False
        assert len(backend.requests) == 1
        projected = backend.requests[0]
        assert secret not in projected.evidence[0]
        assert json.loads(projected.evidence[0]) == {
            "credential": "[REDACTED]",
            "text": "Python 3.11 is required.",
        }
        assert sink.observations[0].metadata["redaction_facts"] == ["evidence_redacted"]

    asyncio.run(scenario())


def test_hosted_redacted_withholds_plain_numeric_known_secret_evidence(monkeypatch) -> None:
    secret = "87654321"  # noqa: S105 - synthetic redaction sentinel.
    monkeypatch.setenv("REVIEW_PASSWORD", secret)

    async def scenario() -> None:
        backend = _RemoteRecordingDecisionModel(_verdict(DecisionOutcome.NO))
        sink = _RecordingObservationSink()
        gate = build_memory_write_gate(
            backend,
            enabled=True,
            privacy_boundary=DecisionPrivacyBoundary.HOSTED_REDACTED,
            decision_request_sanitizer=KnownSecretDecisionRequestSanitizer(),
        )
        assert isinstance(gate, DecisionMemoryWriteGate)

        assessment = await gate.assess(
            _request(
                candidates=("The project uses Python 3.11.",),
                evidence=(secret,),
                observation_sink=sink,
            )
        )

        assert assessment.verdict is MemoryWriteVerdict.ACCEPT
        assert assessment.used_fallback is True
        assert backend.requests == []
        observation = sink.observations[0]
        assert observation.assessment.coverage is DecisionCoverage.UNADJUDICATED
        assert observation.fallback_reason == "projection_material_removed"
        assert observation.privacy_outcome == "hosted_redacted_withheld"

    asyncio.run(scenario())


def test_hosted_redacted_sanitizes_the_original_numeric_evidence_representation(monkeypatch) -> None:
    secret = "1234567.50"  # noqa: S105 - synthetic redaction sentinel.
    monkeypatch.setenv("REVIEW_PASSWORD", secret)

    async def scenario() -> None:
        backend = _RemoteRecordingDecisionModel(_verdict(DecisionOutcome.NO))
        sink = _RecordingObservationSink()
        gate = build_memory_write_gate(
            backend,
            enabled=True,
            privacy_boundary=DecisionPrivacyBoundary.HOSTED_REDACTED,
            decision_request_sanitizer=KnownSecretDecisionRequestSanitizer(),
        )
        assert isinstance(gate, DecisionMemoryWriteGate)
        evidence = f'{{"text":"Safe material remains.","credential":{secret}}}'

        assessment = await gate.assess(
            _request(
                candidates=("The project records one measurement.",),
                evidence=(evidence,),
                observation_sink=sink,
            )
        )

        assert assessment.used_fallback is False
        assert len(backend.requests) == 1
        projected = backend.requests[0]
        assert secret not in projected.evidence[0]
        assert json.loads(projected.evidence[0]) == {
            "credential": "[REDACTED]",
            "text": "Safe material remains.",
        }
        assert sink.observations[0].metadata["redaction_facts"] == ["evidence_redacted"]

    asyncio.run(scenario())


def test_hosted_redacted_withholds_plain_known_secret_before_numeric_normalization(monkeypatch) -> None:
    secret = "1234567.50"  # noqa: S105 - synthetic redaction sentinel.
    monkeypatch.setenv("REVIEW_PASSWORD", secret)

    async def scenario() -> None:
        backend = _RemoteRecordingDecisionModel(_verdict(DecisionOutcome.NO))
        sink = _RecordingObservationSink()
        gate = build_memory_write_gate(
            backend,
            enabled=True,
            privacy_boundary=DecisionPrivacyBoundary.HOSTED_REDACTED,
            decision_request_sanitizer=KnownSecretDecisionRequestSanitizer(),
        )
        assert isinstance(gate, DecisionMemoryWriteGate)

        assessment = await gate.assess(
            _request(
                candidates=("The project records one measurement.",),
                evidence=(secret,),
                observation_sink=sink,
            )
        )

        assert assessment.verdict is MemoryWriteVerdict.ACCEPT
        assert assessment.used_fallback is True
        assert backend.requests == []
        observation = sink.observations[0]
        assert observation.assessment.coverage is DecisionCoverage.UNADJUDICATED
        assert observation.fallback_reason == "projection_material_removed"
        assert observation.privacy_outcome == "hosted_redacted_withheld"

    asyncio.run(scenario())


def test_hosted_redacted_withholds_when_json_decoding_hides_a_raw_known_secret(monkeypatch) -> None:
    secret = "1234567.50"  # noqa: S105 - synthetic redaction sentinel.
    monkeypatch.setenv("REVIEW_PASSWORD", secret)

    async def scenario() -> None:
        backend = _RemoteRecordingDecisionModel(_verdict(DecisionOutcome.NO))
        sink = _RecordingObservationSink()
        gate = build_memory_write_gate(
            backend,
            enabled=True,
            privacy_boundary=DecisionPrivacyBoundary.HOSTED_REDACTED,
            decision_request_sanitizer=KnownSecretDecisionRequestSanitizer(),
        )
        assert isinstance(gate, DecisionMemoryWriteGate)
        evidence = f'{{"a":{secret},"a":"safe"}}'

        assessment = await gate.assess(
            _request(
                candidates=("The project records one measurement.",),
                evidence=(evidence,),
                observation_sink=sink,
            )
        )

        assert assessment.verdict is MemoryWriteVerdict.ACCEPT
        assert assessment.used_fallback is True
        assert backend.requests == []
        observation = sink.observations[0]
        assert observation.assessment.coverage is DecisionCoverage.UNADJUDICATED
        assert observation.fallback_reason == "projection_material_removed"
        assert observation.privacy_outcome == "hosted_redacted_withheld"

    asyncio.run(scenario())


def test_hosted_redacted_withholds_when_duplicate_key_hides_an_escaped_known_secret(monkeypatch) -> None:
    secret = 'synthetic-quote"line\nback\\slash-1987'  # noqa: S105 - synthetic redaction sentinel.
    monkeypatch.setenv("REVIEW_PASSWORD", secret)

    async def scenario() -> None:
        backend = _RemoteRecordingDecisionModel(_verdict(DecisionOutcome.NO))
        sink = _RecordingObservationSink()
        gate = build_memory_write_gate(
            backend,
            enabled=True,
            privacy_boundary=DecisionPrivacyBoundary.HOSTED_REDACTED,
            decision_request_sanitizer=KnownSecretDecisionRequestSanitizer(),
        )
        assert isinstance(gate, DecisionMemoryWriteGate)
        evidence = f'{{"a":{json.dumps(secret)},"a":"safe"}}'
        assert secret not in evidence

        assessment = await gate.assess(
            _request(
                candidates=("The project records one measurement.",),
                evidence=(evidence,),
                observation_sink=sink,
            )
        )

        assert assessment.verdict is MemoryWriteVerdict.ACCEPT
        assert assessment.used_fallback is True
        assert backend.requests == []
        observation = sink.observations[0]
        assert observation.assessment.coverage is DecisionCoverage.UNADJUDICATED
        assert observation.fallback_reason == "projection_material_removed"
        assert observation.privacy_outcome == "hosted_redacted_withheld"

    asyncio.run(scenario())


@pytest.mark.parametrize("nonfinite", ["NaN", "Infinity", "-Infinity"])
def test_hosted_redacted_preserves_nonfinite_numeric_evidence_without_sanitizer_failure(nonfinite: str) -> None:
    async def scenario() -> None:
        backend = _RemoteRecordingDecisionModel(_verdict(DecisionOutcome.NO))
        sink = _RecordingObservationSink()
        gate = build_memory_write_gate(
            backend,
            enabled=True,
            privacy_boundary=DecisionPrivacyBoundary.HOSTED_REDACTED,
            decision_request_sanitizer=KnownSecretDecisionRequestSanitizer(),
        )
        assert isinstance(gate, DecisionMemoryWriteGate)
        evidence = f'{{"text":"Safe material remains.","measurement":{nonfinite}}}'

        assessment = await gate.assess(
            _request(
                candidates=("The measurement was recorded.",),
                evidence=(evidence,),
                observation_sink=sink,
            )
        )

        assert assessment.used_fallback is False
        assert len(backend.requests) == 1
        assert backend.requests[0].evidence == (evidence,)
        observation = sink.observations[0]
        assert observation.assessment.coverage is DecisionCoverage.ADJUDICATED
        assert observation.fallback_reason is None
        assert observation.metadata["redaction_facts"] == []
        assert observation.privacy_outcome == "hosted_redacted_model_called"

    asyncio.run(scenario())


def test_hosted_redacted_withholds_when_candidate_text_is_entirely_redacted() -> None:
    async def scenario() -> None:
        backend = _RemoteRecordingDecisionModel(_verdict(DecisionOutcome.NO))
        sink = _RecordingObservationSink()
        gate = build_memory_write_gate(
            backend,
            enabled=True,
            privacy_boundary=DecisionPrivacyBoundary.HOSTED_REDACTED,
            decision_request_sanitizer=KnownSecretDecisionRequestSanitizer(),
        )
        assert isinstance(gate, DecisionMemoryWriteGate)

        assessment = await gate.assess(
            _request(
                candidates=("sk-reviewsynthetic1987abcdef",),
                evidence=("Safe evidence remains available.",),
                observation_sink=sink,
            )
        )

        assert assessment.verdict is MemoryWriteVerdict.ACCEPT
        assert assessment.used_fallback is True
        assert backend.requests == []
        observation = sink.observations[0]
        assert observation.assessment.coverage is DecisionCoverage.UNADJUDICATED
        assert observation.assessment.used_fallback is True
        assert observation.fallback_reason == "projection_material_removed"
        assert observation.privacy_outcome == "hosted_redacted_withheld"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("result", "reason"),
    [
        (DecisionRequestProjection.withheld("projection_material_removed"), "projection_material_removed"),
        (object(), "projection_sanitizer_invalid"),
        (DecisionRequest(DecisionKind.MEMORY_WRITE_GATE.value, "Keep this?", "raw"), "projection_sanitizer_invalid"),
    ],
)
def test_hosted_redacted_projection_refusal_never_calls_the_backend(result: object, reason: str) -> None:
    class _Sanitizer:
        def sanitize(self, request: DecisionRequest, /) -> Any:
            return result

    async def scenario() -> None:
        backend = _RemoteRecordingDecisionModel(_verdict(DecisionOutcome.YES))
        sink = _RecordingObservationSink()
        gate = build_memory_write_gate(
            backend,
            enabled=True,
            privacy_boundary=DecisionPrivacyBoundary.HOSTED_REDACTED,
            decision_request_sanitizer=_Sanitizer(),
        )
        assert isinstance(gate, DecisionMemoryWriteGate)

        assessment = await gate.assess(_request(evidence=("evidence",), observation_sink=sink))

        assert assessment.verdict is MemoryWriteVerdict.ACCEPT
        assert assessment.used_fallback is True
        assert backend.requests == []
        observation = sink.observations[0]
        assert observation.assessment.coverage is DecisionCoverage.UNADJUDICATED
        assert observation.assessment.used_fallback is True
        assert observation.fallback_reason == reason
        assert observation.privacy_outcome in {
            "hosted_redacted_withheld",
            "hosted_redacted_sanitizer_invalid",
        }

    asyncio.run(scenario())


def test_hosted_redacted_sanitizer_failure_is_a_bounded_no_call() -> None:
    class _FailingSanitizer:
        def sanitize(self, request: DecisionRequest, /) -> DecisionRequestProjection:
            raise RuntimeError("RAW_SENTINEL sanitizer failure")  # noqa: TRY003

    async def scenario() -> None:
        backend = _RemoteRecordingDecisionModel(_verdict(DecisionOutcome.NO))
        sink = _RecordingObservationSink()
        gate = build_memory_write_gate(
            backend,
            enabled=True,
            privacy_boundary=DecisionPrivacyBoundary.HOSTED_REDACTED,
            decision_request_sanitizer=_FailingSanitizer(),
        )
        assert isinstance(gate, DecisionMemoryWriteGate)

        assessment = await gate.assess(_request(evidence=("RAW_EVIDENCE",), observation_sink=sink))

        assert assessment.verdict is MemoryWriteVerdict.ACCEPT
        assert assessment.used_fallback is True
        assert backend.requests == []
        observation = sink.observations[0]
        assert observation.fallback_reason == "projection_sanitizer_failed"
        assert "RAW_SENTINEL" not in observation.model_dump_json()
        assert "RAW_EVIDENCE" not in observation.model_dump_json()

    asyncio.run(scenario())


def _request(
    *,
    candidates: tuple[str, ...] = ("Remember the contract change.",),
    evidence: tuple[str, ...] = (),
    observation_sink: _RecordingObservationSink | None = None,
) -> MemoryWriteGateRequest:
    return MemoryWriteGateRequest(
        candidates=candidates,
        evidence=evidence,
        expected_revision=1,
        observation_sink=observation_sink,
    )
