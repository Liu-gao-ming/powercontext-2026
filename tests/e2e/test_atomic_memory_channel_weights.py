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
import sys
from pathlib import Path

import httpx
import pytest

from powercontext.builtin.artifacts.memory import EmbeddingProfile, MemoryRerankDecision
from powercontext.builtin.inference import EmbeddingResult, InferenceUsage
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.records import ArtifactWrite
from powercontext.builtin.runtime import BuiltinConfig, RuntimeConfig, open_builtin_contexts
from powercontext.builtin.runtime.recall_sufficiency import build_recall_candidates
from powercontext.server.factory import create_server_app
from powercontext.server.settings import McpConfig, ServerSettings


class _Embedding:
    profile = EmbeddingProfile(profile_id="channel-weights", model="fixture", dimension=3)

    async def embed(self, texts, /):
        return EmbeddingResult(
            vectors=tuple((0.0, 1.0, 0.0) if "lexical" in text else (1.0, 0.0, 0.0) for text in texts)
        )


class _SelectingReranker:
    policy_id = "channel-weights-reranker"
    supports_atomic_memory = True

    async def rerank(self, query, candidates, limit, /):
        return MemoryRerankDecision(selected_ranks=(2, 1)[:limit], usage=InferenceUsage(requests=1))


@pytest.mark.parametrize(
    ("fts_weight", "vector_weight", "expected_texts", "expected_scores"),
    [
        (1.0, 1.0, {"alpha lexical", "unrelated semantic"}, {"alpha lexical": 1 / 61, "unrelated semantic": 1 / 61}),
        (
            3.0,
            1.0,
            {"alpha lexical", "unrelated semantic"},
            {"alpha lexical": 1.5 / 61, "unrelated semantic": 0.5 / 61},
        ),
        (
            1.0,
            3.0,
            {"alpha lexical", "unrelated semantic"},
            {"alpha lexical": 0.5 / 61, "unrelated semantic": 1.5 / 61},
        ),
        (0.0, 1.0, {"unrelated semantic"}, {"unrelated semantic": 2 / 61}),
        (1.0, 0.0, {"alpha lexical"}, {"alpha lexical": 2 / 61}),
        (
            1e-320,
            1e-320,
            {"alpha lexical", "unrelated semantic"},
            {"alpha lexical": 1 / 61, "unrelated semantic": 1 / 61},
        ),
    ],
)
def test_http_atomic_search_applies_deployment_weights_only_to_hybrid(
    tmp_path: Path, fts_weight, vector_weight, expected_texts, expected_scores
) -> None:
    app = create_server_app(
        settings=ServerSettings(
            database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'weights.db'}"),
            runtime=RuntimeConfig(recall_fts_weight=fts_weight, recall_vector_weight=vector_weight),
            mcp=McpConfig(enabled=False),
        ),
        embedding_model=_Embedding(),
    )

    async def scenario():
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client,
        ):
            scope_response = await client.get("/v1/scopes/default")
            assert scope_response.status_code == 200, scope_response.text
            scope_id = scope_response.json()["scope_id"]
            for text in ("alpha lexical", "unrelated semantic"):
                response = await client.post(
                    "/v1/memory/remember", json={"scope_id": scope_id, "kind": "fact", "text": text}
                )
                assert response.status_code == 200, response.text
            for mode in ("hybrid", "auto", "fts", "vector"):
                response = await client.post(
                    "/v1/memory/search", json={"scope_id": scope_id, "query": "alpha", "mode": mode}
                )
                assert response.status_code == 200, response.text
                hits = response.json()["hits"]
                actual = {hit["memory"]["text"]: hit["score"] for hit in hits}
                if mode in {"hybrid", "auto"}:
                    assert response.json()["mode"] == "hybrid"
                    assert set(actual) == expected_texts
                    assert actual == pytest.approx(expected_scores)
                    if fts_weight != vector_weight:
                        assert hits[0]["memory"]["text"] == (
                            "alpha lexical" if fts_weight > vector_weight else "unrelated semantic"
                        )
                else:
                    text = "alpha lexical" if mode == "fts" else "unrelated semantic"
                    assert actual == pytest.approx({text: 1 / 61})
                assert all("score_upper_bound" not in hit for hit in hits)
            artifact_response = await client.post(
                f"/v1/scopes/{scope_id}/artifacts/atomic-memory/search", json={"query": "alpha", "mode": "hybrid"}
            )
            assert artifact_response.status_code == 200, artifact_response.text
            results = artifact_response.json()["results"]
            assert {item["content"]["text"] for item in results} == expected_texts
            if fts_weight != vector_weight:
                assert results[0]["content"]["text"] == (
                    "alpha lexical" if fts_weight > vector_weight else "unrelated semantic"
                )
            assert all("score_upper_bound" not in item for item in results)
            for params, expected in (
                ({}, {"alpha lexical": 0.5, "unrelated semantic": 0.5}),
                ({"weights": {"vector": 0}}, {"alpha lexical": 1.0}),
                ({"rank_constant": 10**400}, {"alpha lexical": 0.5, "unrelated semantic": 0.5}),
            ):
                override = await client.post(
                    f"/v1/scopes/{scope_id}/artifacts/atomic-memory/search",
                    json={
                        "query": "alpha",
                        "mode": "hybrid",
                        "fusion": {"method": "rrf", "params": params},
                        "include_scores": True,
                    },
                )
                assert override.status_code == 200, override.text
                scored = override.json()["results"]
                assert {item["content"]["text"]: item["scores"]["retrieval"] for item in scored} == pytest.approx(
                    expected
                )
                assert all(item["scores"]["channels"] for item in scored)

    asyncio.run(scenario())


@pytest.mark.parametrize("worker", [False, True])
@pytest.mark.parametrize(("fts_weight", "vector_weight"), [(0.0, 1.0), (1.0, 0.0), (sys.float_info.min, 2.0)])
def test_composed_atomic_search_preserves_active_channels_and_gate_metadata(
    tmp_path: Path, worker: bool, fts_weight: float, vector_weight: float
) -> None:
    async def scenario():
        config = BuiltinConfig(
            database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'composed.db'}"),
            runtime=RuntimeConfig(recall_fts_weight=fts_weight, recall_vector_weight=vector_weight),
        )
        if worker:
            # Workers consume the retrieval shape established by ordinary Runtime startup.
            async with open_builtin_contexts(config, embedding_model=_Embedding()):
                pass
        async with open_builtin_contexts(
            config,
            embedding_model=_Embedding(),
            _topic_memory_worker=worker,
        ) as contexts:
            await contexts.get("project")
            for text in ("alpha lexical", "unrelated semantic", "alpha shared"):
                await contexts.records.create_artifact(
                    "project", "atomic-memory", ArtifactWrite(content={"kind": "fact", "text": text})
                )
            page = await contexts.atomic_memory.for_scope("project").search("alpha", mode="hybrid")
            assert page.embedding_calls == 1
            shared = next(hit for hit in page.hits if hit.text == "alpha shared")
            expected_channels = tuple(
                channel for channel, weight in (("text", fts_weight), ("vector", vector_weight)) if weight > 0
            )
            assert shared.matched_by == expected_channels
            assert shared.score_upper_bound == pytest.approx(2 / 61)
            assert shared.relevance == (pytest.approx(1.0) if vector_weight > 0 else None)
            assert {hit.text for hit in page.hits} == {
                "alpha shared",
                *(("alpha lexical",) if fts_weight > 0 else ()),
                *(("unrelated semantic",) if vector_weight > 0 else ()),
            }
            candidates = build_recall_candidates(memory_hits=page.hits, topic_memory_hits=(), experience_hits=())
            for hit, candidate in zip(page.hits, candidates, strict=True):
                assert hit.score_upper_bound is not None and hit.score_upper_bound > 0
                assert candidate.score == pytest.approx(hit.hit.score / hit.score_upper_bound)
                assert candidate.relevance == hit.relevance
            if vector_weight == 0:
                assert all(hit.relevance is None for hit in page.hits)

    asyncio.run(scenario())


def test_atomic_rerank_preserves_weighted_gate_metadata(tmp_path: Path) -> None:
    async def scenario():
        async with open_builtin_contexts(
            BuiltinConfig(
                database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'rerank.db'}"),
                runtime=RuntimeConfig(recall_fts_weight=3.0, recall_vector_weight=1.0),
            ),
            embedding_model=_Embedding(),
            memory_reranker=_SelectingReranker(),
        ) as contexts:
            await contexts.get("project")
            for text in ("alpha lexical", "unrelated semantic"):
                await contexts.records.create_artifact(
                    "project", "atomic-memory", ArtifactWrite(content={"kind": "fact", "text": text})
                )
            page = await contexts.atomic_memory.for_scope("project").search("alpha", mode="hybrid", limit=2)
            assert tuple(hit.text for hit in page.hits) == ("unrelated semantic", "alpha lexical")
            assert page.hits[0].score_upper_bound == pytest.approx(0.5 / 61)
            assert page.hits[1].score_upper_bound == pytest.approx(1.5 / 61)
            assert page.hits[0].relevance == pytest.approx(1.0)
            assert page.hits[1].relevance is None
            candidates = build_recall_candidates(memory_hits=page.hits, topic_memory_hits=(), experience_hits=())
            assert [candidate.score for candidate in candidates] == pytest.approx([1.0, 1.0])

    asyncio.run(scenario())
