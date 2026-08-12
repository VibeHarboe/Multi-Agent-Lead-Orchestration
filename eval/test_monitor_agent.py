"""Week 5 exit-gate tests — the Monitor Agent's three responsibilities +
re-injection, asserted against the seeded scenario contract (SCENARIOS.md).
"""

from __future__ import annotations

from datetime import date

import pytest

from src.agents.monitor import (TemplateNarrator, build_weekly_report,
                                escalate_breaches, post_digest, sweep)
from src.config import load_config

AS_OF = date(2024, 12, 31)


@pytest.fixture()
def config(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-placeholder")
    return load_config(require_api_key=False)


# ── scenario #4: sla-breach (NL) ────────────────────────────────────────────

@pytest.mark.warehouse
@pytest.mark.scenarios
def test_scenario_4_sla_breaches_detected(warehouse_path, config):
    report = sweep(warehouse_path, config, as_of=AS_OF)
    nl = [b for b in report.sla_breaches if b.country == "NL"]
    assert len(nl) == 3, f"expected the 3 seeded NL breaches, got {len(nl)}"
    for b in nl:
        assert b.days_since_booking > config.post_booking_close_sla_days
        assert b.breaching_partner_id, "breaching partner must be identified"
        assert b.severity == 3


# ── scenario #6: slow-churn P010 ────────────────────────────────────────────

@pytest.mark.warehouse
@pytest.mark.scenarios
def test_scenario_6_p010_churn_intervention_fires(warehouse_path, config):
    report = sweep(warehouse_path, config, as_of=AS_OF)
    kinds = {(i.kind, i.partner_id) for i in report.interventions}
    assert ("churn_risk", "P010") in kinds, (
        f"P010 churn intervention missing — got {kinds}")
    p010 = next(i for i in report.interventions
                if i.kind == "churn_risk" and i.partner_id == "P010")
    assert p010.sustained_days >= config.churn_risk_sustained_days
    assert p010.metric_now >= config.churn_risk_threshold


# ── scenario #7: unprofitable-but-friendly P016 ─────────────────────────────

@pytest.mark.warehouse
@pytest.mark.scenarios
def test_scenario_7_p016_roi_intervention_fires(warehouse_path, config):
    report = sweep(warehouse_path, config, as_of=AS_OF)
    kinds = {(i.kind, i.partner_id) for i in report.interventions}
    assert ("roi_decline", "P016") in kinds
    p016 = next(i for i in report.interventions
                if i.kind == "roi_decline" and i.partner_id == "P016")
    assert p016.metric_now < 0, "P016's ROI must be negative"
    # …and crucially P016 must NOT fire on churn_risk (its engagement is
    # healthy) — the ROI rule alone catches it:
    assert ("churn_risk", "P016") not in kinds


# ── scenario #8: slow-referring R009 ────────────────────────────────────────

@pytest.mark.warehouse
@pytest.mark.scenarios
def test_scenario_8_r009_volume_collapse_fires(warehouse_path, config):
    report = sweep(warehouse_path, config, as_of=AS_OF)
    r009 = [i for i in report.interventions
            if i.partner_id == "R009" and i.kind == "volume_collapse"]
    assert r009, "R009 volume collapse must fire"
    assert r009[0].partner_side == "referring"


# ── scenario #5: cross-market imbalance (Nov 2024) ──────────────────────────

@pytest.mark.warehouse
@pytest.mark.scenarios
def test_scenario_5_imbalance_de_saturated_dk_starved(warehouse_path, config):
    report = sweep(warehouse_path, config, as_of=date(2024, 11, 30))
    alerts = {(m.kind, m.country) for m in report.market_alerts}
    assert ("saturated", "DE") in alerts
    assert ("starved", "DK") in alerts


@pytest.mark.warehouse
def test_dk_cold_start_shows_as_saturation_in_dec(warehouse_path, config):
    """Scenario #1's market-level echo: DK at hard-cap all December."""
    report = sweep(warehouse_path, config, as_of=AS_OF)
    alerts = {(m.kind, m.country) for m in report.market_alerts}
    assert ("saturated", "DK") in alerts


# ── no false positives ──────────────────────────────────────────────────────

@pytest.mark.warehouse
def test_no_intervention_on_healthy_partners(warehouse_path, config):
    """Healthy partners (e.g. P011, P004) must not fire anything."""
    report = sweep(warehouse_path, config, as_of=AS_OF)
    flagged = {i.partner_id for i in report.interventions}
    for healthy in ("P004", "P011"):
        assert healthy not in flagged


# ── escalation writes ───────────────────────────────────────────────────────

@pytest.mark.warehouse
def test_escalation_writes_monitor_notes(warehouse_path, config,
                                         warehouse_cleaner):
    import duckdb
    from src.mcp_stack import MCPStack
    report = sweep(warehouse_path, config, as_of=AS_OF)
    stack = MCPStack(warehouse_path)
    results = escalate_breaches(report, stack)
    assert all(r["ok"] for r in results)
    with duckdb.connect(str(warehouse_path)) as con:
        rows = con.execute(
            "SELECT agent_name, event_type FROM main.deal_events "
            "WHERE event_id LIKE 'RT%'").fetchall()
    assert rows and all(r == ("monitor", "note") for r in rows)


# ── re-injection (scenario #4's resolution: §3.4a) ─────────────────────────

@pytest.mark.warehouse
@pytest.mark.scenarios
def test_reinjection_excludes_breaching_partner(warehouse_path, config,
                                                warehouse_cleaner, tmp_path):
    """A breached NL deal re-enters the graph and must book with a DIFFERENT
    partner than the one that breached it."""
    from src.data.warehouse import get_referral_lead_by_id
    from src.dry_run import lead_state_from_seed
    from src.graph import extract_interrupt, lead_out, open_graph, run_lead
    from src.mcp_stack import MCPStack

    report = sweep(warehouse_path, config, as_of=AS_OF)
    breach = next(b for b in report.sla_breaches if b.country == "NL")
    warehouse_cleaner.snapshot_lead(breach.referral_lead_id)

    stack = MCPStack(warehouse_path)          # auto-accept
    seed = get_referral_lead_by_id(warehouse_path, breach.referral_lead_id)
    lead = lead_state_from_seed(seed)
    with open_graph(stack, config, as_of=AS_OF,
                    checkpoint_dir=tmp_path / "ckpt") as graph:
        result = run_lead(graph, lead,
                          exclude_partner_ids=[breach.breaching_partner_id])
    assert extract_interrupt(result) is None
    final = lead_out(result)
    assert final.status == "booked"
    new_partner = final.negotiation_history[-1]["partner_id"]
    assert new_partner != breach.breaching_partner_id, (
        "re-injection must never re-route to the breaching partner")


# ── weekly report (d) ───────────────────────────────────────────────────────

@pytest.mark.warehouse
def test_weekly_report_renders_with_roi_and_trends(warehouse_path, config):
    report = build_weekly_report(warehouse_path, config,
                                 as_of=date(2024, 12, 30))
    assert len(report["partners"]) >= 10
    for row in report["partners"]:
        assert row["roi_trend"] in ("up", "flat", "down")
        assert row["churn_trend"] in ("worsening", "stable", "improving")
        assert row["narrative"]
        assert row["partner_id"] in row["narrative"]
    # the seeded troublemakers surface honestly
    p010 = next(p for p in report["partners"] if p["partner_id"] == "P010")
    assert p010["avg_roi_pct"] < 0
    assert "elevated churn-risk" in p010["narrative"]


@pytest.mark.warehouse
def test_weekly_narrative_compares_to_peers(warehouse_path, config):
    report = build_weekly_report(warehouse_path, config,
                                 as_of=date(2024, 12, 30))
    p010 = next(p for p in report["partners"] if p["partner_id"] == "P010")
    assert "below peer avg" in p010["narrative"]


@pytest.mark.warehouse
def test_digest_and_weekly_post_to_slack(warehouse_path, config):
    from src.agents.monitor import post_weekly_report
    from src.mcp_stack import MCPStack
    stack = MCPStack(warehouse_path)
    report = sweep(warehouse_path, config, as_of=AS_OF)
    post_digest(report, stack)
    weekly = build_weekly_report(warehouse_path, config,
                                 as_of=date(2024, 12, 30))
    post_weekly_report(weekly, stack)
    sent = stack.call("slack", "get_sent")
    channels = [m.get("channel") for m in sent["sent"] if m["kind"] == "channel"]
    assert "#nordledger-ops" in channels
    assert "#nordledger-weekly" in channels
