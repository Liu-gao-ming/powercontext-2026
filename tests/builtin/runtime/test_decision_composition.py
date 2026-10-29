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

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from pydantic import AnyHttpUrl, SecretStr

from powercontext.builtin.inference import InferenceUnavailableError, InferenceUsage
from powercontext.builtin.persistence.oceanbase import OceanBaseConfig
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import (
    BuiltinConfig,
    RuntimeConfig,
    composition,
    open_builtin_runtime,
    preflight_builtin_runtime,
)
from powercontext.builtin.runtime.composition import (
    BuiltinConfigurationError,
    _generation_pipelines,
    _resolve_memory_write_gate_sanitizer,
)
from powercontext.builtin.runtime.config import InferenceConfig
from powercontext.builtin.runtime.decision_model import (
    DECISION_INSTRUCTIONS_VERSION,
    DecisionOutcome,
    DecisionRequest,
    DecisionResult,
    FailOpenDecisionModel,
    LLMDecisionModel,
)
from powercontext.builtin.runtime.decision_policy import DecisionPrivacyBoundary
from powercontext.builtin.runtime.decision_projection import (
    DecisionRequestProjection,
    KnownSecretDecisionRequestSanitizer,
)
from powercontext.builtin.sources import BUILTIN_SOURCE_REGISTRY


class _FakeDecisionModel:
    """Injected backend used to prove the seam, wrapping, and fail-open path."""

    policy_id = "powercontext.decision.fake.v1"

    def __init__(self, *, outcome: DecisionOutcome = DecisionOutcome.YES, fail: bool = False) -> None:
        self._outcome = outcome
        self._fail = fail
        self.requests: list[DecisionRequest] = []

    async def evaluate(self, request: DecisionRequest, /) -> DecisionResult:
        self.requests.append(request)
        if self._fail:
            raise InferenceUnavailableError("evaluate")
        return DecisionResult(self._outcome, self.policy_id, InferenceUsage(requests=1))


class _LocalDecisionModel(_FakeDecisionModel):
    is_local_only = True


class _HangingDecisionModel:
    policy_id = "powercontext.decision.hanging.v1"

    async def evaluate(self, request: DecisionRequest, /) -> DecisionResult:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


def _config(tmp_path: Path, **runtime: Any) -> BuiltinConfig:
    return BuiltinConfig(
        database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'runtime.db'}"),
        runtime=RuntimeConfig(**runtime),
    )


def test_decision_role_is_disabled_without_an_opt_in(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with open_builtin_runtime(_config(tmp_path)) as runtime:
            assert runtime.decision_model is None

    asyncio.run(scenario())


def test_injected_decision_model_is_exposed_fail_open_wrapped(tmp_path: Path) -> None:
    async def scenario() -> None:
        delegate = _FakeDecisionModel()
        async with open_builtin_runtime(_config(tmp_path), decision_model=delegate) as runtime:
            exposed = runtime.decision_model
            assert isinstance(exposed, FailOpenDecisionModel)
            assert exposed.policy_id == delegate.policy_id

            request = DecisionRequest("memory.write-gate", "Keep this?", "note")
            result = await exposed.evaluate(request)

            assert result.outcome is DecisionOutcome.YES
            assert result.used_fallback is False
            assert delegate.requests == [request]

    asyncio.run(scenario())


def test_injected_decision_failure_degrades_on_the_exposed_seam(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with open_builtin_runtime(_config(tmp_path), decision_model=_FakeDecisionModel(fail=True)) as runtime:
            exposed = runtime.decision_model
            assert exposed is not None

            result = await exposed.evaluate(DecisionRequest("memory.write-gate", "Keep this?", "note"))

            assert result.outcome is DecisionOutcome.ABSTAIN
            assert result.used_fallback is True

    asyncio.run(scenario())


def test_injected_decision_model_uses_the_configured_timeout(tmp_path: Path) -> None:
    config = BuiltinConfig(
        database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'runtime.db'}"),
        inference=InferenceConfig(decision_timeout_seconds=0.01),
    )

    async def scenario() -> None:
        async with open_builtin_runtime(config, decision_model=_HangingDecisionModel()) as runtime:
            exposed = runtime.decision_model
            assert exposed is not None

            result = await asyncio.wait_for(
                exposed.evaluate(DecisionRequest("memory.write-gate", "Keep this?", "note")),
                timeout=0.5,
            )

            assert result.outcome is DecisionOutcome.ABSTAIN
            assert result.used_fallback is True
            assert result.policy_id == "powercontext.decision.hanging.v1"

    asyncio.run(scenario())


def test_preflight_rejects_an_enabled_decision_role_without_a_model() -> None:
    config = BuiltinConfig(runtime=RuntimeConfig(decision_assistance_enabled=True))

    async def scenario() -> None:
        with pytest.raises(BuiltinConfigurationError):
            await preflight_builtin_runtime(config)

    asyncio.run(scenario())


def test_hosted_redacted_gate_resolves_the_explicit_known_secret_sanitizer(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    config = BuiltinConfig(
        runtime=RuntimeConfig(
            memory_write_gate_enabled=True,
            memory_write_gate_privacy_boundary=DecisionPrivacyBoundary.HOSTED_REDACTED,
            memory_write_gate_sanitizer="known_secrets",
        ),
        inference=InferenceConfig(decision_model="openai-chat:decision-model"),
    )

    sanitizer = _resolve_memory_write_gate_sanitizer(config.runtime, None)

    assert isinstance(sanitizer, KnownSecretDecisionRequestSanitizer)

    async def scenario() -> None:
        await preflight_builtin_runtime(config)

    asyncio.run(scenario())


def test_hosted_redacted_gate_without_a_selected_sanitizer_fails_preflight() -> None:
    config = _config(
        Path("unused.db"),
        memory_write_gate_enabled=True,
        memory_write_gate_privacy_boundary=DecisionPrivacyBoundary.HOSTED_REDACTED,
    )

    async def scenario() -> None:
        with pytest.raises(BuiltinConfigurationError, match="decision projection sanitizer"):
            await preflight_builtin_runtime(config)

    asyncio.run(scenario())


def test_disabled_hosted_redacted_gate_does_not_require_a_projection_sanitizer() -> None:
    config = _config(
        Path("unused.db"),
        memory_write_gate_privacy_boundary=DecisionPrivacyBoundary.HOSTED_REDACTED,
    )

    async def scenario() -> None:
        await preflight_builtin_runtime(config)

    asyncio.run(scenario())


def test_runtime_composition_accepts_an_explicit_hosted_projection_sanitizer(tmp_path: Path) -> None:
    class _Sanitizer:
        def sanitize(self, request: DecisionRequest, /) -> DecisionRequestProjection:
            return DecisionRequestProjection.permitted(request)

    async def scenario() -> None:
        async with open_builtin_runtime(
            _config(
                tmp_path,
                memory_write_gate_enabled=True,
                memory_write_gate_privacy_boundary=DecisionPrivacyBoundary.HOSTED_REDACTED,
            ),
            decision_model=_FakeDecisionModel(),
            decision_request_sanitizer=_Sanitizer(),
        ) as runtime:
            assert runtime.decision_model is not None

    asyncio.run(scenario())


def test_runtime_composition_allows_a_hosted_boundary_for_a_local_model_without_sanitizer(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with open_builtin_runtime(
            _config(
                tmp_path,
                memory_write_gate_enabled=True,
                memory_write_gate_privacy_boundary=DecisionPrivacyBoundary.HOSTED_REDACTED,
            ),
            decision_model=_LocalDecisionModel(),
        ) as runtime:
            assert runtime.decision_model is not None

    asyncio.run(scenario())


def test_preflight_allows_an_injected_gate_without_configured_projection_sanitizer() -> None:
    config = _config(
        Path("unused.db"),
        memory_write_gate_enabled=True,
        memory_write_gate_privacy_boundary=DecisionPrivacyBoundary.HOSTED_REDACTED,
    )

    async def scenario() -> None:
        await preflight_builtin_runtime(config, memory_write_gate=Mock())

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "database",
    [
        pytest.param(SQLiteConfig(), id="sqlite"),
        pytest.param(
            OceanBaseConfig(
                url=SecretStr("mysql+aoceanbase://root%40tenant:secret@127.0.0.1:2881/powercontext?charset=utf8mb4")
            ),
            id="oceanbase-simulated",
        ),
    ],
)
def test_hosted_redacted_projection_composes_for_supported_database_configs(database) -> None:
    config = BuiltinConfig(
        database=database,
        runtime=RuntimeConfig(
            memory_write_gate_enabled=True,
            memory_write_gate_privacy_boundary=DecisionPrivacyBoundary.HOSTED_REDACTED,
            memory_write_gate_sanitizer="known_secrets",
        ),
    )

    async def scenario() -> None:
        await preflight_builtin_runtime(config)

    asyncio.run(scenario())


def test_runtime_composition_installs_the_selected_hosted_projection_sanitizer(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with open_builtin_runtime(
            _config(
                tmp_path,
                memory_write_gate_enabled=True,
                memory_write_gate_privacy_boundary=DecisionPrivacyBoundary.HOSTED_REDACTED,
                memory_write_gate_sanitizer="known_secrets",
            ),
            decision_model=_FakeDecisionModel(),
        ) as runtime:
            assert runtime.decision_model is not None

    asyncio.run(scenario())


def test_preflight_accepts_an_enabled_decision_role_with_a_dedicated_model(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    config = BuiltinConfig(
        runtime=RuntimeConfig(decision_assistance_enabled=True),
        inference=InferenceConfig(decision_model="openai-chat:decision-model"),
    )

    async def scenario() -> None:
        await preflight_builtin_runtime(config)

    asyncio.run(scenario())


def test_generation_pipelines_builds_the_decision_backend_when_enabled(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    async def scenario() -> None:
        async with AsyncExitStack() as resources:
            pipelines = await _generation_pipelines(
                InferenceConfig(decision_model="openai-chat:decision-model"),
                RuntimeConfig(decision_assistance_enabled=True),
                resources,
                None,
                BUILTIN_SOURCE_REGISTRY,
            )

        decision = pipelines[7]
        assert isinstance(decision, LLMDecisionModel)
        assert decision.policy_id == DECISION_INSTRUCTIONS_VERSION
        # A dedicated decision model also yields a non-blocking readiness probe.
        assert pipelines[10] is not None

    asyncio.run(scenario())


def test_decision_role_is_not_registered_as_an_mcp_tool() -> None:
    from powercontext.server import mcp

    assert all("decision" not in operation_id for operation_id in mcp._MCP_OPERATION_IDS)


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("http://localhost:11434/v1", True),
        ("http://127.0.0.1:11434/v1", True),
        ("http://[::1]:11434/v1", True),
        ("http://0.0.0.0:11434/v1", False),
        ("http://192.168.1.2:11434/v1", False),
        ("https://api.example.test/v1", False),
    ],
)
def test_only_explicit_loopback_decision_endpoints_are_local(endpoint: str, expected: bool) -> None:
    assert composition._is_loopback_endpoint(AnyHttpUrl(endpoint)) is expected


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("http://127.0.0.1:11434/v1", True),
        ("https://api.example.test/v1", False),
    ],
)
def test_loopback_classifier_accepts_runtime_string_overrides(endpoint: str, expected: bool) -> None:
    assert composition._is_loopback_endpoint(endpoint) is expected


def test_loopback_openai_provider_disables_redirects() -> None:
    async def scenario() -> None:
        async with AsyncExitStack() as resources:
            factory = composition._unbounded_provider_factory(
                AnyHttpUrl("http://127.0.0.1:11434/v1"),
                {},
                workload="generation",
                resources=resources,
            )

            provider = factory("openai-chat")

            assert provider.client._client.follow_redirects is False  # pyright: ignore[reportPrivateUsage]

    asyncio.run(scenario())


def test_loopback_anthropic_headers_provider_disables_redirects() -> None:
    async def scenario() -> None:
        async with AsyncExitStack() as resources:
            factory = composition._unbounded_provider_factory(
                AnyHttpUrl("http://127.0.0.1:11434"),
                {"x-api-key": SecretStr("test-key")},
                workload="generation",
                resources=resources,
            )

            provider = factory("anthropic")

            assert provider.client._client.follow_redirects is False  # pyright: ignore[reportPrivateUsage]

    asyncio.run(scenario())
