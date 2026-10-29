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

"""Opt-in actual-host acceptance; explicitly missing prerequisites fail, never skip."""

import pytest

from .cross_host_work_continuity.runner import CrossHostRun


@pytest.mark.cross_host_acceptance
@pytest.mark.parametrize("case", ["accepted", "diverged", "unavailable", "unauthorized"])
def test_pi_to_zcode_work_continuity(case: str, request: pytest.FixtureRequest) -> None:
    report = CrossHostRun(request.config.getoption("cross_host_acceptance_output"), case=case).run()
    print(f"Pi -> ZCode {case}: {report}")
