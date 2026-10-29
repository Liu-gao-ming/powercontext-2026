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

import json
from datetime import UTC, datetime
from typing import Self

import pytest
from typer.testing import CliRunner

import powercontext.client.cli as client_cli
from powercontext.cli.app import create_cli
from powercontext.http import Capabilities, ExtractionStatus, MemorySearchMode, PreparedContextSchema

_SINCE = datetime(2026, 9, 28, tzinfo=UTC)
_FAILURE_AT = datetime(2026, 9, 29, 1, tzinfo=UTC)
_SUCCESS_AT = datetime(2026, 9, 29, 12, tzinfo=UTC)


class _CapabilitiesClient:
    def __init__(self, capabilities: Capabilities) -> None:
        self.capabilities = capabilities

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def get_capabilities(self) -> Capabilities:
        return self.capabilities


def _capabilities(extraction: ExtractionStatus | None, *, memory_extraction: bool = True) -> Capabilities:
    return Capabilities(
        source_types=["git-commit"],
        artifact_families=["memory"],
        memory_extraction=memory_extraction,
        extraction=extraction,
        handoff_generation=False,
        search_modes=[MemorySearchMode.FTS],
        context_versions=[PreparedContextSchema.POWERCONTEXT_PREPARED_CONTEXT_V1],
    )


def _invoke(monkeypatch: pytest.MonkeyPatch, capabilities: Capabilities, *arguments: str) -> str:
    monkeypatch.setattr(client_cli, "PowerContextClient", lambda *_args, **_kwargs: _CapabilitiesClient(capabilities))
    result = CliRunner().invoke(create_cli([]), [*arguments, "capabilities"])
    assert result.exit_code == 0
    return result.output


def test_capabilities_human_output_prints_available_extraction_diagnostics(monkeypatch: pytest.MonkeyPatch) -> None:
    capabilities = _capabilities(
        ExtractionStatus.model_validate({
            "configuration": "configured",
            "background": {
                "location": "local",
                "role": "leader",
                "state": "running",
                "automatic_processing_enabled": True,
            },
            "observation": {
                "status": "observed",
                "since": _SINCE,
                "last_success_at": _SUCCESS_AT,
                "last_failure": {
                    "code": "model_timeout",
                    "stage": "inference",
                    "occurred_at": _FAILURE_AT,
                },
            },
        })
    )

    output = _invoke(monkeypatch, capabilities)

    assert "Memory extraction: enabled" in output
    assert "Extraction configuration: configured" in output
    assert "Extraction background: location=local, role=leader, state=running, automatic processing=enabled" in output
    assert f"Extraction observation: observed since {_SINCE.isoformat()}" in output
    assert f"Extraction last success: {_SUCCESS_AT.isoformat()}" in output
    assert f"Extraction last failure: model_timeout at inference, {_FAILURE_AT.isoformat()}" in output
    assert "healthy" not in output.lower()
    assert "recovered" not in output.lower()
    assert "unresolved" not in output.lower()


def test_capabilities_human_output_reports_missing_extraction_diagnostics(monkeypatch: pytest.MonkeyPatch) -> None:
    output = _invoke(monkeypatch, _capabilities(None, memory_extraction=False))

    assert "Memory extraction: disabled" in output
    assert "Extraction diagnostics: unavailable" in output
    assert "Extraction configuration:" not in output
    assert "Extraction observation:" not in output
    assert "Extraction last success:" not in output
    assert "Extraction last failure:" not in output


def test_capabilities_human_output_reports_unverified_unknown_diagnostics(monkeypatch: pytest.MonkeyPatch) -> None:
    capabilities = _capabilities(
        ExtractionStatus.model_validate({
            "configuration": "unknown",
            "background": {
                "location": "external",
                "role": None,
                "state": "unknown",
                "automatic_processing_enabled": None,
            },
            "observation": {"status": "unverified", "since": _SINCE},
        })
    )

    output = _invoke(monkeypatch, capabilities)

    assert "Extraction configuration: unknown" in output
    assert (
        "Extraction background: location=external, role=absent, state=unknown, automatic processing=unknown" in output
    )
    assert f"Extraction observation: unverified since {_SINCE.isoformat()}" in output
    assert "Extraction last success:" not in output
    assert "Extraction last failure:" not in output
    assert "healthy" not in output.lower()


def test_capabilities_json_output_preserves_extraction_diagnostics(monkeypatch: pytest.MonkeyPatch) -> None:
    capabilities = _capabilities(
        ExtractionStatus.model_validate({
            "configuration": "unconfigured",
            "background": {
                "location": "none",
                "role": None,
                "state": "stopped",
                "automatic_processing_enabled": False,
            },
            "observation": {
                "status": "observed",
                "since": _SINCE,
                "last_failure": {
                    "code": "worker_crash",
                    "stage": "worker",
                    "occurred_at": _FAILURE_AT,
                },
            },
        })
    )

    output = _invoke(monkeypatch, capabilities, "--json")

    assert json.loads(output) == capabilities.model_dump(mode="json")
    assert "Extraction diagnostics:" not in output
