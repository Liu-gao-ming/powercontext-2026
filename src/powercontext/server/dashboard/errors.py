# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Normalized dashboard read failures."""

from __future__ import annotations


class ReadError(Exception):
    def __init__(self, status: int, code: str, request_id: str | None = None) -> None:
        self.status = status
        self.code = code
        self.request_id = request_id
        super().__init__(code)

    @classmethod
    def from_exception(cls, error: Exception) -> ReadError:
        from powercontext.server.app import _map_error
        from powercontext.server.context import current_request_id

        if isinstance(error, cls):
            return error
        if isinstance(error, TimeoutError):
            return cls(503, "service_unavailable", current_request_id())
        status, code, _, _ = _map_error(error)
        return cls(status, code, current_request_id())
