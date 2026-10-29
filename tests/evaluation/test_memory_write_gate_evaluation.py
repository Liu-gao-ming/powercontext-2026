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

"""Observable offline evaluation behavior for Memory write-gate promotion evidence."""

from __future__ import annotations

import asyncio
import json
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
import rfc8785

from evaluation.memory.write_gate.dataset import (
    ArtifactRef,
    Case,
    Dataset,
    Label,
    SourceRef,
    validate_split_pair,
)
from evaluation.memory.write_gate.report import calibration_report, report
from evaluation.memory.write_gate.resolver import (
    AuthorizedClientResolver,
    MissingInput,
    ResolvedCase,
    SplitCheckedResolver,
    Unauthorized,
    UnsupportedReference,
)
from evaluation.memory.write_gate.runner import EvaluationStatus, evaluate_dataset
from powercontext.builtin.decision_observations import DecisionCoverage
from powercontext.builtin.inference import InferenceUsage
from powercontext.builtin.runtime.decision_model import DecisionOutcome, DecisionResult
from powercontext.client.client import PowerContextClient
from powercontext.client.errors import ForbiddenResponseError

DIGEST = "sha256:" + "a" * 64


class _Resolver:
    async def resolve(self, case: Case) -> ResolvedCase:
        return ResolvedCase(candidate=f"Candidate {case.case_id}", evidence=("会议决定在周五发布。",))


class _Model:
    policy_id = "test.write-gate"
    is_local_only = True

    def __init__(self, *, used_fallback: bool = False):
        self.used_fallback = used_fallback
        self.outcomes = iter(("no", "yes", "yes", "yes", "no", "yes", "no", "abstain"))

    async def evaluate(self, request):
        return DecisionResult(
            outcome=DecisionOutcome(next(self.outcomes)),
            confidence=0.8,
            policy_id=self.policy_id,
            used_fallback=self.used_fallback,
            usage=InferenceUsage(requests=1),
        )


def _datasets():
    datasets = []
    for split in ("calibration", "held_out"):
        cases = tuple(
            Case(
                case_id=f"{split}-{index}",
                group_id=f"{split}-{index}",
                scope_id="test-scope",
                language="en" if index < 4 else "zh",
                label=Label.ACCEPT if index % 2 == 0 else Label.HOLD,
                label_reason="test_support",
                subject=ArtifactRef(
                    artifact_id=f"{split}-{index}",
                    revision=1,
                    content_digest="sha256:" + sha256(f"{split}-{index}".encode()).hexdigest(),
                ),
                evidence=(ArtifactRef(artifact_id="evidence", revision=1, content_digest=DIGEST),),
            )
            for index in range(8)
        )
        datasets.append(Dataset(dataset_id="test", split=split, cases=cases))
    validate_split_pair(*datasets)
    return *datasets, {}


def test_calibration_and_held_out_report_fallback_separately() -> None:
    calibration, held_out, hashes = _datasets()
    resolver = _Resolver()

    async def scenario():
        return (
            await evaluate_dataset(calibration, resolver, model=_Model()),
            await evaluate_dataset(held_out, resolver, model=_Model()),
        )

    calibration_results, held_results = asyncio.run(scenario())
    result = report(
        calibration_results,
        held_results,
        dataset_id=calibration.dataset_id,
        dataset_hashes=hashes,
        code_commit="test",
        execution="controlled_model",
        selected_configuration="hold_on=yes;threshold=none",
    )

    assert result["selected_configuration"] == "hold_on=yes;threshold=none"
    assert result["held_out"]["overall"]["status_counts"] == {"evaluated": 7, "evaluation_fallback": 1}
    assert result["held_out"]["overall"]["false_holds"] == 1
    assert result["held_out"]["overall"]["effective_false_passes_including_fallback"] == 1
    assert result["held_out"]["overall"]["cost"]["model_requests"] == 8
    assert result["held_out"]["en"]["false_holds"] == 1
    assert result["held_out"]["zh"]["false_holds"] == 0
    assert result["promotion_decision"] == "not_assessed"
    assert result["held_out"]["overall"]["hold_count"] == 4
    assert result["held_out"]["overall"]["action_denominator"] == 8
    serialized = json.dumps(result, ensure_ascii=False)
    assert "Candidate held_out-0" not in serialized
    assert "会议决定" not in serialized


def test_calibration_requires_an_explicit_configuration_before_held_out_scoring() -> None:
    calibration, held_out, hashes = _datasets()
    resolver = _Resolver()

    async def scenario():
        return (
            await evaluate_dataset(calibration, resolver, model=_Model()),
            await evaluate_dataset(held_out, resolver, model=_Model()),
        )

    calibration_results, held_results = asyncio.run(scenario())
    only = calibration_report(
        calibration_results,
        dataset_id=calibration.dataset_id,
        dataset_hashes=hashes,
        code_commit="test",
        execution="controlled_model",
    )
    assert only["phase"] == "calibration_only"
    assert "held_out" not in only
    with pytest.raises(ValueError, match="declare a calibration configuration"):
        report(
            calibration_results,
            held_results,
            dataset_id=calibration.dataset_id,
            dataset_hashes=hashes,
            code_commit="test",
            execution="controlled_model",
        )


def test_missing_input_is_not_scored_as_negative() -> None:
    class MissingResolver:
        async def resolve(self, case):
            raise MissingInput

    calibration, _, _ = _datasets()
    result = asyncio.run(evaluate_dataset(calibration, MissingResolver(), model=_Model()))
    assert {row.status for row in result} == {EvaluationStatus.MISSING_INPUT}
    assert all(row.actions is None for row in result)


def test_no_call_and_backend_fallback_are_not_counted_as_adjudication() -> None:
    calibration, _, _ = _datasets()
    resolver = _Resolver()
    no_call = asyncio.run(evaluate_dataset(calibration, resolver))
    assert {row.status for row in no_call} == {EvaluationStatus.EVALUATION_FALLBACK}
    assert all(row.coverage is DecisionCoverage.UNADJUDICATED and row.used_fallback for row in no_call)
    no_call_report = report(
        no_call,
        no_call,
        dataset_id=calibration.dataset_id,
        dataset_hashes={},
        code_commit="test",
        execution="no_call",
    )
    assert no_call_report["selected_configuration"] is None
    assert no_call_report["selected_policy_version"] is None
    assert no_call_report["held_out"]["overall"]["adjudication_coverage"] == 0

    backend_fallback = asyncio.run(evaluate_dataset(calibration, resolver, model=_Model(used_fallback=True)))
    assert backend_fallback[0].status is EvaluationStatus.EVALUATION_FALLBACK
    assert backend_fallback[0].coverage is DecisionCoverage.UNADJUDICATED
    assert backend_fallback[0].used_fallback is True


def test_rejects_scenario_group_leakage_between_splits() -> None:
    calibration, held_out, _ = _datasets()
    copied = held_out.cases[0].model_copy(update={"group_id": calibration.cases[0].group_id})
    bad = held_out.model_copy(update={"cases": (copied, *held_out.cases[1:])})
    with pytest.raises(ValueError, match="scenario groups"):
        validate_split_pair(calibration, bad)


def test_rejects_copied_private_inputs_with_distinct_case_and_group_ids() -> None:
    calibration, held_out, _ = _datasets()
    value = held_out.model_dump()
    for case, original in zip(value["cases"], calibration.cases, strict=True):
        case["subject"] = original.subject.model_dump()
        case["evidence"] = tuple(ref.model_dump() for ref in original.evidence)
    copied = Dataset.model_validate(value)
    with pytest.raises(ValueError, match="private inputs must not cross splits"):
        validate_split_pair(calibration, copied)


def test_rejects_duplicate_immutable_references_even_with_changed_artifact_digests() -> None:
    calibration, held_out, _ = _datasets()
    original = calibration.cases[0]
    copied = held_out.cases[0].model_copy(
        update={
            "subject": original.subject.model_copy(update={"content_digest": DIGEST}),
            "evidence": tuple(
                ref.model_copy(update={"content_digest": "sha256:" + "b" * 64}) for ref in original.evidence
            ),
        }
    )
    duplicate = Dataset.model_validate({**held_out.model_dump(), "cases": (copied, *held_out.cases[1:])})
    with pytest.raises(ValueError, match="private inputs must not cross splits"):
        validate_split_pair(calibration, duplicate)


@pytest.mark.parametrize("evidence_kind", ["artifact", "source"])
def test_rejects_duplicate_content_under_different_references_and_evidence_order(evidence_kind: str) -> None:
    calibration, held_out, _ = _datasets()
    if evidence_kind == "source":
        evidence = (
            SourceRef(source_id="source-a", content_digest=DIGEST, gate_input_digest=DIGEST),
            SourceRef(source_id="source-b", content_digest="sha256:" + "b" * 64),
        )
        renamed = tuple(ref.model_copy(update={"source_id": f"copy-{index}"}) for index, ref in enumerate(evidence))
    else:
        evidence = (
            ArtifactRef(artifact_id="evidence-a", revision=1, content_digest=DIGEST),
            ArtifactRef(artifact_id="evidence-b", revision=1, content_digest="sha256:" + "b" * 64),
        )
        renamed = tuple(
            ref.model_copy(update={"artifact_id": f"copy-{index}", "revision": 2}) for index, ref in enumerate(evidence)
        )
    original = calibration.cases[0].model_copy(update={"evidence": evidence})
    calibration = calibration.model_copy(update={"cases": (original, *calibration.cases[1:])})
    copied = held_out.cases[0].model_copy(
        update={
            "scope_id": "another-scope",
            "subject": original.subject.model_copy(update={"artifact_id": "copied-memory", "revision": 2}),
            "evidence": tuple(reversed(renamed)),
        }
    )
    held_out = held_out.model_copy(update={"cases": (copied, *held_out.cases[1:])})
    with pytest.raises(ValueError, match="private inputs must not cross splits"):
        validate_split_pair(calibration, held_out)


def test_allows_shared_evidence_for_distinct_private_candidates() -> None:
    calibration, held_out, _ = _datasets()
    assert calibration.cases[0].evidence == held_out.cases[0].evidence
    validate_split_pair(calibration, held_out)


def test_allows_same_private_candidate_with_different_evidence() -> None:
    calibration, held_out, _ = _datasets()
    changed = held_out.cases[0].model_copy(
        update={
            "subject": calibration.cases[0].subject,
            "evidence": (ArtifactRef(artifact_id="other-evidence", revision=1, content_digest="sha256:" + "b" * 64),),
        }
    )
    validate_split_pair(calibration, held_out.model_copy(update={"cases": (changed, *held_out.cases[1:])}))


def test_cli_rejects_duplicate_private_inputs_before_server_reads(tmp_path: Path, monkeypatch) -> None:
    from evaluation.memory.write_gate import cli

    calibration, held_out, _ = _datasets()
    copied = held_out.cases[0].model_copy(
        update={
            "subject": calibration.cases[0].subject,
            "evidence": calibration.cases[0].evidence,
        }
    )
    held_out = held_out.model_copy(update={"cases": (copied, *held_out.cases[1:])})
    for dataset in (calibration, held_out):
        (tmp_path / f"{dataset.split}.json").write_text(dataset.model_dump_json(), encoding="utf-8")

    def unexpected_read(*args):
        pytest.fail("overlapping inputs must be rejected before Server reads")

    monkeypatch.setenv("POWERCONTEXT_CLIENT_API_TOKEN", "test-token")
    monkeypatch.setattr(cli, "_evaluate_remote", unexpected_read)
    with pytest.raises(ValueError, match="private inputs must not cross splits"):
        cli.main([
            "--calibration",
            str(tmp_path / "calibration.json"),
            "--held-out",
            str(tmp_path / "held_out.json"),
            "--server-url",
            "http://localhost:8000",
        ])


@pytest.mark.parametrize("scenario", ["duplicate", "reordered_duplicate", "different_candidate", "different_evidence"])
def test_resolved_split_validation_uses_gate_text_instead_of_artifact_metadata(scenario: str) -> None:
    records = {}
    datasets = []

    def reference(scope_id, artifact_id, kind, text, citations=()):
        content = {"schema": "powercontext.atomic-memory.v1", "kind": kind, "text": text, "creation": None}
        digest = "sha256:" + sha256(rfc8785.dumps(content)).hexdigest()
        records[artifact_id] = SimpleNamespace(
            scope_id=scope_id,
            family="atomic-memory",
            artifact_id=artifact_id,
            revision=1,
            content_digest=digest,
            content=content,
            sources=(),
            artifacts=citations,
        )
        return ArtifactRef(artifact_id=artifact_id, revision=1, content_digest=digest)

    for dataset, kind in zip(_datasets()[:2], ("fact", "preference"), strict=True):
        held_out = dataset.split == "held_out"
        cases = []
        for index, case in enumerate(dataset.cases):
            scope = f"scope-{dataset.split}"
            evidence_text = f"evidence {index}"
            if held_out and scenario == "different_evidence":
                evidence_text += " changed"
            evidence = (
                reference(scope, f"{case.case_id}-evidence-a", kind, evidence_text),
                reference(scope, f"{case.case_id}-evidence-b", kind, "additional support"),
            )
            if held_out and scenario == "reordered_duplicate":
                evidence = tuple(reversed(evidence))
            citations = tuple(
                SimpleNamespace(family=ref.family, artifact_id=ref.artifact_id, revision=ref.revision)
                for ref in evidence
            )
            candidate = f"candidate {index}"
            if held_out and scenario == "different_candidate":
                candidate += " changed"
            subject = reference(scope, case.case_id, kind, candidate, citations)
            cases.append(case.model_copy(update={"scope_id": scope, "subject": subject, "evidence": evidence}))
        datasets.append(Dataset.model_validate({**dataset.model_dump(), "cases": cases}))

    class ExactReadClient:
        async def get_artifact_revision(self, scope_id, family, artifact_id, revision):
            return records[artifact_id]

    calibration, held_out = datasets
    validate_split_pair(calibration, held_out)
    assert calibration.cases[0].subject.content_digest != held_out.cases[0].subject.content_digest
    resolver = SplitCheckedResolver(
        calibration, held_out, AuthorizedClientResolver(cast(PowerContextClient, ExactReadClient()))
    )

    async def evaluate_splits():
        await evaluate_dataset(calibration, resolver)
        return await evaluate_dataset(held_out, resolver)

    if scenario in {"duplicate", "reordered_duplicate"}:
        with pytest.raises(ValueError, match="private gate inputs must not cross splits"):
            asyncio.run(evaluate_splits())
    else:
        asyncio.run(evaluate_splits())


def test_cli_rejects_duplicate_gate_text_without_writing_report(tmp_path: Path, monkeypatch) -> None:
    from evaluation.memory.write_gate import cli

    calibration, held_out, _ = _datasets()
    for dataset in (calibration, held_out):
        (tmp_path / f"{dataset.split}.json").write_text(dataset.model_dump_json(), encoding="utf-8")

    async def resolve(self, case):
        return ResolvedCase(candidate="same candidate", evidence=("same evidence",))

    monkeypatch.setenv("POWERCONTEXT_CLIENT_API_TOKEN", "test-token")
    monkeypatch.setattr(AuthorizedClientResolver, "resolve", resolve)
    output = tmp_path / "coverage.json"
    with pytest.raises(ValueError, match="private gate inputs must not cross splits"):
        cli.main([
            "--calibration",
            str(tmp_path / "calibration.json"),
            "--held-out",
            str(tmp_path / "held_out.json"),
            "--server-url",
            "http://localhost:8000",
            "--output",
            str(output),
        ])
    assert not output.exists()


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (MissingInput, EvaluationStatus.MISSING_INPUT),
        (Unauthorized, EvaluationStatus.UNAUTHORIZED),
        (UnsupportedReference, EvaluationStatus.UNSUPPORTED_REFERENCE),
    ],
)
def test_resolved_split_validation_preserves_incomplete_coverage(error, status) -> None:
    class IncompleteResolver:
        async def resolve(self, case):
            raise error

    calibration, held_out, _ = _datasets()

    async def scenario():
        resolver = SplitCheckedResolver(calibration, held_out, IncompleteResolver())
        return await evaluate_dataset(held_out, resolver)

    rows = asyncio.run(scenario())
    assert all(row.status is status and row.actions is None for row in rows)


def _remote_case(*, evidence: tuple[SourceRef | ArtifactRef, ...]) -> Case:
    return Case(
        case_id="remote-1",
        group_id="remote-group",
        scope_id="scope-a",
        language="en",
        label=Label.ACCEPT,
        label_reason="direct_support",
        subject=ArtifactRef(artifact_id="memory-1", revision=1, content_digest=DIGEST),
        evidence=evidence,
    )


def test_current_authorization_failure_is_reported_separately() -> None:
    class DenyingClient:
        async def get_artifact_revision(self, scope_id, family, artifact_id, revision):
            assert scope_id == "scope-a"
            raise ForbiddenResponseError(status_code=403, request_id=None)

    case = _remote_case(evidence=(ArtifactRef(artifact_id="evidence", revision=1, content_digest=DIGEST),))
    dataset = Dataset(dataset_id="remote", split="calibration", cases=(case,))
    result = asyncio.run(evaluate_dataset(dataset, AuthorizedClientResolver(cast(PowerContextClient, DenyingClient()))))
    assert result[0].status is EvaluationStatus.UNAUTHORIZED
    assert result[0].actions is None


def test_exact_revision_and_citations_are_required_before_model_call() -> None:
    class ChangedClient:
        async def get_artifact_revision(self, scope_id, family, artifact_id, revision):
            assert scope_id == "scope-a"
            return SimpleNamespace(
                scope_id=scope_id,
                family=family,
                artifact_id=artifact_id,
                revision=revision,
                content_digest="sha256:" + "b" * 64,
                content={"text": "changed"},
                sources=[],
                artifacts=[],
            )

    case = _remote_case(evidence=(ArtifactRef(artifact_id="evidence", revision=1, content_digest=DIGEST),))
    dataset = Dataset(dataset_id="remote", split="calibration", cases=(case,))
    result = asyncio.run(evaluate_dataset(dataset, AuthorizedClientResolver(cast(PowerContextClient, ChangedClient()))))
    assert result[0].status is EvaluationStatus.MISSING_INPUT


def test_current_scope_and_exact_citations_cannot_be_substituted() -> None:
    class IncorrectClient:
        def __init__(self, scope_id: str) -> None:
            self.scope_id = scope_id

        async def get_artifact_revision(self, scope_id, family, artifact_id, revision):
            return SimpleNamespace(
                scope_id=self.scope_id,
                family=family,
                artifact_id=artifact_id,
                revision=revision,
                content_digest=DIGEST,
                content={"text": "candidate"},
                sources=[],
                artifacts=[SimpleNamespace(family="atomic-memory", artifact_id="other", revision=1)],
            )

    case = _remote_case(evidence=(ArtifactRef(artifact_id="evidence", revision=1, content_digest=DIGEST),))
    dataset = Dataset(dataset_id="remote", split="calibration", cases=(case,))
    wrong_scope = asyncio.run(
        evaluate_dataset(dataset, AuthorizedClientResolver(cast(PowerContextClient, IncorrectClient("scope-b"))))
    )
    wrong_citation = asyncio.run(
        evaluate_dataset(dataset, AuthorizedClientResolver(cast(PowerContextClient, IncorrectClient("scope-a"))))
    )
    assert wrong_scope[0].status is EvaluationStatus.MISSING_INPUT
    assert wrong_citation[0].status is EvaluationStatus.UNSUPPORTED_REFERENCE


def test_source_read_without_exact_gate_projection_is_incomplete() -> None:
    source = SourceRef(source_id="source-1", content_digest=DIGEST)

    class SourceClient:
        async def get_artifact_revision(self, scope_id, family, artifact_id, revision):
            return SimpleNamespace(
                scope_id=scope_id,
                family=family,
                artifact_id=artifact_id,
                revision=revision,
                content_digest=DIGEST,
                content={"text": "candidate"},
                sources=[SimpleNamespace(source_type="content", source_id="source-1")],
                artifacts=[],
            )

        async def get_source(self, scope_id, source_type, source_id):
            return SimpleNamespace(
                scope_id=scope_id,
                source_type=source_type,
                source_id=source_id,
                content_digest=DIGEST,
                content="source text",
            )

    case = _remote_case(evidence=(source,))
    dataset = Dataset(dataset_id="remote", split="calibration", cases=(case,))
    result = asyncio.run(evaluate_dataset(dataset, AuthorizedClientResolver(cast(PowerContextClient, SourceClient()))))
    assert result[0].status is EvaluationStatus.UNSUPPORTED_REFERENCE


def test_uncommitted_candidate_reference_is_incomplete() -> None:
    case = Case(
        case_id="pending",
        group_id="pending",
        scope_id="scope-a",
        language="en",
        label=Label.HOLD,
        label_reason="unsupported",
        subject=SourceRef(source_id="candidate:1", content_digest=DIGEST),
        evidence=(SourceRef(source_id="source-1", content_digest=DIGEST),),
    )
    dataset = Dataset(dataset_id="remote", split="calibration", cases=(case,))
    result = asyncio.run(evaluate_dataset(dataset, AuthorizedClientResolver(cast(PowerContextClient, object()))))
    assert result[0].status is EvaluationStatus.UNSUPPORTED_REFERENCE


@pytest.mark.parametrize("split", ["calibration", "held_out"])
def test_no_call_cli_reports_local_preflight_hold_without_model_configuration(
    tmp_path: Path, split: str, monkeypatch
) -> None:
    from evaluation.memory.write_gate.cli import main

    calibration, held_out, _ = _datasets()
    for dataset in (calibration, held_out):
        (tmp_path / f"{dataset.split}.json").write_text(dataset.model_dump_json(), encoding="utf-8")

    async def resolve(self, case):
        candidate = "x" * 4000 if case.case_id == f"{split}-0" else f"candidate {case.case_id}"
        return ResolvedCase(candidate=candidate, evidence=("evidence",))

    monkeypatch.setattr(AuthorizedClientResolver, "resolve", resolve)
    monkeypatch.setenv("POWERCONTEXT_CLIENT_API_TOKEN", "unit-test-token")
    output = tmp_path / "coverage.json"
    assert (
        main([
            "--backend",
            "no_call",
            "--calibration",
            str(tmp_path / "calibration.json"),
            "--held-out",
            str(tmp_path / "held_out.json"),
            "--server-url",
            "http://localhost:8000",
            "--output",
            str(output),
        ])
        == 0
    )
    value = json.loads(output.read_text())
    assert value["phase"] == "input_coverage_audit"
    assert value["selected_configuration"] is None
    assert value["selected_policy_version"] is None
    row = value["cases"][split][0]
    assert row["coverage"] == "adjudicated"
    assert row["used_fallback"] is False
    assert row["outcome"] is None
    assert set(row["actions"].values()) == {"hold"}
    assert all(item["requests"] == 0 for rows in value["cases"].values() for item in rows)
    assert "x" * 4000 not in output.read_text()
