#!/usr/bin/env python
# -*- coding: UTF-8 -*-
"""Contract tests for the U4-U6 semantics prepared ahead of the official
powercontext second question (powermem-mcp).

U4 - nested citation addressing for exact-entry memory tools
    get_memory_by_id / update_memory / delete_memory /
    delete_memory_with_profile accept a nested ``citation`` object with
    ``memory_ref`` / ``entry_id`` / ``entry_version_id``; resolution
    precedence is memory_ref > entry_id > entry_version_id, while the legacy
    flat ``memory_id`` remains a backward-compatible alias.

U5 - logical request_id bridging
    A MCP tool call shares one request_id between the tool body
    (``powermem.mcp.access.current_request_id``) and the emitted access log;
    internal bridge work can be flagged via ``mark_internal_bridge`` and is
    reflected in the log's ``internal`` field.

U6 - single-count access log
    Exactly one ``transport.request.completed`` record per logical tool call
    (operation ``mcp.tools.call``, transport ``mcp``); bridge / HTTP logs are
    never counted as additional tool calls.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

from powermem.mcp import access as mcp_access
from powermem.mcp import server as mcp_server
from powermem.mcp.server import (
    MemoryCitation,
    _resolve_memory_id,
    delete_memory,
    delete_memory_with_profile,
    get_memory_by_id,
    mcp,
    update_memory,
)

SNOWFLAKE = 725653563933458432
SNOWFLAKE_STR = "725653563933458432"

EXACT_TOOLS = ("get_memory_by_id", "update_memory", "delete_memory", "delete_memory_with_profile")


# ---------------------------------------------------------------------------
# U4: nested citation schema + resolution
# ---------------------------------------------------------------------------


def _tool_schema(tool_name: str) -> Dict[str, Any]:
    import asyncio

    tools = asyncio.run(mcp.list_tools())
    for t in tools:
        if t.name == tool_name:
            return t.parameters
    raise AssertionError(f"tool {tool_name} not registered")


@pytest.mark.parametrize("tool_name", EXACT_TOOLS)
def test_u4_exact_tools_expose_nested_citation(tool_name: str) -> None:
    """Exact-entry tools carry a nested ``citation`` with the three keys."""
    props = _tool_schema(tool_name).get("properties", {})
    assert "citation" in props
    cit_props = props["citation"]["anyOf"][0]["properties"]
    assert set(cit_props) == {"memory_ref", "entry_id", "entry_version_id"}


@pytest.mark.parametrize("tool_name", EXACT_TOOLS)
def test_u4_flat_memory_id_still_available_for_backward_compat(tool_name: str) -> None:
    """Existing clients keep working: memory_id stays a plain string schema."""
    props = _tool_schema(tool_name).get("properties", {})
    assert props["memory_id"]["type"] == "string"
    assert "anyOf" not in props["memory_id"]


def test_u4_memory_ref_resolves_to_memory_id() -> None:
    mock_mem = MagicMock()
    mock_mem.get.return_value = {"id": SNOWFLAKE, "content": "x"}
    with patch.object(mcp_server, "get_memory", return_value=mock_mem):
        out = get_memory_by_id(memory_id="0", citation={"memory_ref": SNOWFLAKE_STR})
    assert json.loads(out)["id"] == SNOWFLAKE_STR
    mock_mem.get.assert_called_once_with(memory_id=SNOWFLAKE, user_id=None, agent_id=None)


def test_u4_memory_ref_accepts_prefix() -> None:
    mock_mem = MagicMock()
    mock_mem.get.return_value = {"id": SNOWFLAKE, "content": "x"}
    with patch.object(mcp_server, "get_memory", return_value=mock_mem):
        out = get_memory_by_id(
            memory_id="0", citation={"memory_ref": f"memory:{SNOWFLAKE_STR}"}
        )
    assert json.loads(out)["success"] if "success" in out else True
    mock_mem.get.assert_called_once_with(memory_id=SNOWFLAKE, user_id=None, agent_id=None)


def test_u4_entry_id_and_version_id_fallbacks() -> None:
    mock_mem = MagicMock()
    mock_mem.get.return_value = {"id": SNOWFLAKE, "content": "x"}
    with patch.object(mcp_server, "get_memory", return_value=mock_mem):
        get_memory_by_id(memory_id="0", citation={"entry_id": SNOWFLAKE_STR})
    mock_mem.get.assert_called_once_with(memory_id=SNOWFLAKE, user_id=None, agent_id=None)

    mock_mem2 = MagicMock()
    mock_mem2.get.return_value = {"id": SNOWFLAKE, "content": "x"}
    with patch.object(mcp_server, "get_memory", return_value=mock_mem2):
        get_memory_by_id(memory_id="0", citation={"entry_version_id": SNOWFLAKE_STR})
    mock_mem2.get.assert_called_once_with(memory_id=SNOWFLAKE, user_id=None, agent_id=None)


def test_u4_citation_precedence_over_flat_id() -> None:
    """memory_ref wins even when a conflicting flat memory_id is supplied."""
    mock_mem = MagicMock()
    mock_mem.get.return_value = {"id": SNOWFLAKE, "content": "x"}
    with patch.object(mcp_server, "get_memory", return_value=mock_mem):
        get_memory_by_id(memory_id="111", citation={"memory_ref": SNOWFLAKE_STR})
    mock_mem.get.assert_called_once_with(memory_id=SNOWFLAKE, user_id=None, agent_id=None)


def test_u4_update_delete_and_profile_delete_via_citation() -> None:
    mock_mem = MagicMock()
    mock_mem.update.return_value = {"id": SNOWFLAKE, "content": "m2"}
    mock_mem.delete.return_value = True
    with patch.object(mcp_server, "get_memory", return_value=mock_mem):
        out_u = update_memory(
            memory_id="0", content="m2", citation={"entry_id": SNOWFLAKE_STR}
        )
        out_d = delete_memory(memory_id="0", citation={"entry_version_id": SNOWFLAKE_STR})
    assert json.loads(out_u)["id"] == SNOWFLAKE_STR
    assert json.loads(out_d)["success"] is True
    mock_mem.update.assert_called_once_with(
        memory_id=SNOWFLAKE, content="m2", user_id=None, agent_id=None, metadata=None
    )
    mock_mem.delete.assert_called_once_with(memory_id=SNOWFLAKE, user_id=None, agent_id=None)

    mock_um = MagicMock()
    mock_um.delete.return_value = True
    with patch.object(mcp_server, "get_user_memory", return_value=mock_um):
        out_p = delete_memory_with_profile(
            memory_id="0", user_id="u1", citation={"memory_ref": SNOWFLAKE_STR}
        )
    assert json.loads(out_p)["success"] is True
    mock_um.delete.assert_called_once_with(
        memory_id=SNOWFLAKE, user_id="u1", agent_id=None, delete_profile=False
    )


def test_u4_resolve_memory_id_unit() -> None:
    assert _resolve_memory_id(None, MemoryCitation(memory_ref=SNOWFLAKE_STR)) == SNOWFLAKE
    assert _resolve_memory_id(None, MemoryCitation(entry_id="123")) == 123
    assert _resolve_memory_id(None, MemoryCitation(entry_version_id="456")) == 456
    assert _resolve_memory_id("789", None) == 789
    assert _resolve_memory_id("789", MemoryCitation()) == 789  # empty citation falls back
    assert _resolve_memory_id(None, MemoryCitation(memory_ref="entry:321")) == 321
    with pytest.raises(ValueError):
        _resolve_memory_id(None, MemoryCitation(memory_ref="not-a-number"))
    with pytest.raises(ValueError):
        _resolve_memory_id(None, None)


# ---------------------------------------------------------------------------
# U5: logical request_id bridging
# ---------------------------------------------------------------------------


@pytest.fixture
def access_caplog(caplog):
    caplog.set_level(logging.INFO, logger="powermem.mcp.access")
    return caplog


def _call_delete_all(inside_rids: list, mark_internal: bool = False):
    mock_mem = MagicMock()
    mock_mem.delete_all.return_value = True

    def _inside(user_id=None, agent_id=None, run_id=None):
        inside_rids.append(mcp_access.current_request_id())
        if mark_internal:
            mcp_access.mark_internal_bridge()
        return True

    mock_mem.delete_all.side_effect = _inside
    with patch.object(mcp_server, "get_memory", return_value=mock_mem):
        return mcp_server.delete_all_memories(user_id="u1")


def test_u5_tool_call_emits_access_log_with_request_id(access_caplog) -> None:
    assert mcp_access.current_request_id() is None
    inside_rids: list = []
    _call_delete_all(inside_rids)
    assert mcp_access.current_request_id() is None  # scope cleaned up

    recs = [r for r in access_caplog.records if getattr(r, "event", None) == "transport.request.completed"]
    assert len(recs) == 1
    rec = recs[0]
    assert rec.operation == "mcp.tools.call"
    assert rec.transport == "mcp"
    assert rec.status == "ok"
    assert rec.request_id
    assert rec.internal is False
    # request_id is shared with the tool body (U5 bridge semantics)
    assert inside_rids == [rec.request_id]


def test_u5_request_id_changes_per_call(access_caplog) -> None:
    _call_delete_all([])
    _call_delete_all([])
    recs = [r for r in access_caplog.records if getattr(r, "event", None) == "transport.request.completed"]
    assert len(recs) == 2
    assert recs[0].request_id != recs[1].request_id


def test_u5_internal_bridge_flag_propagates_to_log(access_caplog) -> None:
    _call_delete_all([], mark_internal=True)
    recs = [r for r in access_caplog.records if getattr(r, "event", None) == "transport.request.completed"]
    assert len(recs) == 1
    assert recs[0].internal is True


def test_u5_error_call_logs_status_error_and_reraises(access_caplog) -> None:
    mock_mem = MagicMock()
    mock_mem.delete_all.side_effect = RuntimeError("boom")
    with patch.object(mcp_server, "get_memory", return_value=mock_mem):
        with pytest.raises(RuntimeError):
            mcp_server.delete_all_memories()
    recs = [r for r in access_caplog.records if getattr(r, "event", None) == "transport.request.completed"]
    assert len(recs) == 1
    assert recs[0].status == "error"


# ---------------------------------------------------------------------------
# U6: access log counts each logical tool call exactly once
# ---------------------------------------------------------------------------


def test_u6_single_access_record_per_tool_call(access_caplog) -> None:
    _call_delete_all([])
    recs = [r for r in access_caplog.records if getattr(r, "event", None) == "transport.request.completed"]
    tool_recs = [r for r in recs if r.operation == "mcp.tools.call"]
    assert len(recs) == 1
    assert len(tool_recs) == 1
    assert tool_recs[0].transport == "mcp"


def test_u6_bridge_http_logs_not_counted_as_tool_calls(access_caplog) -> None:
    mock_mem = MagicMock()
    mock_mem.delete_all.return_value = True

    def _inside(user_id=None, agent_id=None, run_id=None):
        # a bridge would emit its own transport log (e.g. http) — this must
        # not be counted as an additional mcp.tools.call record
        mcp_access.log_access(operation="list_memory_entries", transport="http")
        return True

    mock_mem.delete_all.side_effect = _inside
    with patch.object(mcp_server, "get_memory", return_value=mock_mem):
        mcp_server.delete_all_memories()

    recs = [r for r in access_caplog.records if getattr(r, "event", None) == "transport.request.completed"]
    tool_recs = [r for r in recs if r.operation == "mcp.tools.call"]
    http_recs = [r for r in recs if r.transport == "http"]
    assert len(tool_recs) == 1  # exactly one logical tool call
    assert len(http_recs) == 1
    assert http_recs[0].operation == "list_memory_entries"
    assert all(r.request_id == tool_recs[0].request_id for r in http_recs)
