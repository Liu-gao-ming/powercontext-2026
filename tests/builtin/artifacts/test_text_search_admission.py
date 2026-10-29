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

import pytest

from powercontext.builtin.artifacts.search import admits_fts_text, analyze_fts_query, analyze_text


@pytest.mark.parametrize(
    "suffix",
    [
        "Answer briefly and cite the artifact reference from the provided context. "
        "If no reliable context is present, say so.",
        "Answer concisely and identify source references from supplied context. "
        "If no evidence is available, answer unknown.",
    ],
)
def test_answer_format_does_not_displace_topic_evidence(suffix: str) -> None:
    query = "What working approach did I previously prefer for this project?"
    fact = "For this project, the user prefers a multi-Agent working approach."
    assert admits_fts_text(query, fact)
    assert admits_fts_text(f"{query} {suffix}", fact)
    assert not admits_fts_text(f"{query} {suffix}", f"Execution policy: {suffix}")
    assert not admits_fts_text(f"{query} {suffix}", "PostgreSQL locks coordinate leader election.")


@pytest.mark.parametrize(
    "content,protected",
    [
        (
            '"Answer briefly and cite the artifact reference from the provided context."',
            "answer briefly and cite the artifact reference from the provided context",
        ),
        ("If no reliable backup is present, stop deployment.", "no reliable backup stop deployment"),
        ("Answer briefly and cite rollback_mode v2.7 without approval.", "rollback_mode v2.7 without approval"),
        ("请保留 release_gate_v3 并且不要部署未审核的版本。", "release_gate_v3 不要部署未审核版本"),
    ],
)
def test_query_normalization_preserves_quotes_domain_conditions_and_identifiers(content: str, protected: str) -> None:
    normalized = set(analyze_fts_query(f"deployment constraints? {content}").split())
    # Function words may be removed, but no domain term or negative condition may disappear.
    expected = set(analyze_text(protected).split())
    assert expected <= normalized
    assert admits_fts_text(content, content)


def test_execution_policy_can_itself_be_the_entire_search_subject() -> None:
    policy = "Answer briefly and cite the artifact reference from the provided context."
    assert set(analyze_fts_query(policy).split()) <= set(analyze_text(policy).split())
    assert admits_fts_text(policy, policy)
