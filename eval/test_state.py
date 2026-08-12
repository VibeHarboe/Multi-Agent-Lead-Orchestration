"""Tests for the LeadState schema + build_lead_state helper.

All deterministic — no LLM, no warehouse. Runs on every push.
"""

from __future__ import annotations

from datetime import date

import pytest

from src.state import (
    EXTRACTED_FIELDS,
    LeadState,
    MatchedCandidate,
    Provenance,
    build_lead_state,
)


def test_extracted_fields_count_is_12():
    """The 'extracts ~12 structured fields' claim in ARCHITECTURE §3.1 must
    match what the state actually carries."""
    assert len(EXTRACTED_FIELDS) == 12


def test_leadstate_defaults_are_all_none():
    s = LeadState(
        referral_lead_id="RL00001",
        raw_text="test",
        received_at=date(2024, 12, 15),
    )
    for f in EXTRACTED_FIELDS:
        assert getattr(s, f) is None
    assert s.intake_confidence == 0.0
    assert s.status == "new"
    assert s.matched_candidates == []


def test_build_lead_state_from_clean_extraction():
    extraction = {
        "country": "DK",
        "industry": "retail",
        "service_type": "bookkeeping",
        "urgency": "medium",
        "deal_size_estimate": 25000,
        "company_name": "Andersen Retail ApS",
        "contact_name": "Lars Andersen",
        "contact_role": "CFO",
        "contact_email": "lars@andersen.dk",
        "timeline": "6 weeks",
        "tech_stack": "e-conomic",
        "budget_signal": "budget approved",
    }
    provenance = {f: {"confidence": 0.9, "source_span": "..."} for f in EXTRACTED_FIELDS}

    s = build_lead_state(
        referral_lead_id="RL00042",
        raw_text="...",
        received_at=date(2024, 12, 15),
        extraction=extraction,
        provenance=provenance,
    )
    assert s.country == "DK"
    assert s.service_type == "bookkeeping"
    assert s.intake_confidence == 0.9
    assert s.status == "classified"
    assert s.extracted_fields_count() == 12
    assert s.missing_essential_fields() == []
    assert not s.is_ambiguous()


def test_build_lead_state_from_ambiguous_extraction():
    """Empty extraction produces a low-confidence LeadState with all Nones."""
    extraction = {f: None for f in EXTRACTED_FIELDS}
    provenance = {f: {"confidence": 0.0, "source_span": None} for f in EXTRACTED_FIELDS}

    s = build_lead_state(
        referral_lead_id="RL00099",
        raw_text="hey — partnership?",
        received_at=date(2024, 12, 15),
        extraction=extraction,
        provenance=provenance,
    )
    assert s.intake_confidence == 0.0
    assert s.extracted_fields_count() == 0
    assert "country" in s.missing_essential_fields()
    assert "service_type" in s.missing_essential_fields()
    assert s.is_ambiguous()
    assert s.is_ambiguous(0.5)


def test_build_lead_state_partial_extraction():
    """A LeadState with country + service_type but nothing else is ambiguous
    but matchable — the graph will HITL on low overall confidence but the
    minimum fields for Matching are there."""
    extraction = {f: None for f in EXTRACTED_FIELDS}
    extraction["country"] = "SE"
    extraction["service_type"] = "audit"
    provenance = {f: {"confidence": 0.0, "source_span": None} for f in EXTRACTED_FIELDS}
    provenance["country"] = {"confidence": 0.9, "source_span": "Sweden"}
    provenance["service_type"] = {"confidence": 0.85, "source_span": "audit"}

    s = build_lead_state(
        referral_lead_id="RL00100",
        raw_text="need audit in Sweden",
        received_at=date(2024, 12, 15),
        extraction=extraction,
        provenance=provenance,
    )
    assert s.missing_essential_fields() == []
    # Only 2 of 12 fields have signal → overall confidence ~ (0.9+0.85)/12 = 0.146
    assert s.intake_confidence < 0.2
    assert s.is_ambiguous()


def test_leadstate_rejects_invalid_country():
    with pytest.raises(ValueError):
        LeadState(
            referral_lead_id="RL00001",
            raw_text="x",
            received_at=date(2024, 12, 15),
            country="XX",  # not in the Country Literal
        )


def test_leadstate_rejects_negative_deal_size():
    with pytest.raises(ValueError):
        LeadState(
            referral_lead_id="RL00001",
            raw_text="x",
            received_at=date(2024, 12, 15),
            deal_size_estimate=-1000,
        )


def test_provenance_confidence_range():
    with pytest.raises(ValueError):
        Provenance(confidence=1.5)


def test_matched_candidate_rank_positive():
    with pytest.raises(ValueError):
        MatchedCandidate(
            partner_id="P001",
            rank=0,
            close_rate_signal=80.0,
            partner_roi_pct=25.0,
            churn_risk_score=20.0,
            response_latency_p50_hours=4.0,
            composite_score=75.0,
            rationale="ok",
        )


def test_leadstate_field_confidence_lookup():
    s = build_lead_state(
        referral_lead_id="RL1",
        raw_text="x",
        received_at=date(2024, 12, 15),
        extraction={f: None for f in EXTRACTED_FIELDS},
        provenance={"country": {"confidence": 0.9, "source_span": "Denmark"}},
    )
    assert s.field_confidence("country") == 0.9
    assert s.field_confidence("industry") == 0.0
    assert s.field_confidence("nonexistent") == 0.0
