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

"""Capture native startup, shutdown and transaction evidence for a SeekDB migration stall."""

from __future__ import annotations

import argparse
import gzip
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from types import ModuleType
from typing import Any
from xml.etree import ElementTree

import pytest

NODE = (
    "tests/builtin/persistence/test_mysql_migration_runner.py::"
    "test_mysql_baseline_adoption_recovers_an_empty_committed_version_table[seekdb]"
)


def record(directory: Path, event: str, **values: object) -> None:
    with (directory / "lifecycle.jsonl").open("a") as output:
        output.write(json.dumps({"time": datetime.now(UTC).isoformat(), "event": event, **values}) + "\n")


def pytest_sessionstart(session: pytest.Session) -> None:
    """Optional pytest plugin: observe only this subprocess's native instance lifetime."""
    from powercontext.builtin.persistence.seekdb import profile

    directory = Path(os.environ["POWERCONTEXT_SEEKDB_PROBE"])
    original = profile._open_instance

    class ObservedInstance:
        def __init__(self, instance: Any, path: Path) -> None:
            self.instance = instance
            self.path = path

        def connection_options(self) -> Any:
            return self.instance.connection_options()

        def close(self) -> None:
            record(directory, "closing", path=str(self.path))
            try:
                self.instance.close()
            finally:
                record(directory, "close_returned", path=str(self.path))

    async def observed_open(module: ModuleType, path: Path) -> profile._SeekDBInstance:
        record(directory, "opening", path=str(path))
        instance = await original(module, path)
        options = dict(instance.connection_options())
        # Embedded scratch instance only. Never capture credentials or external connection settings.
        socket = options.get("unix_socket")
        (directory / "socket.json").write_text(json.dumps({"unix_socket": socket}))
        record(directory, "opened", path=str(path))
        return ObservedInstance(instance, path)

    patch = pytest.MonkeyPatch()
    patch.setattr(profile, "_open_instance", observed_open)
    session.config.add_cleanup(patch.undo)


class NativeLogs:
    """Read through open file handles so rotation and fixture deletion retain early evidence."""

    def __init__(self, temporary: Path, output: Path) -> None:
        self.temporary = temporary
        self.output = output
        self.files: dict[tuple[int, int], tuple[Any, Any]] = {}

    def poll(self, *, drain: bool = False) -> None:
        for path in self.temporary.glob("pc-*/seekdb/log/*"):
            if not path.is_file() or ".log" not in path.name:
                continue
            try:
                source = path.open("rb")
                stat = os.fstat(source.fileno())
            except FileNotFoundError:
                continue
            identity = stat.st_dev, stat.st_ino
            if identity in self.files:
                source.close()
                continue
            name = f"native-{len(self.files):03d}-{path.name}.gz"
            destination = gzip.open(self.output / name, "wb", compresslevel=1)  # noqa: SIM115 -- retained across rotations
            self.files[identity] = source, destination
            record(self.output, "native_log", path=str(path), archive=name)
        for source, destination in self.files.values():
            remaining = 8
            while drain or remaining:
                data = source.read(1024 * 1024)
                if not data:
                    break
                destination.write(data)
                remaining -= 1

    def close(self) -> None:
        self.poll(drain=True)
        for source, destination in self.files.values():
            source.close()
            destination.close()


def snapshot(directory: Path) -> None:
    """Read live diagnostic views through the latest embedded Unix socket, never a remote endpoint."""
    import pymysql

    socket = json.loads((directory / "socket.json").read_text()).get("unix_socket")
    if not socket:
        raise RuntimeError("The probe did not publish an embedded Unix socket")  # noqa: TRY003
    queries = (
        "SELECT VERSION()",
        "SHOW FULL PROCESSLIST",
        "SELECT * FROM oceanbase.__all_virtual_trans_stat LIMIT 100",
        "SELECT * FROM oceanbase.__all_virtual_obj_lock LIMIT 100",
        "SELECT * FROM oceanbase.__all_virtual_lock_wait_stat LIMIT 100",
        "SELECT * FROM oceanbase.__all_virtual_trans_scheduler LIMIT 100",
    )
    for query in queries:
        try:
            # Reconnect after an unsupported view or a read timeout, preserving the other evidence.
            with (
                pymysql.connect(
                    unix_socket=socket,
                    user="root",
                    database="test",
                    autocommit=True,
                    connect_timeout=2,
                    read_timeout=2,
                    write_timeout=2,
                ) as connection,
                connection.cursor() as cursor,
            ):
                cursor.execute(query)
                print(
                    json.dumps({"query": query, "columns": cursor.description, "rows": cursor.fetchall()}, default=str),
                    flush=True,
                )
        except Exception as error:
            print(json.dumps({"query": query, "error": str(error)}), flush=True)


def successful_report(report: Path, code: int) -> bool:
    if code != 0 or not report.exists():
        return False
    cases = list(ElementTree.parse(report).iter("testcase"))  # noqa: S314 -- locally produced report
    return len(cases) == 1 and not any(
        case.find(tag) is not None for case in cases for tag in ("failure", "error", "skipped")
    )


def run_attempt(directory: Path, timeout: int) -> bool:
    directory.mkdir(parents=True)
    temporary = directory / "tmp"
    temporary.mkdir()
    environment = dict(
        os.environ,
        TMPDIR=str(temporary),
        POWERCONTEXT_SEEKDB_PROBE=str(directory),
        POWERCONTEXT_TEST_MIGRATION_SEEKDB="1",
        POWERCONTEXT_TEST_MIGRATION_SQL_LOG="1",
    )
    report = directory / "junit.xml"
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "scripts.probe_seekdb_migration_lock",
        "-p",
        "no:cacheprovider",
        "-vv",
        "--log-cli-level=INFO",
        "-o",
        "faulthandler_timeout=120",
        f"--junitxml={report}",
        NODE,
    ]
    logs = NativeLogs(temporary, directory)
    snapshots: list[tuple[subprocess.Popen[bytes], Any, float]] = []
    started = time.monotonic()
    next_snapshot = 45.0
    timed_out = False
    process = None
    try:
        with (directory / "pytest.log").open("w") as output:
            process = subprocess.Popen(command, env=environment, stdout=output, stderr=subprocess.STDOUT)  # noqa: S603
            while process.poll() is None:
                logs.poll()
                elapsed = time.monotonic() - started
                if elapsed >= next_snapshot:
                    log = (directory / f"snapshot-{int(next_snapshot)}.jsonl").open("w")
                    probe = subprocess.Popen(  # noqa: S603
                        [sys.executable, __file__, "--snapshot", str(directory)], stdout=log, stderr=subprocess.STDOUT
                    )
                    snapshots.append((probe, log, time.monotonic()))
                    next_snapshot += 45
                for probe, _, created in snapshots:
                    if probe.poll() is None and time.monotonic() - created > 20:
                        probe.kill()
                if elapsed >= timeout:
                    timed_out = True
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    break
                time.sleep(0.1)
            code = process.wait()
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        for probe, output, _ in snapshots:
            if probe.poll() is None:
                probe.kill()
            probe.wait()
            output.close()
        logs.close()
        # Only remove this attempt's disposable engine files, after preserving native logs.
        shutil.rmtree(temporary)
    passed = not timed_out and successful_report(report, code)
    result = {"passed": passed, "exit_code": code, "timed_out": timed_out, "seconds": time.monotonic() - started}
    (directory / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(directory.name, json.dumps(result), flush=True)
    return passed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--output", type=Path, default=Path(".powercontext/seekdb-lock-probe"))
    parser.add_argument("--attempts", type=int, default=12)
    parser.add_argument("--timeout", type=int, default=180)
    arguments = parser.parse_args()
    if arguments.snapshot:
        snapshot(arguments.snapshot)
        return 0
    directory = arguments.output.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "environment.json").write_text(
        json.dumps(
            {
                "platform": platform.platform(),
                "python": sys.version,
                "pylibseekdb": version("pylibseekdb"),
                "node": NODE,
            },
            indent=2,
        )
        + "\n"
    )
    for attempt in range(1, arguments.attempts + 1):
        attempt_directory = directory / f"attempt-{attempt:02d}"
        if not run_attempt(attempt_directory, arguments.timeout):
            return 1
        # Keep a complete successful baseline and every failure. Other successful
        # repeats retain their Python trace, lifecycle and test result only.
        if attempt > 1:
            for archive in attempt_directory.glob("native-*.gz"):
                archive.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
