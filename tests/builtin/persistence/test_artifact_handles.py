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

import asyncio
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import insert
from sqlalchemy.exc import IntegrityError

from powercontext.artifacts import ArtifactRef, StableHandle
from powercontext.builtin.persistence.artifact_handles import ArtifactHandleRepository
from powercontext.builtin.persistence.errors import RepositoryNotFoundError, StoredPayloadConflictError
from powercontext.builtin.persistence.sqlite import SQLiteConfig, SQLiteProfile
from powercontext.builtin.persistence.tables import ARTIFACTS_TABLE, BUILTIN_TABLES


class _Result:
    def __init__(self, value) -> None:
        self._value = value

    def one_or_none(self):
        return self._value

    def scalar_one_or_none(self):
        return self._value


class _Connection:
    def __init__(self, results) -> None:
        self._results = iter(results)

    async def execute(self, _statement):
        result = next(self._results)
        if isinstance(result, BaseException):
            raise result
        return _Result(result)


class _Database:
    def __init__(self, *connections: _Connection) -> None:
        self._connections = iter(connections)

    @asynccontextmanager
    async def transaction(self):
        yield next(self._connections)


def test_bind_and_resolve_exact_artifact_revision() -> None:
    async def scenario() -> None:
        async with SQLiteProfile.open(SQLiteConfig(), tables=BUILTIN_TABLES) as profile:
            repository = ArtifactHandleRepository(profile.database)
            target = ArtifactRef(family="profile", artifact_id="profile-1", revision=1)
            handle = StableHandle(family="profile", key="support-agent")
            async with profile.database.transaction() as connection:
                await connection.execute(
                    insert(ARTIFACTS_TABLE).values(
                        scope_id="scope-a",
                        family=target.family,
                        artifact_id=target.artifact_id,
                        revision=target.revision,
                        content=b"{}",
                    )
                )

            assert await repository.bind("scope-a", handle, target) == target
            assert await repository.bind("scope-a", handle, target) == target
            assert await repository.resolve("scope-a", handle) == target

    asyncio.run(scenario())


def test_handle_is_scope_local_and_cannot_be_rebound() -> None:
    async def scenario() -> None:
        async with SQLiteProfile.open(SQLiteConfig(), tables=BUILTIN_TABLES) as profile:
            repository = ArtifactHandleRepository(profile.database)
            handle = StableHandle(family="profile", key="support-agent")
            first = ArtifactRef(family="profile", artifact_id="profile-1", revision=1)
            second = ArtifactRef(family="profile", artifact_id="profile-2", revision=1)
            async with profile.database.transaction() as connection:
                await connection.execute(
                    insert(ARTIFACTS_TABLE),
                    [
                        {
                            "scope_id": scope_id,
                            "family": target.family,
                            "artifact_id": target.artifact_id,
                            "revision": target.revision,
                            "content": b"{}",
                        }
                        for scope_id in ("scope-a", "scope-b")
                        for target in (first, second)
                    ],
                )

            await repository.bind("scope-a", handle, first)
            await repository.bind("scope-b", handle, second)

            assert await repository.resolve("scope-a", handle) == first
            assert await repository.resolve("scope-b", handle) == second
            with pytest.raises(StoredPayloadConflictError):
                await repository.bind("scope-a", handle, second)

    asyncio.run(scenario())


def test_missing_handle_and_target_are_explicit() -> None:
    async def scenario() -> None:
        async with SQLiteProfile.open(SQLiteConfig(), tables=BUILTIN_TABLES) as profile:
            repository = ArtifactHandleRepository(profile.database)
            handle = StableHandle(family="profile", key="missing")
            target = ArtifactRef(family="profile", artifact_id="missing", revision=1)

            with pytest.raises(RepositoryNotFoundError, match="artifact_handle"):
                await repository.resolve("scope-a", handle)
            with pytest.raises(RepositoryNotFoundError, match="artifact"):
                await repository.bind("scope-a", handle, target)

    asyncio.run(scenario())


def test_foreign_key_race_reports_the_missing_artifact_not_a_missing_handle() -> None:
    async def scenario() -> None:
        integrity_error = IntegrityError("insert artifact handle", {}, Exception("foreign key failed"))
        database = _Database(
            _Connection([None, 1, integrity_error]),
            _Connection([None, None]),
        )
        repository = ArtifactHandleRepository(database)  # ty: ignore[invalid-argument-type]
        handle = StableHandle(family="profile", key="support-agent")
        target = ArtifactRef(family="profile", artifact_id="profile-1", revision=1)

        with pytest.raises(RepositoryNotFoundError) as error:
            await repository.bind("scope-a", handle, target)
        assert error.value.kind == "artifact"
        assert error.value.identity == ("scope-a", "profile", "profile-1", 1)

    asyncio.run(scenario())
