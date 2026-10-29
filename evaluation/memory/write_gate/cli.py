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

"""Offline Memory write-gate evaluation; output contains no source bodies."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

import httpx
from pydantic import SecretStr

from powercontext.client.client import PowerContextClient

from .artifacts import load_calibration_results
from .dataset import load_dataset, validate_split_pair
from .jev import JEV_ENDPOINT, JEV_MODEL, PublicJevModel
from .report import calibration_report, case_record, report
from .resolver import AuthorizedClientResolver, PublicDatasetResolver, SplitCheckedResolver
from .runner import HOLD_DIRECTIONS, THRESHOLDS, configuration_key, evaluate_dataset, evaluate_public_dataset


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evaluation.memory.write_gate")
    parser.add_argument("--calibration", type=Path)
    parser.add_argument("--held-out", type=Path)
    parser.add_argument("--public-archive", type=Path, help="external official XNLI archive pinned by the manifests")
    parser.add_argument("--backend", choices=("no_call", "jev"), default="no_call")
    parser.add_argument("--jev-key-file", type=Path, help="file containing only an OpenRouter API key")
    parser.add_argument(
        "--calibration-report", type=Path, help="reuse a full measured Jev calibration run for held-out"
    )
    parser.add_argument(
        "--calibration-only", action="store_true", help="compare candidates without evaluating held-out"
    )
    parser.add_argument("--server-url", help="use current Server authorization for exact production references")
    parser.add_argument(
        "--configuration",
        choices=[configuration_key(direction, threshold) for direction in HOLD_DIRECTIONS for threshold in THRESHOLDS],
        help="configuration frozen from calibration before scoring held-out",
    )
    parser.add_argument("--output", type=Path, help="write a content-free JSON report")
    args = parser.parse_args(argv)
    if args.output and (
        args.output.exists() or (args.backend == "jev" and args.output.with_suffix(".cases.jsonl").exists())
    ):
        parser.error("output or case checkpoint already exists; choose a new report path")
    _validate_arguments(parser, args)

    calibration, calibration_hash = load_dataset(args.calibration)
    held_out, held_out_hash = load_dataset(args.held_out)
    validate_split_pair(calibration, held_out)
    args.dataset_hashes = {"calibration": calibration_hash, "held_out": held_out_hash}
    if bool(calibration.public_source) != bool(args.public_archive):
        parser.error("public manifests require --public-archive; private manifests cannot use it")
    if args.server_url:
        token = os.environ.get("POWERCONTEXT_CLIENT_API_TOKEN")
        if not token:
            parser.error("POWERCONTEXT_CLIENT_API_TOKEN is required for authorized Server reads")
        result = asyncio.run(_evaluate_remote(args, calibration, held_out, token))
    else:
        resolver = PublicDatasetResolver(args.public_archive)
        result = asyncio.run(_evaluate(calibration, held_out, resolver, args))
    hashes = {"calibration": calibration_hash, "held_out": held_out_hash}
    calibration_results, held_out_results, selected = result
    metadata: dict[str, Any] = {
        "dataset_id": calibration.dataset_id,
        "dataset_hashes": hashes,
        "code_commit": _commit(),
        "evaluation_code_digest": _code_digest(),
        "evaluation_code_files": _code_identity(),
        "execution": "hosted_public_benchmark" if args.backend == "jev" else "no_call",
        "backend_model_id": JEV_MODEL if args.backend == "jev" else None,
        "input_source": "authorized_references" if args.server_url else "public_xnli",
    }
    if args.calibration_only:
        value = calibration_report(calibration_results, **metadata)
    else:
        value = report(calibration_results, held_out_results, selected_configuration=selected, **metadata)
    if args.backend == "jev":
        value.update({
            "completed_at_utc": datetime.now(UTC).isoformat(),
            "backend_endpoint": JEV_ENDPOINT,
            "decision_instructions_version": args.jev_policy_id,
            "model_revision_guarantee": "versioned provider ID; immutable server weights are not verifiable",
            "runtime_hosted_egress_validated": False,
            "evaluation_path": "public benchmark model call followed by local Runtime outcome rescoring",
            "run_scope": "evaluation",
            "calibration_report_sha256": sha256(args.calibration_report.read_bytes()).hexdigest()
            if args.calibration_report
            else None,
            "calibration_calls_reused": bool(args.calibration_report),
            "case_checkpoint": args.output.with_suffix(".cases.jsonl").name,
        })
    output = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as destination:
            destination.write(output)
    else:
        print(output, end="")
    return 0


def _validate_arguments(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.calibration is None or args.held_out is None:
        parser.error("provide explicit --calibration and --held-out manifests")
    _validate_jev_arguments(parser, args)
    if bool(args.public_archive) == bool(args.server_url):
        parser.error("provide either --public-archive or --server-url")
    if not args.calibration_only and args.backend != "no_call" and args.configuration is None:
        parser.error("declare --configuration before scoring held-out cases")
    if (args.backend == "no_call" or args.calibration_only) and args.configuration is not None:
        parser.error("this run cannot select a decision configuration")


def _validate_jev_arguments(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.calibration_report and (args.backend != "jev" or args.calibration_only):
        parser.error("--calibration-report requires a Jev held-out run")
    if args.jev_key_file and args.backend != "jev":
        parser.error("--jev-key-file requires --backend jev")
    if args.backend != "jev":
        return
    if not args.public_archive or args.server_url:
        parser.error("Jev is available only for the pinned public XNLI archive, without Server reads")
    if not args.output:
        parser.error("Jev evaluation requires --output to preserve the paid run's report")
    try:
        key = (
            args.jev_key_file.read_text(encoding="utf-8")
            if args.jev_key_file
            else os.environ.get("POWERCONTEXT_EVAL_JEV_API_KEY") or os.environ.get("OPENROUTER_API_KEY", "")
        ).strip()
    except (OSError, UnicodeError):
        parser.error("cannot read --jev-key-file")
    if not key:
        parser.error("configure POWERCONTEXT_EVAL_JEV_API_KEY, OPENROUTER_API_KEY, or --jev-key-file")
    args.jev_key = SecretStr(key)


async def _evaluate_remote(args, calibration, held_out, token):
    async with PowerContextClient(args.server_url, token=token) as client:
        return await _evaluate(calibration, held_out, AuthorizedClientResolver(client), args)


async def _evaluate(calibration, held_out, resolver, args):
    if args.backend == "jev":
        return await _evaluate_jev(calibration, held_out, resolver, args)
    if calibration.public_source is None:
        resolver = SplitCheckedResolver(calibration, held_out, resolver)
    calibration_results = await evaluate_dataset(calibration, resolver)
    if args.calibration_only:
        return calibration_results, (), None
    held_out_results = await evaluate_dataset(held_out, resolver)
    return calibration_results, held_out_results, args.configuration


async def _evaluate_jev(calibration, held_out, resolver, args):
    async with httpx.AsyncClient() as client:
        model = PublicJevModel(client, args.jev_key)
        args.jev_policy_id = model.policy_id
        cached = None
        if args.calibration_report:
            cached = load_calibration_results(
                args.calibration_report,
                calibration,
                dataset_hashes=args.dataset_hashes,
                code_digest=_code_digest(),
                backend_policy_id=model.policy_id,
                model_id=JEV_MODEL,
            )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.with_suffix(".cases.jsonl").open("x", encoding="utf-8") as checkpoint:

            async def evaluate(dataset):
                count = 0

                def save(row):
                    nonlocal count
                    count += 1
                    checkpoint.write(
                        json.dumps({"split": dataset.split, **case_record(row)}, ensure_ascii=False) + "\n"
                    )
                    checkpoint.flush()
                    if count % 25 == 0 or count == len(dataset.cases):
                        print(
                            f"{dataset.split}: {count}/{len(dataset.cases)} records saved", file=sys.stderr, flush=True
                        )

                return await evaluate_public_dataset(dataset, resolver, model=model, on_result=save)

            calibration_results = cached if cached is not None else await evaluate(calibration)
            if args.calibration_only:
                return calibration_results, (), None
            held_out_results = await evaluate(held_out)
            return calibration_results, held_out_results, args.configuration


def _commit() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()  # noqa: S607


def _code_identity() -> dict[str, str]:
    root = Path(__file__).resolve().parents[3]
    paths = (
        *(
            Path("evaluation/memory/write_gate") / name
            for name in (
                "artifacts.py",
                "cli.py",
                "dataset.py",
                "jev.py",
                "prepare_public.py",
                "report.py",
                "resolver.py",
                "runner.py",
            )
        ),
        Path("examples/systemone/adapter.py"),
        Path("examples/systemone/laya.py"),
        Path("src/powercontext/builtin/runtime/memory_write_gate.py"),
        Path("src/powercontext/builtin/runtime/decision_model.py"),
        Path("src/powercontext/builtin/runtime/decision_policy.py"),
        Path("src/powercontext/builtin/artifacts/memory/protocols.py"),
        Path("src/powercontext/builtin/inference/models.py"),
        Path("src/powercontext/builtin/inference/errors.py"),
        Path("src/powercontext/builtin/decision_observations.py"),
    )
    return {path.as_posix(): sha256((root / path).read_bytes()).hexdigest() for path in paths}


def _code_digest() -> str:
    """Bind saved measurements to the evaluator and its assessment contracts."""

    return sha256(json.dumps(_code_identity(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
