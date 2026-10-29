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

"""Public regressions for the supported Access enforcement switch (#2299).

Data accepted while Access was disabled stays readable but immutable after
switching to enforced mode until an administrator migrates the missing
ownership data. These tests keep the switch flow honest: pre-switch inventory,
explicit attribution, idempotent application, preserved denials for
unauthorized identities, and conflict reporting.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import RuntimeConfig
from powercontext.server.authentication import AuthenticationResult, ProviderReadiness
from powercontext.server.authz import AccessConflictError, PrincipalRef
from powercontext.server.authz.composition import open_builtin_access_control
from powercontext.server.factory import create_server_app
from powercontext.server.ownership import apply_ownership_migration
from powercontext.server.settings import AccessControlConfig, McpConfig, MetricsConfig, ServerSettings

ADMIN = PrincipalRef(type="service", id="admin")
SERVER_TOKEN = PrincipalRef(type="service", id="server-token")
DEPLOYMENT = "ownership-migration"


class _Authentication:
    async def authenticate(self, request):
        identity = request.headers.get("authorization", "Bearer admin").removeprefix("Bearer ")
        subject = (
            ADMIN
            if identity == "admin"
            else SERVER_TOKEN
            if identity == "server-token"
            else PrincipalRef(type="user", id=identity)
        )
        return AuthenticationResult(subject=subject)

    async def readiness(self):
        return ProviderReadiness(ready=True)


def _settings(database: SQLiteConfig, mode: str) -> ServerSettings:
    return ServerSettings(
        database=database,
        runtime=RuntimeConfig(artifact_processing_families=()),
        access=AccessControlConfig(mode=mode, deployment_id=DEPLOYMENT),
        mcp=McpConfig(enabled=False),
        metrics=MetricsConfig(enabled=False),
    )


def checked(response: httpx.Response, expected: int) -> dict:
    data = response.json()
    assert response.status_code == expected, data
    return data


async def _create_legacy_data(client: httpx.AsyncClient) -> tuple[str, str, dict]:
    scope = checked(
        await client.post(
            "/v1/scopes",
            json={"title": "Legacy scope", "summary": "Created before enforcement", "idempotency_key": "legacy"},
        ),
        201,
    )["scope_id"]
    artifact = checked(
        await client.post(
            f"/v1/scopes/{scope}/artifacts",
            json={"family": "atomic-memory", "content": {"kind": "fact", "text": "Existing fact."}},
        ),
        201,
    )
    source = checked(
        await client.post(
            "/v1/sources/content",
            json={"scope_id": scope, "source_id": "legacy-source", "content": "A test passed."},
        ),
        202,
    )["source"]
    candidate = checked(
        await client.post(
            "/v1/experience/propose",
            json={
                "scope_id": scope,
                "source_refs": [source],
                "artifact_refs": [],
                "proposal": {
                    "situation": "API changes.",
                    "action": "Run tests.",
                    "outcome": "Tests pass.",
                    "lesson": "Run tests before delivery.",
                },
            },
        ),
        201,
    )
    return scope, f"/v1/scopes/{scope}/artifacts/atomic-memory/{artifact['artifact_id']}", candidate


def test_disabled_data_becomes_mutable_again_after_ownership_migration(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'migration.db'}")

        app = create_server_app(
            settings=_settings(database, "disabled"),
            scheduler_path=tmp_path / "scheduler.db",
        )
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
        ):
            scope, artifact_path, candidate = await _create_legacy_data(client)
            plan = checked(
                await client.post("/v1/access/ownership/plan", json={"scope_ids": [scope]}),
                200,
            )
            assert plan["mode"] == "disabled"
            scope_plan = next(item for item in plan["scopes"] if item["scope_id"] == scope)
            assert [gap["family"] for gap in scope_plan["artifact_gaps"]] == ["atomic-memory"]
            assert [gap["candidate_id"] for gap in scope_plan["candidate_gaps"]] == [candidate["candidate_id"]]
            apply_while_disabled = await client.post(
                "/v1/access/ownership/apply",
                json={"owner": {"type": "service", "id": "admin"}, "scope_ids": [scope]},
            )
            assert apply_while_disabled.status_code == 503
            assert apply_while_disabled.json()["error"]["code"] == "ownership_migration_unavailable"

        async with open_builtin_access_control(
            database, bootstrap_administrators=(ADMIN,), deployment_id=DEPLOYMENT
        ) as access:
            app = create_server_app(
                settings=_settings(database, "enforced"),
                authentication_provider=_Authentication(),
                access_control=access,
                scheduler_path=tmp_path / "scheduler.db",
            )
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
            ):
                headers = {"Authorization": "Bearer admin"}
                artifact_headers = {**headers, "If-Match": '"revision:1"'}
                assert (
                    await client.put(
                        artifact_path,
                        headers=artifact_headers,
                        json={"content": {"kind": "fact", "text": "Updated fact."}},
                    )
                ).status_code == 403
                assert (
                    await client.post(
                        "/v1/candidates/approve",
                        headers=headers,
                        json={
                            "scope_id": scope,
                            "candidate_id": candidate["candidate_id"],
                            "expected_version": candidate["version"],
                        },
                    )
                ).status_code == 503

                plan = checked(
                    await client.post("/v1/access/ownership/plan", headers=headers, json={"scope_ids": [scope]}),
                    200,
                )
                scope_plan = next(item for item in plan["scopes"] if item["scope_id"] == scope)
                assert [gap["family"] for gap in scope_plan["artifact_gaps"]] == ["atomic-memory"]

                background_plan = checked(
                    await client.post(
                        "/v1/access/ownership/plan",
                        headers=headers,
                        json={
                            "scope_ids": [scope],
                            "background_principal": {"type": "service", "id": "background-worker"},
                        },
                    ),
                    200,
                )
                assert [gap["scope_id"] for gap in background_plan["background"]] == [scope]
                assert background_plan["background"][0]["principal_id"] == "background-worker"
                assert background_plan["background"][0]["allowed"] is False

                preview = checked(
                    await client.post(
                        "/v1/access/ownership/apply",
                        headers=headers,
                        json={"owner": {"type": "service", "id": "admin"}, "scope_ids": [scope], "dry_run": True},
                    ),
                    200,
                )
                assert preview["dry_run"] is True
                assert len(preview["applied"]) == 2
                assert preview["conflicts"] == []

                applied = checked(
                    await client.post(
                        "/v1/access/ownership/apply",
                        headers=headers,
                        json={"owner": {"type": "service", "id": "admin"}, "scope_ids": [scope]},
                    ),
                    200,
                )
                assert sorted(item["kind"] for item in applied["applied"]) == [
                    "artifact_owner",
                    "candidate_attestation",
                ]
                assert applied["conflicts"] == []

                updated = checked(
                    await client.put(
                        artifact_path,
                        headers=artifact_headers,
                        json={"content": {"kind": "fact", "text": "Updated fact."}},
                    ),
                    200,
                )
                assert updated["revision"] >= 2
                approved = checked(
                    await client.post(
                        "/v1/candidates/approve",
                        headers=headers,
                        json={
                            "scope_id": scope,
                            "candidate_id": candidate["candidate_id"],
                            "expected_version": candidate["version"],
                        },
                    ),
                    200,
                )
                assert approved["status"] == "approved"

                repeated = checked(
                    await client.post(
                        "/v1/access/ownership/apply",
                        headers=headers,
                        json={"owner": {"type": "service", "id": "admin"}, "scope_ids": [scope]},
                    ),
                    200,
                )
                assert repeated["applied"] == []
                assert repeated["conflicts"] == []

                unprivileged = {"Authorization": "Bearer worker"}
                denied_plan = await client.post(
                    "/v1/access/ownership/plan", headers=unprivileged, json={"scope_ids": [scope]}
                )
                assert denied_plan.status_code == 403
                denied_apply = await client.post(
                    "/v1/access/ownership/apply",
                    headers=unprivileged,
                    json={"owner": {"type": "user", "id": "worker"}, "scope_ids": [scope]},
                )
                assert denied_apply.status_code == 403

    asyncio.run(scenario())


def test_offline_cli_migration_repairs_the_switch(tmp_path: Path) -> None:
    """The offline command plans and applies without a running Server, before or after the switch."""

    async def scenario() -> None:
        from powercontext.server.cli import _access_ownership_maintenance

        database = SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'cli.db'}")
        app = create_server_app(
            settings=_settings(database, "disabled"),
            scheduler_path=tmp_path / "scheduler.db",
        )
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
        ):
            scope, artifact_path, candidate = await _create_legacy_data(client)

        settings = _settings(database, "disabled")
        planned = await _access_ownership_maintenance(settings, "plan", owner=None, scope_ids=(scope,))
        assert planned is True
        applied = await _access_ownership_maintenance(
            settings, "apply", owner="service:server-token", scope_ids=(scope,)
        )
        assert applied is True

        async with open_builtin_access_control(
            database, bootstrap_administrators=(ADMIN,), deployment_id=DEPLOYMENT
        ) as access:
            app = create_server_app(
                settings=_settings(database, "enforced"),
                authentication_provider=_Authentication(),
                access_control=access,
                scheduler_path=tmp_path / "scheduler.db",
            )
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
            ):
                headers = {"Authorization": "Bearer admin"}
                assert (
                    await client.put(
                        artifact_path,
                        headers={"Authorization": "Bearer server-token", "If-Match": '"revision:1"'},
                        json={"content": {"kind": "fact", "text": "Updated fact."}},
                    )
                ).status_code == 200
                approved = checked(
                    await client.post(
                        "/v1/candidates/approve",
                        headers=headers,
                        json={
                            "scope_id": scope,
                            "candidate_id": candidate["candidate_id"],
                            "expected_version": candidate["version"],
                        },
                    ),
                    200,
                )
                assert approved["status"] == "approved"
                again = await _access_ownership_maintenance(
                    settings, "apply", owner="service:server-token", scope_ids=(scope,)
                )
                assert again is True

    asyncio.run(scenario())


def test_apply_reports_conflicting_ownership_without_failing(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'conflict.db'}")
        app = create_server_app(
            settings=_settings(database, "disabled"),
            scheduler_path=tmp_path / "scheduler.db",
        )
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
        ):
            scope, artifact_path, _ = await _create_legacy_data(client)
        artifact_id = artifact_path.rsplit("/", 1)[-1]

        async with open_builtin_access_control(
            database, bootstrap_administrators=(ADMIN,), deployment_id=DEPLOYMENT
        ) as access:
            app = create_server_app(
                settings=_settings(database, "enforced"),
                authentication_provider=_Authentication(),
                access_control=access,
                scheduler_path=tmp_path / "scheduler.db",
            )
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
            ):
                application = app.state.application
                original_establish = access.establish_artifact_owner
                attempts = 0

                async def conflicting_establish(resource, owner, *, idempotency_key, context):
                    nonlocal attempts
                    if resource.artifact_id == artifact_id:
                        attempts += 1
                        raise AccessConflictError("artifact-owner")
                    return await original_establish(resource, owner, idempotency_key=idempotency_key, context=context)

                access.establish_artifact_owner = conflicting_establish  # type: ignore[method-assign]
                result = await apply_ownership_migration(
                    application,
                    access,
                    [scope],
                    owner=ADMIN,
                )
                assert attempts == 1
                assert [item.gap.artifact_id for item in result.conflicts] == [artifact_id]
                assert result.conflicts[0].detail == "artifact-owner"
                assert result.applied

    asyncio.run(scenario())
