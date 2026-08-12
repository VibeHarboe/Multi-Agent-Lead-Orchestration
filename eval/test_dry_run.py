"""Week 3 exit-gate tests — the dry-run walks the whole graph against MCP
mocks without touching the LLM.

The no-LLM guarantee is asserted structurally: importing src.dry_run and
src.cli must not import anthropic, and a full dry-run must complete with the
ANTHROPIC_API_KEY absent from the environment.
"""

from __future__ import annotations

import sys
from datetime import date

import pytest

from src.data.warehouse import find_referral_lead
from src.dry_run import dry_run, lead_state_from_seed


# ── the no-LLM guarantee ────────────────────────────────────────────────────

def test_dry_run_path_never_imports_anthropic():
    """src.dry_run, src.cli, src.mcp_stack and the MCP servers must be
    importable without pulling in the anthropic SDK."""
    for mod in list(sys.modules):
        if mod.startswith("anthropic"):
            del sys.modules[mod]
    import importlib
    import src.cli
    import src.dry_run
    import src.mcp_stack
    importlib.reload(src.mcp_stack)
    importlib.reload(src.dry_run)
    importlib.reload(src.cli)
    assert not any(m.startswith("anthropic") for m in sys.modules), (
        "anthropic was imported on the dry-run path"
    )


# ── seed-fixture intake ─────────────────────────────────────────────────────

@pytest.mark.warehouse
def test_lead_state_from_seed_maps_fields(warehouse_path):
    seed = find_referral_lead(warehouse_path, country="SE",
                              status="pending", limit=1)[0]
    lead = lead_state_from_seed(seed)
    assert lead.referral_lead_id == seed["referral_lead_id"]
    assert lead.country == "SE"
    assert lead.status == "classified"
    assert lead.field_confidence("country") == 0.95


@pytest.mark.warehouse
def test_lead_state_from_ambiguous_seed_lacks_essentials(warehouse_path):
    """Ambiguous seeds (industry/service NULL — scenario #2) must produce a
    LeadState missing essentials, which matching then routes to HITL."""
    import duckdb
    with duckdb.connect(str(warehouse_path)) as con:
        row = con.execute(
            "SELECT * FROM main_staging.stg_referral_leads "
            "WHERE industry IS NULL AND service_type IS NULL LIMIT 1"
        ).fetchone()
        cols = [d[0] for d in con.description]
    seed = dict(zip(cols, row))
    lead = lead_state_from_seed(seed)
    assert "service_type" in lead.missing_essential_fields()


# ── the exit gate: full graph walks, zero LLM ───────────────────────────────

@pytest.mark.warehouse
@pytest.mark.scenarios
def test_dry_run_happy_path_books(warehouse_path, warehouse_cleaner, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    subject = find_referral_lead(warehouse_path, country="SE",
                                 status="pending", limit=1)[0]
    warehouse_cleaner.snapshot_lead(subject["referral_lead_id"])

    result = dry_run(subject["referral_lead_id"],
                     warehouse_path=warehouse_path,
                     top_k=3, as_of=date(2024, 12, 31))
    assert result.outcome == "booked"
    assert result.lead.negotiation_history[0]["outcome"] == "accept"
    assert result.mcp_calls >= 6          # match + hold + dm + reply + upsert + stage
    assert any("0 LLM calls" in line for line in result.walk_log)


@pytest.mark.warehouse
@pytest.mark.scenarios
def test_dry_run_cold_start_dk_pauses_at_capacity(warehouse_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    subject = find_referral_lead(warehouse_path, country="DK",
                                 status="pending", limit=1)[0]
    result = dry_run(subject["referral_lead_id"],
                     warehouse_path=warehouse_path,
                     top_k=3, as_of=date(2024, 12, 15))
    assert result.outcome == "hitl_capacity"
    assert "DK" in result.lead.hitl_reason


@pytest.mark.warehouse
def test_dry_run_scripted_decline_routes_to_hitl_negotiate(warehouse_path,
                                                           warehouse_cleaner,
                                                           monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    subject = find_referral_lead(warehouse_path, country="SE",
                                 status="pending", limit=2)[1]
    warehouse_cleaner.snapshot_lead(subject["referral_lead_id"])

    result = dry_run(subject["referral_lead_id"],
                     warehouse_path=warehouse_path,
                     top_k=3, as_of=date(2024, 12, 31),
                     scripted_replies=["decline"])
    assert result.outcome == "hitl_negotiate"
    assert result.lead.negotiation_history[0]["outcome"] == "decline"
    # capacity must have been released again
    released = [c for c in result.walk_log if "release_capacity" in c]
    assert released, "decline must release the held capacity slot"


@pytest.mark.warehouse
def test_dry_run_no_reply_times_out_and_releases(warehouse_path,
                                                 warehouse_cleaner,
                                                 monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    subject = find_referral_lead(warehouse_path, country="NL",
                                 status="pending", limit=1)[0]
    warehouse_cleaner.snapshot_lead(subject["referral_lead_id"])

    result = dry_run(subject["referral_lead_id"],
                     warehouse_path=warehouse_path,
                     top_k=3, as_of=date(2024, 12, 31),
                     scripted_replies=["no_reply"])
    assert result.outcome == "hitl_negotiate"
    assert result.lead.negotiation_history[0]["outcome"] == "no_reply"
    assert result.lead.negotiation_history[0]["elapsed_hours"] == 24.0


@pytest.mark.warehouse
def test_dry_run_writes_live_deal_events(warehouse_path, warehouse_cleaner,
                                         monkeypatch):
    """Q4 decision verified end-to-end: a dry-run booking must be visible in
    the warehouse immediately (live deal_events write)."""
    import duckdb
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    subject = find_referral_lead(warehouse_path, country="US",
                                 status="pending", limit=1)[0]
    lead_id = subject["referral_lead_id"]
    warehouse_cleaner.snapshot_lead(lead_id)

    result = dry_run(lead_id, warehouse_path=warehouse_path,
                     top_k=3, as_of=date(2024, 12, 31))
    assert result.outcome == "booked"
    with duckdb.connect(str(warehouse_path)) as con:
        events = con.execute(
            "SELECT event_type FROM main.deal_events "
            "WHERE referral_lead_id = ? AND event_id LIKE 'RT%'",
            [lead_id]).fetchall()
        status = con.execute(
            "SELECT referral_status FROM main.referral_leads "
            "WHERE referral_lead_id = ?", [lead_id]).fetchone()[0]
    assert ("booked",) in events
    assert status == "booked"
