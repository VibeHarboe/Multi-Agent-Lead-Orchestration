"""slack_mock_mcp — the mocked Slack surface (§7.2).

Three production-shaped tools (send_dm, post_to_channel, wait_for_reply) plus
two mock-only helpers (prime_replies, get_sent) that exist so tests and the
dry-run can script partner behaviour deterministically. In production this
whole server is swapped for Slack's real MCP — same three tool names, the
mock helpers simply disappear.

Reply model: `wait_for_reply` pops from a primed FIFO queue; when the queue is
empty it falls back to the server's `auto_reply` mode ("accept" by default).
The sentinel "no_reply" simulates a 24h timeout (returns reply=None).

Run standalone (stdio):
    python -m mcp_servers.slack_mock.server
"""

from __future__ import annotations

import itertools
import json

from mcp.server.mcpserver import MCPServer


def build_server(auto_reply: str = "accept",
                 reply_delay_hours: float = 6.0) -> MCPServer:
    app = MCPServer(
        name="slack_mock",
        instructions=(
            "Mocked Slack for the NordLedger simulation. send_dm / "
            "post_to_channel / wait_for_reply mirror the real Slack MCP "
            "surface; prime_replies and get_sent are mock-only test helpers."
        ),
    )

    counter = itertools.count(1)
    sent: list[dict] = []                  # every message sent, in order
    primed: list[str] = []                 # scripted replies, FIFO

    _REPLY_TEXT = {
        "accept": "Yes, we can take this on. Let's kick off.",
        "decline": "Thanks, but we have to pass on this one.",
        "counter": "Could we propose an alternative timeline?",
    }

    @app.tool()
    def send_dm(user_id: str, text: str) -> str:
        """Send a direct message to a partner contact. Returns message_id."""
        message_id = f"msg_{next(counter):05d}"
        sent.append({"message_id": message_id, "kind": "dm",
                     "user_id": user_id, "text": text})
        return json.dumps({"ok": True, "message_id": message_id})

    @app.tool()
    def post_to_channel(channel: str, text: str) -> str:
        """Post to a channel (digests, weekly reports). Returns message_id."""
        message_id = f"msg_{next(counter):05d}"
        sent.append({"message_id": message_id, "kind": "channel",
                     "channel": channel, "text": text})
        return json.dumps({"ok": True, "message_id": message_id})

    @app.tool()
    def wait_for_reply(message_id: str, timeout_hours: int = 24) -> str:
        """Wait for the partner's reply to a DM. Mock semantics: pops the
        primed FIFO; empty queue falls back to the auto_reply mode. The
        'no_reply' sentinel simulates a timeout — reply is null and
        elapsed_hours equals the timeout (the 24h dynamic re-routing SLA)."""
        outcome = primed.pop(0) if primed else auto_reply
        if outcome == "no_reply":
            return json.dumps({"ok": True, "message_id": message_id,
                               "reply": None, "outcome": "no_reply",
                               "elapsed_hours": float(timeout_hours)})
        return json.dumps({"ok": True, "message_id": message_id,
                           "reply": _REPLY_TEXT.get(outcome, outcome),
                           "outcome": outcome,
                           "elapsed_hours": reply_delay_hours})

    @app.tool()
    def prime_replies(replies: list[str]) -> str:
        """MOCK-ONLY: queue scripted outcomes for the next wait_for_reply
        calls. Valid entries: accept | decline | counter | no_reply."""
        primed.extend(replies)
        return json.dumps({"ok": True, "queued": len(primed)})

    @app.tool()
    def get_sent(limit: int = 50) -> str:
        """MOCK-ONLY: inspect the outbox (most recent last)."""
        return json.dumps({"ok": True, "sent": sent[-limit:], "total": len(sent)})

    return app


def main() -> None:
    build_server().run()   # stdio transport


if __name__ == "__main__":
    main()
