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

"""Pinned Jev transport for public benchmark inputs, never a local runtime backend."""

from __future__ import annotations

import math
from typing import Any

import httpx
from pydantic import SecretStr
from typing_extensions import override

from examples.systemone.adapter import SystemOneConfig, SystemOneDecisionModel
from powercontext.builtin.inference import (
    InferenceConfigurationError,
    InferenceTimeoutError,
    InferenceUnavailableError,
    InvalidInferenceOutputError,
)
from powercontext.builtin.runtime.decision_model import DecisionRequest, DecisionResult

JEV_MODEL = "typesafe/jev-1.13"
JEV_ENDPOINT = "https://openrouter.ai/api/alpha/decisions"


class PublicJevModel(SystemOneDecisionModel):
    """Reuse the example's choice protocol and expose bounded measurement facts.

    A versioned provider ID does not guarantee immutable server-side weights.
    Missing/invalid provider cost remains unknown; raw responses are not retained.
    """

    is_local_only = False
    provider_id = "openrouter-typesafe"
    backend_model_id = JEV_MODEL

    def __init__(self, client: httpx.AsyncClient, api_key: SecretStr) -> None:
        super().__init__(
            SystemOneConfig(provider="jev", endpoint=JEV_ENDPOINT, model=JEV_MODEL, api_key=api_key), client
        )
        self.last_cost_usd: float | None = None
        self.failure_reason: str | None = None
        self._http_status: int | None = None
        client.event_hooks["response"].append(self._record_status)

    async def _record_status(self, response: httpx.Response) -> None:
        self._http_status = response.status_code

    @override
    async def evaluate(self, request: DecisionRequest, /) -> DecisionResult:
        self.last_cost_usd = None
        self.failure_reason = None
        self._http_status = None
        try:
            return await super().evaluate(request)
        except InferenceTimeoutError:
            self.failure_reason = "backend_timeout"
            raise
        except InferenceUnavailableError:
            self.failure_reason = {
                401: "credential_rejected",
                402: "insufficient_credit",
                403: "credential_rejected",
                429: "rate_limited",
            }.get(self._http_status, "backend_unavailable")
            raise
        except InvalidInferenceOutputError:
            self.failure_reason = "invalid_backend_output"
            raise
        except InferenceConfigurationError:
            self.failure_reason = "backend_input_rejected"
            raise

    @override
    def _result(self, payload: Any) -> DecisionResult:
        result = super()._result(payload)
        cost = payload.get("usage", {}).get("cost")
        if type(cost) in (int, float) and math.isfinite(cost) and cost >= 0:
            self.last_cost_usd = float(cost)
        return result
