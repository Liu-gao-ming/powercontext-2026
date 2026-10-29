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


"""Installation failure restores exact user configuration bytes."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from powercontext.cli import installation
from powercontext.cli.app import create_cli


def test_failed_local_publication_restores_exact_configuration_bytes(tmp_path, monkeypatch):
    original = b'{\r\n  "custom": true,\n  "note": "keep original line endings"\r\n}\r\n'
    home = tmp_path / "workbuddy"
    home.mkdir()
    settings = home / "settings.json"
    settings.write_bytes(original)
    monkeypatch.setenv("WORKBUDDY_HOME", str(home))
    write = installation.write_bytes

    def fail_settings(path, content):
        if path == settings:
            raise OSError("settings write failed")  # noqa: TRY003
        write(path, content)

    monkeypatch.setattr(installation, "write_bytes", fail_settings)
    with pytest.raises(OSError, match="settings write failed"):
        installation.installers()["workbuddy"]().install(installation.PluginSource(str(Path(__file__).parents[1])))
    assert settings.read_bytes() == original
    assert not (home / "hooks/workbuddy_powercontext_hook.py").exists()


def test_retry_preserves_personal_skill_files_after_failed_restore(tmp_path, monkeypatch, installed_agent_checks):
    home = tmp_path / "workbuddy"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("WORKBUDDY_HOME", str(home))
    monkeypatch.setenv("POWERCONTEXT_HOME", str(tmp_path / "data"))
    cli, runner = create_cli(), CliRunner()
    command = ["setup", "workbuddy", "--source", str(Path(__file__).parents[1]), "--json"]
    result = runner.invoke(cli, command)
    assert result.exit_code == 0, result.output
    skill = home / "skills/powercontext-project-context"
    (skill / "personal.md").write_text("Keep my own instructions.\n", encoding="utf-8")
    settings = home / "settings.json"
    write, replace = installation.write_bytes, installation.os.replace
    recovery = None

    def fail_settings(path, content):
        if path == settings:
            raise OSError("settings write failed")  # noqa: TRY003
        write(path, content)

    def fail_restore(source, target):
        nonlocal recovery
        if target == skill and source.suffix == ".bak":
            recovery = source
            raise OSError("Skill restore failed")  # noqa: TRY003
        replace(source, target)

    with monkeypatch.context() as failure:
        failure.setattr(installation, "write_bytes", fail_settings)
        failure.setattr(installation.os, "replace", fail_restore)
        result = runner.invoke(cli, command)
    assert result.exit_code == 1
    assert recovery is not None
    result = runner.invoke(cli, command)
    assert result.exit_code == 1, result.output
    assert str(recovery) in json.loads(result.output)["hosts"]["workbuddy"]["error"]
    assert (recovery / "personal.md").read_text(encoding="utf-8") == "Keep my own instructions.\n"
    recovery.rename(skill)
    result = runner.invoke(cli, command)
    assert result.exit_code == 0, result.output
    assert (skill / "personal.md").read_text(encoding="utf-8") == "Keep my own instructions.\n"
