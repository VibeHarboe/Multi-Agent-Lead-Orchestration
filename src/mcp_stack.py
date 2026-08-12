"""MCPStack — the sync facade the graph calls MCP tools through.

Holds the three MCP servers (partner_capacity, slack_mock, crm_mock) and
exposes one synchronous entrypoint:

    stack = MCPStack(warehouse_path)
    result = stack.call("partner_capacity", "list_candidates",
                        country="SE", service_type="bookkeeping",
                        as_of="2024-12-15")           # -> dict

Transport note: the servers are real MCP servers — every call goes through
the MCP tool layer (schema validation, serialization, CallToolResult), using
in-process invocation by default. The same server objects run over stdio for
external clients (`python -m mcp_servers.partner_capacity.server`), and the
JSON payloads are identical on either path — which is exactly what the Week 3
parity tests assert.

Sync-over-async: agents and the CLI are synchronous; each call runs the
server's async `call_tool` to completion via asyncio.run. At our per-lead
call volume (tens of calls) the loop-per-call overhead is irrelevant.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from mcp_servers.crm_mock.server import build_server as build_crm
from mcp_servers.partner_capacity.server import build_server as build_capacity
from mcp_servers.slack_mock.server import build_server as build_slack


class MCPToolError(RuntimeError):
    """Raised when a tool call errors at the MCP layer (is_error=True)."""


class MCPStack:
    def __init__(self, warehouse_path: Path, *,
                 slack_auto_reply: str = "accept",
                 slack_reply_delay_hours: float = 6.0,
                 recorder=None):
        self.warehouse_path = Path(warehouse_path)
        self.servers = {
            "partner_capacity": build_capacity(self.warehouse_path),
            "slack": build_slack(auto_reply=slack_auto_reply,
                                 reply_delay_hours=slack_reply_delay_hours),
            "crm": build_crm(self.warehouse_path),
        }
        self.call_log: list[dict] = []      # every call, for audit/tests
        self.recorder = recorder            # optional TraceRecorder

    # ── core ────────────────────────────────────────────────────────────
    def call(self, server: str, tool: str, **arguments) -> dict:
        """Invoke one MCP tool synchronously and return the parsed JSON dict.
        With a recorder attached, every call becomes a child span under the
        caller's current span — the `mcp.<server>.<tool>` leaves of §10's
        reference trace tree."""
        if self.recorder is not None:
            with self.recorder.span(f"mcp.{server}.{tool}"):
                return self._call_inner(server, tool, arguments)
        return self._call_inner(server, tool, arguments)

    def _call_inner(self, server: str, tool: str, arguments: dict) -> dict:
        app = self.servers[server]
        result = asyncio.run(app.call_tool(tool, arguments))
        if getattr(result, "is_error", False):
            raise MCPToolError(f"{server}.{tool} errored: {result!r}")
        payload = self._extract_json(result)
        self.call_log.append({"server": server, "tool": tool,
                              "arguments": arguments, "result": payload})
        return payload

    @staticmethod
    def _extract_json(result) -> dict:
        # CallToolResult.content -> [TextContent(text=<json str>)]
        content = getattr(result, "content", result)
        for block in content:
            text = getattr(block, "text", None)
            if text is not None:
                return json.loads(text)
        raise MCPToolError(f"no text content in tool result: {result!r}")

    # ── introspection (used by contract tests) ──────────────────────────
    def list_tools(self, server: str) -> list[str]:
        tools = asyncio.run(self.servers[server].list_tools())
        return sorted(t.name for t in tools)

    # ── convenience for tests / dry-run scripting ───────────────────────
    def prime_slack_replies(self, replies: list[str]) -> None:
        self.call("slack", "prime_replies", replies=replies)
