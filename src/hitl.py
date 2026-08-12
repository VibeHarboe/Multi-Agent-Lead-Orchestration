"""HITL machinery — interrupt payloads + decision application (§3.5, §9).

Each of the graph's HITL nodes builds an *interrupt payload*: the complete
reasoning chain to that point, plus the options the manager can choose. The
payload is what LangGraph surfaces to the caller when the run pauses, what
`--paused` prints, and (in Week 5/6) what the Slack DM + console render.

Resume decisions are plain dicts:

  hitl_intake    {"action": "enrich", "fields": {...}, "note": "..."}
                 {"action": "drop", "note": "..."}
  hitl_capacity  {"action": "reactivate", "partner_id": "P018", "note": "..."}
                 {"action": "drop", "note": "..."}
  hitl_negotiate {"action": "override", "partner_id": "P012", "note": "..."}
                 {"action": "drop", "note": "..."}

`apply_enrichment` is the only non-trivial applier: manager-supplied fields
are merged with confidence 1.0 and provenance "(manager)", and the LeadState
is rebuilt so downstream nodes see a normally-classified lead.
"""

from __future__ import annotations

from .state import EXTRACTED_FIELDS, LeadState, build_lead_state


def _provenance_summary(lead: LeadState) -> list[dict]:
    out = []
    for f in EXTRACTED_FIELDS:
        p = lead.provenance.get(f)
        out.append({"field": f, "value": getattr(lead, f),
                    "confidence": p.confidence if p else 0.0,
                    "source_span": p.source_span if p else None})
    return out


def _candidates_summary(lead: LeadState) -> list[dict]:
    return [{"partner_id": c.partner_id, "rank": c.rank,
             "composite_score": c.composite_score, "rationale": c.rationale}
            for c in lead.matched_candidates]


def build_interrupt_payload(destination: str, lead: LeadState,
                            **extra) -> dict:
    """The full reasoning chain for the manager. Everything the agents did,
    why the graph paused, and what the manager can do about it."""
    payload = {
        "destination": destination,
        "referral_lead_id": lead.referral_lead_id,
        "status": lead.status,
        "hitl_reason": lead.hitl_reason,
        "received_at": lead.received_at.isoformat(),
        "referring_partner_id": lead.referring_partner_id,
        "intake": {
            "confidence": lead.intake_confidence,
            "missing_essentials": lead.missing_essential_fields(),
            "fields": _provenance_summary(lead),
        },
        "matching": {"candidates": _candidates_summary(lead)},
        "negotiation": {"history": lead.negotiation_history},
    }
    options = {
        "hitl_intake": ["enrich", "drop"],
        "hitl_capacity": ["reactivate", "drop"],
        "hitl_negotiate": ["override", "drop"],
        "hitl_monitor": ["reassign", "drop"],
    }
    payload["options"] = options.get(destination, ["drop"])
    payload.update(extra)
    return payload


def apply_enrichment(lead: LeadState, fields: dict) -> LeadState:
    """Merge manager-supplied field values into the lead and rebuild it.

    Manager input is ground truth: confidence 1.0, provenance "(manager)".
    Existing extracted values are kept unless the manager overrides them."""
    extraction = {f: getattr(lead, f) for f in EXTRACTED_FIELDS}
    provenance = {
        f: {"confidence": (lead.provenance[f].confidence
                           if f in lead.provenance else 0.0),
            "source_span": (lead.provenance[f].source_span
                            if f in lead.provenance else None)}
        for f in EXTRACTED_FIELDS
    }
    for field, value in fields.items():
        if field not in EXTRACTED_FIELDS:
            raise ValueError(f"unknown enrichment field: {field}")
        extraction[field] = value
        provenance[field] = {"confidence": 1.0, "source_span": "(manager)"}

    return build_lead_state(
        referral_lead_id=lead.referral_lead_id,
        raw_text=lead.raw_text,
        received_at=lead.received_at,
        extraction=extraction,
        provenance=provenance,
        referring_partner_id=lead.referring_partner_id,
    )


def mark_lost(lead: LeadState, note: str | None) -> LeadState:
    return lead.model_copy(update={
        "status": "lost",
        "hitl_reason": f"dropped by manager: {note or '(no note)'}"})


def validate_decision(destination: str, decision: dict) -> None:
    """Fail fast on malformed resume decisions — before the graph moves."""
    action = decision.get("action")
    valid = {
        "hitl_intake": {"enrich", "drop"},
        "hitl_capacity": {"reactivate", "drop"},
        "hitl_negotiate": {"override", "drop"},
        "hitl_monitor": {"reassign", "drop"},
    }.get(destination, {"drop"})
    if action not in valid:
        raise ValueError(
            f"invalid action {action!r} for {destination} — valid: {sorted(valid)}")
    if action == "enrich" and not decision.get("fields"):
        raise ValueError("enrich requires a non-empty 'fields' dict")
    if action in ("reactivate", "override") and not decision.get("partner_id"):
        raise ValueError(f"{action} requires 'partner_id'")
