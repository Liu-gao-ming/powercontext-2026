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

"""Scope-local stable handles for exact Artifact revisions."""

from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError

from powercontext.artifacts import ArtifactRef, StableHandle
from powercontext.builtin.persistence.database import AsyncDatabase
from powercontext.builtin.persistence.errors import (
    InvalidRepositoryArgumentError,
    RepositoryNotFoundError,
    StoredPayloadConflictError,
)
from powercontext.builtin.persistence.tables import ARTIFACT_HANDLES_TABLE, ARTIFACTS_TABLE


class ArtifactHandleRepository:
    """Bind immutable, human-copyable names to exact Artifact revisions."""

    def __init__(self, database: AsyncDatabase, /) -> None:
        self._database = database

    async def bind(self, scope_id: str, handle: StableHandle, target: ArtifactRef, /) -> ArtifactRef:
        if handle.family != target.family:
            raise InvalidRepositoryArgumentError("family", "handle and target families must match")

        try:
            async with self._database.transaction() as connection:
                existing = await _resolve(connection, scope_id, handle)
                if existing is not None:
                    return _require_same_binding(scope_id, handle, existing, target)
                if not await _target_exists(connection, scope_id, target):
                    raise RepositoryNotFoundError("artifact", (scope_id, *target.model_dump().values()))
                await connection.execute(
                    insert(ARTIFACT_HANDLES_TABLE).values(
                        scope_id=scope_id,
                        family=handle.family,
                        handle_key=handle.key,
                        artifact_id=target.artifact_id,
                        revision=target.revision,
                    )
                )
        except IntegrityError as error:
            async with self._database.transaction() as connection:
                existing = await _resolve(connection, scope_id, handle)
                target_exists = await _target_exists(connection, scope_id, target)
            if existing is not None:
                # A concurrent binder won the handle-key race. Preserve
                # idempotency only when it committed the requested target.
                return _require_same_binding(scope_id, handle, existing, target)
            if not target_exists:
                raise RepositoryNotFoundError("artifact", (scope_id, *target.model_dump().values())) from error
            raise
        return target

    async def resolve(self, scope_id: str, handle: StableHandle, /) -> ArtifactRef:
        async with self._database.transaction() as connection:
            target = await _resolve(connection, scope_id, handle)
        if target is None:
            raise RepositoryNotFoundError("artifact_handle", (scope_id, handle.family, handle.key))
        return target


async def _resolve(connection, scope_id: str, handle: StableHandle) -> ArtifactRef | None:
    row = (
        await connection.execute(
            select(ARTIFACT_HANDLES_TABLE.c.artifact_id, ARTIFACT_HANDLES_TABLE.c.revision).where(
                ARTIFACT_HANDLES_TABLE.c.scope_id == scope_id,
                ARTIFACT_HANDLES_TABLE.c.family == handle.family,
                ARTIFACT_HANDLES_TABLE.c.handle_key == handle.key,
            )
        )
    ).one_or_none()
    if row is None:
        return None
    return ArtifactRef(family=handle.family, artifact_id=str(row.artifact_id), revision=int(row.revision))


async def _target_exists(connection, scope_id: str, target: ArtifactRef) -> bool:
    revision = (
        await connection.execute(
            select(ARTIFACTS_TABLE.c.revision).where(
                ARTIFACTS_TABLE.c.scope_id == scope_id,
                ARTIFACTS_TABLE.c.family == target.family,
                ARTIFACTS_TABLE.c.artifact_id == target.artifact_id,
                ARTIFACTS_TABLE.c.revision == target.revision,
            )
        )
    ).scalar_one_or_none()
    return revision is not None


def _require_same_binding(
    scope_id: str,
    handle: StableHandle,
    existing: ArtifactRef,
    requested: ArtifactRef,
) -> ArtifactRef:
    if existing != requested:
        raise StoredPayloadConflictError("artifact_handle", (scope_id, handle.family, handle.key))
    return existing


__all__ = ["ArtifactHandleRepository"]
