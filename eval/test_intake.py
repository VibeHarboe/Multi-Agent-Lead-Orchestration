"""Tests for the Intake Agent.

Deterministic:
- `parse_extraction` correctly splits tool-input into values + provenance
- `build_extraction_tool` produces a valid schema
Live (opt-in, `-m llm`):
- Actual Claude calls against sample referrals — verifies real-world behaviour
"""

from __future__ import annotations

from datetime import date

import pytest

from src.agents.intake import (
    build_extraction_tool,
    parse_extraction,
)
from src.state import EXTRACTED_FIELDS


def test_extraction_tool_schema_shape():
    tool = build_extraction_tool()
    assert tool["name"] == "extract_lead_fields"
    schema = tool["input_schema"]
    assert schema["type"] == "object"
    # every extracted field must appear in properties + be required
    for field in EXTRACTED_FIELDS:
        assert field in schema["properties"], f"missing property: {field}"
        assert field in schema["required"], f"missing required: {field}"
        prop = schema["properties"][field]
        assert set(prop["properties"].keys()) == {"value", "confidence", "source_span"}


def test_extraction_tool_country_is_enum_constrained():
    """One of the constrained-generation guarantees: country can't be any
    string, it must be one of the six ISO-2 codes."""
    tool = build_extraction_tool()
    country_value_schema = tool["input_schema"]["properties"]["country"]["properties"]["value"]
    assert "enum" in country_value_schema
    assert "DK" in country_value_schema["enum"]
    assert "US" in country_value_schema["enum"]
    # explicitly enumerate + include null
    assert None in country_value_schema["enum"]
    # anything not in the enum should be excluded
    assert "XX" not in country_value_schema["enum"]


def test_parse_extraction_clean():
    tool_input = {
        f: {"value": None, "confidence": 0.0, "source_span": None}
        for f in EXTRACTED_FIELDS
    }
    tool_input["country"] = {"value": "DK", "confidence": 0.95, "source_span": "Denmark"}
    tool_input["service_type"] = {"value": "bookkeeping", "confidence": 0.9, "source_span": "bookkeeping"}

    values, provenance = parse_extraction(tool_input)
    assert values["country"] == "DK"
    assert values["service_type"] == "bookkeeping"
    assert values["industry"] is None
    assert provenance["country"] == {"confidence": 0.95, "source_span": "Denmark"}
    assert provenance["industry"]["confidence"] == 0.0


def test_parse_extraction_missing_fields_default_to_zero():
    """A malformed tool_input missing some fields should still parse — every
    field defaults to (None, 0.0)."""
    tool_input = {"country": {"value": "DE", "confidence": 0.9, "source_span": "Germany"}}
    values, provenance = parse_extraction(tool_input)
    assert values["country"] == "DE"
    for other in EXTRACTED_FIELDS:
        if other == "country":
            continue
        assert values[other] is None
        assert provenance[other]["confidence"] == 0.0


# -- Live LLM tests (opt-in) --------------------------------------------------

@pytest.mark.llm
def test_intake_extracts_clear_swedish_referral(sample_referral_texts):
    """The clear Swedish referral should classify with high confidence,
    country=SE, service_type=bookkeeping."""
    from src.agents.intake import classify_lead
    from src.config import load_config
    config = load_config()

    s = classify_lead(
        raw_text=sample_referral_texts["clear_swedish"],
        referral_lead_id="RL_test_swe",
        received_at=date(2024, 12, 15),
        referring_partner_id=None,
        config=config,
    )
    assert s.country == "SE"
    assert s.service_type == "bookkeeping"
    assert s.intake_confidence > 0.4, f"got {s.intake_confidence}"
    assert not s.is_ambiguous(threshold=0.3)


@pytest.mark.llm
def test_intake_flags_very_ambiguous_referral(sample_referral_texts):
    """A two-word referral should escalate to HITL — the graph should never
    guess a country or a service from nothing."""
    from src.agents.intake import classify_lead
    from src.config import load_config
    config = load_config()

    s = classify_lead(
        raw_text=sample_referral_texts["very_ambiguous"],
        referral_lead_id="RL_test_amb",
        received_at=date(2024, 12, 15),
        referring_partner_id=None,
        config=config,
    )
    assert s.status == "hitl_intake"
    assert s.country is None
    assert s.service_type is None
    assert s.intake_confidence < 0.5


@pytest.mark.llm
def test_intake_extracts_urgency_from_language(sample_referral_texts):
    """The Dutch referral says 'urgently' + 'previous auditor quit' — urgency
    should extract as 'high'."""
    from src.agents.intake import classify_lead
    from src.config import load_config
    config = load_config()

    s = classify_lead(
        raw_text=sample_referral_texts["clear_dutch_urgent"],
        referral_lead_id="RL_test_nl",
        received_at=date(2024, 12, 15),
        referring_partner_id=None,
        config=config,
    )
    assert s.country == "NL"
    assert s.service_type == "audit"
    assert s.urgency == "high"
