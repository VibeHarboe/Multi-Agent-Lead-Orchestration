"""Dry-run — walk the whole graph against the MCP mocks WITHOUT the LLM.

This is Week 3's exit gate: proof that the plumbing (state transitions, MCP
tool calls, warehouse writes) works end-to-end before a single API token is
spent. Intake is replaced by a seed-fixture (the seeded referral row already
carries country/service_type/industry), and negotiation is a deterministic
single-round walk driven by slack_mock's primed/auto replies.

No `anthropic` import anywhere on this path — that's asserted by tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .agents.matching import match_mcp
from .data.warehouse import get_referral_lead_by_id
from .mcp_stack import MCPStack
from .state import EXTRACTED_FIELDS, LeadState, build_lead_state


@dataclass
class DryRunResult:
    lead: LeadState
    walk_log: list[str] = field(default_factory=list)
    dormant_partners: list[dict] = field(default_factory=list)
    mcp_calls: int = 0

    @property
    def outcome(self) -> str:
        return self.lead.status


def lead_state_from_seed(seed: dict) -> LeadState:
    """Build a LeadState from a seeded referral row — the no-LLM intake.

    Present fields get confidence 0.95 with a synthetic span; empty fields
    stay None at 0.0 — so the ambiguous-inquiry seeds (industry/service NULL)
    correctly route to hitl_intake, same as the real Intake Agent would."""
    def _clean(v):
        return v if v not in ("", None) else None

    extraction = {f: None for f in EXTRACTED_FIELDS}
    extraction["country"] = _clean(seed.get("country"))
    extraction["industry"] = _clean(seed.get("industry"))
    extraction["service_type"] = _clean(seed.get("service_type"))
    extraction["urgency"] = _clean(seed.get("urgency"))
    deal_size = seed.get("deal_size_estimate")
    extraction["deal_size_estimate"] = int(deal_size) if deal_size else None

    provenance = {
        f: {"confidence": 0.95 if extraction[f] is not None else 0.0,
            "source_span": "(seed fixture)" if extraction[f] is not None else None}
        for f in EXTRACTED_FIELDS
    }
    received = seed["referred_at"]
    if not isinstance(received, date):
        received = date.fromisoformat(str(received))

    return build_lead_state(
        referral_lead_id=seed["referral_lead_id"],
        raw_text="(dry-run: seeded referral, no raw text processed)",
        received_at=received,
        extraction=extraction,
        provenance=provenance,
        referring_partner_id=_clean(seed.get("referring_partner_id")),
    )


def dry_run(
    referral_lead_id: str,
    *,
    warehouse_path: Path,
    top_k: int = 3,
    as_of: date | None = None,
    scripted_replies: list[str] | None = None,
) -> DryRunResult:
    """Walk one seeded referral through classify → match → negotiate → book,
    entirely against the MCP mocks. Returns the final LeadState + a
    human-readable walk log."""
    as_of = as_of or date.today()
    stack = MCPStack(warehouse_path)
    if scripted_replies:
        stack.prime_slack_replies(scripted_replies)

    log: list[str] = []
    result = DryRunResult(lead=None, walk_log=log)  # type: ignore[arg-type]

    # ── 1. intake (seed fixture — no LLM) ───────────────────────────────
    seed = get_referral_lead_by_id(warehouse_path, referral_lead_id)
    if seed is None:
        raise ValueError(f"unknown referral_lead_id: {referral_lead_id}")
    lead = lead_state_from_seed(seed)
    log.append(f"[1] intake (seed fixture, no LLM) → status={lead.status} "
               f"confidence={lead.intake_confidence:.2f} "
               f"country={lead.country} service={lead.service_type}")

    # ── 2. matching via MCP ─────────────────────────────────────────────
    lead = match_mcp(lead, stack=stack, top_k=top_k, as_of=as_of)
    if lead.status == "hitl_intake":
        log.append(f"[2] matching → HITL_INTAKE ({lead.hitl_reason})")
        result.lead = lead
        result.mcp_calls = len(stack.call_log)
        return result
    if lead.status == "hitl_capacity":
        log.append(f"[2] matching via MCP → HITL_CAPACITY ({lead.hitl_reason})")
        dormant = stack.call("partner_capacity", "list_dormant_partners",
                             country=lead.country,
                             service_type=lead.service_type,
                             industry=lead.industry)
        result.dormant_partners = dormant.get("dormant", [])
        log.append(f"[3] list_dormant_partners → "
                   f"{len(result.dormant_partners)} reactivation candidate(s)")
        result.lead = lead
        result.mcp_calls = len(stack.call_log)
        return result
    top = lead.matched_candidates[0]
    log.append(f"[2] matching via MCP → {len(lead.matched_candidates)} candidates, "
               f"top={top.partner_id} (score {top.composite_score})")

    # ── 3. deterministic negotiation walk (single round, scripted reply) ─
    hold = stack.call("partner_capacity", "hold_capacity",
                      partner_id=top.partner_id,
                      lead_id=lead.referral_lead_id,
                      ttl_hours=48, as_of=as_of.isoformat())
    if not hold.get("ok"):
        log.append(f"[3] hold_capacity {top.partner_id} → FAILED ({hold.get('error')})")
        result.lead = lead.model_copy(update={
            "status": "hitl_capacity",
            "hitl_reason": f"hold_capacity failed: {hold.get('error')}"})
        result.mcp_calls = len(stack.call_log)
        return result
    log.append(f"[3] hold_capacity {top.partner_id} → ok")

    dm = stack.call("slack", "send_dm",
                    user_id=top.partner_id,
                    text=f"New referral {lead.referral_lead_id}: "
                         f"{lead.service_type} · {lead.country} · "
                         f"est. {lead.deal_size_estimate or 'n/a'}. Interested?")
    log.append(f"[4] slack.send_dm → {dm['message_id']}")

    reply = stack.call("slack", "wait_for_reply",
                       message_id=dm["message_id"], timeout_hours=24)
    outcome = reply["outcome"]
    log.append(f"[5] slack.wait_for_reply → {outcome} "
               f"({reply['elapsed_hours']:.1f}h)")

    history = [{"round": 1, "partner_id": top.partner_id,
                "message_id": dm["message_id"], "outcome": outcome,
                "elapsed_hours": reply["elapsed_hours"]}]

    if outcome != "accept":
        # dry-run walks a single round; non-accept releases and stops (the
        # full 3×3 loop is Week 4's Negotiation Agent)
        stack.call("partner_capacity", "release_capacity",
                   partner_id=top.partner_id, lead_id=lead.referral_lead_id)
        log.append(f"[6] release_capacity {top.partner_id} → ok "
                   f"(dry-run stops after one round on non-accept)")
        result.lead = lead.model_copy(update={
            "status": "hitl_negotiate",
            "hitl_reason": f"dry-run single round ended in {outcome}",
            "negotiation_history": history})
        result.mcp_calls = len(stack.call_log)
        return result

    # ── 4. book via CRM ─────────────────────────────────────────────────
    up = stack.call("crm", "upsert_lead",
                    referral_lead_id=lead.referral_lead_id,
                    lead_json=lead.model_dump_json())
    log.append(f"[6] crm.upsert_lead → ok (existed={up.get('existed')})")

    st = stack.call("crm", "update_stage",
                    referral_lead_id=lead.referral_lead_id,
                    stage="booked",
                    rationale=f"{top.partner_id} accepted in dry-run round 1",
                    partner_id=top.partner_id,
                    as_of=as_of.isoformat())
    log.append(f"[7] crm.update_stage booked → ok (event {st.get('event_id')})")

    result.lead = lead.model_copy(update={
        "status": "booked", "negotiation_history": history})
    result.mcp_calls = len(stack.call_log)
    log.append(f"RESULT: booked · partner {top.partner_id} · "
               f"{result.mcp_calls} MCP calls · 0 LLM calls · $0.00")
    return result
