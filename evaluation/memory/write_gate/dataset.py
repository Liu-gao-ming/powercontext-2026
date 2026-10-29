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

"""Versioned, reference-only labels for Memory write-gate evaluation."""

from __future__ import annotations

from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Label(StrEnum):
    ACCEPT = "accept"
    HOLD = "hold"


class PublicDatasetRef(_StrictModel):
    kind: Literal["public_dataset"] = "public_dataset"
    dataset: Literal["xnli-1.0"] = "xnli-1.0"
    archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    split: Literal["dev", "test"]
    pair_id: str = Field(min_length=1, max_length=256)
    language: Literal["en", "zh"]
    field: Literal["premise", "hypothesis"]
    content_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class PublicDatasetSource(_StrictModel):
    dataset: Literal["xnli-1.0"] = "xnli-1.0"
    archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_url: Literal["https://dl.fbaipublicfiles.com/XNLI/XNLI-1.0.zip"] = (
        "https://dl.fbaipublicfiles.com/XNLI/XNLI-1.0.zip"
    )
    license: Literal["CC-BY-NC-4.0"] = "CC-BY-NC-4.0"
    preparation_version: Literal["xnli-reference-v1"] = "xnli-reference-v1"


class SourceRef(_StrictModel):
    kind: Literal["source"] = "source"
    source_type: Literal["content"] = "content"
    source_id: str = Field(min_length=1)
    content_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    gate_input_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")


class ArtifactRef(_StrictModel):
    kind: Literal["artifact"] = "artifact"
    family: Literal["atomic-memory"] = "atomic-memory"
    artifact_id: str = Field(min_length=1)
    revision: int = Field(ge=1)
    content_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


Reference = Annotated[PublicDatasetRef | SourceRef | ArtifactRef, Field(discriminator="kind")]


class Case(_StrictModel):
    case_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    group_id: str = Field(min_length=1, max_length=128)
    scope_id: str = Field(min_length=1, max_length=256)
    language: Literal["en", "zh"]
    label: Label
    label_reason: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_]*$")
    original_label: Literal["entailment", "contradiction", "neutral"] | None = None
    subject: Reference
    evidence: tuple[Reference, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_label_mapping(self) -> Case:
        if isinstance(self.subject, PublicDatasetRef):
            expected = Label.ACCEPT if self.original_label == "entailment" else Label.HOLD
            if self.original_label is None or self.label != expected or self.label_reason != self.original_label:
                raise ValueError("public dataset labels must preserve the declared NLI mapping")  # noqa: TRY003
        return self


class Dataset(_StrictModel):
    schema_version: Literal[1] = 1
    dataset_id: str = Field(min_length=1, max_length=128)
    split: Literal["calibration", "held_out"]
    public_source: PublicDatasetSource | None = None
    cases: tuple[Case, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_cases(self) -> Dataset:
        ids = [case.case_id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("case IDs must be unique")  # noqa: TRY003
        public = [
            ref for case in self.cases for ref in (case.subject, *case.evidence) if isinstance(ref, PublicDatasetRef)
        ]
        if bool(self.public_source) != bool(public) or (
            public and len(public) != sum(1 + len(case.evidence) for case in self.cases)
        ):
            raise ValueError("public manifests cannot mix private references")  # noqa: TRY003
        if public:
            expected_split = "dev" if self.split == "calibration" else "test"
            if self.public_source is None or any(
                ref.archive_sha256 != self.public_source.archive_sha256 or ref.split != expected_split for ref in public
            ):
                raise ValueError("public references must match the pinned source and original partition")  # noqa: TRY003
        return self


def load_dataset(path: Path) -> tuple[Dataset, str]:
    raw = path.read_bytes()
    return Dataset.model_validate_json(raw), sha256(raw).hexdigest()


def _private_reference_identity(ref: Reference) -> tuple[str | int, ...]:
    if isinstance(ref, ArtifactRef):
        return ref.kind, ref.family, ref.artifact_id, ref.revision
    if isinstance(ref, SourceRef):
        return ref.kind, ref.source_type, ref.source_id, ref.content_digest
    raise ValueError("private inputs cannot contain public references")  # noqa: TRY003


def _validate_private_input_overlap(calibration: Dataset, held_out: Dataset) -> None:
    reference_keys = [
        {
            (
                case.scope_id,
                _private_reference_identity(case.subject),
                tuple(sorted(_private_reference_identity(ref) for ref in case.evidence)),
            )
            for case in dataset.cases
        }
        for dataset in (calibration, held_out)
    ]
    # Digests also catch copied content under new identities or another Scope.
    # Evidence order and caller-assigned labels do not make a new input.
    content_keys = [
        {
            (case.subject.content_digest, tuple(sorted(ref.content_digest for ref in case.evidence)))
            for case in dataset.cases
        }
        for dataset in (calibration, held_out)
    ]
    if reference_keys[0] & reference_keys[1] or content_keys[0] & content_keys[1]:
        raise ValueError("private inputs must not cross splits")  # noqa: TRY003


def validate_split_pair(calibration: Dataset, held_out: Dataset) -> None:
    if calibration.split != "calibration" or held_out.split != "held_out":
        raise ValueError("expected calibration and held-out datasets")  # noqa: TRY003
    if calibration.dataset_id != held_out.dataset_id:
        raise ValueError("dataset IDs must match")  # noqa: TRY003
    if calibration.public_source != held_out.public_source:
        raise ValueError("dataset sources must match")  # noqa: TRY003
    if {case.case_id for case in calibration.cases} & {case.case_id for case in held_out.cases}:
        raise ValueError("case IDs must not cross splits")  # noqa: TRY003
    if {case.group_id for case in calibration.cases} & {case.group_id for case in held_out.cases}:
        raise ValueError("scenario groups must not cross splits")  # noqa: TRY003
    if calibration.public_source is None:
        _validate_private_input_overlap(calibration, held_out)
    for dataset in (calibration, held_out):
        for language in ("en", "zh"):
            if {case.label for case in dataset.cases if case.language == language} != {Label.ACCEPT, Label.HOLD}:
                raise ValueError(f"{dataset.split} needs accept and hold cases in {language}")  # noqa: TRY003
