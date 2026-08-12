"""partner_capacity_mcp — the capacity + engagement MCP server (§7.1).

Eight tools over the NordLedger warehouse. The two hard rules the whole
architecture leans on are enforced HERE, server-side — not in the model:

  1. an inactive partner is never returned by list_candidates
  2. an over-hard-cap partner is never returned by list_candidates
     (including partners whose remaining headroom is consumed by active
     in-memory holds — the concurrency guard of failure mode #9)

Every tool returns a JSON string so the payload shape is identical over any
transport (in-process, stdio, HTTP+SSE).

Run standalone (stdio, e.g. for Claude Desktop):
    DBT_PROJECT_DIR=./warehouse python -m mcp_servers.partner_capacity.server
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import duckdb
from mcp.server.mcpserver import MCPServer

from src.data import warehouse as wh


def _json_default(o):
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    if isinstance(o, Decimal):
        return float(o)      # DuckDB aggregates (MAX/AVG) come back as Decimal
    raise TypeError(f"not JSON serializable: {type(o)}")


def _dumps(payload) -> str:
    return json.dumps(payload, default=_json_default)


def build_server(warehouse_path: Path) -> MCPServer:
    app = MCPServer(
        name="partner_capacity",
        instructions=(
            "Capacity, specialization, engagement and status for NordLedger "
            "fulfillment + referring partners. list_candidates enforces the "
            "hard filters (active status, under hard-cap) server-side."
        ),
    )

    # In-memory capacity holds: partner_id -> {lead_id: expires_at_iso}.
    # Holds are runtime state (the negotiation window), not warehouse rows —
    # the hold table IS the concurrency source of truth (failure mode #9).
    holds: dict[str, dict[str, str]] = {}

    def _active_holds(partner_id: str) -> int:
        return len(holds.get(partner_id, {}))

    @app.tool()
    def list_candidates(country: str, service_type: str | None = None,
                        industry: str | None = None,
                        as_of: str = "") -> str:
        """Ranked-signal candidate list for a market. Hard filters enforced
        server-side: only active partners, only partners with headroom left
        after subtracting live holds. Returns candidate dicts with capacity,
        specialization, ROI + trend, churn-risk and latency signals."""
        as_of_d = date.fromisoformat(as_of) if as_of else date.today()
        raw = wh.list_candidates(
            warehouse_path=warehouse_path, country=country,
            service_type=service_type, industry=industry, as_of=as_of_d,
        )
        out = []
        for c in raw:
            headroom = c.hard_cap - c.active_deals_count - _active_holds(c.partner_id)
            if headroom <= 0:
                continue        # holds consumed the remaining capacity
            d = asdict(c)
            d["live_holds"] = _active_holds(c.partner_id)
            d["headroom"] = headroom
            out.append(d)
        return _dumps({"candidates": out, "as_of": as_of_d.isoformat()})

    @app.tool()
    def list_dormant_partners(country: str, service_type: str | None = None,
                              industry: str | None = None) -> str:
        """Inactive partners that would otherwise match — reactivation
        candidates for the manager console when the active list is empty."""
        rows = wh.list_dormant_partners(
            warehouse_path=warehouse_path, country=country,
            service_type=service_type, industry=industry,
        )
        return _dumps({"dormant": rows})

    @app.tool()
    def set_partner_status(partner_id: str, status: str, reason: str,
                           changed_by: str = "hitl") -> str:
        """Activate / deactivate a partner. Writes the audit trail to
        partner_status_events. Restricted to the HITL flow — the graph never
        calls this autonomously."""
        if status not in ("active", "inactive"):
            return _dumps({"ok": False, "error": f"invalid status {status!r}"})
        with duckdb.connect(str(warehouse_path)) as con:
            prior = con.execute(
                "SELECT status FROM main.partners WHERE partner_id = ?",
                [partner_id]).fetchone()
            if prior is None:
                return _dumps({"ok": False, "error": f"unknown partner {partner_id}"})
            prior_status = prior[0]
            con.execute(
                "UPDATE main.partners SET status = ? WHERE partner_id = ?",
                [status, partner_id])
            event_id = "RT" + os.urandom(4).hex().upper()
            con.execute(
                "INSERT INTO main.partner_status_events "
                "(event_id, partner_id, prior_status, new_status, event_at, reason, changed_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [event_id, partner_id, prior_status, status,
                 date.today().isoformat(), reason, changed_by])
        return _dumps({"ok": True, "partner_id": partner_id,
                       "prior_status": prior_status, "new_status": status,
                       "event_id": event_id})

    @app.tool()
    def hold_capacity(partner_id: str, lead_id: str, ttl_hours: int = 48,
                      as_of: str = "") -> str:
        """Reserve a capacity slot on a partner during negotiation. Fails when
        the partner has no headroom left (active deals + live holds >= hard
        cap) — the concurrency guard: two graph runs can't book the same last
        slot. Idempotent per (partner, lead)."""
        as_of_d = date.fromisoformat(as_of) if as_of else date.today()
        partner_holds = holds.setdefault(partner_id, {})
        if lead_id in partner_holds:
            return _dumps({"ok": True, "idempotent": True})
        with duckdb.connect(str(warehouse_path)) as con:
            row = con.execute(
                "SELECT active_deals_count, hard_cap FROM main_staging.stg_partner_capacity "
                "WHERE partner_id = ? AND snapshot_date = ?",
                [partner_id, as_of_d.isoformat()]).fetchone()
        active, hard_cap = (int(row[0]), int(row[1])) if row else (0, 12)
        if active + len(partner_holds) >= hard_cap:
            return _dumps({"ok": False,
                           "error": f"no headroom on {partner_id}: "
                                    f"{active} active + {len(partner_holds)} holds >= cap {hard_cap}"})
        expires = (datetime.now() + timedelta(hours=ttl_hours)).isoformat()
        partner_holds[lead_id] = expires
        return _dumps({"ok": True, "partner_id": partner_id,
                       "lead_id": lead_id, "expires_at": expires})

    @app.tool()
    def release_capacity(partner_id: str, lead_id: str) -> str:
        """Free a held slot — on decline, timeout, or booking-elsewhere.
        Idempotent: releasing a non-existent hold is a no-op success."""
        released = holds.get(partner_id, {}).pop(lead_id, None) is not None
        return _dumps({"ok": True, "released": released})

    @app.tool()
    def get_partner_load(partner_id: str, as_of: str = "") -> str:
        """Current active deals + caps + live holds for one partner."""
        as_of_d = date.fromisoformat(as_of) if as_of else date.today()
        with duckdb.connect(str(warehouse_path)) as con:
            row = con.execute(
                "SELECT active_deals_count, soft_cap, hard_cap "
                "FROM main_staging.stg_partner_capacity "
                "WHERE partner_id = ? AND snapshot_date = ?",
                [partner_id, as_of_d.isoformat()]).fetchone()
        if row is None:
            return _dumps({"ok": False, "error": f"no capacity snapshot for {partner_id} on {as_of_d}"})
        active, soft, hard = int(row[0]), int(row[1]), int(row[2])
        return _dumps({"ok": True, "partner_id": partner_id,
                       "active_deals_count": active, "soft_cap": soft,
                       "hard_cap": hard, "live_holds": _active_holds(partner_id),
                       "headroom": hard - active - _active_holds(partner_id)})

    @app.tool()
    def get_partner_engagement(partner_id: str, window_days: int = 90,
                               as_of: str = "") -> str:
        """Rolling engagement + ROI rollup for a fulfillment partner —
        used by the Monitor's weekly report and the manager console."""
        as_of_d = date.fromisoformat(as_of) if as_of else date.today()
        data = wh.get_partner_engagement(
            warehouse_path, partner_id=partner_id,
            window_days=window_days, as_of=as_of_d)
        return _dumps({"ok": bool(data), "engagement": data})

    @app.tool()
    def get_referring_partner_engagement(referring_partner_id: str,
                                         window_days: int = 90,
                                         as_of: str = "") -> str:
        """Ambassador-side rollup (the Billy-analogs) — symmetrical to
        get_partner_engagement."""
        as_of_d = date.fromisoformat(as_of) if as_of else date.today()
        data = wh.get_referring_partner_engagement(
            warehouse_path, referring_partner_id=referring_partner_id,
            window_days=window_days, as_of=as_of_d)
        return _dumps({"ok": bool(data), "engagement": data})

    return app


def main() -> None:
    warehouse_dir = Path(os.environ.get("DBT_PROJECT_DIR", "./warehouse")).resolve()
    app = build_server(warehouse_dir / "nordledger.duckdb")
    app.run()          # stdio transport


if __name__ == "__main__":
    main()
