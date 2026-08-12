"""Week 6 exit-gate tests — the decision dashboard + console resolution flow.

The gate: "a paused lead resolves through the console with the dashboard
rendering real partner/market/financial metrics". The console's UI is a thin
Streamlit skin over src/console_data.py + src/graph.py — so the gate is
asserted here UI-free, plus import smokes for both Streamlit apps.
"""

from __future__ import annotations

from datetime import date

import pytest

from src.config import load_config
from src.console_data import (build_dashboard, financial_context,
                              lead_vs_book, list_paused_leads, market_health,
                              partner_health)
from src.data.warehouse import find_referral_lead
from src.dry_run import lead_state_from_seed
from src.graph import extract_interrupt, lead_out, open_graph, resume_lead, run_lead
from src.mcp_stack import MCPStack

AS_OF = date(2024, 12, 31)


@pytest.fixture()
def config(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-placeholder")
    return load_config(require_api_key=False)


# ── dashboard sections render real metrics ──────────────────────────────────

@pytest.mark.warehouse
def test_partner_health_renders_real_metrics(warehouse_path):
    h = partner_health(warehouse_path, "P010", as_of=AS_OF)
    assert h["available"]
    assert h["avg_roi_pct"] < 0                    # the slow-churn partner
    assert h["max_churn_risk"] >= 65
    assert h["roi_trend"] == "down"
    assert h["avg_p50_hours"] > 24                 # degraded latency
    healthy = partner_health(warehouse_path, "P011", as_of=AS_OF)
    assert healthy["avg_roi_pct"] > 0
    assert healthy["max_churn_risk"] < 65


@pytest.mark.warehouse
def test_market_health_dk_shows_the_crunch(warehouse_path):
    m = market_health(warehouse_path, "DK", as_of=date(2024, 12, 15))
    assert m["partners_under_cap"] == 0            # scenario #1
    assert m["avg_util_7d_pct"] > 90


@pytest.mark.warehouse
def test_financial_context_reads_shared_core(warehouse_path):
    f = financial_context(warehouse_path, "DE", as_of=AS_OF)
    assert f["active_mrr"] > 0
    assert f["churned_mrr"] > 0
    assert 0 < f["churn_rate_pct"] < 100
    assert 0 < f["overdue_rate_pct"] < 100


@pytest.mark.warehouse
def test_lead_vs_book_compares(warehouse_path):
    seed = find_referral_lead(warehouse_path, country="SE",
                              status="pending", limit=1)[0]
    lb = lead_vs_book(warehouse_path,
                      referral_lead_id=seed["referral_lead_id"])
    assert lb["available"]
    assert lb["book_avg"] > 0 and lb["market_avg"] > 0
    assert lb["vs_book_pct"] is not None


# ── THE exit gate: pause → dashboard → decision → resolved ─────────────────

@pytest.mark.warehouse
@pytest.mark.scenarios
def test_paused_lead_resolves_through_console_flow(warehouse_path,
                                                   warehouse_cleaner,
                                                   config, tmp_path):
    """End-to-end through exactly the functions the console calls:
    1. a lead pauses at hitl_negotiate
    2. list_paused_leads finds it (from the checkpoint DB)
    3. build_dashboard renders real partner/market/financial metrics
    4. the manager's override decision resumes the graph → booked"""
    ckpt = tmp_path / "ckpt"
    lead = lead_state_from_seed(find_referral_lead(
        warehouse_path, country="SE", status="pending", limit=1)[0])
    warehouse_cleaner.snapshot_lead(lead.referral_lead_id)

    # 1 — pause it
    stack_a = MCPStack(warehouse_path)
    stack_a.prime_slack_replies(["decline"] * 3)
    with open_graph(stack_a, config, as_of=AS_OF,
                    checkpoint_dir=ckpt) as graph:
        result = run_lead(graph, lead)
    assert extract_interrupt(result)["destination"] == "hitl_negotiate"

    # 2 — the console's sidebar listing finds it
    stack_b = MCPStack(warehouse_path)
    paused = list_paused_leads(stack_b, config, as_of=AS_OF,
                               checkpoint_dir=ckpt)
    assert [p["referral_lead_id"] for p in paused] == [lead.referral_lead_id]
    payload = paused[0]

    # 3 — the dashboard renders REAL metrics for the candidates considered
    dashboard = build_dashboard(warehouse_path, payload, as_of=AS_OF)
    assert dashboard["destination"] == "hitl_negotiate"
    assert dashboard["partners"], "candidate partner cards must render"
    for h in dashboard["partners"].values():
        assert h["available"]
        assert isinstance(h["avg_roi_pct"], float)
        assert isinstance(h["max_churn_risk"], float)
    assert dashboard["market"]["country"] == "SE"
    assert dashboard["market"]["partners_active_total"] > 0
    assert dashboard["financial"]["active_mrr"] > 0
    assert dashboard["lead_vs_book"]["available"]

    # 4 — the manager's decision resumes to booked
    stack_c = MCPStack(warehouse_path)          # auto-accept
    with open_graph(stack_c, config, as_of=AS_OF,
                    checkpoint_dir=ckpt) as graph:
        result2 = resume_lead(graph, lead.referral_lead_id,
                              {"action": "override", "partner_id": "P008",
                               "note": "console e2e test"})
    assert extract_interrupt(result2) is None
    assert lead_out(result2).status == "booked"

    # …and the paused list is empty again
    stack_d = MCPStack(warehouse_path)
    assert list_paused_leads(stack_d, config, as_of=AS_OF,
                             checkpoint_dir=ckpt) == []


# ── Q18: the vendored BI Agent reads THIS warehouse ────────────────────────

@pytest.mark.warehouse
def test_q18_bi_agent_catalog_loads_from_this_warehouse(warehouse_path):
    from vendor.bi_agent.catalog import load_catalog
    cat = load_catalog(warehouse_path.parent / "models" / "semantic_layer")
    assert len(cat.metrics) == 51
    assert "churn_rate" in cat.metrics
    assert "partner_roi_pct" in cat.metrics


# ── app import smokes ───────────────────────────────────────────────────────

def test_console_app_imports():
    import app.console  # noqa: F401


def test_demo_app_imports():
    import app.streamlit_demo as demo
    assert len(demo.SCENARIOS) == 4
