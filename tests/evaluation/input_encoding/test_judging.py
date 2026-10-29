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

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from evaluation.performance.input_encoding import judging
from evaluation.performance.input_encoding.identity import ConfigurationIdentityError
from powercontext.builtin.inference.errors import InferencePrivacyError, InvalidInferenceOutputError
from powercontext.builtin.inference.models import InferenceUsage
from powercontext.builtin.runtime.config import InferenceConfig


@pytest.mark.parametrize("detail", ["provider did not return a valid result", "untrusted-provider-secret"])
def test_judge_failure_keeps_usage_but_only_allowlisted_error_detail(tmp_path, detail):
    class Generator:
        async def generate(self, value):
            error = InvalidInferenceOutputError("generate", detail)
            error.usage = InferenceUsage(requests=2, input_tokens=100, output_tokens=25)
            raise error

    path = tmp_path / "record.json"
    record = asyncio.run(judging._call(Generator(), judging.JudgeInput(), path))
    assert record["status"] == "failed"
    assert record["usage"]["total_tokens"] == 125
    assert record.get("error_detail") == (detail if detail == "provider did not return a valid result" else None)
    assert "untrusted-provider-secret" not in path.read_text()


def test_judge_records_safe_privacy_diagnostics_and_wrapped_usage(tmp_path):
    class Generator:
        async def generate(self, value):
            error = InferencePrivacyError(
                "before_release",
                "withhold",
                ("response_incomplete",),
                usage=InferenceUsage(requests=1, input_tokens=100, output_tokens=4096),
            )
            raise RuntimeError("untrusted-provider-secret") from error

    path = tmp_path / "record.json"
    record = asyncio.run(judging._call(Generator(), judging.JudgeInput(), path))
    assert record["privacy_error"] == {
        "phase": "before_release",
        "decision": "withhold",
        "reason_codes": ["response_incomplete"],
    }
    assert record["usage"]["total_tokens"] == 4196
    assert "untrusted-provider-secret" not in path.read_text()


def test_judge_applies_protection_and_output_budget_before_opening_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-provider-before")
    (tmp_path / "manifest.json").write_text(json.dumps({"model": "openai-chat:example"}))
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"mode": "enforcing", "policy_version": "test-policy"}))
    args = SimpleNamespace(
        dataset=tmp_path / "unused-dataset",
        env_file=tmp_path / "unused-env",
        output=tmp_path,
        protection_config=policy,
        max_output_tokens=4096,
        timeout=180,
        concurrency=1,
    )
    opened = []
    generators = []

    @contextmanager
    def settings_context(**kwargs):
        yield SimpleNamespace(
            inference=InferenceConfig(
                generation_model="openai-chat:example", generation_model_settings={"max_tokens": 7}
            )
        )

    async def open_model(model, **kwargs):
        opened.append(kwargs)
        return None, object()

    def generator(**kwargs):
        generators.append(kwargs)
        return object()

    monkeypatch.setattr(judging, "server_settings_context", settings_context)
    monkeypatch.setattr(judging, "load_catalog", lambda path: object())
    monkeypatch.setattr(judging, "_open_pydantic_ai_model", open_model)
    monkeypatch.setattr(judging, "PydanticAIStructuredGenerator", generator)
    asyncio.run(judging.judge(args))
    assert opened[0]["protection"].mode == "enforcing"
    assert opened[0]["protection"].policy_version == "test-policy"
    assert opened[0]["disable_provider_retries"] is True
    assert all(item["limits"].max_output_tokens_per_request == 4096 for item in generators)
    assert all(item["model_settings"]["max_tokens"] == 4096 for item in generators)
    saved = json.loads((tmp_path / "judge/manifest.json").read_text())
    assert saved["max_output_tokens"] == 4096
    assert saved["protection_mode"] == "enforcing"
    assert set(saved["prompt_hashes"]) == {"author", "solver", "grader"}
    assert saved["configuration_identity"]["private_config_digest"]

    before = (tmp_path / "judge/manifest.json").read_bytes()
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-provider-after")
    with pytest.raises(ConfigurationIdentityError, match="Private configuration changed"):
        asyncio.run(judging.judge(args))
    assert (tmp_path / "judge/manifest.json").read_bytes() == before
    assert not (tmp_path / "judge/questions").exists()
    assert not (tmp_path / "judge/answers").exists()
