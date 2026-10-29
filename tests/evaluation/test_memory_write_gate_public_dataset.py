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

"""Public data identity, label isolation, and bilingual uncertainty behavior."""

import asyncio
import json
from pathlib import Path
from zipfile import ZipFile

import httpx
import pytest
from pydantic import SecretStr

from evaluation.memory.write_gate import cli, runner
from evaluation.memory.write_gate.artifacts import load_calibration_results
from evaluation.memory.write_gate.cli import main
from evaluation.memory.write_gate.dataset import Case, Dataset, Label, PublicDatasetRef, PublicDatasetSource
from evaluation.memory.write_gate.jev import JEV_ENDPOINT, JEV_MODEL, PublicJevModel
from evaluation.memory.write_gate.prepare_public import load_xnli, prepare, text_digest
from evaluation.memory.write_gate.report import calibration_report, score
from evaluation.memory.write_gate.resolver import MissingInput, PublicDatasetResolver
from evaluation.memory.write_gate.runner import CaseResult, EvaluationStatus, evaluate_dataset, evaluate_public_dataset
from powercontext.builtin.artifacts.memory.protocols import MemoryWriteVerdict
from powercontext.builtin.inference import InferenceUsage
from powercontext.builtin.runtime.decision_model import DecisionOutcome, DecisionResult


@pytest.fixture
def public_archive(tmp_path: Path) -> Path:
    path = tmp_path / "corpus.zip"
    with ZipFile(path, "w") as archive:
        for split in ("dev", "test"):
            rows = [
                {
                    "pairID": "1",
                    "language": language,
                    "gold_label": "entailment",
                    "sentence1": text,
                    "sentence2": text,
                }
                for language, text in (("en", "The meeting is on Friday."), ("zh", "会议定在周五。"))
            ]
            archive.writestr(
                f"XNLI-1.0/xnli.{split}.jsonl", "\n".join(json.dumps(row, ensure_ascii=False) for row in rows)
            )
    return path


def _dataset(path: Path) -> Dataset:
    rows, digest = load_xnli(path)
    pair = rows["dev"][("1", "en")]
    ref = {"archive_sha256": digest, "split": "dev", "pair_id": pair.pair_id, "language": "en"}
    case = Case(
        case_id="public-1",
        group_id="public-1",
        scope_id="public-xnli",
        language="en",
        label=Label.ACCEPT,
        label_reason="entailment",
        original_label="entailment",
        subject=PublicDatasetRef.model_validate({
            **ref,
            "field": "hypothesis",
            "content_digest": text_digest(pair.hypothesis),
        }),
        evidence=(
            PublicDatasetRef.model_validate({**ref, "field": "premise", "content_digest": text_digest(pair.premise)}),
        ),
    )
    return Dataset(
        dataset_id="public-test",
        split="calibration",
        public_source=PublicDatasetSource(archive_sha256=digest),
        cases=(case,),
    )


def test_public_inputs_recover_without_exposing_gold_labels_to_model(public_archive: Path) -> None:
    class CheckingModel:
        policy_id = "test.public-label-isolation"
        is_local_only = True

        async def evaluate(self, request):
            assert request.evidence == ("The meeting is on Friday.",)
            assert "The meeting is on Friday." in request.subject
            assert "entailment" not in request.subject + " ".join(request.evidence)
            return DecisionResult(
                outcome=DecisionOutcome.NO, policy_id=self.policy_id, usage=InferenceUsage(requests=1)
            )

    result = asyncio.run(
        evaluate_dataset(_dataset(public_archive), PublicDatasetResolver(public_archive), model=CheckingModel())
    )
    assert result[0].status is EvaluationStatus.EVALUATED
    assert result[0].actions is not None
    assert result[0].actions["hold_on=yes;threshold=none"] is MemoryWriteVerdict.ACCEPT


def test_public_reference_requires_matching_text_and_original_label(public_archive: Path) -> None:
    case = _dataset(public_archive).cases[0]
    changed = case.model_copy(update={"original_label": "neutral"})
    with pytest.raises(MissingInput):
        asyncio.run(PublicDatasetResolver(public_archive).resolve(changed))
    changed = case.model_copy(
        update={"subject": case.subject.model_copy(update={"content_digest": "sha256:" + "0" * 64})}
    )
    with pytest.raises(MissingInput):
        asyncio.run(PublicDatasetResolver(public_archive).resolve(changed))


def test_public_source_rejects_wrong_partition_and_label_mapping(public_archive: Path) -> None:
    dataset = _dataset(public_archive)
    with pytest.raises(ValueError, match="original partition"):
        Dataset.model_validate({**dataset.model_dump(), "split": "held_out"})
    with pytest.raises(ValueError, match="NLI mapping"):
        Case.model_validate({**dataset.cases[0].model_dump(), "label": "hold"})


def test_preparation_rejects_unpinned_archive_without_writing_outputs(public_archive: Path, tmp_path: Path) -> None:
    output = tmp_path / "output"
    with pytest.raises(ValueError, match="pinned official"):
        prepare(public_archive, output)
    assert not output.exists()


def test_translation_pairs_do_not_double_the_independent_sample_count() -> None:
    rows = tuple(
        CaseResult(
            case_id=f"pair-{number}-{language}",
            group_id=f"pair-{number}",
            language=language,
            label=Label.ACCEPT,
            status=EvaluationStatus.EVALUATED,
            actions={"hold_on=yes;threshold=none": MemoryWriteVerdict.ACCEPT},
        )
        for number in range(300)
        for language in ("en", "zh")
    )
    assert score(rows, "hold_on=yes;threshold=none")["false_hold_wilson_95"] is None
    english = score(tuple(row for row in rows if row.language == "en"), "hold_on=yes;threshold=none")
    assert 0.01 < english["false_hold_wilson_95"]["upper"] < 0.02


def test_cli_requires_explicit_input_manifests() -> None:
    with pytest.raises(SystemExit) as error:
        main([])
    assert error.value.code == 2


def test_hosted_benchmark_rejects_an_unpinned_archive_before_any_call(public_archive: Path) -> None:
    class ForbiddenModel:
        policy_id = "test.must-not-call"

        async def evaluate(self, request):
            pytest.fail("untrusted inputs reached a hosted model")

    with pytest.raises(ValueError, match="pinned official"):
        asyncio.run(
            evaluate_public_dataset(
                _dataset(public_archive), PublicDatasetResolver(public_archive), model=ForbiddenModel()
            )
        )


def test_jev_public_call_scores_real_response_fields_without_labels_or_local_attestation(
    public_archive: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dataset = _dataset(public_archive)
    # Only this unit test trusts its small archive; real runs require the published checksum.
    assert dataset.public_source is not None
    monkeypatch.setattr(runner, "ARCHIVE_SHA256", dataset.public_source.archive_sha256)
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == JEV_ENDPOINT
        assert request.headers["Authorization"] == "Bearer unit-test-key"
        payload = json.loads(request.content)
        assert payload["model"] == JEV_MODEL
        assert "The meeting is on Friday." in payload["state"]
        assert "entailment" not in request.content.decode()
        assert "public-1" not in request.content.decode()
        sent.append(payload)
        return httpx.Response(
            200,
            json={
                "answers": {
                    "decision": {
                        "type": "choice",
                        "choice": "no",
                        "probabilities": {"yes": 0.02, "no": 0.96, "abstain": 0.02},
                        "confidence": 0.94,
                    }
                },
                "usage": {"input_tokens": 50, "output_tokens": 0, "cost": 0.0000021},
            },
        )

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            model = PublicJevModel(client, SecretStr("unit-test-key"))
            resolver = PublicDatasetResolver(public_archive)
            # The real gate still refuses a cloud backend, even for public inputs.
            refused = await evaluate_dataset(dataset, resolver, model=model)
            assert refused[0].fallback_reason == "privacy_boundary"
            assert not sent
            result = await evaluate_public_dataset(dataset, resolver, model=model)
            assert model.is_local_only is False
            return result

    rows = asyncio.run(scenario())
    assert len(sent) == 1  # Eight policy configurations rescore this one paid answer.
    assert rows[0].actions["hold_on=yes;threshold=none"] is MemoryWriteVerdict.ACCEPT
    assert rows[0].actions["hold_on=no;threshold=0.9"] is MemoryWriteVerdict.HOLD
    assert rows[0].status is EvaluationStatus.EVALUATED
    assert rows[0].requests == 1
    assert rows[0].latency_ms is not None
    summary = calibration_report(
        rows, dataset_id=dataset.dataset_id, dataset_hashes={}, code_commit="test", execution="mock_transport"
    )
    cost = summary["calibration_grid"]["hold_on=yes;threshold=none"]["cost"]
    assert cost["currency_cost"] == 0.0000021
    assert cost["input_tokens"] == 50
    assert cost["currency_cost_source"] == "provider_reported"
    serialized = json.dumps(summary)
    assert "The meeting is on Friday." not in serialized
    assert "unit-test-key" not in serialized


def test_jev_failure_remains_unadjudicated_and_does_not_reuse_previous_cost(
    public_archive: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dataset = _dataset(public_archive)
    assert dataset.public_source is not None
    monkeypatch.setattr(runner, "ARCHIVE_SHA256", dataset.public_source.archive_sha256)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="sensitive provider error details")

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            model = PublicJevModel(client, SecretStr("unit-test-key"))
            model.last_cost_usd = 99.0
            return await evaluate_public_dataset(dataset, PublicDatasetResolver(public_archive), model=model)

    row = asyncio.run(scenario())[0]
    assert row.used_fallback
    assert row.coverage.value == "unadjudicated"
    assert row.status is EvaluationStatus.EVALUATION_FALLBACK
    assert row.fallback_reason == "backend_unavailable"
    assert row.currency_cost_usd is None
    assert all(action is MemoryWriteVerdict.ACCEPT for action in row.actions.values())
    summary = score((row,), "hold_on=yes;threshold=none")
    assert summary["cost"]["backend_attempts"] == 1
    assert summary["cost"]["currency_cost"] is None
    assert "sensitive provider" not in json.dumps(summary)


def test_jev_cli_rejects_private_inputs_and_missing_credentials(tmp_path: Path, monkeypatch, capsys) -> None:
    for name in ("POWERCONTEXT_EVAL_JEV_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    args = [
        "--backend",
        "jev",
        "--calibration-only",
        "--calibration",
        "unused.json",
        "--held-out",
        "unused.json",
        "--output",
        str(tmp_path / "report.json"),
    ]
    with pytest.raises(SystemExit) as error:
        main([*args, "--server-url", "https://private.example"])
    assert error.value.code == 2
    assert "only for the pinned public" in capsys.readouterr().err
    with pytest.raises(SystemExit) as error:
        main([*args, "--public-archive", "unused.zip"])
    assert error.value.code == 2
    assert "configure POWERCONTEXT_EVAL_JEV_API_KEY" in capsys.readouterr().err
    assert not (tmp_path / "report.json").exists()


def test_calibration_command_never_calls_held_out_and_preserves_measurement_metadata(
    tmp_path: Path, monkeypatch
) -> None:
    archive_path = tmp_path / "public.zip"
    with ZipFile(archive_path, "w") as archive:
        for split in ("dev", "test"):
            rows = [
                {
                    "pairID": f"{split}-{label}",
                    "language": language,
                    "gold_label": label,
                    "sentence1": f"{split} evidence {language} {index}",
                    "sentence2": f"{split} candidate {language} {index}",
                }
                for index, label in enumerate(("entailment", "contradiction", "neutral"))
                for language in ("en", "zh")
            ]
            archive.writestr(f"XNLI-1.0/xnli.{split}.jsonl", "\n".join(json.dumps(row) for row in rows))
    splits, digest = load_xnli(archive_path)
    monkeypatch.setattr(runner, "ARCHIVE_SHA256", digest)
    for split, name in (("dev", "calibration"), ("test", "held_out")):
        cases = []
        for pair in splits[split].values():
            ref = {"archive_sha256": digest, "split": split, "pair_id": pair.pair_id, "language": pair.language}
            cases.append(
                Case.model_validate({
                    "case_id": f"{pair.pair_id}-{pair.language}",
                    "group_id": pair.pair_id,
                    "scope_id": "public-xnli",
                    "language": pair.language,
                    "label": Label.ACCEPT if pair.label == "entailment" else Label.HOLD,
                    "label_reason": pair.label,
                    "original_label": pair.label,
                    "subject": PublicDatasetRef.model_validate({
                        **ref,
                        "field": "hypothesis",
                        "content_digest": text_digest(pair.hypothesis),
                    }),
                    "evidence": (
                        PublicDatasetRef.model_validate({
                            **ref,
                            "field": "premise",
                            "content_digest": text_digest(pair.premise),
                        }),
                    ),
                })
            )
        dataset = Dataset.model_validate({
            "dataset_id": "mock-public-corpus",
            "split": name,
            "public_source": {"archive_sha256": digest},
            "cases": cases,
        })
        (tmp_path / f"{name}.json").write_text(dataset.model_dump_json(), encoding="utf-8")

    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert "test candidate" not in payload["state"]
        assert "dev candidate" in payload["state"]
        requests.append(payload)
        return httpx.Response(
            200,
            json={
                "answers": {
                    "decision": {
                        "type": "choice",
                        "choice": "yes",
                        "probabilities": {"yes": 0.8, "no": 0.1, "abstain": 0.1},
                        "confidence": 0.7,
                    }
                },
                "usage": {"input_tokens": 100, "output_tokens": 0, "cost": 0.0000042},
            },
        )

    client_type = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda: client_type(transport=httpx.MockTransport(handler)))
    monkeypatch.setenv("POWERCONTEXT_EVAL_JEV_API_KEY", "unit-test-key")
    output = tmp_path / "report.json"
    assert (
        main([
            "--backend",
            "jev",
            "--calibration-only",
            "--calibration",
            str(tmp_path / "calibration.json"),
            "--held-out",
            str(tmp_path / "held_out.json"),
            "--public-archive",
            str(archive_path),
            "--output",
            str(output),
        ])
        == 0
    )
    value = json.loads(output.read_text())
    assert len(requests) == 6
    assert len(value["cases"]) == 6
    assert value["runtime_hosted_egress_validated"] is False
    assert value["run_scope"] == "evaluation"
    assert value["backend_model_id"] == JEV_MODEL
    assert value["decision_instructions_version"] == f"powercontext.decision.systemone.jev.v2:{JEV_MODEL}"
    assert "held_out" not in value
    assert set(value["calibration_by_language"]) == {"en", "zh"}
    assert "unit-test-key" not in output.read_text()
    assert "dev candidate" not in output.read_text()


def test_partial_provider_cost_is_not_reported_as_a_complete_total() -> None:
    rows = tuple(
        CaseResult(
            case_id=f"case-{index}",
            group_id=f"group-{index}",
            language="en",
            label=Label.ACCEPT,
            status=EvaluationStatus.EVALUATED,
            outcome=DecisionOutcome.NO,
            currency_cost_usd=cost,
            actions={"hold_on=yes;threshold=none": MemoryWriteVerdict.ACCEPT},
        )
        for index, cost in enumerate((0.001, None))
    )
    cost = score(rows, "hold_on=yes;threshold=none")["cost"]
    assert cost["currency_cost"] is None
    assert cost["currency_cost_known_subtotal"] == 0.001
    assert cost["currency_cost_known_cases"] == 1


def test_calibration_reuse_requires_full_matching_data_code_and_backend(public_archive: Path, tmp_path: Path) -> None:
    dataset = _dataset(public_archive)
    rows = asyncio.run(evaluate_dataset(dataset, PublicDatasetResolver(public_archive)))
    hashes = {"calibration": "calibration-digest", "held_out": "held-digest"}
    value = calibration_report(
        rows,
        dataset_id=dataset.dataset_id,
        dataset_hashes=hashes,
        code_commit="test",
        evaluation_code_digest="test-code",
        execution="hosted_public_benchmark",
        backend_model_id=JEV_MODEL,
    )
    value.update(run_scope="evaluation", decision_instructions_version="test-policy")
    path = tmp_path / "calibration.json"
    path.write_text(json.dumps(value))
    restored = load_calibration_results(
        path,
        dataset,
        dataset_hashes=hashes,
        code_digest="test-code",
        backend_policy_id="test-policy",
        model_id=JEV_MODEL,
    )
    assert restored == rows
    with pytest.raises(ValueError, match="does not match"):
        load_calibration_results(
            path,
            dataset,
            dataset_hashes=hashes,
            code_digest="changed-code",
            backend_policy_id="test-policy",
            model_id=JEV_MODEL,
        )
    value["cases"] = []
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="incomplete case coverage"):
        load_calibration_results(
            path,
            dataset,
            dataset_hashes=hashes,
            code_digest="test-code",
            backend_policy_id="test-policy",
            model_id=JEV_MODEL,
        )


def test_jev_credit_exhaustion_preserves_attempted_and_unattempted_cases(
    public_archive: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dataset = _dataset(public_archive)
    assert dataset.public_source is not None
    monkeypatch.setattr(runner, "ARCHIVE_SHA256", dataset.public_source.archive_sha256)
    case = dataset.cases[0]
    dataset = dataset.model_copy(update={"cases": (case, case.model_copy(update={"case_id": "public-2"}))})
    sent = []
    saved = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(402, json={"error": {"code": 402, "message": "private provider details"}})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await evaluate_public_dataset(
                dataset,
                PublicDatasetResolver(public_archive),
                model=PublicJevModel(client, SecretStr("unit-test-key")),
                on_result=saved.append,
            )

    rows = asyncio.run(scenario())
    assert len(sent) == 1
    assert saved == list(rows)
    assert rows[0].status is EvaluationStatus.EVALUATION_FALLBACK
    assert rows[0].used_fallback
    assert rows[0].coverage.value == "unadjudicated"
    assert rows[0].fallback_reason == "insufficient_credit"
    assert rows[1].status is EvaluationStatus.NOT_ATTEMPTED
    summary = score(rows, "hold_on=yes;threshold=none")
    assert summary["input_coverage"] == 0.5
    assert summary["adjudication_coverage"] == 0
    assert summary["false_hold_denominator"] == 0
    assert summary["cost"]["backend_attempts"] == 1


def test_cached_calibration_rejected_when_shared_assessor_changes(
    public_archive: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dataset = _dataset(public_archive)
    rows = asyncio.run(evaluate_dataset(dataset, PublicDatasetResolver(public_archive)))
    hashes = {"calibration": "calibration-digest", "held_out": "held-digest"}
    root = Path(cli.__file__).resolve().parents[3]
    copied = tmp_path / "checkout"
    for relative in cli._code_identity():
        target = copied / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((root / relative).read_bytes())
    monkeypatch.setattr(cli, "__file__", str(copied / "evaluation/memory/write_gate/cli.py"))
    value = calibration_report(
        rows,
        dataset_id=dataset.dataset_id,
        dataset_hashes=hashes,
        code_commit="test",
        evaluation_code_digest=cli._code_digest(),
        evaluation_code_files=cli._code_identity(),
        execution="hosted_public_benchmark",
        backend_model_id=JEV_MODEL,
    )
    value.update(run_scope="evaluation", decision_instructions_version="test-policy")
    path = tmp_path / "calibration.json"
    path.write_text(json.dumps(value))
    assessor = copied / "src/powercontext/builtin/runtime/decision_policy.py"
    original = assessor.read_text()
    assert "verdict=mapping[result.outcome]" in original
    assessor.write_text(original.replace("verdict=mapping[result.outcome]", "verdict=DecisionVerdict.ALLOW"))
    with pytest.raises(ValueError, match="does not match"):
        load_calibration_results(
            path,
            dataset,
            dataset_hashes=hashes,
            code_digest=cli._code_digest(),
            backend_policy_id="test-policy",
            model_id=JEV_MODEL,
        )
