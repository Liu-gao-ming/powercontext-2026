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

"""Supported ownership migration after switching Access enforcement on.

Data accepted while Access was disabled has no Artifact owner and no Review
Candidate owner attestation, so switching ``ACCESS_MODE`` to ``enforced``
leaves that data read-only for every Principal and blocks Candidate approval.
Migration never invents identity: an administrator names the Principal for one
explicit call, existing relations stay untouched, and every item write is one
idempotent Access-store transaction that can be safely repeated.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING

from powercontext.server.authz import (
    AccessAction,
    AccessAuditContext,
    AccessConflictError,
    AccessInvalidRequestError,
    PrincipalRef,
    ResourceRef,
)
from powercontext.server.authz.models import MemoryEntrySelector

if TYPE_CHECKING:
    from powercontext.builtin.runtime import BuiltinRuntime
    from powercontext.server.app import ServerApplication
    from powercontext.server.authz.service import AccessControlService

    Application = ServerApplication | BuiltinRuntime

OWNED_ARTIFACT_FAMILIES = ("atomic-memory", "experience", "skill", "profile")
RESOURCE_PAGE_SIZE = 100
CANDIDATE_PAGE_SIZE = 100

PLAN_OPERATION = "access.ownership.plan"
MIGRATION_OPERATION = "access.ownership.apply"


@dataclass(frozen=True, slots=True)
class ArtifactOwnershipGap:
    """One logical Artifact identity whose owner relation is absent."""

    scope_id: str
    family: str
    artifact_id: str
    entry_id: str | None = None


@dataclass(frozen=True, slots=True)
class CandidateOwnershipGap:
    """One pending artifact-kind Candidate whose owner attestation is absent."""

    scope_id: str
    candidate_id: str
    family: str
    version: int
    entry_ids: tuple[str, ...] = ()
    target_family: str | None = None
    target_artifact_id: str | None = None


@dataclass(frozen=True, slots=True)
class ScopeOwnershipPlan:
    scope_id: str
    artifact_gaps: tuple[ArtifactOwnershipGap, ...]
    candidate_gaps: tuple[CandidateOwnershipGap, ...]


@dataclass(frozen=True, slots=True)
class BackgroundPermissionGap:
    """One affected Scope where the named background Principal lacks write authority."""

    scope_id: str
    principal_id: str
    reason_code: str | None = None


@dataclass(frozen=True, slots=True)
class OwnershipPlan:
    """The complete missing-attribution inventory for the requested Scopes."""

    scopes: tuple[ScopeOwnershipPlan, ...]
    background: tuple[BackgroundPermissionGap, ...]


@dataclass(frozen=True, slots=True)
class OwnershipDisposition:
    """One migration item with its per-item result."""

    gap: ArtifactOwnershipGap | CandidateOwnershipGap
    outcome: str  # "applied" or "conflict"
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class OwnershipMigrationResult:
    applied: tuple[OwnershipDisposition, ...]
    conflicts: tuple[OwnershipDisposition, ...]


def migration_context(operation: str, *, transport: str = "http") -> AccessAuditContext:
    return AccessAuditContext(transport=transport, operation=operation)


def ownership_report(value: OwnershipPlan | OwnershipMigrationResult) -> dict[str, object]:
    """Serialize a plan or migration result for operator-facing JSON output."""

    from dataclasses import asdict

    return asdict(value)


async def plan_ownership_migration(
    application: Application,
    access: AccessControlService | None,
    scope_ids: Sequence[str],
    *,
    background_principal: PrincipalRef | None = None,
    transport: str = "http",
) -> OwnershipPlan:
    """Inventory missing Artifact owners and Candidate attestations for Scopes.

    Without an active Access store (pre-switch ``disabled`` mode) every owned
    Artifact Family and every pending Candidate is a gap, which is the check an
    administrator runs before enabling enforcement.
    """

    scopes: list[ScopeOwnershipPlan] = []
    for scope_id in scope_ids:
        scopes.append(
            ScopeOwnershipPlan(
                scope_id=scope_id,
                artifact_gaps=await _artifact_gaps(application, access, scope_id),
                candidate_gaps=await _candidate_gaps(application, access, scope_id),
            )
        )
    background: list[BackgroundPermissionGap] = []
    if background_principal is not None and access is not None:
        context = migration_context(PLAN_OPERATION, transport=transport)
        affected = sorted({scope.scope_id for scope in scopes if scope.artifact_gaps or scope.candidate_gaps})
        for scope_id in affected:
            decision = await access.check(
                background_principal,
                AccessAction.SCOPE_CONTRIBUTE,
                ResourceRef.scope(scope_id),
                context=context,
            )
            if not decision.allowed:
                background.append(
                    BackgroundPermissionGap(
                        scope_id=scope_id,
                        principal_id=background_principal.id,
                        reason_code=decision.reason_code,
                    )
                )
    return OwnershipPlan(scopes=tuple(scopes), background=tuple(background))


async def apply_ownership_migration(
    application: Application,
    access: AccessControlService,
    scope_ids: Sequence[str],
    *,
    owner: PrincipalRef,
    dry_run: bool = False,
    transport: str = "http",
) -> OwnershipMigrationResult:
    """Backfill the relations missing per plan, attributed to ``owner``.

    The inventory is recomputed at apply time, so interrupted or repeated runs
    refill only still-missing items. An existing relation with a different
    Principal is reported as a conflict, never overwritten.
    """

    _validate_migration_owner(owner)
    context = migration_context(MIGRATION_OPERATION, transport=transport)
    plan = await plan_ownership_migration(application, access, scope_ids)
    items = (
        *(gap for scope in plan.scopes for gap in scope.artifact_gaps),
        *(gap for scope in plan.scopes for gap in scope.candidate_gaps),
    )
    if dry_run:
        preview: list[OwnershipDisposition] = []
        for item in items:
            if isinstance(item, CandidateOwnershipGap):
                preview.extend(
                    OwnershipDisposition(
                        gap=ArtifactOwnershipGap(
                            scope_id=item.scope_id,
                            family=item.family,
                            artifact_id=item.target_artifact_id or "",
                            entry_id=entry_id,
                        ),
                        outcome="applied",
                    )
                    for entry_id in item.entry_ids
                )
            preview.append(OwnershipDisposition(gap=item, outcome="applied"))
        return OwnershipMigrationResult(applied=tuple(preview), conflicts=())
    applied: list[OwnershipDisposition] = []
    conflicts: list[OwnershipDisposition] = []
    for item in items:
        item_applied, item_conflicts = await _apply_gap(access, item, owner, context)
        applied.extend(item_applied)
        conflicts.extend(item_conflicts)
    return OwnershipMigrationResult(applied=tuple(applied), conflicts=tuple(conflicts))


async def _apply_gap(
    access: AccessControlService,
    item: ArtifactOwnershipGap | CandidateOwnershipGap,
    owner: PrincipalRef,
    context: AccessAuditContext,
) -> tuple[list[OwnershipDisposition], list[OwnershipDisposition]]:
    """Write one idempotent item; conflicts report the loser without failing the run."""

    if isinstance(item, ArtifactOwnershipGap):
        idempotency_key, resource = _owner_identity(item)
        try:
            await access.establish_artifact_owner(resource, owner, idempotency_key=idempotency_key, context=context)
        except AccessConflictError as error:
            return [], [_conflict(item, error)]
        return [OwnershipDisposition(gap=item, outcome="applied")], []
    applied: list[OwnershipDisposition] = []
    conflicts: list[OwnershipDisposition] = []
    for entry_id in item.entry_ids:
        entry_gap = ArtifactOwnershipGap(
            scope_id=item.scope_id,
            family=item.family,
            artifact_id=item.target_artifact_id or "",
            entry_id=entry_id,
        )
        identity_key, entry_resource = _owner_identity(entry_gap)
        try:
            await access.establish_artifact_owner(
                entry_resource,
                owner,
                idempotency_key=identity_key,
                context=context,
            )
        except AccessConflictError as error:
            conflicts.append(_conflict(entry_gap, error))
            continue
        applied.append(OwnershipDisposition(gap=entry_gap, outcome="applied"))
    try:
        await _attest_gap_owner(access, item, owner)
    except AccessConflictError as error:
        conflicts.append(_conflict(item, error))
        return applied, conflicts
    applied.append(OwnershipDisposition(gap=item, outcome="applied"))
    return applied, conflicts


def _conflict(
    gap: ArtifactOwnershipGap | CandidateOwnershipGap,
    error: AccessConflictError,
) -> OwnershipDisposition:
    return OwnershipDisposition(gap=gap, outcome="conflict", detail=error.code)


def _validate_migration_owner(owner: PrincipalRef) -> None:
    if owner.type not in {"user", "service"}:
        raise AccessInvalidRequestError("principal")
    if not owner.id or owner.id != owner.id.strip() or len(owner.id) > 256:
        raise AccessInvalidRequestError("principal")


async def _artifact_gaps(
    application: Application,
    access: AccessControlService | None,
    scope_id: str,
) -> tuple[ArtifactOwnershipGap, ...]:
    records = application.records.for_scope(scope_id)

    async def family_gaps(family: str) -> tuple[ArtifactOwnershipGap, ...]:
        gaps: list[ArtifactOwnershipGap] = []
        cursor: str | None = None
        while True:
            page = await records.query_artifacts(family, cursor=cursor, limit=RESOURCE_PAGE_SIZE)
            for record in page.items:
                resource = ResourceRef.artifact(scope_id, family=family, artifact_id=record.artifact_id)
                if access is not None and await access.artifact_owner(resource) is not None:
                    continue
                gaps.append(ArtifactOwnershipGap(scope_id=scope_id, family=family, artifact_id=record.artifact_id))
            cursor = page.next_cursor
            if cursor is None:
                return tuple(gaps)

    gaps: list[ArtifactOwnershipGap] = []
    for family in OWNED_ARTIFACT_FAMILIES:
        gaps.extend(await family_gaps(family))
    if application.handoff is not None and await application.handoff.for_scope(scope_id).revisions():
        resource = ResourceRef.artifact(scope_id, family="handoff", artifact_id="handoff")
        if access is None or await access.artifact_owner(resource) is None:
            gaps.append(ArtifactOwnershipGap(scope_id=scope_id, family="handoff", artifact_id="handoff"))
    return tuple(gaps)


async def _candidate_gaps(
    application: Application,
    access: AccessControlService | None,
    scope_id: str,
) -> tuple[CandidateOwnershipGap, ...]:
    from powercontext.builtin.review.models import CandidateStatus
    from powercontext.builtin.runtime.models import ListCandidatesRequest

    review = application.review.for_scope(scope_id)
    gaps: list[CandidateOwnershipGap] = []
    cursor: str | None = None
    while True:
        page = await review.list(
            ListCandidatesRequest(status=CandidateStatus.PENDING, cursor=cursor, limit=CANDIDATE_PAGE_SIZE)
        )
        for candidate in page.candidates:
            if candidate.candidate_kind != "artifact":
                continue
            if access is not None and await access.candidate_owner(scope_id, candidate.candidate_id) is not None:
                continue
            target = candidate.target
            gaps.append(
                CandidateOwnershipGap(
                    scope_id=scope_id,
                    candidate_id=candidate.candidate_id,
                    family=candidate.family,
                    version=candidate.version,
                    entry_ids=_memory_entry_ids(candidate) if candidate.family == "memory" else (),
                    target_family=None if target is None else target.family,
                    target_artifact_id=None if target is None else target.artifact_id,
                )
            )
        cursor = page.next_cursor
        if cursor is None:
            return tuple(gaps)


async def _attest_gap_owner(
    access: AccessControlService,
    gap: CandidateOwnershipGap,
    owner: PrincipalRef,
) -> None:
    target = (
        None
        if gap.target_family is None or gap.target_artifact_id is None
        else ResourceRef.artifact(gap.scope_id, family=gap.target_family, artifact_id=gap.target_artifact_id)
    )
    await access.attest_candidate_owner(
        scope_id=gap.scope_id,
        candidate_id=gap.candidate_id,
        family=gap.family,
        proposed_owner=owner,
        target=target,
        idempotency_key=f"candidate-owner:{gap.scope_id}:{gap.candidate_id}",
    )


def _memory_entry_ids(candidate: object) -> tuple[str, ...]:
    changes = getattr(getattr(candidate, "proposal", None), "changes", None)
    if not isinstance(changes, Sequence):
        return ()
    entry_ids: list[str] = []
    for change in changes:
        entry_id = getattr(change, "entry_id", None)
        if isinstance(entry_id, str) and entry_id not in entry_ids:
            entry_ids.append(entry_id)
    return tuple(entry_ids)


def _owner_identity(gap: ArtifactOwnershipGap) -> tuple[str, ResourceRef]:
    if gap.entry_id is not None:
        resource = ResourceRef.artifact(
            gap.scope_id,
            family=gap.family,
            artifact_id=gap.artifact_id,
            selector=MemoryEntrySelector(entry_id=gap.entry_id),
        )
        return f"base-artifact-owner:{sha256(resource.key.encode()).hexdigest()}", resource
    if gap.family == "profile" and gap.artifact_id == "profile":
        key = f"profile-owner:{gap.scope_id}"
    else:
        key = f"{gap.family}-owner:{gap.scope_id}:{gap.artifact_id}"
    return key, ResourceRef.artifact(gap.scope_id, family=gap.family, artifact_id=gap.artifact_id)
