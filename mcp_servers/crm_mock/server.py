"""crm_mock_mcp — the mocked CRM surface (§7.3).

Three tools mirroring a real Salesforce/HubSpot MCP: upsert_lead,
update_stage, attach_note. Unlike a real CRM MCP that pushes to a remote
system, this mock persists directly into the NordLedger warehouse — every
stage change writes a `deal_events` row and updates `referral_leads`, so the
decision dashboard and the embedded BI Agent see live state (Q4 decision).

Runtime event ids use the "RT" prefix so they never collide with the seeded
"EV" ids — and so tests can clean up after themselves with a single DELETE.

Run standalone (stdio):
    DBT_PROJECT_DIR=./warehouse python -m mcp_servers.crm_mock.server
"""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

import duckdb
from mcp.server.mcpserver import MCPServer

# referral_status enum: pending|matching|negotiating|booked|lost.
# 'resolved' is tracked via resolved_at + a deal_events row, while
# referral_status stays 'booked' (a resolved deal was a booked deal).


def _rt_id() -> str:
    return "RT" + os.urandom(4).hex().upper()


def build_server(warehouse_path: Path) -> MCPServer:
    app = MCPServer(
        name="crm_mock",
        instructions=(
            "Mocked CRM for the NordLedger simulation. Persists stage changes "
            "and notes straight into the warehouse (deal_events + "
            "referral_leads) so dashboards read live state."
        ),
    )

    def _insert_event(con, referral_lead_id: str, partner_id: str | None,
                      event_type: str, agent_name: str, rationale: str,
                      event_at: str) -> str:
        event_id = _rt_id()
        con.execute(
            "INSERT INTO main.deal_events "
            "(event_id, referral_lead_id, partner_id, event_type, event_at, agent_name, rationale) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [event_id, referral_lead_id, partner_id or "", event_type,
             event_at, agent_name, rationale])
        return event_id

    @app.tool()
    def upsert_lead(referral_lead_id: str, lead_json: str) -> str:
        """Create-or-confirm the CRM record for a lead. In this simulation the
        seeded referral row IS the CRM record — upsert verifies it exists and
        returns its current stage; a missing id is an error (leads enter via
        the referral stream, not via the CRM)."""
        with duckdb.connect(str(warehouse_path)) as con:
            row = con.execute(
                "SELECT referral_status FROM main.referral_leads "
                "WHERE referral_lead_id = ?", [referral_lead_id]).fetchone()
        if row is None:
            return json.dumps({"ok": False,
                               "error": f"unknown referral_lead_id {referral_lead_id}"})
        return json.dumps({"ok": True, "referral_lead_id": referral_lead_id,
                           "existed": True, "current_status": row[0]})

    @app.tool()
    def update_stage(referral_lead_id: str, stage: str, rationale: str,
                     partner_id: str = "", agent_name: str = "negotiation",
                     as_of: str = "") -> str:
        """Move the lead to a new stage. Writes referral_leads.referral_status
        (+ booked_at / resolved_at when relevant) and a deal_events audit row
        for terminal stages. Stages: matching | negotiating | booked | lost |
        resolved."""
        if stage not in ("matching", "negotiating", "booked", "lost", "resolved"):
            return json.dumps({"ok": False, "error": f"invalid stage {stage!r}"})
        event_at = as_of or date.today().isoformat()
        with duckdb.connect(str(warehouse_path)) as con:
            row = con.execute(
                "SELECT referral_status FROM main.referral_leads "
                "WHERE referral_lead_id = ?", [referral_lead_id]).fetchone()
            if row is None:
                return json.dumps({"ok": False,
                                   "error": f"unknown referral_lead_id {referral_lead_id}"})
            prior = row[0]

            if stage == "resolved":
                con.execute(
                    "UPDATE main.referral_leads SET resolved_at = ? "
                    "WHERE referral_lead_id = ?", [event_at, referral_lead_id])
                event_id = _insert_event(con, referral_lead_id, partner_id,
                                         "resolved", agent_name, rationale, event_at)
                new_status = prior
            else:
                sets = "referral_status = ?"
                params: list = [stage]
                if stage == "booked":
                    sets += ", booked_at = ?"
                    params.append(event_at)
                if stage == "lost":
                    sets += ", resolved_at = ?"
                    params.append(event_at)
                params.append(referral_lead_id)
                con.execute(
                    f"UPDATE main.referral_leads SET {sets} WHERE referral_lead_id = ?",
                    params)
                event_type = {"booked": "booked", "lost": "lost"}.get(stage)
                event_id = (_insert_event(con, referral_lead_id, partner_id,
                                          event_type, agent_name, rationale, event_at)
                            if event_type else None)
                new_status = stage

        return json.dumps({"ok": True, "referral_lead_id": referral_lead_id,
                           "prior_status": prior, "new_status": new_status,
                           "event_id": event_id})

    @app.tool()
    def attach_note(referral_lead_id: str, note: str,
                    agent_name: str = "hitl", as_of: str = "") -> str:
        """Attach a free-text note to the lead's audit trail (deal_events
        with event_type='note') — manager comments, override reasons, etc."""
        event_at = as_of or date.today().isoformat()
        with duckdb.connect(str(warehouse_path)) as con:
            row = con.execute(
                "SELECT 1 FROM main.referral_leads WHERE referral_lead_id = ?",
                [referral_lead_id]).fetchone()
            if row is None:
                return json.dumps({"ok": False,
                                   "error": f"unknown referral_lead_id {referral_lead_id}"})
            event_id = _insert_event(con, referral_lead_id, None, "note",
                                     agent_name, note, event_at)
        return json.dumps({"ok": True, "event_id": event_id})

    return app


def main() -> None:
    warehouse_dir = Path(os.environ.get("DBT_PROJECT_DIR", "./warehouse")).resolve()
    build_server(warehouse_dir / "nordledger.duckdb").run()   # stdio


if __name__ == "__main__":
    main()
