"""Warehouse-backed MCP tests — partner_capacity + crm_mock against the
built DuckDB. All warehouse-marked; mutating tests restore state via the
warehouse_cleaner fixture.
"""

from __future__ import annotations

from datetime import date

import pytest

from src.agents.matching import match, match_mcp
from src.mcp_stack import MCPStack
from src.state import EXTRACTED_FIELDS, build_lead_state


def _lead(country: str, service_type: str, industry: str = "professional_services"):
    extraction = {f: None for f in EXTRACTED_FIELDS}
    extraction.update(country=country, service_type=service_type, industry=industry)
    return build_lead_state(
        referral_lead_id="RL_mcp_test", raw_text="…",
        received_at=date(2024, 12, 15), extraction=extraction,
        provenance={f: {"confidence": 0.9, "source_span": "…"}
                    for f in EXTRACTED_FIELDS})


# ── parity: fixture path ≡ MCP path ─────────────────────────────────────────

@pytest.mark.warehouse
def test_match_parity_fixture_vs_mcp(warehouse_path):
    """The Week 3 keystone: matching through MCP must produce IDENTICAL
    rankings to the direct fixture path on the same warehouse state."""
    stack = MCPStack(warehouse_path)
    as_of = date(2024, 12, 15)
    for country, service in [("SE", "bookkeeping"), ("NL", "accounting"),
                             ("DE", "tax_advisory"), ("US", "payroll")]:
        lead = _lead(country, service)
        direct = match(lead, warehouse_path=warehouse_path, top_k=3, as_of=as_of)
        via_mcp = match_mcp(lead, stack=stack, top_k=3, as_of=as_of)
        assert direct.status == via_mcp.status, f"{country}/{service}"
        assert ([ (c.partner_id, c.rank, c.composite_score)
                  for c in direct.matched_candidates ] ==
                [ (c.partner_id, c.rank, c.composite_score)
                  for c in via_mcp.matched_candidates ]), f"{country}/{service}"


# ── hard filters through the MCP layer ──────────────────────────────────────

@pytest.mark.warehouse
def test_mcp_list_candidates_excludes_inactive_p018(warehouse_path):
    stack = MCPStack(warehouse_path)
    payload = stack.call("partner_capacity", "list_candidates",
                         country="US", service_type=None, industry=None,
                         as_of="2024-12-15")
    ids = [c["partner_id"] for c in payload["candidates"]]
    assert "P018" not in ids


@pytest.mark.warehouse
def test_mcp_cold_start_dk_returns_zero_candidates(warehouse_path):
    stack = MCPStack(warehouse_path)
    payload = stack.call("partner_capacity", "list_candidates",
                         country="DK", service_type=None, industry=None,
                         as_of="2024-12-15")
    assert payload["candidates"] == []


# ── capacity holds: the concurrency guard ───────────────────────────────────

@pytest.mark.warehouse
def test_holds_consume_headroom_and_release_restores_it(warehouse_path):
    stack = MCPStack(warehouse_path)
    as_of = "2024-12-15"
    payload = stack.call("partner_capacity", "list_candidates",
                         country="SE", service_type=None, industry=None,
                         as_of=as_of)
    assert payload["candidates"], "need at least one SE candidate"
    target = payload["candidates"][0]
    pid, headroom = target["partner_id"], target["headroom"]

    # Fill every remaining slot with holds…
    for i in range(headroom):
        r = stack.call("partner_capacity", "hold_capacity",
                       partner_id=pid, lead_id=f"RL_hold_{i}",
                       ttl_hours=1, as_of=as_of)
        assert r["ok"], f"hold {i} should succeed"

    # …the next hold must fail (failure mode #9)…
    r = stack.call("partner_capacity", "hold_capacity",
                   partner_id=pid, lead_id="RL_hold_overflow",
                   ttl_hours=1, as_of=as_of)
    assert not r["ok"] and "no headroom" in r["error"]

    # …and the partner disappears from list_candidates entirely.
    payload2 = stack.call("partner_capacity", "list_candidates",
                          country="SE", service_type=None, industry=None,
                          as_of=as_of)
    assert pid not in [c["partner_id"] for c in payload2["candidates"]]

    # Releasing restores availability.
    for i in range(headroom):
        stack.call("partner_capacity", "release_capacity",
                   partner_id=pid, lead_id=f"RL_hold_{i}")
    payload3 = stack.call("partner_capacity", "list_candidates",
                          country="SE", service_type=None, industry=None,
                          as_of=as_of)
    assert pid in [c["partner_id"] for c in payload3["candidates"]]


@pytest.mark.warehouse
def test_hold_is_idempotent_per_lead(warehouse_path):
    stack = MCPStack(warehouse_path)
    a = stack.call("partner_capacity", "hold_capacity",
                   partner_id="P007", lead_id="RL_same", as_of="2024-12-15")
    b = stack.call("partner_capacity", "hold_capacity",
                   partner_id="P007", lead_id="RL_same", as_of="2024-12-15")
    assert a["ok"] and b["ok"] and b.get("idempotent")


# ── partner status: HITL reactivation flow (scenario #9) ────────────────────

@pytest.mark.warehouse
def test_set_partner_status_reactivation_with_audit(warehouse_path, warehouse_cleaner):
    warehouse_cleaner.snapshot_partner("P018")
    stack = MCPStack(warehouse_path)

    dormant = stack.call("partner_capacity", "list_dormant_partners",
                         country="US", service_type=None, industry=None)
    assert "P018" in [d["partner_id"] for d in dormant["dormant"]]

    r = stack.call("partner_capacity", "set_partner_status",
                   partner_id="P018", status="active",
                   reason="HITL reactivation test", changed_by="hitl")
    assert r["ok"] and r["prior_status"] == "inactive"
    assert r["event_id"].startswith("RT")

    # After reactivation P018 becomes eligible again…
    payload = stack.call("partner_capacity", "list_candidates",
                         country="US", service_type=None, industry=None,
                         as_of="2024-12-15")
    ids = [c["partner_id"] for c in payload["candidates"]]
    # (P018 has no capacity snapshots — the server defaults it to available)
    assert "P018" in ids


# ── crm writes: live warehouse persistence (Q4) ─────────────────────────────

@pytest.mark.warehouse
def test_crm_update_stage_persists_booked(warehouse_path, warehouse_cleaner):
    import duckdb
    from src.data.warehouse import find_referral_lead
    subject = find_referral_lead(warehouse_path, country="NO",
                                 status="pending", limit=1)[0]
    lead_id = subject["referral_lead_id"]
    warehouse_cleaner.snapshot_lead(lead_id)

    stack = MCPStack(warehouse_path)
    r = stack.call("crm", "update_stage", referral_lead_id=lead_id,
                   stage="booked", rationale="test booking",
                   partner_id="P004", as_of="2024-12-15")
    assert r["ok"] and r["new_status"] == "booked"

    with duckdb.connect(str(warehouse_path)) as con:
        status = con.execute(
            "SELECT referral_status FROM main.referral_leads "
            "WHERE referral_lead_id = ?", [lead_id]).fetchone()[0]
        events = con.execute(
            "SELECT event_type, agent_name FROM main.deal_events "
            "WHERE referral_lead_id = ? AND event_id LIKE 'RT%'",
            [lead_id]).fetchall()
    assert status == "booked"
    assert ("booked", "negotiation") in events


@pytest.mark.warehouse
def test_crm_attach_note_writes_note_event(warehouse_path, warehouse_cleaner):
    import duckdb
    from src.data.warehouse import find_referral_lead
    subject = find_referral_lead(warehouse_path, country="NO",
                                 status="pending", limit=1)[0]
    lead_id = subject["referral_lead_id"]

    stack = MCPStack(warehouse_path)
    r = stack.call("crm", "attach_note", referral_lead_id=lead_id,
                   note="manager override context", as_of="2024-12-15")
    assert r["ok"]
    with duckdb.connect(str(warehouse_path)) as con:
        row = con.execute(
            "SELECT event_type, rationale FROM main.deal_events "
            "WHERE event_id = ?", [r["event_id"]]).fetchone()
    assert row == ("note", "manager override context")


@pytest.mark.warehouse
def test_crm_rejects_unknown_lead(warehouse_path):
    stack = MCPStack(warehouse_path)
    r = stack.call("crm", "update_stage", referral_lead_id="RL_NOPE",
                   stage="booked", rationale="x")
    assert not r["ok"] and "unknown" in r["error"]
