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

"""Content-free calibration and held-out measurements."""

from __future__ import annotations

from collections import Counter
from math import sqrt
from statistics import median
from typing import Any

from powercontext.builtin.artifacts.memory.protocols import MemoryWriteVerdict
from powercontext.builtin.runtime.decision_model import DECISION_INSTRUCTIONS_VERSION, DecisionOutcome
from powercontext.builtin.runtime.memory_write_gate import memory_write_policy

from .runner import (
    HOLD_DIRECTIONS,
    THRESHOLDS,
    CaseResult,
    EvaluationStatus,
    configuration_key,
)


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = (len(ordered) - 1) * percentile
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower)


def _rate(count: int, total: int) -> float | None:
    return None if total == 0 else count / total


def _interval(count: int, rows: tuple[CaseResult, ...]) -> dict[str, float] | None:
    if not rows or len({row.group_id for row in rows}) != len(rows):
        return None
    n = len(rows)
    p = count / n
    z = 1.959963984540054
    divisor = 1 + z * z / n
    center = (p + z * z / (2 * n)) / divisor
    radius = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / divisor
    return {"lower": max(0.0, center - radius), "upper": min(1.0, center + radius)}


def _cost(rows: tuple[CaseResult, ...]) -> dict[str, Any]:
    evaluated = [
        row for row in rows if row.status in {EvaluationStatus.EVALUATED, EvaluationStatus.EVALUATION_FALLBACK}
    ]
    latencies = [row.latency_ms for row in evaluated if row.latency_ms is not None]
    attempts = sum(row.outcome is not None for row in evaluated)
    billed = [row for row in evaluated if row.outcome is not None]
    costs = [row.currency_cost_usd for row in billed if row.currency_cost_usd is not None]
    return {
        "backend_attempts": attempts,
        "model_requests": sum(row.requests for row in evaluated),
        "input_tokens": 0
        if attempts == 0
        else None
        if any(row.input_tokens is None for row in evaluated)
        else sum(row.input_tokens or 0 for row in evaluated),
        "output_tokens": 0
        if attempts == 0
        else None
        if any(row.output_tokens is None for row in evaluated)
        else sum(row.output_tokens or 0 for row in evaluated),
        "latency_samples": len(latencies),
        "latency_p50_ms": median(latencies) if latencies else None,
        "latency_p95_ms": _percentile(latencies, 0.95),
        "currency_cost": sum(costs) if billed and len(costs) == len(billed) else None,
        "currency": "USD" if costs else None,
        "currency_cost_source": "provider_reported" if costs else None,
        "currency_cost_known_subtotal": sum(costs) if costs else None,
        "currency_cost_known_cases": len(costs),
        "human_review_minutes": None,
    }


def score(rows: tuple[CaseResult, ...], configuration: str) -> dict[str, Any]:
    """Keep missing inputs and non-adjudication out of classifier-error denominators."""

    statuses = Counter(row.status.value for row in rows)
    complete = tuple(
        row for row in rows if row.status in {EvaluationStatus.EVALUATED, EvaluationStatus.EVALUATION_FALLBACK}
    )
    judged = tuple(row for row in rows if row.status is EvaluationStatus.EVALUATED)
    expected_accept = tuple(row for row in judged if row.label == "accept")
    expected_hold = tuple(row for row in judged if row.label == "hold")
    false_holds = sum(row.actions[configuration] is MemoryWriteVerdict.HOLD for row in expected_accept if row.actions)
    false_passes = sum(
        row.actions[configuration] is not MemoryWriteVerdict.HOLD for row in expected_hold if row.actions
    )
    all_expected_hold = tuple(row for row in complete if row.label == "hold")
    effective_false_passes = sum(
        row.actions[configuration] is not MemoryWriteVerdict.HOLD for row in all_expected_hold if row.actions
    )
    holds = sum(row.actions[configuration] is MemoryWriteVerdict.HOLD for row in complete if row.actions)
    flags = sum(row.actions[configuration] is MemoryWriteVerdict.FLAG for row in complete if row.actions)
    fallback = Counter(
        row.fallback_reason or "unknown" for row in rows if row.status is EvaluationStatus.EVALUATION_FALLBACK
    )
    return {
        "cases": len(rows),
        "status_counts": dict(sorted(statuses.items())),
        "input_coverage": _rate(len(complete), len(rows)),
        "adjudication_coverage": _rate(len(judged), len(rows)),
        "judged": len(judged),
        "false_holds": false_holds,
        "false_hold_denominator": len(expected_accept),
        "false_hold_rate": _rate(false_holds, len(expected_accept)),
        "false_hold_wilson_95": _interval(false_holds, expected_accept),
        "false_passes": false_passes,
        "false_pass_denominator": len(expected_hold),
        "false_pass_rate": _rate(false_passes, len(expected_hold)),
        "false_pass_wilson_95": _interval(false_passes, expected_hold),
        "false_passes_by_original_label": dict(
            Counter(
                row.original_label
                for row in expected_hold
                if row.original_label is not None
                and row.actions
                and row.actions[configuration] is not MemoryWriteVerdict.HOLD
            )
        ),
        "effective_false_passes_including_fallback": effective_false_passes,
        "effective_false_pass_denominator": len(all_expected_hold),
        "fallback_by_reason": dict(sorted(fallback.items())),
        "hold_count": holds,
        "flag_count": flags,
        "action_denominator": len(complete),
        "hold_rate_in_sample": _rate(holds, len(complete)),
        "flag_rate_in_sample": _rate(flags, len(complete)),
        "missing_confidence": sum(row.confidence is None for row in judged if row.outcome is not None),
        "cost": _cost(rows),
    }


def configuration_grid(rows: tuple[CaseResult, ...]) -> dict[str, dict[str, Any]]:
    return {
        configuration_key(direction, threshold): score(rows, configuration_key(direction, threshold))
        for direction in HOLD_DIRECTIONS
        for threshold in THRESHOLDS
    }


def calibration_report(
    calibration: tuple[CaseResult, ...],
    *,
    dataset_id: str,
    dataset_hashes: dict[str, str],
    code_commit: str,
    evaluation_code_digest: str | None = None,
    evaluation_code_files: dict[str, str] | None = None,
    execution: str,
    backend_model_id: str | None = None,
    backend_model_revision: str | None = None,
    input_source: str = "unspecified",
) -> dict[str, Any]:
    """Report calibration without opening or scoring held-out outcomes."""

    return {
        "schema_version": 1,
        "phase": "calibration_only",
        "dataset_id": dataset_id,
        "dataset_hashes": dataset_hashes,
        "code_commit": code_commit,
        "evaluation_code_digest": evaluation_code_digest,
        "evaluation_code_files": evaluation_code_files,
        "execution": execution,
        "input_source": input_source,
        "backend_model_id": backend_model_id,
        "backend_model_revision": backend_model_revision,
        "decision_instructions_version": DECISION_INSTRUCTIONS_VERSION,
        "policy_id": "memory.write.evidence_sufficiency.v1",
        "calibration_grid": configuration_grid(calibration),
        "calibration_by_language": {
            language: configuration_grid(
                tuple(row for row in calibration if row.language == language),
            )
            for language in ("en", "zh")
        },
        "cases": [case_record(row) for row in calibration],
    }


def report(
    calibration: tuple[CaseResult, ...],
    held_out: tuple[CaseResult, ...],
    *,
    dataset_id: str,
    dataset_hashes: dict[str, str],
    code_commit: str,
    evaluation_code_digest: str | None = None,
    evaluation_code_files: dict[str, str] | None = None,
    execution: str,
    backend_model_id: str | None = None,
    backend_model_revision: str | None = None,
    input_source: str = "unspecified",
    selected_configuration: str | None = None,
) -> dict[str, Any]:
    if execution == "no_call" and selected_configuration is not None:
        raise ValueError("no-call coverage audits cannot select a model configuration")  # noqa: TRY003
    grid = configuration_grid(calibration)
    if selected_configuration is not None and selected_configuration not in grid:
        raise ValueError("selected configuration is outside the declared calibration grid")  # noqa: TRY003
    if selected_configuration is not None and not grid[selected_configuration]["judged"]:
        raise ValueError("cannot select a configuration without adjudicated calibration cases")  # noqa: TRY003
    if execution != "no_call" and selected_configuration is None and any(result["judged"] for result in grid.values()):
        raise ValueError("declare a calibration configuration before scoring held-out cases")  # noqa: TRY003
    selected = selected_configuration
    selected_policy_version = None
    if selected is not None:
        hold_part, threshold_part = selected.split(";")
        hold_on = DecisionOutcome(hold_part.removeprefix("hold_on="))
        threshold_text = threshold_part.removeprefix("threshold=")
        threshold = None if threshold_text == "none" else float(threshold_text)
        selected_policy_version = memory_write_policy(hold_on=hold_on, threshold=threshold).version
    scoring_key = selected or configuration_key(DecisionOutcome.YES, None)
    return {
        "schema_version": 1,
        "phase": "input_coverage_audit" if execution == "no_call" else "held_out_evaluation",
        "dataset_id": dataset_id,
        "dataset_hashes": dataset_hashes,
        "code_commit": code_commit,
        "evaluation_code_digest": evaluation_code_digest,
        "evaluation_code_files": evaluation_code_files,
        "execution": execution,
        "input_source": input_source,
        "backend_model_id": backend_model_id,
        "backend_model_revision": backend_model_revision,
        "decision_instructions_version": DECISION_INSTRUCTIONS_VERSION,
        "policy_id": "memory.write.evidence_sufficiency.v1",
        "selected_configuration": selected,
        "selected_policy_version": selected_policy_version,
        "calibration_grid": grid,
        "held_out": {
            "overall": score(held_out, scoring_key),
            "en": score(tuple(row for row in held_out if row.language == "en"), scoring_key),
            "zh": score(tuple(row for row in held_out if row.language == "zh"), scoring_key),
        },
        "cases": {
            "calibration": [case_record(row) for row in calibration],
            "held_out": [case_record(row) for row in held_out],
        },
        "promotion_decision": "not_assessed",
        "promotion_reason": (
            "no model inference was attempted; local preflight rules may adjudicate"
            if execution == "no_call"
            else "human hold/review friction and promotion budgets have not been measured"
        ),
    }


def case_record(row: CaseResult) -> dict[str, Any]:
    return {
        "case_id": row.case_id,
        "group_id": row.group_id,
        "language": row.language,
        "label": row.label.value,
        "original_label": row.original_label,
        "status": row.status.value,
        "coverage": None if row.coverage is None else row.coverage.value,
        "used_fallback": row.used_fallback,
        "outcome": None if row.outcome is None else row.outcome.value,
        "confidence": row.confidence,
        "fallback_reason": row.fallback_reason,
        "latency_ms": row.latency_ms,
        "requests": row.requests,
        "input_tokens": row.input_tokens,
        "output_tokens": row.output_tokens,
        "currency_cost_usd": row.currency_cost_usd,
        "actions": None if row.actions is None else {key: value.value for key, value in row.actions.items()},
    }
