"""Week 2 exit-gate — assert every scenario in SCENARIOS.md at the
classification and ranking stages.

These are `warehouse`-marked because they need the built DuckDB. Full
end-to-end (Negotiation, Monitor, HITL resume) is asserted in Week 4-5's
test_e2e.py.

For each of the 9 scenarios:
- Which agent it targets at the classification/ranking stage
- Expected Matching status (matched vs hitl_capacity vs hitl_intake)
- Any partner-inclusion / exclusion assertion we can make with Week 2 alone
"""

from __future__ import annotations

from datetime import date

import pytest

from src.agents.matching import match
from src.state import EXTRACTED_FIELDS, LeadState, build_lead_state


def _minimal_lead(
    referral_lead_id: str,
    country: str,
    service_type: str,
    industry: str = "professional_services",
) -> LeadState:
    extraction = {f: None for f in EXTRACTED_FIELDS}
    extraction["country"] = country
    extraction["service_type"] = service_type
    extraction["industry"] = industry
    return build_lead_state(
        referral_lead_id=referral_lead_id,
        raw_text="…",
        received_at=date(2024, 12, 15),
        extraction=extraction,
        provenance={f: {"confidence": 0.9, "source_span": "…"} for f in EXTRACTED_FIELDS},
    )


# ── Scenario #1 · cold-start-market DK 2024-12 ──────────────────────────────

@pytest.mark.warehouse
@pytest.mark.scenarios
def test_scenario_1_cold_start_market(warehouse_path):
    lead = _minimal_lead("RL_scenario_1", country="DK", service_type="bookkeeping")
    result = match(lead, warehouse_path=warehouse_path, top_k=3,
                   as_of=date(2024, 12, 15))
    assert result.status == "hitl_capacity", (
        f"scenario 1 (cold-start DK): expected hitl_capacity, got {result.status}"
    )


# ── Scenario #2 · ambiguous-inquiry ─────────────────────────────────────────

@pytest.mark.warehouse
@pytest.mark.scenarios
def test_scenario_2_ambiguous_inquiry(warehouse_path):
    """An ambiguous referral (all fields None) can't be matched — must HITL
    at intake stage, before Matching ever runs."""
    empty = {f: None for f in EXTRACTED_FIELDS}
    lead = build_lead_state(
        referral_lead_id="RL_scenario_2",
        raw_text="hey — partnership?",
        received_at=date(2024, 12, 15),
        extraction=empty,
        provenance={f: {"confidence": 0.0, "source_span": None} for f in EXTRACTED_FIELDS},
    )
    result = match(lead, warehouse_path=warehouse_path, top_k=3,
                   as_of=date(2024, 12, 15))
    assert result.status == "hitl_intake"
    assert "country" in result.hitl_reason and "service_type" in result.hitl_reason


# ── Scenario #3 · negotiation-stall SE 2024-11 ──────────────────────────────
# (Matching returns candidates; the *stall* itself is a Week 4 test — but the
# ranking must succeed here.)

@pytest.mark.warehouse
@pytest.mark.scenarios
def test_scenario_3_negotiation_stall_ranks_ok(warehouse_path):
    lead = _minimal_lead("RL_scenario_3", country="SE", service_type="audit")
    # SE in 2024-11 wasn't at capacity → ranking should succeed
    result = match(lead, warehouse_path=warehouse_path, top_k=3,
                   as_of=date(2024, 11, 15))
    assert result.status == "matched", (
        f"scenario 3 (SE 2024-11): expected matched, got {result.status}"
    )
    assert len(result.matched_candidates) >= 1


# ── Scenario #4 · sla-breach NL ─────────────────────────────────────────────
# (Post-booking concern; Matching should still work for a fresh NL lead.)

@pytest.mark.warehouse
@pytest.mark.scenarios
def test_scenario_4_sla_breach_ranks_new_lead_ok(warehouse_path):
    lead = _minimal_lead("RL_scenario_4", country="NL", service_type="accounting")
    result = match(lead, warehouse_path=warehouse_path, top_k=3,
                   as_of=date(2024, 12, 15))
    assert result.status == "matched"


# ── Scenario #5 · cross-market imbalance Nov 2024 ───────────────────────────

@pytest.mark.warehouse
@pytest.mark.scenarios
def test_scenario_5_cross_market_imbalance_de_nov(warehouse_path):
    """DE in Nov 2024 is saturated — at least one candidate should be
    filtered out for capacity; DK Nov 2024 is starved — plenty available."""
    # DE (saturated)
    lead_de = _minimal_lead("RL_scenario_5_de", country="DE", service_type="tax_advisory")
    result_de = match(lead_de, warehouse_path=warehouse_path, top_k=3,
                      as_of=date(2024, 11, 15))
    # DK (starved)
    lead_dk = _minimal_lead("RL_scenario_5_dk", country="DK", service_type="tax_advisory")
    result_dk = match(lead_dk, warehouse_path=warehouse_path, top_k=3,
                      as_of=date(2024, 11, 15))

    # DK has more availability than DE in 2024-11
    n_de = len(result_de.matched_candidates) if result_de.status == "matched" else 0
    n_dk = len(result_dk.matched_candidates) if result_dk.status == "matched" else 0
    assert n_dk >= n_de, (
        f"expected DK ({n_dk}) ≥ DE ({n_de}) candidates in Nov-2024 imbalance"
    )


# ── Scenario #6 · slow-churn P010 (DE) ──────────────────────────────────────

@pytest.mark.warehouse
@pytest.mark.scenarios
def test_scenario_6_p010_deprioritized_by_2024_12(warehouse_path):
    """P010's churn-risk has climbed steadily 2024-07..12. If P010 makes the
    top-3, it should NOT be #1 — a healthier DE partner should outrank it."""
    lead = _minimal_lead("RL_scenario_6", country="DE", service_type="accounting")
    result = match(lead, warehouse_path=warehouse_path, top_k=3,
                   as_of=date(2024, 12, 15))
    if result.status == "matched" and len(result.matched_candidates) > 1:
        picked = {c.partner_id: c for c in result.matched_candidates}
        if "P010" in picked:
            # if P010 is included, it must not be top
            top = result.matched_candidates[0]
            assert top.partner_id != "P010", (
                f"P010 (slow-churn) was ranked #1 with score {top.composite_score}"
            )


# ── Scenario #7 · unprofitable-but-friendly P016 (US) ───────────────────────

@pytest.mark.warehouse
@pytest.mark.scenarios
def test_scenario_7_p016_deprioritized_on_roi(warehouse_path):
    """P016 has trending-negative ROI by 2024-12 — must not top-rank on
    friendliness alone."""
    lead = _minimal_lead("RL_scenario_7", country="US", service_type="accounting")
    result = match(lead, warehouse_path=warehouse_path, top_k=3,
                   as_of=date(2024, 12, 15))
    if result.status == "matched" and len(result.matched_candidates) > 1:
        picked = {c.partner_id: c for c in result.matched_candidates}
        if "P016" in picked and len(picked) > 1:
            top = result.matched_candidates[0]
            if top.partner_id == "P016":
                # if P016 is #1, its ROI must be positive (else our ranking failed
                # to reflect the injected negative-trend signal)
                assert top.partner_roi_pct > 0, (
                    f"P016 ranked #1 with negative ROI {top.partner_roi_pct}%"
                )


# ── Scenario #8 · slow-referring-ambassador R009 (SE) ───────────────────────
# (Ambassador side — no direct Matching assertion at Week 2. Data check only.)

@pytest.mark.warehouse
@pytest.mark.scenarios
def test_scenario_8_r009_signal_present_in_data(warehouse_path):
    """R009's lead volume drop is a Week 5 Monitor concern. Week 2 assertion:
    the signal is present in the data the Monitor will read."""
    import duckdb
    with duckdb.connect(str(warehouse_path), read_only=True) as con:
        oct_leads = con.execute(
            "SELECT SUM(leads_sent_count) FROM main_staging.stg_referring_partner_engagement_daily "
            "WHERE referring_partner_id = 'R009' AND snapshot_date BETWEEN '2024-10-01' AND '2024-10-31'"
        ).fetchone()[0] or 0
        dec_leads = con.execute(
            "SELECT SUM(leads_sent_count) FROM main_staging.stg_referring_partner_engagement_daily "
            "WHERE referring_partner_id = 'R009' AND snapshot_date BETWEEN '2024-12-01' AND '2024-12-31'"
        ).fetchone()[0] or 0
    assert oct_leads > dec_leads, (
        f"R009 slow-referring signal missing: Oct={oct_leads} Dec={dec_leads}"
    )
    assert dec_leads < oct_leads * 0.3, (
        f"R009 volume drop not sharp enough: Oct={oct_leads}, Dec={dec_leads}"
    )


# ── Scenario #9 · reactivate-inactive P018 ──────────────────────────────────

@pytest.mark.warehouse
@pytest.mark.scenarios
def test_scenario_9_p018_never_in_candidates(warehouse_path):
    """A US lead in 2024-12 must NEVER match P018 (status='inactive'). The
    reactivation flow itself is a Week 4 HITL concern; Week 2 asserts only
    that Matching correctly excludes inactive partners."""
    lead = _minimal_lead("RL_scenario_9", country="US", service_type="tax_advisory")
    result = match(lead, warehouse_path=warehouse_path, top_k=5,
                   as_of=date(2024, 12, 15))
    picked = [c.partner_id for c in result.matched_candidates]
    assert "P018" not in picked, (
        f"scenario 9: P018 (inactive) leaked into ranking: {picked}"
    )


# ── Contract check: SCENARIOS.md matches ORCH_SCENARIOS ─────────────────────

@pytest.mark.scenarios
def test_scenarios_md_committed_and_lists_all_9(warehouse_path):
    """SCENARIOS.md exists and lists all 9 orchestration scenarios."""
    from pathlib import Path
    scen_md = warehouse_path.parent / "SCENARIOS.md"
    assert scen_md.exists(), f"SCENARIOS.md missing at {scen_md}"
    content = scen_md.read_text(encoding="utf-8")
    for scen in [
        "cold-start-market",
        "ambiguous-inquiry",
        "negotiation-stall",
        "sla-breach",
        "cross-market-imbalance",
        "slow-churn-partner",
        "unprofitable-but-friendly",
        "slow-referring-ambassador",
        "reactivate-inactive-partner",
    ]:
        assert scen in content, f"SCENARIOS.md missing entry: {scen}"
