#!/usr/bin/env python3
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

"""Select PR checks from the complete Git diff; other events run every suite."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tomllib
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def select_suites(paths: list[str], rules: dict[str, Any]) -> dict[str, bool]:
    selected = dict.fromkeys(rules["suites"], False)
    for path in paths:
        if any(fnmatchcase(path, pattern) for pattern in rules["full"]):
            return dict.fromkeys(selected, True)
        matches = {
            suite
            for suite, patterns in rules["suites"].items()
            if any(fnmatchcase(path, pattern) for pattern in patterns)
        }
        if any(fnmatchcase(path, pattern) for pattern in rules["shared"]["patterns"]):
            matches.update(rules["shared"]["suites"])
        if not matches and not any(fnmatchcase(path, pattern) for pattern in rules["ignore"]):
            # New directories must not silently bypass checks before they have an owner.
            return dict.fromkeys(selected, True)
        for suite in matches:
            selected[suite] = True
    return selected


def changed_paths(base: str, head: str) -> list[str]:
    if not all(re.fullmatch(r"[0-9a-fA-F]{40}", commit) for commit in (base, head)):
        message = "Expected full base and head commit SHAs"
        raise ValueError(message)
    # Three-dot comparison covers the whole PR, excluding unrelated base changes.
    # Disabling rename detection includes both sides of moves across suite boundaries.
    result = subprocess.run(  # noqa: S603 -- validated SHAs, no shell
        ["git", "diff", "--name-only", "--no-renames", "-z", f"{base}...{head}", "--"],  # noqa: S607 -- runner tool
        check=True,
        capture_output=True,
        cwd=ROOT,
    )
    return [os.fsdecode(path) for path in result.stdout.split(b"\0") if path]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-name", default=os.environ.get("GITHUB_EVENT_NAME", ""))
    parser.add_argument("--base", default=os.environ.get("PR_BASE_SHA", ""))
    parser.add_argument("--head", default=os.environ.get("PR_HEAD_SHA", ""))
    arguments = parser.parse_args()
    rules = tomllib.loads((ROOT / ".github/ci-paths.toml").read_text(encoding="utf-8"))
    if arguments.event_name == "pull_request":
        try:
            selected = select_suites(changed_paths(arguments.base, arguments.head), rules)
        except (OSError, subprocess.CalledProcessError, ValueError):
            print("::warning::PR diff unavailable; running every CI suite.")
            selected = dict.fromkeys(rules["suites"], True)
    else:
        selected = dict.fromkeys(rules["suites"], True)

    print(json.dumps(selected, indent=2))
    if output_path := os.environ.get("GITHUB_OUTPUT"):
        with Path(output_path).open("a", encoding="utf-8") as output:
            output.write(f"suites={json.dumps(selected)}\n")
    if summary_path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary_path).open("a", encoding="utf-8") as summary:
            summary.write("## CI scope\n\n| Suite | Run |\n| --- | --- |\n")
            for suite, enabled in selected.items():
                summary.write(f"| {suite} | {'yes' if enabled else 'no'} |\n")


if __name__ == "__main__":
    main()
