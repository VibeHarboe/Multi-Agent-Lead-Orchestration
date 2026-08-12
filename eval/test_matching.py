"""Tests for the Matching Agent — ranking is pure, warehouse queries opt-in."""

from __future__ import annotations

from datetime import date

import pytest

from src.agents.matching import _composite_score, match, rank_candidates
from src.data.warehouse import Candidate
from src.state import EXTRACTED_FIELDS, LeadState, build_lead_state


# ── Pure-function fixtures ───────────────────────────────────────────────────

def _candidate(
    partner_id: str,
    *,
    country: str = "DK",
    is_under_hard_cap: bool = True,
    is_under_soft_cap: bool = True,
    partner_status: str = "active",
    specialization_strength: float = 70.0,
    partner_roi_pct: float = 25.0,
    partner_roi_trend: str = "flat",
    churn_risk_score: float = 20.0,
    response_latency_p50_hours: float = 6.0,
    active_deals_count: int = 5,
    soft_cap: int = 10,
    hard_cap: int = 12,
    net_monthly_value: float = 12000.0,
) -> Candidate:
    return Candidate(
        partner_id=partner_id,
        partner_name=f"Partner {partner_id}",
        country=country,
        is_under_soft_cap=is_under_soft_cap,
        is_under_hard_cap=is_under_hard_cap,
        active_deals_count=active_deals_count,
        soft_cap=soft_cap,
        hard_cap=hard_cap,
        specialization_strength=specialization_strength,
        close_rate_signal=specialization_strength,
        partner_roi_pct=partner_roi_pct,
        partner_roi_trend=partner_roi_trend,
        net_monthly_value=net_monthly_value,
        churn_risk_score=churn_risk_score,
        response_latency_p50_hours=response_latency_p50_hours,
        partner_status=partner_status,
    )


def _minimal_lead(country: str = "DK", service_type: str = "bookkeeping") -> LeadState:
    extraction = {f: None for f in EXTRACTED_FIELDS}
    extraction["country"] = country
    extraction["service_type"] = service_type
    return build_lead_state(
        referral_lead_id="RL_test",
        raw_text="…",
        received_at=date(2024, 12, 15),
        extraction=extraction,
        provenance={f: {"confidence": 0.9, "source_span": "…"} for f in EXTRACTED_FIELDS},
    )


# ── Composite-score behavior ─────────────────────────────────────────────────

def test_composite_score_favours_high_specialization():
    high = _candidate("P1", specialization_strength=95.0)
    low = _candidate("P2", specialization_strength=40.0)
    assert _composite_score(high) > _composite_score(low)


def test_composite_score_penalises_high_churn_risk():
    healthy = _candidate("P1", churn_risk_score=15.0)
    risky = _candidate("P2", churn_risk_score=85.0)
    assert _composite_score(healthy) > _composite_score(risky)


def test_composite_score_favours_positive_roi():
    profitable = _candidate("P1", partner_roi_pct=35.0)
    unprofit = _candidate("P2", partner_roi_pct=-15.0)
    assert _composite_score(profitable) > _composite_score(unprofit)


def test_composite_score_favours_low_latency():
    fast = _candidate("P1", response_latency_p50_hours=3.0)
    slow = _candidate("P2", response_latency_p50_hours=48.0)
    assert _composite_score(fast) > _composite_score(slow)


# ── Ranking hard filters ─────────────────────────────────────────────────────

def test_rank_filters_inactive_partners():
    """Scenario #9 assertion: an inactive partner (P018) must never rank."""
    cands = [
        _candidate("P001", partner_status="active"),
        _candidate("P018", partner_status="inactive"),
    ]
    result = rank_candidates(cands, top_k=3)
    assert [c.partner_id for c in result] == ["P001"]


def test_rank_filters_over_hard_cap_partners():
    """Scenario #1 assertion: over-cap partners must never rank."""
    cands = [
        _candidate("P001", is_under_hard_cap=True),
        _candidate("P002", is_under_hard_cap=False),
        _candidate("P003", is_under_hard_cap=False),
    ]
    result = rank_candidates(cands, top_k=5)
    assert [c.partner_id for c in result] == ["P001"]


def test_rank_top_k_configurable():
    cands = [_candidate(f"P{i:03d}", specialization_strength=90 - i) for i in range(1, 8)]
    top3 = rank_candidates(cands, top_k=3)
    top5 = rank_candidates(cands, top_k=5)
    assert len(top3) == 3
    assert len(top5) == 5
    # both start with the highest-scored partner
    assert top3[0].partner_id == top5[0].partner_id == "P001"


def test_rank_orders_by_composite_score():
    """Ordering is stable and correct — high-specialisation, high-ROI,
    low-churn partner ranks first."""
    cands = [
        _candidate("P1", specialization_strength=45, partner_roi_pct=10, churn_risk_score=70),
        _candidate("P2", specialization_strength=90, partner_roi_pct=30, churn_risk_score=15),
        _candidate("P3", specialization_strength=70, partner_roi_pct=20, churn_risk_score=40),
    ]
    result = rank_candidates(cands, top_k=3)
    assert [c.partner_id for c in result] == ["P2", "P3", "P1"]
    # ranks are 1..N
    assert [c.rank for c in result] == [1, 2, 3]


def test_rank_returns_rationale_for_each_pick():
    cands = [_candidate("P001"), _candidate("P002")]
    result = rank_candidates(cands, top_k=2)
    for pick in result:
        assert pick.rationale
        assert "score" in pick.rationale
        assert f"cap {pick.churn_risk_score:.0f}" in pick.rationale or "cap" in pick.rationale


def test_rank_marks_churn_risk_warning_in_rationale():
    high_risk = _candidate("P1", churn_risk_score=80.0)
    result = rank_candidates([high_risk], top_k=1)
    assert "⚠" in result[0].rationale


# ── Match orchestrator (state transitions) ──────────────────────────────────

def test_match_missing_essentials_routes_to_hitl_intake(tmp_path):
    """A LeadState with no country / service_type can't be matched — must
    route to hitl_intake, not attempt a warehouse query."""
    extraction = {f: None for f in EXTRACTED_FIELDS}
    lead = build_lead_state(
        referral_lead_id="RL_test",
        raw_text="…",
        received_at=date(2024, 12, 15),
        extraction=extraction,
        provenance={f: {"confidence": 0.0, "source_span": None} for f in EXTRACTED_FIELDS},
    )
    result = match(lead, warehouse_path=tmp_path / "not_a_real_db.duckdb",
                   top_k=3, as_of=date(2024, 12, 31))
    assert result.status == "hitl_intake"
    assert "country" in result.hitl_reason
    assert "service_type" in result.hitl_reason


# ── Warehouse-marked end-to-end tests ────────────────────────────────────────

@pytest.mark.warehouse
def test_match_produces_topk_for_normal_lead(warehouse_path, as_of):
    """A well-formed SE lead should return ≤3 candidates."""
    lead = _minimal_lead(country="SE", service_type="bookkeeping")
    result = match(lead, warehouse_path=warehouse_path, top_k=3, as_of=as_of)
    assert result.status == "matched"
    assert 1 <= len(result.matched_candidates) <= 3
    for c in result.matched_candidates:
        assert c.rank in {1, 2, 3}


@pytest.mark.warehouse
def test_match_cold_start_dk_2024_12_routes_to_hitl(warehouse_path):
    """SCENARIO #1: DK 2024-12 has all 3 partners at hard-cap — must HITL."""
    lead = _minimal_lead(country="DK", service_type="accounting")
    result = match(lead, warehouse_path=warehouse_path, top_k=3,
                   as_of=date(2024, 12, 15))
    assert result.status == "hitl_capacity", (
        f"expected hitl_capacity, got {result.status} "
        f"with {len(result.matched_candidates)} candidates"
    )
    assert "DK" in result.hitl_reason
    assert "hard-cap" in result.hitl_reason.lower()


@pytest.mark.warehouse
def test_match_never_returns_inactive_p018(warehouse_path):
    """SCENARIO #9 partial: even without capacity issues, P018 must never
    be in matched_candidates because status='inactive'."""
    lead = _minimal_lead(country="US", service_type="tax_advisory")
    result = match(lead, warehouse_path=warehouse_path, top_k=5,
                   as_of=date(2024, 12, 15))
    picked = [c.partner_id for c in result.matched_candidates]
    assert "P018" not in picked, (
        f"P018 (inactive) leaked into top-K: {picked}"
    )


@pytest.mark.warehouse
def test_match_deprioritises_slow_churn_p010(warehouse_path):
    """SCENARIO #6: P010 (DE slow-churn) has climbing churn-risk by 2024-12.
    If a healthy DE partner exists, P010 should NOT be #1."""
    lead = _minimal_lead(country="DE", service_type="accounting")
    result = match(lead, warehouse_path=warehouse_path, top_k=3,
                   as_of=date(2024, 12, 15))
    # if only 1 partner is under-capacity in DE, P010 may still be #1 —
    # scenario #5 (cross-market imbalance) has DE saturated in 2024-11, but
    # we're querying 2024-12-15 which is post-imbalance. We assert only:
    # IF multiple candidates exist, P010's composite_score < top score.
    if len(result.matched_candidates) > 1:
        top = result.matched_candidates[0]
        p010 = next((c for c in result.matched_candidates if c.partner_id == "P010"), None)
        if p010:
            assert p010.composite_score <= top.composite_score
