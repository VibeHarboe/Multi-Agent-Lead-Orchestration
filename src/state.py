"""LeadState — the typed state that flows through the LangGraph.

Every field the Intake Agent extracts carries provenance: the confidence score
and the exact text span it came from (per ARCHITECTURE.md §3.1). If a fact
isn't in the text, the field is None with confidence 0.0 — never guessed.

Downstream nodes (Matching, Negotiation, HITL, Monitor) read + write to this
same object. Every graph transition is a pure function over LeadState.
"""

from __future__ import annotations

from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

# Enums matching the warehouse schema — kept in sync with §5 of ARCHITECTURE.md.
Country = Literal["DK", "NO", "SE", "DE", "NL", "US"]
Urgency = Literal["low", "medium", "high"]
ServiceType = Literal["accounting", "tax_advisory", "bookkeeping", "audit", "payroll"]
Industry = Literal["retail", "hospitality", "professional_services", "tech", "other"]

# LangGraph terminal states + interrupt destinations.
LeadStatus = Literal[
    "new",                  # just arrived, not classified
    "classified",           # Intake done
    "matched",              # Matching produced top-K
    "negotiating",          # Negotiation in progress
    "booked",               # deal accepted by partner
    "resolved",             # deal closed successfully
    "lost",                 # deal fell through, no partner found
    "hitl_intake",          # paused: intake confidence too low
    "hitl_capacity",        # paused: no under-capacity candidate in market
    "hitl_negotiate",       # paused: negotiation round budget exceeded
    "hitl_monitor",         # paused: severity-3 breach from monitor
]

# The 12 fields the Intake Agent extracts.
EXTRACTED_FIELDS: tuple[str, ...] = (
    "country",
    "industry",
    "service_type",
    "urgency",
    "deal_size_estimate",
    "company_name",
    "contact_name",
    "contact_role",
    "contact_email",
    "timeline",
    "tech_stack",
    "budget_signal",
)


class Provenance(BaseModel):
    """Per-field extraction metadata."""
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    source_span: Optional[str] = None


class MatchedCandidate(BaseModel):
    """One entry in the Matching Agent's top-K output."""
    partner_id: str
    rank: int = Field(..., ge=1)
    close_rate_signal: float = Field(0.0, ge=0.0, le=100.0)
    partner_roi_pct: float
    partner_roi_trend: Literal["up", "flat", "down"] = "flat"
    churn_risk_score: float = Field(0.0, ge=0.0, le=100.0)
    response_latency_p50_hours: float = Field(0.0, ge=0.0)
    composite_score: float
    rationale: str


class LeadState(BaseModel):
    """The typed state that flows through the orchestration graph."""

    model_config = ConfigDict(str_strip_whitespace=True)

    # ── Identity ──────────────────────────────────────────────────────────
    referral_lead_id: str
    raw_text: str
    received_at: date
    referring_partner_id: Optional[str] = None

    # ── Intake extraction (Optional — None means "not extractable") ──────
    country: Optional[Country] = None
    industry: Optional[Industry] = None
    service_type: Optional[ServiceType] = None
    urgency: Optional[Urgency] = None
    deal_size_estimate: Optional[int] = Field(None, ge=0)
    company_name: Optional[str] = None
    contact_name: Optional[str] = None
    contact_role: Optional[str] = None
    contact_email: Optional[str] = None
    timeline: Optional[str] = None
    tech_stack: Optional[str] = None
    budget_signal: Optional[str] = None

    # ── Provenance for every field the Intake Agent tried to extract ─────
    provenance: dict[str, Provenance] = Field(default_factory=dict)
    intake_confidence: float = Field(0.0, ge=0.0, le=1.0)

    # ── Matching output ──────────────────────────────────────────────────
    matched_candidates: list[MatchedCandidate] = Field(default_factory=list)

    # ── Negotiation history (Week 4) ─────────────────────────────────────
    negotiation_history: list[dict] = Field(default_factory=list)

    # ── Lifecycle ────────────────────────────────────────────────────────
    status: LeadStatus = "new"
    hitl_reason: Optional[str] = None

    # ── Cost tracking (Q15) ──────────────────────────────────────────────
    tokens_spent_usd: float = 0.0

    # ── Convenience methods ──────────────────────────────────────────────
    def is_ambiguous(self, threshold: float = 0.5) -> bool:
        """Whether intake_confidence is low enough to escalate to HITL."""
        return self.intake_confidence < threshold

    def field_confidence(self, field: str) -> float:
        p = self.provenance.get(field)
        return p.confidence if p else 0.0

    def extracted_fields_count(self) -> int:
        """How many of the 12 fields we actually got a value for."""
        return sum(1 for f in EXTRACTED_FIELDS if getattr(self, f) is not None)

    def missing_essential_fields(self) -> list[str]:
        """Fields Matching needs — country and service_type. Both must be
        present for the graph to route past intake without HITL."""
        missing = []
        if self.country is None:
            missing.append("country")
        if self.service_type is None:
            missing.append("service_type")
        return missing

    def use_opus_for_intake(self, threshold: float) -> bool:
        """Q16: escalate Intake to Opus when confidence < threshold."""
        return self.intake_confidence < threshold


def build_lead_state(
    referral_lead_id: str,
    raw_text: str,
    received_at: date,
    extraction: dict,
    provenance: dict[str, dict] | None = None,
    referring_partner_id: str | None = None,
) -> LeadState:
    """Construct a LeadState from an Intake Agent's structured extraction.

    Pure function — used by intake.py at runtime *and* by tests with a fixture
    extraction dict. Keeps the Pydantic construction and confidence roll-up in
    one place, independently testable.
    """
    prov: dict[str, Provenance] = {}
    for field in EXTRACTED_FIELDS:
        p_raw = (provenance or {}).get(field, {})
        prov[field] = Provenance(
            confidence=float(p_raw.get("confidence", 0.0)),
            source_span=p_raw.get("source_span"),
        )
    # Overall intake confidence = mean of per-field confidence (all 12 fields,
    # including zeros for empty extractions — so ambiguous inputs score low).
    overall = round(sum(p.confidence for p in prov.values()) / len(EXTRACTED_FIELDS), 3)

    return LeadState(
        referral_lead_id=referral_lead_id,
        raw_text=raw_text,
        received_at=received_at,
        referring_partner_id=referring_partner_id,
        country=extraction.get("country"),
        industry=extraction.get("industry"),
        service_type=extraction.get("service_type"),
        urgency=extraction.get("urgency"),
        deal_size_estimate=extraction.get("deal_size_estimate"),
        company_name=extraction.get("company_name"),
        contact_name=extraction.get("contact_name"),
        contact_role=extraction.get("contact_role"),
        contact_email=extraction.get("contact_email"),
        timeline=extraction.get("timeline"),
        tech_stack=extraction.get("tech_stack"),
        budget_signal=extraction.get("budget_signal"),
        provenance=prov,
        intake_confidence=overall,
        status="classified",
    )
