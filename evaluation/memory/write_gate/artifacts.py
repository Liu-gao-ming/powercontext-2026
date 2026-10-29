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

"""Read measured calibration answers without paying to evaluate them again."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import TypeAdapter

from powercontext.builtin.decision_observations import DecisionCoverage

from .dataset import Dataset
from .runner import HOLD_DIRECTIONS, THRESHOLDS, CaseResult, EvaluationStatus, configuration_key


def load_calibration_results(
    path: Path,
    dataset: Dataset,
    *,
    dataset_hashes: dict[str, str],
    code_digest: str,
    backend_policy_id: str,
    model_id: str,
) -> tuple[CaseResult, ...]:
    """Require the full calibration run from the same code, model, policy and data."""

    value = json.loads(path.read_text(encoding="utf-8"))
    expected = {
        "schema_version": 1,
        "phase": "calibration_only",
        "run_scope": "evaluation",
        "execution": "hosted_public_benchmark",
        "dataset_id": dataset.dataset_id,
        "dataset_hashes": dataset_hashes,
        "evaluation_code_digest": code_digest,
        "backend_model_id": model_id,
        "decision_instructions_version": backend_policy_id,
    }
    if not isinstance(value, dict) or any(value.get(key) != item for key, item in expected.items()):
        raise ValueError("calibration report does not match this full evaluation")  # noqa: TRY003
    rows = TypeAdapter(tuple[CaseResult, ...]).validate_python(value.get("cases"))
    if len(rows) != len(dataset.cases):
        raise ValueError("calibration report has incomplete case coverage")  # noqa: TRY003
    configurations = {
        configuration_key(direction, threshold) for direction in HOLD_DIRECTIONS for threshold in THRESHOLDS
    }
    for case, row in zip(dataset.cases, rows, strict=True):
        if (
            (row.case_id, row.group_id, row.language, row.label, row.original_label)
            != (case.case_id, case.group_id, case.language, case.label, case.original_label)
            or row.status is EvaluationStatus.NOT_ATTEMPTED
            or (row.used_fallback and row.coverage is not DecisionCoverage.UNADJUDICATED)
            or row.actions is None
            or set(row.actions) != configurations
        ):
            raise ValueError("calibration report has invalid case identity or coverage")  # noqa: TRY003
    return rows
