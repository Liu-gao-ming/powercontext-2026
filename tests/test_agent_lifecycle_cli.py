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


"""The default CLI completes selected integrations and reports their actual checks."""

import json

import pytest
from typer.testing import CliRunner

from powercontext.cli import hosts, installation, system
from powercontext.cli.app import create_cli
from powercontext.cli.system import Diagnostic, DiagnosticStatus, SetupError
from powercontext.cli.transport import configure_connection


@pytest.fixture
def agents(tmp_path, monkeypatch):  # noqa: C901 - executable fixture adapters for the public lifecycle.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("POWERCONTEXT_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("POWERCONTEXT_CLIENT_CONFIG_FILE", str(tmp_path / "clients.json"))
    monkeypatch.setattr(hosts, "stdin_is_tty", lambda: False)
    monkeypatch.setattr(
        system, "server_diagnostics", lambda **kwargs: {"server": Diagnostic(DiagnosticStatus.OK, "available")}
    )
    monkeypatch.setattr(system, "_run_codex_json", lambda *_args: {"installed": []})
    failures = {}

    def adapter(name):
        path = tmp_path / name

        class Agent:
            def __init__(self, **options):
                pass

            def install(self, source):
                if failures.get(name) == "install":
                    raise SetupError("native installation failed")  # noqa: TRY003
                path.mkdir(exist_ok=True)
                (path / "plugin").write_text(source.location)
                return {"plugin": name, "plugin_path": str(path)}

            def configure(self, connection):
                if not (path / "plugin").exists():
                    raise SetupError("integration is not installed")  # noqa: TRY003
                (path / "connection").write_text(connection.server_url)
                configure_connection(connection)
                return {}

            def enable(self):
                (path / "enabled").touch()

            def verify(self):
                status = DiagnosticStatus.OK if (path / "enabled").exists() else DiagnosticStatus.FAILED
                checks = {"plugin": Diagnostic(status, "native plugin loading")}
                if failures.get(name) == "malformed":
                    raise KeyError("missing native configuration field")  # noqa: TRY003
                if failures.get(name) not in {None, "install"}:
                    checks[failures[name]] = Diagnostic(DiagnosticStatus.FAILED, "native check failed")
                return checks

        return Agent

    monkeypatch.setattr(installation, "installers", lambda: {name: adapter(name) for name in ("codex", "claude-code")})
    return tmp_path, failures


def invoke(*arguments, stdin_text=None):
    return CliRunner().invoke(create_cli(), list(arguments), input=stdin_text)


def test_setup_completes_multiple_agents_in_one_command(agents):
    root, _ = agents
    result = invoke("setup", "codex", "claude-code", "--server-url", "https://memory.example", "--json")
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["ok"] is True
    assert set(report["hosts"]) == {"codex", "claude-code"}
    for name, row in report["hosts"].items():
        assert row["setup"] == "complete"
        assert row["checks"]["plugin"]["status"] == "ok"
        assert (root / name / "enabled").exists()
        assert (root / name / "connection").read_text() == "https://memory.example"


def test_setup_keeps_failed_verification_in_its_result(agents):
    _, failures = agents
    failures["codex"] = "mcp_tools"
    result = invoke("setup", "codex", "--json")
    assert result.exit_code == 1
    report = json.loads(result.output)
    assert report["ok"] is False
    row = report["hosts"]["codex"]
    assert row["setup"] == "complete"
    assert row["checks"]["mcp_tools"]["status"] == "failed"


def test_one_agent_failure_does_not_stop_the_other_agent(agents):
    root, failures = agents
    failures["codex"] = "install"
    result = invoke("setup", "codex", "claude-code", "--json")
    assert result.exit_code == 1
    rows = json.loads(result.output)["hosts"]
    assert rows["codex"]["error"] == "native installation failed"
    assert rows["claude-code"]["setup"] == "complete"
    assert (root / "claude-code/enabled").exists()


def test_setup_prompts_once_for_multiple_targets(agents, monkeypatch):
    root, _ = agents
    monkeypatch.setattr(hosts, "stdin_is_tty", lambda: True)
    result = invoke("setup", stdin_text="1,2,1\n")
    assert result.exit_code == 0, result.output
    assert (root / "codex/enabled").exists()
    assert (root / "claude-code/enabled").exists()


@pytest.mark.parametrize(
    "arguments",
    [("setup",), ("setup", "codex", "unknown")],
)
def test_invalid_or_implicit_noninteractive_targets_make_no_changes(agents, arguments):
    root, _ = agents
    result = invoke(*arguments)
    assert result.exit_code != 0
    assert not (root / "codex").exists()
    assert not (root / "clients.json").exists()


def test_config_changes_connection_without_installing_or_enabling(agents):
    root, _ = agents
    assert invoke("setup", "codex", "--json").exit_code == 0
    plugin = root / "codex/plugin"
    plugin.write_text("local plugin edit")
    (root / "codex/enabled").unlink()
    result = invoke("config", "agent", "codex", "--server-url", "https://new.example", "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["hosts"]["codex"]["status"] == "configured"
    assert plugin.read_text() == "local plugin edit"
    assert not (root / "codex/enabled").exists()
    assert (root / "codex/connection").read_text() == "https://new.example"


def test_doctor_defaults_to_saved_connections_and_keeps_missing_targets_visible(agents):
    root, _ = agents
    assert invoke("setup", "codex", "--json").exit_code == 0
    (root / "codex/enabled").unlink()
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    result = invoke("doctor", "--json")
    assert result.exit_code == 1
    report = json.loads(result.output)
    assert set(report["hosts"]) == {"codex"}
    assert report["hosts"]["codex"]["checks"]["plugin"]["status"] == "failed"
    assert {p: p.read_bytes() for p in root.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize(
    "arguments",
    [
        ("setup", "codex", "--no-capture-prompts"),
        ("config", "agent", "codex", "--scope-id", "wrong-agent"),
        ("doctor", "codex", "--runtime"),
        ("doctor", "codex", "--prepare"),
    ],
)
def test_native_options_require_an_applicable_target_before_writes(agents, arguments):
    root, _ = agents
    result = invoke(*arguments)
    assert result.exit_code == 2
    assert not (root / "codex").exists()
    assert not (root / "clients.json").exists()


def test_malformed_native_configuration_does_not_hide_completed_setup_or_other_targets(agents):
    _, failures = agents
    failures["codex"] = "malformed"
    result = invoke("setup", "codex", "claude-code", "--json")
    assert result.exit_code == 1
    rows = json.loads(result.output)["hosts"]
    assert rows["codex"]["setup"] == "complete"
    assert rows["codex"]["checks"]["verification"]["status"] == "failed"
    assert rows["claude-code"]["ok"] is True


def test_explicit_server_diagnostics_remain_available_after_agent_setup(agents):
    assert invoke("setup", "codex", "--json").exit_code == 0
    result = invoke("doctor", "--server-url", "https://new.example", "--json")
    assert result.exit_code == 0
    assert "hosts" not in json.loads(result.output)
    assert json.loads(result.output)["checks"]["server"]["status"] == "ok"
