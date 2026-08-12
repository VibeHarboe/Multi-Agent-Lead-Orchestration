"""Intake Agent — Station 1.

Turns a raw referral text into a typed LeadState with per-field provenance.
Uses Claude's tool-use to force a structured JSON extraction: the model MUST
call the `extract_lead_fields` tool, so there's no free-form text to parse.

Rebuild 3 §3.1: every extracted value carries its confidence + source_span.
A field with no evidence stays None with confidence 0.0 — never guessed.

Testability: `_build_extraction_tool()` is a pure function returning the tool
schema; `parse_extraction(tool_input)` is a pure function that constructs a
LeadState from an LLM response. The live LLM call (`extract_lead_fields`) is
what `@pytest.mark.llm` gates.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import anthropic

from ..config import Config
from ..state import (
    EXTRACTED_FIELDS,
    Country,
    Industry,
    LeadState,
    ServiceType,
    Urgency,
    build_lead_state,
)


_SYSTEM_PROMPT = """You are the Intake Agent for the NordLedger Marketplace lead-orchestration system.

Your job: given a raw partner referral in English, extract structured fields
about the lead using the `extract_lead_fields` tool. You MUST call the tool.

RULES
- Extract ONLY what is present in the text. If a fact is not in the text, set
  its value to null and confidence to 0.0. NEVER guess.
- For every extracted field, include:
  1. the value (typed per the tool schema),
  2. a confidence score from 0.0 to 1.0 for how sure you are,
  3. the exact text span (short substring) it came from.
- Country must be an ISO-2 code: DK, NO, SE, DE, NL, US. If the text says
  "Denmark" set country="DK".
- Service type: one of accounting, tax_advisory, bookkeeping, audit, payroll.
- Industry: one of retail, hospitality, professional_services, tech, other.
- Urgency: one of low, medium, high — infer from timeline language.
- Deal size estimate: an integer in local currency; leave null if not stated.
- If the entire referral is too vague to extract anything (a two-line message
  with no company, no country, no service), set every value to null and
  every confidence to 0.0 — the graph will surface this to a human."""


def _extraction_field_schema(value_type: str, enum: list[str] | None = None) -> dict:
    """Reusable per-field sub-schema — value + confidence + source_span."""
    value_prop: dict[str, Any] = {"type": [value_type, "null"]}
    if enum is not None:
        value_prop["enum"] = list(enum) + [None]
    return {
        "type": "object",
        "properties": {
            "value": value_prop,
            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "source_span": {"type": ["string", "null"]},
        },
        "required": ["value", "confidence", "source_span"],
    }


def build_extraction_tool() -> dict:
    """The tool Claude MUST call. Every field has the same shape: value +
    confidence + source_span. The `enum` constraints on country / service_type
    / industry / urgency remove a whole class of hallucination-space."""
    return {
        "name": "extract_lead_fields",
        "description": (
            "Extract 12 structured fields from a partner referral. Every field "
            "must include a value (or null if not in the text), a confidence "
            "score, and the source_span it came from."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "country": _extraction_field_schema("string", enum=list(Country.__args__)),
                "industry": _extraction_field_schema("string", enum=list(Industry.__args__)),
                "service_type": _extraction_field_schema("string", enum=list(ServiceType.__args__)),
                "urgency": _extraction_field_schema("string", enum=list(Urgency.__args__)),
                "deal_size_estimate": _extraction_field_schema("integer"),
                "company_name": _extraction_field_schema("string"),
                "contact_name": _extraction_field_schema("string"),
                "contact_role": _extraction_field_schema("string"),
                "contact_email": _extraction_field_schema("string"),
                "timeline": _extraction_field_schema("string"),
                "tech_stack": _extraction_field_schema("string"),
                "budget_signal": _extraction_field_schema("string"),
            },
            "required": list(EXTRACTED_FIELDS),
        },
    }


def parse_extraction(tool_input: dict) -> tuple[dict, dict]:
    """Split Claude's tool_input into (values_by_field, provenance_by_field).

    Pure function — the same conversion is used both at runtime and in tests
    with fixture tool_input dicts.
    """
    values: dict = {}
    provenance: dict = {}
    for field in EXTRACTED_FIELDS:
        entry = tool_input.get(field) or {}
        values[field] = entry.get("value")
        provenance[field] = {
            "confidence": float(entry.get("confidence", 0.0) or 0.0),
            "source_span": entry.get("source_span"),
        }
    return values, provenance


def classify_lead(
    raw_text: str,
    *,
    referral_lead_id: str,
    received_at: date,
    referring_partner_id: str | None,
    config: Config,
) -> LeadState:
    """Live Intake — calls Claude and constructs the LeadState from the tool
    response. Escalates to Opus when the overall confidence lands below the
    Q16 threshold and the classification is re-tried once.
    """
    client = anthropic.Anthropic(api_key=config.anthropic_api_key)
    tool = build_extraction_tool()

    def _extract_with(model: str) -> dict:
        response = client.messages.create(
            model=model,
            max_tokens=2048,
            system=_SYSTEM_PROMPT,
            tools=[tool],
            tool_choice={"type": "tool", "name": "extract_lead_fields"},
            messages=[{"role": "user", "content": raw_text}],
        )
        for block in response.content:
            if getattr(block, "type", None) == "tool_use":
                return dict(block.input)
        raise RuntimeError("Intake Agent: model did not call extract_lead_fields")

    # First pass on Sonnet (default per Q16)
    tool_input = _extract_with(config.planner_model)
    values, provenance = parse_extraction(tool_input)
    state = build_lead_state(
        referral_lead_id=referral_lead_id,
        raw_text=raw_text,
        received_at=received_at,
        extraction=values,
        provenance=provenance,
        referring_partner_id=referring_partner_id,
    )

    # Q16: retry on Opus if Sonnet's confidence was too low
    if state.use_opus_for_intake(config.intake_opus_confidence_threshold):
        tool_input2 = _extract_with(config.negotiation_opus_model)
        values2, provenance2 = parse_extraction(tool_input2)
        state2 = build_lead_state(
            referral_lead_id=referral_lead_id,
            raw_text=raw_text,
            received_at=received_at,
            extraction=values2,
            provenance=provenance2,
            referring_partner_id=referring_partner_id,
        )
        # Keep the higher-confidence version
        if state2.intake_confidence > state.intake_confidence:
            state = state2

    # If still ambiguous after the escalation, mark for HITL
    if state.is_ambiguous(config.intake_opus_confidence_threshold):
        return state.model_copy(update={
            "status": "hitl_intake",
            "hitl_reason": (
                f"intake_confidence {state.intake_confidence:.2f} < "
                f"threshold {config.intake_opus_confidence_threshold} — "
                f"missing essentials: {state.missing_essential_fields() or 'multiple'}"
            ),
        })
    return state
