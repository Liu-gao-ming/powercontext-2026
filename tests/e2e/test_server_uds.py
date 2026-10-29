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

"""Formal CLI UDS listener, authentication, and restart behavior in real processes."""

from __future__ import annotations

import json
import os
import signal
import socket
import stat
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.skipif(sys.platform not in {"linux", "darwin"}, reason="UDS supports Linux and macOS")

_TOKEN = "disposable-uds-test-token"  # noqa: S105
_HEADERS = {"Authorization": f"Bearer {_TOKEN}"}
_CLI = [sys.executable, "-c", "from powercontext.cli.app import main; main()", "server", "run", "--no-env-file"]


def _environment(directory: Path) -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith(("POWERCONTEXT_", "OTEL_", "OPENAI_"))
    }
    environment.update({
        "POWERCONTEXT_HOME": str(directory / "home"),
        "POWERCONTEXT_SERVER_DATABASE_KIND": "sqlite",
        "POWERCONTEXT_SERVER_DATABASE_URL": f"sqlite+aiosqlite:///{directory / 'runtime.db'}",
        "POWERCONTEXT_SERVER_ACCESS_MODE": "enforced",
        "POWERCONTEXT_SERVER_AUTH_TOKEN": _TOKEN,
        "POWERCONTEXT_SERVER_MCP_ENABLED": "false",
        "POWERCONTEXT_SERVER_DASHBOARD_ENABLED": "false",
        "POWERCONTEXT_SERVER_LOGGING_ACCESS": "false",
        "POWERCONTEXT_SERVER_RUNTIME": json.dumps({"artifact_processing_families": []}),
        "POWERCONTEXT_SERVER_CURSOR_SIGNING_SECRET": "disposable-uds-signing-secret-32-bytes",
        "POWERCONTEXT_SERVER_INFERENCE": json.dumps({
            "protection": {
                "mode": "enforcing",
                "forbidden_values": ["synthetic-uds-secret"],
                "targets": {
                    "model": {
                        "endpoint": "https://model.example/v1/chat/completions",
                        "model": "test",
                        "protocol": "openai-chat",
                        "context_window_tokens": 65536,
                        "max_output_tokens": 64,
                        "validated_utf8_token_bound": True,
                    }
                },
            }
        }),
    })
    return environment


@contextmanager
def _running_server(
    directory: Path,
    socket_path: Path,
    environment: dict[str, str],
    *,
    from_environment: bool = False,
) -> Iterator[tuple[subprocess.Popen[bytes], httpx.Client]]:
    command = _CLI if from_environment else [*_CLI, "--uds", str(socket_path)]
    log_path = directory / "server.log"
    with log_path.open("w", encoding="utf-8") as output:
        process = subprocess.Popen(
            command,
            cwd=directory,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        try:
            with httpx.Client(
                transport=httpx.HTTPTransport(uds=str(socket_path), retries=0),
                base_url="http://localhost",
                headers=_HEADERS,
                trust_env=False,
                timeout=3,
            ) as client:
                deadline = time.monotonic() + 45
                while True:
                    if process.poll() is not None or time.monotonic() >= deadline:
                        pytest.fail(f"UDS CLI did not become ready: {log_path.read_text()}")
                    try:
                        ready = client.get("/health/ready")
                        if ready.status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(0.05)
                yield process, client
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)


def _rejected_launch(
    directory: Path, socket_path: Path, environment: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [*_CLI, "--uds", str(socket_path)],
        cwd=directory,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    return result


def _prepare(client: httpx.Client, scope_id: str) -> dict[str, Any]:
    response = client.post(
        "/v1/context/prepare",
        json={
            "scope_id": scope_id,
            "query": "UDS",
            "include_reuse": True,
            "assembly": {"sections": [{"family": "memory", "limit": 8}]},
        },
    )
    assert response.status_code == 200, response.text
    prepared = response.json()
    assert "8123" in prepared["context"]["content"]
    assert prepared["reuse"]["binding"]
    return prepared


def _check_phases(client: httpx.Client, scope_id: str, prepared: dict[str, Any]) -> None:
    content = prepared["context"]["content"]
    response = client.post(
        "/v1/context/check",
        json={
            "scope_id": scope_id,
            "phase": "before_send",
            "target_profile": "model",
            "payload": {
                "schema": "powercontext.model-json-payload.v1",
                "headers": {"content-type": "application/json"},
                "body": json.dumps(
                    {"model": "test", "max_tokens": 64, "messages": [{"role": "user", "content": content}]},
                    ensure_ascii=False,
                ),
            },
            "prepared_contexts": [
                {
                    "binding": prepared["reuse"]["binding"],
                    "location": {
                        "schema": "powercontext.model-json-text-location.v1",
                        "pointer": "/messages/0/content",
                        "start_byte": 0,
                        "end_byte": len(content.encode()),
                    },
                }
            ],
        },
    )
    assert response.status_code == 200, response.text
    before_send = response.json()
    assert before_send["decision"] == "allow" and before_send["coverage"]["complete"]
    assert before_send["binding"]
    response = client.post(
        "/v1/context/check",
        json={
            "scope_id": scope_id,
            "phase": "before_release",
            "target_profile": "model",
            "request_binding": before_send["binding"],
            "completion": "complete",
            "payload": {
                "schema": "powercontext.model-json-payload.v1",
                "headers": {"content-type": "application/json"},
                "body": json.dumps({
                    "model": "test",
                    "choices": [{"message": {"role": "assistant", "content": "Use 8123."}, "finish_reason": "stop"}],
                }),
            },
        },
    )
    assert response.status_code == 200, response.text
    released = response.json()
    assert released["decision"] == "allow" and released["coverage"]["complete"]


def _check_mcp(client: httpx.Client) -> None:
    request = {
        "jsonrpc": "2.0",
        "id": "uds-initialize",
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "uds-acceptance", "version": "1"},
        },
    }
    headers = {"Accept": "application/json, text/event-stream"}
    refused = client.post("/mcp", json=request, headers=headers | {"Authorization": ""}, follow_redirects=True)
    assert refused.status_code == 401, refused.text
    response = client.post("/mcp", json=request, headers=headers, follow_redirects=True)
    assert response.status_code == 200, response.text
    initialized = (
        response.json()
        if response.headers["content-type"].startswith("application/json")
        else next(json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: "))
    )
    assert initialized["id"] == "uds-initialize"
    assert initialized["result"]["protocolVersion"]
    assert "capabilities" in initialized["result"]
    assert initialized["result"]["serverInfo"]["name"]


def test_cli_uds_authorization_context_and_process_restarts(tmp_path, short_tmp_path):
    socket_path = short_tmp_path / "private" / "core.sock"
    environment = _environment(tmp_path)
    environment["POWERCONTEXT_SERVER_MCP_ENABLED"] = "true"
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        reserved.listen()
        # A successful UDS start while this address is occupied proves it never binds the configured TCP listener.
        environment["POWERCONTEXT_SERVER_HTTP_HOST"] = "127.0.0.1"
        environment["POWERCONTEXT_SERVER_HTTP_PORT"] = str(reserved.getsockname()[1])
        with _running_server(tmp_path, socket_path, environment) as (process, client):
            assert stat.S_IMODE(socket_path.parent.stat().st_mode) == 0o700
            assert stat.S_IMODE(socket_path.stat().st_mode) == 0o600
            assert client.get("/v1/capabilities", headers={"Authorization": ""}).status_code == 401
            assert client.get("/v1/capabilities", headers={"Authorization": "Bearer invalid"}).status_code == 401
            assert client.get("/v1/capabilities").status_code == 200
            assert client.get("/metrics").status_code == 200
            _check_mcp(client)
            created = client.post(
                "/v1/scopes",
                json={"title": "UDS", "summary": "Gateway listener acceptance", "idempotency_key": "uds-scope"},
            )
            assert created.status_code == 201, created.text
            scope_id = created.json()["scope_id"]
            granted = client.post(
                "/v1/access/bindings/create",
                json={
                    "subject": {"type": "service", "id": "server-token"},
                    "resource": {"type": "scope", "scope_id": scope_id},
                    "role": "scope.contributor",
                    "idempotency_key": "uds-contributor",
                },
            )
            assert granted.status_code == 201, granted.text
            remembered = client.post(
                "/v1/memory/remember",
                json={"scope_id": scope_id, "kind": "fact", "text": "UDS 中文网关保留端口 8123。"},
            )
            assert remembered.status_code == 200, remembered.text
            _check_phases(client, scope_id, _prepare(client, scope_id))

            # Competing launches must leave the first process and its socket usable.
            socket_identity = socket_path.stat().st_ino
            _rejected_launch(tmp_path, socket_path, environment)
            assert socket_path.stat().st_ino == socket_identity
            assert client.get("/v1/capabilities").status_code == 200
            process.send_signal(signal.SIGTERM)
            assert process.wait(timeout=20) in {0, -signal.SIGTERM}
        assert not socket_path.exists()

        environment["POWERCONTEXT_SERVER_HTTP_UDS"] = str(socket_path)
        with _running_server(tmp_path, socket_path, environment, from_environment=True) as (process, client):
            _prepare(client, scope_id)
            process.kill()
            assert process.wait(timeout=10) == -signal.SIGKILL
        assert socket_path.is_socket()

        # A real crash leaves the tracked socket behind; the next CLI start reclaims it and retains the database.
        with _running_server(tmp_path, socket_path, environment, from_environment=True) as (process, client):
            _check_phases(client, scope_id, _prepare(client, scope_id))
            process.send_signal(signal.SIGTERM)
            assert process.wait(timeout=20) in {0, -signal.SIGTERM}
        assert not socket_path.exists()


@pytest.mark.parametrize("occupied_by", ["file", "symlink", "untracked_socket"])
def test_cli_uds_preserves_occupied_paths(tmp_path, short_tmp_path, occupied_by):
    socket_path = short_tmp_path / "occupied.sock"
    protected = short_tmp_path / "protected.txt"
    protected.write_text("leave this file intact", encoding="utf-8")
    with socket.socket(socket.AF_UNIX) as foreign_listener:
        if occupied_by == "file":
            socket_path.write_text("another application's file", encoding="utf-8")
        elif occupied_by == "symlink":
            socket_path.symlink_to(protected)
        else:
            foreign_listener.bind(str(socket_path))
            foreign_listener.listen()
        original = socket_path.lstat()
        _rejected_launch(tmp_path, socket_path, _environment(tmp_path))
        current = socket_path.lstat()
        assert (current.st_ino, current.st_mode) == (original.st_ino, original.st_mode)
        assert protected.read_text(encoding="utf-8") == "leave this file intact"
        if occupied_by == "file":
            assert socket_path.read_text(encoding="utf-8") == "another application's file"
        elif occupied_by == "symlink":
            assert socket_path.readlink() == protected
        else:
            with socket.socket(socket.AF_UNIX) as connection:
                connection.settimeout(1)
                connection.connect(str(socket_path))
                accepted, _ = foreign_listener.accept()
                accepted.close()


def test_cli_uds_startup_failure_releases_socket(tmp_path, short_tmp_path):
    socket_path = short_tmp_path / "failed.sock"
    invalid_database = tmp_path / "database-is-a-directory"
    invalid_database.mkdir()
    environment = _environment(tmp_path)
    environment["POWERCONTEXT_SERVER_DATABASE_URL"] = f"sqlite+aiosqlite:///{invalid_database}"
    result = _rejected_launch(tmp_path, socket_path, environment)
    assert "Application startup failed" in result.stdout + result.stderr
    assert not socket_path.exists()
    assert invalid_database.is_dir()
