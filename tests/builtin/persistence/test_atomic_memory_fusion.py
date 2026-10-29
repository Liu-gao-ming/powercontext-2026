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

from dataclasses import replace

import pytest

from powercontext import ArtifactRef
from powercontext.builtin.artifacts.search import RecallChannelWeights
from powercontext.builtin.persistence.atomic_memory_index import (
    AtomicMemoryIndexError,
    AtomicMemoryIndexHit,
    AtomicMemorySearchChannels,
    combine_atomic_memory_channels,
)


def _hit(artifact_id: str, *, distance: float | None = None) -> AtomicMemoryIndexHit:
    return AtomicMemoryIndexHit(
        ArtifactRef(family="atomic-memory", artifact_id=artifact_id, revision=1),
        state_version=0,
        kind="fact",
        text=artifact_id,
        score=0.0,
        distance=distance,
    )


def test_default_atomic_weights_preserve_unweighted_results() -> None:
    alpha, beta = _hit("alpha"), _hit("beta", distance=0.1)
    channels = AtomicMemorySearchChannels(fts=(alpha, beta), vector=(beta, alpha))
    assert combine_atomic_memory_channels(channels, channel_weights=RecallChannelWeights()) == (
        combine_atomic_memory_channels(channels)
    )


@pytest.mark.parametrize(("fts", "vector", "first"), [(3.0, 1.0, "lexical"), (1.0, 3.0, "semantic")])
def test_atomic_fusion_orders_disjoint_channels_by_weight(fts: float, vector: float, first: str) -> None:
    hits = combine_atomic_memory_channels(
        AtomicMemorySearchChannels(fts=(_hit("lexical"),), vector=(_hit("semantic", distance=0.1),)),
        channel_weights=RecallChannelWeights(fts=fts, vector=vector),
    )
    assert hits[0].artifact_ref.artifact_id == first
    assert hits[0].score == pytest.approx(1.5 / 61)
    assert hits[1].score == pytest.approx(0.5 / 61)


@pytest.mark.parametrize(("fts", "vector", "selected"), [(0.0, 1.0, "semantic"), (1.0, 0.0, "lexical")])
def test_zero_weight_excludes_atomic_matches_from_only_that_channel(fts: float, vector: float, selected: str) -> None:
    hits = combine_atomic_memory_channels(
        AtomicMemorySearchChannels(fts=(_hit("lexical"),), vector=(_hit("semantic", distance=0.1),)),
        channel_weights=RecallChannelWeights(fts=fts, vector=vector),
    )
    assert len(hits) == 1
    assert hits[0].artifact_ref.artifact_id == selected
    assert hits[0].score == pytest.approx(2 / 61)


@pytest.mark.parametrize("conflict", ["revision", "state_version"])
@pytest.mark.parametrize(("fts", "vector"), [(1.0, 1.0), (0.0, 1.0), (1.0, 0.0)])
def test_atomic_fusion_rejects_conflicting_identity_even_in_zero_weight_channels(conflict: str, fts, vector) -> None:
    original = _hit("shared")
    changed = (
        replace(original, artifact_ref=original.artifact_ref.model_copy(update={"revision": 2}))
        if conflict == "revision"
        else replace(original, state_version=1)
    )
    with pytest.raises(AtomicMemoryIndexError, match="changed between retrieval channels"):
        combine_atomic_memory_channels(
            AtomicMemorySearchChannels(fts=(original,), vector=(changed,)),
            channel_weights=RecallChannelWeights(fts=fts, vector=vector),
        )
