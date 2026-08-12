"""Negotiation Agent — Station 3. The bounded 3×3 loop (Q7).

Control flow is fully deterministic and LLM-free: outcomes come pre-classified
from the slack MCP (`outcome` field), and the loop logic is pure Python. The
LLM's only job here is *drafting* the outreach text — pluggable via the
Drafter protocol:

  - TemplateDrafter  — deterministic, zero-cost. Default for tests, dry runs
                       and the --run CLI.
  - ClaudeDrafter    — live drafting. Sonnet for round 1, Opus for round >= 2
                       (Q16: a counter-proposal needs harder judgment).
                       Lazily imports anthropic so the no-LLM guarantee holds
                       everywhere else.

Loop semantics (per ARCHITECTURE §3.3):
  FOR each candidate (max MAX_CANDIDATES):
      hold_capacity — fails → skip candidate (headroom vanished concurrently)
      FOR round in 1..MAX_NEGOTIATION_ROUNDS:
          draft + send_dm + wait_for_reply (timeout = 24h SLA)
          accept   → book via CRM, DONE
          no_reply → release hold, next candidate   (dynamic in-loop re-routing)
          decline  → release hold, next candidate
          counter  → next round, same candidate
      rounds exhausted on counters → release hold, next candidate
  all candidates exhausted → status = hitl_negotiate
"""

from __future__ import annotations

from datetime import date
from typing import Protocol

from ..config import Config
from ..state import LeadState, MatchedCandidate


class Drafter(Protocol):
    def draft(self, lead: LeadState, candidate: MatchedCandidate,
              round_num: int, history: list[dict]) -> str: ...


class TemplateDrafter:
    """Deterministic outreach text — no LLM, no cost."""

    def draft(self, lead: LeadState, candidate: MatchedCandidate,
              round_num: int, history: list[dict]) -> str:
        base = (f"New referral {lead.referral_lead_id}: "
                f"{lead.service_type} for a {lead.industry or 'SMB'} client "
                f"in {lead.country}, est. deal size "
                f"{lead.deal_size_estimate or 'n/a'}, urgency "
                f"{lead.urgency or 'normal'}.")
        if round_num == 1:
            return base + " Are you interested?"
        return (base + f" (round {round_num} — following up on your "
                       f"counter-proposal; can we find terms that work?)")


class ClaudeDrafter:
    """Live drafting with Q16 model routing. Only imported/instantiated on
    LLM-marked paths — keeps anthropic out of the default import graph."""

    def __init__(self, config: Config):
        import anthropic                      # lazy on purpose
        self._client = anthropic.Anthropic(api_key=config.anthropic_api_key)
        self._config = config

    def _model_for_round(self, round_num: int) -> str:
        if round_num >= self._config.negotiation_opus_round:
            return self._config.negotiation_opus_model
        return self._config.planner_model

    def draft(self, lead: LeadState, candidate: MatchedCandidate,
              round_num: int, history: list[dict]) -> str:
        prior = "\n".join(
            f"- round {h['round']} with {h['partner_id']}: {h['outcome']}"
            for h in history) or "(first contact)"
        response = self._client.messages.create(
            model=self._model_for_round(round_num),
            max_tokens=400,
            system=("You draft short, professional Slack outreach messages to "
                    "fulfillment partners on the NordLedger Marketplace. Keep "
                    "it under 120 words, concrete, no fluff."),
            messages=[{"role": "user", "content":
                       f"Referral: {lead.model_dump_json(include={'referral_lead_id','country','industry','service_type','urgency','deal_size_estimate','timeline'})}\n"
                       f"Partner: {candidate.partner_id} (rationale: {candidate.rationale})\n"
                       f"Round: {round_num}\nPrior attempts:\n{prior}\n\n"
                       f"Draft the outreach message."}],
        )
        return "".join(b.text for b in response.content
                       if getattr(b, "type", None) == "text").strip()


def model_tier_for_round(round_num: int, config: Config) -> str:
    """Q16 record-keeping: which tier a round runs on (asserted by tests
    independently of which drafter is active)."""
    return "opus" if round_num >= config.negotiation_opus_round else "sonnet"


def negotiate(
    lead: LeadState,
    *,
    stack,                        # MCPStack
    config: Config,
    as_of: date,
    drafter: Drafter | None = None,
    override_partner_id: str | None = None,
) -> LeadState:
    """Run the bounded negotiation loop. Returns the lead in one of three
    terminal states: booked · hitl_negotiate · (unchanged hitl_* if input
    wasn't matched).

    `override_partner_id` is the HITL escape hatch: the manager picked a
    specific partner, so the candidate list collapses to that single entry
    (rationale marked as manager override) — budget still applies."""
    if lead.status != "matched" and not override_partner_id:
        return lead
    drafter = drafter or TemplateDrafter()

    if override_partner_id:
        candidates = [MatchedCandidate(
            partner_id=override_partner_id, rank=1,
            close_rate_signal=50.0, partner_roi_pct=0.0,
            partner_roi_trend="flat", churn_risk_score=0.0,
            response_latency_p50_hours=24.0, composite_score=0.0,
            rationale="manager override (HITL)")]
    else:
        candidates = lead.matched_candidates[:config.max_candidates]

    history: list[dict] = list(lead.negotiation_history)

    for candidate in candidates:
        hold = stack.call("partner_capacity", "hold_capacity",
                          partner_id=candidate.partner_id,
                          lead_id=lead.referral_lead_id,
                          ttl_hours=48, as_of=as_of.isoformat())
        if not hold.get("ok"):
            history.append({"round": 0, "partner_id": candidate.partner_id,
                            "message_id": None, "outcome": "hold_failed",
                            "model_tier": None, "elapsed_hours": 0.0})
            continue

        booked = False
        for round_num in range(1, config.max_negotiation_rounds + 1):
            tier = model_tier_for_round(round_num, config)
            text = drafter.draft(lead, candidate, round_num, history)
            dm = stack.call("slack", "send_dm",
                            user_id=candidate.partner_id, text=text)
            reply = stack.call("slack", "wait_for_reply",
                               message_id=dm["message_id"],
                               timeout_hours=config.partner_response_sla_hours)
            outcome = reply["outcome"]
            history.append({"round": round_num,
                            "partner_id": candidate.partner_id,
                            "message_id": dm["message_id"],
                            "outcome": outcome, "model_tier": tier,
                            "elapsed_hours": reply["elapsed_hours"]})

            if outcome == "accept":
                stack.call("crm", "upsert_lead",
                           referral_lead_id=lead.referral_lead_id,
                           lead_json=lead.model_dump_json())
                stack.call("crm", "update_stage",
                           referral_lead_id=lead.referral_lead_id,
                           stage="booked",
                           rationale=(f"{candidate.partner_id} accepted in "
                                      f"round {round_num}"
                                      + (" (manager override)" if override_partner_id else "")),
                           partner_id=candidate.partner_id,
                           as_of=as_of.isoformat())
                booked = True
                break
            if outcome in ("no_reply", "decline"):
                # dynamic in-loop re-routing: drop this candidate, try next
                stack.call("partner_capacity", "release_capacity",
                           partner_id=candidate.partner_id,
                           lead_id=lead.referral_lead_id)
                break
            # counter → another round with the same candidate
        else:
            # round budget exhausted on counters — release, next candidate
            stack.call("partner_capacity", "release_capacity",
                       partner_id=candidate.partner_id,
                       lead_id=lead.referral_lead_id)

        if booked:
            return lead.model_copy(update={
                "status": "booked", "negotiation_history": history})

    attempts = sum(1 for h in history if h["outcome"] != "hold_failed")
    return lead.model_copy(update={
        "status": "hitl_negotiate",
        "hitl_reason": (f"{len(candidates)} candidate(s) × up to "
                        f"{config.max_negotiation_rounds} rounds — "
                        f"{attempts} outreach attempts, no booking"),
        "negotiation_history": history})
