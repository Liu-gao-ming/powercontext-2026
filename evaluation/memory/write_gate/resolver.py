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

"""Recover evaluation inputs through exact, Scope-authorized read paths."""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Protocol

from powercontext.client.client import PowerContextClient
from powercontext.client.errors import (
    ForbiddenResponseError,
    ServerResponseError,
    UnauthorizedResponseError,
)

from .dataset import ArtifactRef, Case, Dataset, PublicDatasetRef, SourceRef, validate_split_pair
from .prepare_public import load_xnli, text_digest


class MissingInput(Exception):
    """An exact input reference no longer resolves."""


class Unauthorized(Exception):
    """Current credentials cannot read an input in the selected Scope."""


class UnsupportedReference(Exception):
    """A reference cannot reconstruct the exact gate input."""


@dataclass(frozen=True, slots=True)
class ResolvedCase:
    candidate: str
    evidence: tuple[str, ...]


class CaseResolver(Protocol):
    async def resolve(self, case: Case) -> ResolvedCase: ...


class SplitCheckedResolver:
    """Check cross-split gate inputs on each authorized read, retaining only hashes."""

    def __init__(self, calibration: Dataset, held_out: Dataset, resolver: CaseResolver) -> None:
        validate_split_pair(calibration, held_out)
        self._resolver = resolver
        self._splits = {case.case_id: dataset.split for dataset in (calibration, held_out) for case in dataset.cases}
        self._inputs: dict[str, set[bytes]] = {"calibration": set(), "held_out": set()}

    async def resolve(self, case: Case) -> ResolvedCase:
        split = self._splits[case.case_id]
        resolved = await self._resolver.resolve(case)
        # Record metadata and evidence order do not distinguish the evaluated input.
        payload = json.dumps([resolved.candidate, sorted(resolved.evidence)], ensure_ascii=False, separators=(",", ":"))
        digest = sha256(payload.encode("utf-8")).digest()
        other_split = "held_out" if split == "calibration" else "calibration"
        if digest in self._inputs[other_split]:
            raise ValueError("private gate inputs must not cross splits")  # noqa: TRY003
        self._inputs[split].add(digest)
        return resolved


class PublicDatasetResolver:
    """Resolve pinned public research inputs; no private Scope or credentials are used."""

    def __init__(self, archive: Path) -> None:
        self._rows, self.archive_sha256 = load_xnli(archive)

    async def resolve(self, case: Case) -> ResolvedCase:
        subject = case.subject
        if case.scope_id != "public-xnli" or not isinstance(subject, PublicDatasetRef) or len(case.evidence) != 1:
            raise UnsupportedReference
        evidence = case.evidence[0]
        if not isinstance(evidence, PublicDatasetRef):
            raise UnsupportedReference
        if (
            subject.field != "hypothesis"
            or evidence.field != "premise"
            or subject.language != case.language
            or evidence.language != subject.language
            or evidence.pair_id != subject.pair_id
            or evidence.split != subject.split
            or subject.archive_sha256 != self.archive_sha256
            or evidence.archive_sha256 != self.archive_sha256
        ):
            raise UnsupportedReference
        pair = self._rows[subject.split].get((subject.pair_id, subject.language))
        if pair is None:
            raise MissingInput
        if (
            text_digest(pair.hypothesis) != subject.content_digest
            or text_digest(pair.premise) != evidence.content_digest
            or pair.label != case.original_label
        ):
            raise MissingInput
        return ResolvedCase(candidate=pair.hypothesis, evidence=(pair.premise,))


class AuthorizedClientResolver:
    """Read an existing candidate revision and its evidence with the caller's Server token.

    Pending candidates have no immutable Artifact revision and are deliberately unsupported.
    Every source and Artifact is read through the same Scope and checked against the
    content digest pinned by the dataset. A changed or unavailable projection is not replayed.
    """

    def __init__(self, client: PowerContextClient) -> None:
        self._client = client

    async def resolve(self, case: Case) -> ResolvedCase:
        if not isinstance(case.subject, ArtifactRef):
            raise UnsupportedReference
        record = await self._artifact_record(case.scope_id, case.subject)
        cited = (
            *(("source", ref.source_type, ref.source_id) for ref in record.sources),
            *(("artifact", ref.family, ref.artifact_id, ref.revision) for ref in record.artifacts),
        )
        supplied = []
        for ref in case.evidence:
            if isinstance(ref, SourceRef):
                supplied.append(("source", ref.source_type, ref.source_id))
            elif isinstance(ref, ArtifactRef):
                supplied.append(("artifact", ref.family, ref.artifact_id, ref.revision))
            else:
                raise UnsupportedReference
        if cited != tuple(supplied):
            raise UnsupportedReference
        candidate = record.content.get("text")
        if not isinstance(candidate, str):
            raise UnsupportedReference
        evidence: list[str] = []
        for ref in case.evidence:
            if isinstance(ref, SourceRef):
                evidence.append(await self._source_text(case.scope_id, ref))
            elif isinstance(ref, ArtifactRef):
                evidence.append(await self._artifact_text(case.scope_id, ref))
            else:
                raise UnsupportedReference
        return ResolvedCase(candidate=candidate, evidence=tuple(evidence))

    async def _artifact_record(self, scope_id: str, ref: ArtifactRef):
        try:
            record = await self._client.get_artifact_revision(scope_id, ref.family, ref.artifact_id, ref.revision)
        except (ForbiddenResponseError, UnauthorizedResponseError) as error:
            raise Unauthorized from error
        except ServerResponseError as error:
            if error.status_code == 404:
                raise MissingInput from error
            if error.status_code in {400, 422}:
                raise UnsupportedReference from error
            raise
        if (
            record.scope_id != scope_id
            or record.family != ref.family
            or record.artifact_id != ref.artifact_id
            or record.revision != ref.revision
            or record.content_digest != ref.content_digest
        ):
            raise MissingInput
        return record

    async def _artifact_text(self, scope_id: str, ref: ArtifactRef) -> str:
        record = await self._artifact_record(scope_id, ref)
        value = record.content.get("text")
        if not isinstance(value, str):
            raise UnsupportedReference
        return value

    async def _source_text(self, scope_id: str, ref: SourceRef) -> str:
        try:
            record = await self._client.get_source(scope_id, ref.source_type, ref.source_id)
        except (ForbiddenResponseError, UnauthorizedResponseError) as error:
            raise Unauthorized from error
        except ServerResponseError as error:
            if error.status_code == 404:
                raise MissingInput from error
            if error.status_code in {400, 422}:
                raise UnsupportedReference from error
            raise
        if (
            record.scope_id != scope_id
            or record.source_type != ref.source_type
            or record.source_id != ref.source_id
            or record.content_digest != ref.content_digest
        ):
            raise MissingInput
        # The public Source read may differ from the original gate projection.
        # A digest captured from that exact projection proves when canonical content
        # reproduces it; without the digest, the input remains incomplete.
        if ref.gate_input_digest is None:
            raise UnsupportedReference
        text = json.dumps(record.content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if f"sha256:{sha256(text.encode('utf-8')).hexdigest()}" != ref.gate_input_digest:
            raise UnsupportedReference
        return text
