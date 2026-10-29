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

"""CI admission, coverage and terminal-result budgets."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import ci_changes
import pytest
import yaml

ROOT = Path(__file__).parents[1]
RULES = tomllib.loads((ROOT / ".github/ci-paths.toml").read_text())


def workflow(name):
    result = yaml.safe_load((ROOT / ".github/workflows" / name).read_text())
    result["on"] = result.pop(True)
    return result


def combinations(matrix):
    if "include" in matrix:
        return {(row["python-version"], row["suite"]) for row in matrix["include"]}
    return {(version, suite) for version in matrix["python-version"] for suite in matrix["suite"]}


def test_ordinary_pr_has_three_compatibility_jobs():
    assert combinations(ci_changes.test_matrix("pull_request", ["src/powercontext/server/settings.py"], RULES)) == {
        ("3.11", "unit"),
        ("3.14", "unit"),
        ("3.12", "e2e"),
    }


@pytest.mark.parametrize("event", ["push", "schedule", "workflow_dispatch"])
def test_full_validation_keeps_all_supported_python_suite_pairs(event):
    assert combinations(ci_changes.test_matrix(event, [], RULES)) == {
        (version, suite) for version in ("3.11", "3.12", "3.13", "3.14") for suite in ("unit", "e2e")
    }


@pytest.mark.parametrize("path", ["uv.lock", "pyproject.toml", "tox.ini", "tests/conftest.py", "new-component/x.py"])
def test_dependency_and_unclassified_pr_changes_keep_full_compatibility(path):
    assert len(combinations(ci_changes.test_matrix("pull_request", [path], RULES))) == 8


def test_explicit_full_pr_profile_overrides_the_small_matrix():
    assert (
        len(
            combinations(
                ci_changes.test_matrix("pull_request", ["src/powercontext/server/settings.py"], RULES, full=True)
            )
        )
        == 8
    )


def test_selected_scopes_have_a_complete_terminal_result_contract():
    main = workflow("master.yml")["jobs"]
    assert RULES["checks"].keys() == RULES["suites"].keys()
    covered = {job for jobs in RULES["checks"].values() for job in jobs}
    assert covered | {"changes", "quality", "license-check", "upgrade-release-gate", "ci-result"} == set(main)
    assert set(main["ci-result"]["needs"]) == set(main) - {"ci-result"}


def test_automatic_host_workflows_share_one_scope_and_admission_gate():
    main = workflow("master.yml")["jobs"]
    for path in (ROOT / ".github/workflows").glob("*.yml"):
        if path.name != "master.yml":
            events = workflow(path.name)["on"]
            assert "pull_request" not in events, path.name
            assert not {"master", "main"} & set(events.get("push", {}).get("branches", [])), path.name
    for job in main.values():
        if (
            not job.get("uses", "").endswith(".yml")
            or "ci-changes" in job["uses"]
            or "upgrade-release" in job["uses"]
            or "license-check" in job["uses"]
        ):
            continue
        child = workflow(Path(job["uses"]).name)
        assert "pull_request" not in child["on"]
        assert "schedule" not in child["on"]
        assert "master" not in child["on"].get("push", {}).get("branches", [])
        assert "main" not in child["on"].get("push", {}).get("branches", [])
        assert job["with"]["suites"] == "${{ needs.changes.outputs.suites }}"
        assert {"changes", "quality", "tests"} <= set(job["needs"])
        assert "needs.quality.result == 'success'" in job["if"]
        assert "needs.tests.result" in job["if"]
        assert child["jobs"]["changes"]["if"] == "${{ inputs.suites == '' }}"
        for child_job in child["jobs"].values():
            if "needs.changes.result" in child_job.get("if", ""):
                assert "(inputs.suites == '' && needs.changes.result != 'success')" in child_job["if"]
                assert "fromJSON(inputs.suites || needs.changes.outputs.suites || '{}')" in child_job["if"]


def test_compatibility_and_upgrade_matrices_have_bounded_runner_bursts():
    assert int(workflow("master.yml")["jobs"]["tests"]["strategy"]["max-parallel"]) <= 3
    assert int(workflow("master.yml")["jobs"]["isolated-installations"]["strategy"]["max-parallel"]) <= 2
    upgrade = workflow("upgrade-release.yml")["jobs"]["cases"]["strategy"]
    assert int(upgrade["max-parallel"]) <= 3
    assert upgrade["fail-fast"] is False  # Complete upgrade evidence remains mandatory.


def test_cancelled_or_draft_runs_cannot_start_terminal_runner_jobs():
    jobs = workflow("master.yml")["jobs"]
    for name in ("upgrade-release-gate", "ci-result"):
        assert "!cancelled()" in jobs[name]["if"]
        assert "!github.event.pull_request.draft" in jobs[name]["if"]
    assert "converted_to_draft" in workflow("master.yml")["on"]["pull_request"]["types"]


@pytest.mark.parametrize(
    ("selected_result", "unselected_result", "passes"),
    [
        ("success", "skipped", True),
        ("skipped", "skipped", False),
        ("cancelled", "skipped", False),
        ("failure", "skipped", False),
        ("success", "failure", False),
    ],
)
def test_actual_ci_result_script_rejects_missing_selected_acceptance(selected_result, unselected_result, passes):
    script = workflow("master.yml")["jobs"]["ci-result"]["steps"][0]["run"]
    script = script.split("<<'PYTHON'\n", 1)[1].rsplit("PYTHON", 1)[0]
    required = ["changes", "quality", "license-check", "upgrade-release-gate", "harness-validation"]
    results = {job: {"result": "success"} for job in required}
    results["harness-validation"]["result"] = selected_result
    results["desktop-validation"] = {"result": unselected_result}
    run = subprocess.run(
        [sys.executable, "-c", script],
        env={**os.environ, "RESULTS": json.dumps(results), "REQUIRED": json.dumps(required)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert (run.returncode == 0) == passes, run.stdout + run.stderr
