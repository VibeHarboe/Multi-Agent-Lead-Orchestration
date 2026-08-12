"""Negotiation Agent tests — the 3×3 bounded loop with scripted replies.

All warehouse-marked (the loop calls hold/release against real capacity).
No LLM anywhere: TemplateDrafter + pre-classified outcomes from slack_mock.
"""

from __future__ import annotations

from datetime import date

import pytest

from src.agents.matching import match_mcp
from src.agents.negotiation import TemplateDrafter, model_tier_for_round, negotiate
from src.config import load_config
from src.mcp_stack import MCPStack
from src.state import EXTRACTED_FIELDS, build_lead_state

AS_OF = date(2024, 12, 31)


def _matched_lead(stack, country="SE", service_type="bookkeeping"):
    extraction = {f: None for f in EXTRACTED_FIELDS}
    extraction.update(country=country, service_type=service_type,
                      industry="professional_services")
    lead = build_lead_state(
        referral_lead_id="RL_neg_test", raw_text="…",
        received_at=AS_OF, extraction=extraction,
        provenance={f: {"confidence": 0.9, "source_span": "…"}
                    for f in EXTRACTED_FIELDS})
    return match_mcp(lead, stack=stack, top_k=3, as_of=AS_OF)


@pytest.fixture()
def config(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-placeholder")
    return load_config(require_api_key=False)


# NB: negotiate books via crm update_stage — RL_neg_test isn't a seeded lead,
# so the crm write returns ok=False (unknown lead) but the loop's own state
# transition still applies. For persistence-asserting tests we use seeded
# leads (test_graph.py); here we assert pure loop semantics.


@pytest.mark.warehouse
def test_accept_round_1_books(warehouse_path, config):
    stack = MCPStack(warehouse_path)          # auto-reply: accept
    lead = _matched_lead(stack)
    result = negotiate(lead, stack=stack, config=config, as_of=AS_OF)
    assert result.status == "booked"
    assert len(result.negotiation_history) == 1
    entry = result.negotiation_history[0]
    assert entry["outcome"] == "accept"
    assert entry["round"] == 1
    assert entry["model_tier"] == "sonnet"    # Q16: round 1 stays on Sonnet


@pytest.mark.warehouse
def test_counter_escalates_to_opus_from_round_2(warehouse_path, config):
    """Q16 assertion without any LLM: the recorded tier flips at round 2."""
    stack = MCPStack(warehouse_path)
    stack.prime_slack_replies(["counter", "counter", "accept"])
    lead = _matched_lead(stack)
    result = negotiate(lead, stack=stack, config=config, as_of=AS_OF)
    assert result.status == "booked"
    tiers = [h["model_tier"] for h in result.negotiation_history]
    assert tiers == ["sonnet", "opus", "opus"]


@pytest.mark.warehouse
def test_no_reply_drops_candidate_immediately(warehouse_path, config):
    """Dynamic in-loop re-routing: 24h silence → next candidate, no retry
    with the silent partner."""
    stack = MCPStack(warehouse_path)
    stack.prime_slack_replies(["no_reply", "accept"])
    lead = _matched_lead(stack)
    result = negotiate(lead, stack=stack, config=config, as_of=AS_OF)
    assert result.status == "booked"
    h = result.negotiation_history
    assert h[0]["outcome"] == "no_reply"
    assert h[0]["elapsed_hours"] == float(config.partner_response_sla_hours)
    assert h[1]["outcome"] == "accept"
    assert h[0]["partner_id"] != h[1]["partner_id"], (
        "the silent partner must be dropped, not retried")


@pytest.mark.warehouse
def test_decline_all_candidates_exhausts_to_hitl(warehouse_path, config):
    stack = MCPStack(warehouse_path)
    stack.prime_slack_replies(["decline", "decline", "decline"])
    lead = _matched_lead(stack)
    result = negotiate(lead, stack=stack, config=config, as_of=AS_OF)
    assert result.status == "hitl_negotiate"
    assert len(result.negotiation_history) == 3
    partners = [h["partner_id"] for h in result.negotiation_history]
    assert len(set(partners)) == 3            # each candidate tried exactly once


@pytest.mark.warehouse
def test_nine_counters_hit_the_q7_budget(warehouse_path, config):
    """Scenario #3 semantics: 3 candidates × 3 rounds of counters = 9
    attempts, then HITL. Never a 10th outreach."""
    stack = MCPStack(warehouse_path)
    stack.prime_slack_replies(["counter"] * 20)     # more than the budget
    lead = _matched_lead(stack)
    result = negotiate(lead, stack=stack, config=config, as_of=AS_OF)
    assert result.status == "hitl_negotiate"
    assert len(result.negotiation_history) == 9
    sent = stack.call("slack", "get_sent")
    assert sent["total"] == 9                       # hard budget respected


@pytest.mark.warehouse
def test_all_holds_released_after_exhaustion(warehouse_path, config):
    """No leaked reservations: after a full exhaustion, every candidate's
    capacity is available again."""
    stack = MCPStack(warehouse_path)
    stack.prime_slack_replies(["decline"] * 3)
    lead = _matched_lead(stack)
    before = {c.partner_id for c in lead.matched_candidates}
    negotiate(lead, stack=stack, config=config, as_of=AS_OF)
    payload = stack.call("partner_capacity", "list_candidates",
                         country="SE", service_type="bookkeeping",
                         industry="professional_services",
                         as_of=AS_OF.isoformat())
    after = {c["partner_id"] for c in payload["candidates"]}
    assert before <= after, "released partners must be listable again"


@pytest.mark.warehouse
def test_override_partner_bypasses_ranking(warehouse_path, config):
    """The HITL escape hatch: negotiate with a manager-picked partner even
    when the lead has no matched_candidates."""
    stack = MCPStack(warehouse_path)
    extraction = {f: None for f in EXTRACTED_FIELDS}
    extraction.update(country="SE", service_type="bookkeeping")
    lead = build_lead_state(
        referral_lead_id="RL_override_test", raw_text="…",
        received_at=AS_OF, extraction=extraction,
        provenance={f: {"confidence": 0.9, "source_span": "…"}
                    for f in EXTRACTED_FIELDS},
    ).model_copy(update={"status": "hitl_negotiate"})

    result = negotiate(lead, stack=stack, config=config, as_of=AS_OF,
                       override_partner_id="P008")
    assert result.status == "booked"
    assert result.negotiation_history[-1]["partner_id"] == "P008"


def test_template_drafter_is_deterministic_and_grounded():
    from src.state import MatchedCandidate
    extraction = {f: None for f in EXTRACTED_FIELDS}
    extraction.update(country="DE", service_type="audit", industry="tech",
                      deal_size_estimate=30000, urgency="high")
    lead = build_lead_state(
        referral_lead_id="RL_x", raw_text="…", received_at=AS_OF,
        extraction=extraction,
        provenance={f: {"confidence": 0.9, "source_span": "…"}
                    for f in EXTRACTED_FIELDS})
    cand = MatchedCandidate(partner_id="P011", rank=1, close_rate_signal=80,
                            partner_roi_pct=25, churn_risk_score=20,
                            response_latency_p50_hours=4, composite_score=80,
                            rationale="x")
    d = TemplateDrafter()
    text1 = d.draft(lead, cand, 1, [])
    assert text1 == d.draft(lead, cand, 1, [])       # deterministic
    for fact in ("RL_x", "audit", "DE", "30000", "high"):
        assert fact in text1                          # grounded in the lead
    assert "round 2" in d.draft(lead, cand, 2, [])


def test_model_tier_boundary(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    config = load_config(require_api_key=False)
    assert model_tier_for_round(1, config) == "sonnet"
    assert model_tier_for_round(2, config) == "opus"
    assert model_tier_for_round(3, config) == "opus"
