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

"""Prepare reference-only bilingual XNLI manifests from the official external archive."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import dataclass
from hashlib import sha256
from io import TextIOWrapper
from pathlib import Path
from typing import Any
from zipfile import ZipFile

from .dataset import Case, Dataset, Label, PublicDatasetSource, validate_split_pair

LABELS = ("entailment", "contradiction", "neutral")
LANGUAGES = ("en", "zh")
ARCHIVE_SHA256 = "4ba1d5e1afdb7161f0f23c66dc787802ccfa8a25a3ddd3b165a35e50df346ab1"


@dataclass(frozen=True, slots=True)
class NliPair:
    pair_id: str
    language: str
    label: str
    premise: str
    hypothesis: str


def text_digest(text: str) -> str:
    return "sha256:" + sha256(text.encode("utf-8")).hexdigest()


def load_xnli(path: Path) -> tuple[dict[str, dict[tuple[str, str], NliPair]], str]:
    """Read only the named official members; no archive extraction or model calls."""

    digest = sha256(path.read_bytes()).hexdigest()
    splits: dict[str, dict[tuple[str, str], NliPair]] = {}
    with ZipFile(path) as archive:
        for split in ("dev", "test"):
            info = archive.getinfo(f"XNLI-1.0/xnli.{split}.jsonl")
            if info.file_size > 100_000_000:
                raise ValueError("XNLI member exceeds the supported input size")  # noqa: TRY003
            rows: dict[tuple[str, str], NliPair] = {}
            with archive.open(info) as binary, TextIOWrapper(binary, encoding="utf-8") as handle:
                for line in handle:
                    row = json.loads(line)
                    language = row.get("language")
                    if language not in LANGUAGES:
                        continue
                    values = (row.get("pairID"), row.get("gold_label"), row.get("sentence1"), row.get("sentence2"))
                    if not all(isinstance(value, str) and value for value in values):
                        raise ValueError("XNLI row has incomplete identity, label, or text")  # noqa: TRY003
                    pair = NliPair(values[0], language, values[1], values[2], values[3])
                    key = (pair.pair_id, pair.language)
                    if key in rows:
                        raise ValueError("XNLI contains a duplicate pair/language identity")  # noqa: TRY003
                    rows[key] = pair
            splits[split] = rows
    return splits, digest


def _groups(rows: dict[tuple[str, str], NliPair], excluded: Counter[str]) -> list[tuple[NliPair, NliPair]]:
    result = []
    for pair_id in sorted({key[0] for key in rows}):
        english = rows.get((pair_id, "en"))
        chinese = rows.get((pair_id, "zh"))
        if english is None or chinese is None:
            excluded["missing_translation"] += 1
        elif english.label not in LABELS or chinese.label != english.label:
            excluded["invalid_or_inconsistent_label"] += 1
        else:
            result.append((english, chinese))
    return sorted(result, key=lambda group: sha256(group[0].pair_id.encode()).hexdigest())


def _identities(group: tuple[NliPair, NliPair]) -> set[str]:
    return {text_digest(text) for pair in group for text in (pair.premise, pair.hypothesis)}


def _select(
    groups: list[tuple[NliPair, NliPair]],
    count: int,
    blocked_text: set[str],
    blocked_ids: set[str],
    excluded: Counter[str],
) -> list[tuple[NliPair, NliPair]]:
    selected: list[tuple[NliPair, NliPair]] = []
    counts: Counter[str] = Counter()
    selected_text: set[str] = set()
    for group in groups:
        if group[0].pair_id in blocked_ids or _identities(group) & blocked_text:
            excluded["cross_split_identity_or_text"] += 1
            continue
        if counts[group[0].label] < count:
            if _identities(group) & selected_text:
                excluded["within_split_repeated_text"] += 1
                continue
            selected.append(group)
            counts[group[0].label] += 1
            selected_text.update(_identities(group))
    if any(counts[label] != count for label in LABELS):
        raise ValueError("not enough eligible bilingual groups for the declared sample")  # noqa: TRY003
    return selected


def _dataset(groups: list[tuple[NliPair, NliPair]], split: str, digest: str) -> Dataset:
    cases = []
    for group in groups:
        group_id = "xnli-" + sha256(group[0].premise.encode()).hexdigest()
        for pair in group:
            reference = {
                "archive_sha256": digest,
                "split": split,
                "pair_id": pair.pair_id,
                "language": pair.language,
            }
            cases.append(
                Case.model_validate({
                    "case_id": f"xnli-{split}-{sha256(pair.pair_id.encode()).hexdigest()[:24]}-{pair.language}",
                    "group_id": group_id,
                    "scope_id": "public-xnli",
                    "language": pair.language,
                    "label": Label.ACCEPT if pair.label == "entailment" else Label.HOLD,
                    "label_reason": pair.label,
                    "original_label": pair.label,
                    "subject": {
                        **reference,
                        "kind": "public_dataset",
                        "field": "hypothesis",
                        "content_digest": text_digest(pair.hypothesis),
                    },
                    "evidence": [
                        {
                            **reference,
                            "kind": "public_dataset",
                            "field": "premise",
                            "content_digest": text_digest(pair.premise),
                        }
                    ],
                })
            )
    return Dataset(
        dataset_id="memory-write-gate-xnli-v1",
        split="calibration" if split == "dev" else "held_out",
        public_source=PublicDatasetSource(archive_sha256=digest),
        cases=tuple(cases),
    )


def prepare(path: Path, output_dir: Path) -> dict[str, Any]:
    """Use fixed dev/test partitions and stable, label-balanced group selection."""

    rows, digest = load_xnli(path)
    if digest != ARCHIVE_SHA256:
        raise ValueError("archive differs from the pinned official XNLI 1.0 download")  # noqa: TRY003
    excluded: Counter[str] = Counter()
    calibration_groups = _select(_groups(rows["dev"], excluded), 100, set(), set(), excluded)
    blocked_text = set().union(*(_identities(group) for group in calibration_groups))
    held_groups = _select(
        _groups(rows["test"], excluded),
        300,
        blocked_text,
        {group[0].pair_id for group in calibration_groups},
        excluded,
    )
    calibration = _dataset(calibration_groups, "dev", digest)
    held_out = _dataset(held_groups, "test", digest)
    validate_split_pair(calibration, held_out)
    output_dir.mkdir(parents=True, exist_ok=False)
    manifest_hashes = {}
    for dataset in (calibration, held_out):
        raw = dataset.model_dump_json(indent=2) + "\n"
        (output_dir / f"{dataset.split}.json").write_text(raw, encoding="utf-8")
        manifest_hashes[dataset.split] = sha256(raw.encode()).hexdigest()
    summary = {
        "preparation_version": "xnli-reference-v1",
        "source": PublicDatasetSource(archive_sha256=digest).model_dump(mode="json"),
        "manifest_hashes": manifest_hashes,
        "excluded_groups": dict(sorted(excluded.items())),
        "splits": {
            dataset.split: {
                "cases": len(dataset.cases),
                "source_pairs": len(dataset.cases) // 2,
                "labels_by_language": {
                    language: dict(Counter(case.original_label for case in dataset.cases if case.language == language))
                    for language in LANGUAGES
                },
            }
            for dataset in (calibration, held_out)
        },
        "measurement_status": "not_run",
        "limitations": ["sentence_pair_benchmark", "training_overlap_unknown", "not_production_write_distribution"],
    }
    (output_dir / "preparation.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.archive, args.output_dir), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
