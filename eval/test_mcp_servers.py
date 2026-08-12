"""MCP contract tests — tool surfaces + the pure (no-warehouse) slack mock.

The tool-listing tests run in the default deterministic suite: they assert
the MCP contract (which tools exist, on which server) without touching the
warehouse. The slack tests exercise the full MCP call path (schema
validation, CallToolResult, JSON payloads) entirely in memory.
"""

from __future__ import annotations

from pathlib import Path

from src.mcp_stack import MCPStack

_FAKE_WAREHOUSE = Path("nonexistent.duckdb")   # listing tools never connects


# ── contract: tool surfaces ─────────────────────────────────────────────────

def test_partner_capacity_exposes_8_tools():
    stack = MCPStack(_FAKE_WAREHOUSE)
    assert stack.list_tools("partner_capacity") == sorted([
        "list_candidates", "list_dormant_partners", "set_partner_status",
        "hold_capacity", "release_capacity", "get_partner_load",
        "get_partner_engagement", "get_referring_partner_engagement",
    ])


def test_slack_exposes_3_prod_tools_plus_2_mock_helpers():
    stack = MCPStack(_FAKE_WAREHOUSE)
    tools = stack.list_tools("slack")
    for prod_tool in ("send_dm", "post_to_channel", "wait_for_reply"):
        assert prod_tool in tools
    for mock_helper in ("prime_replies", "get_sent"):
        assert mock_helper in tools
    assert len(tools) == 5


def test_crm_exposes_3_tools():
    stack = MCPStack(_FAKE_WAREHOUSE)
    assert stack.list_tools("crm") == sorted([
        "upsert_lead", "update_stage", "attach_note",
    ])


# ── slack mock behaviour (pure, in-memory, full MCP call path) ──────────────

def test_slack_send_dm_returns_message_id():
    stack = MCPStack(_FAKE_WAREHOUSE)
    r = stack.call("slack", "send_dm", user_id="P007", text="hello")
    assert r["ok"] and r["message_id"].startswith("msg_")


def test_slack_auto_reply_defaults_to_accept():
    stack = MCPStack(_FAKE_WAREHOUSE)
    dm = stack.call("slack", "send_dm", user_id="P007", text="hi")
    reply = stack.call("slack", "wait_for_reply",
                       message_id=dm["message_id"], timeout_hours=24)
    assert reply["outcome"] == "accept"
    assert reply["reply"] is not None
    assert reply["elapsed_hours"] == 6.0


def test_slack_primed_replies_fifo_then_fallback():
    stack = MCPStack(_FAKE_WAREHOUSE)
    stack.prime_slack_replies(["decline", "counter"])
    outcomes = []
    for _ in range(3):
        dm = stack.call("slack", "send_dm", user_id="P007", text="hi")
        r = stack.call("slack", "wait_for_reply", message_id=dm["message_id"])
        outcomes.append(r["outcome"])
    assert outcomes == ["decline", "counter", "accept"]   # FIFO → fallback


def test_slack_no_reply_sentinel_simulates_timeout():
    stack = MCPStack(_FAKE_WAREHOUSE)
    stack.prime_slack_replies(["no_reply"])
    dm = stack.call("slack", "send_dm", user_id="P007", text="hi")
    r = stack.call("slack", "wait_for_reply",
                   message_id=dm["message_id"], timeout_hours=24)
    assert r["outcome"] == "no_reply"
    assert r["reply"] is None
    assert r["elapsed_hours"] == 24.0       # the full timeout elapsed


def test_slack_outbox_records_everything():
    stack = MCPStack(_FAKE_WAREHOUSE)
    stack.call("slack", "send_dm", user_id="P007", text="dm one")
    stack.call("slack", "post_to_channel", channel="#digest", text="report")
    sent = stack.call("slack", "get_sent")
    assert sent["total"] == 2
    kinds = [m["kind"] for m in sent["sent"]]
    assert kinds == ["dm", "channel"]


def test_stack_call_log_records_every_call():
    stack = MCPStack(_FAKE_WAREHOUSE)
    stack.call("slack", "send_dm", user_id="P007", text="x")
    stack.call("slack", "get_sent")
    assert [c["tool"] for c in stack.call_log] == ["send_dm", "get_sent"]
