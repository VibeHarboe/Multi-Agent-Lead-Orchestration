"""Environment loading + validation. All operational thresholds (Q6-Q16) live
here so agents and tests read from one source of truth."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    # Anthropic
    anthropic_api_key: str
    planner_model: str
    negotiation_opus_model: str
    negotiation_opus_round: int
    intake_opus_confidence_threshold: float

    # Warehouse
    dbt_project_dir: Path

    # Matching (Q6)
    matching_top_k: int

    # Negotiation (Q7)
    max_negotiation_rounds: int
    max_candidates: int
    partner_response_sla_hours: int

    # Monitor (Q8, Q9, Q10, Q11, Q12)
    post_booking_accept_sla_hours: int
    post_booking_close_sla_days: int
    churn_risk_threshold: float
    churn_risk_sustained_days: int
    saturated_util_pct: float
    saturated_consecutive_days: int
    starved_util_pct: float
    starved_consecutive_days: int
    monitor_sweep_hour_cet: int
    weekly_report_day: str
    weekly_report_hour_cet: int

    # HITL (Q13, Q14)
    hitl_timeout_business_hours: int

    # Cost budget (Q15)
    per_lead_budget_usd: float
    budget_circuit_break_pct: float

    # As-of (matches BI Agent pattern)
    as_of_date: date | None

    @property
    def warehouse_dir(self) -> Path:
        return self.dbt_project_dir

    @property
    def duckdb_path(self) -> Path:
        return self.dbt_project_dir / "nordledger.duckdb"

    @property
    def budget_circuit_break_usd(self) -> float:
        return self.per_lead_budget_usd * self.budget_circuit_break_pct


def load_config(*, require_api_key: bool = True) -> Config:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key and require_api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY not set — copy .env.example to .env and fill it in."
        )

    dbt_dir = os.environ.get("DBT_PROJECT_DIR", "./warehouse")
    dbt_path = Path(dbt_dir).expanduser().resolve()

    as_of_raw = os.environ.get("AS_OF_DATE", "").strip()
    as_of = date.fromisoformat(as_of_raw) if as_of_raw else None

    return Config(
        anthropic_api_key=api_key,
        planner_model=os.environ.get("PLANNER_MODEL", "claude-sonnet-4-6"),
        negotiation_opus_model=os.environ.get("NEGOTIATION_OPUS_MODEL", "claude-opus-4-7"),
        negotiation_opus_round=int(os.environ.get("NEGOTIATION_OPUS_ROUND", "2")),
        intake_opus_confidence_threshold=float(
            os.environ.get("INTAKE_OPUS_CONFIDENCE_THRESHOLD", "0.5")),
        dbt_project_dir=dbt_path,
        matching_top_k=int(os.environ.get("MATCHING_TOP_K", "3")),
        max_negotiation_rounds=int(os.environ.get("MAX_NEGOTIATION_ROUNDS", "3")),
        max_candidates=int(os.environ.get("MAX_CANDIDATES", "3")),
        partner_response_sla_hours=int(os.environ.get("PARTNER_RESPONSE_SLA_HOURS", "24")),
        post_booking_accept_sla_hours=int(
            os.environ.get("POST_BOOKING_ACCEPT_SLA_HOURS", "48")),
        post_booking_close_sla_days=int(
            os.environ.get("POST_BOOKING_CLOSE_SLA_DAYS", "30")),
        churn_risk_threshold=float(os.environ.get("CHURN_RISK_THRESHOLD", "65")),
        churn_risk_sustained_days=int(os.environ.get("CHURN_RISK_SUSTAINED_DAYS", "7")),
        saturated_util_pct=float(os.environ.get("SATURATED_UTIL_PCT", "90")),
        saturated_consecutive_days=int(os.environ.get("SATURATED_CONSECUTIVE_DAYS", "7")),
        starved_util_pct=float(os.environ.get("STARVED_UTIL_PCT", "20")),
        starved_consecutive_days=int(os.environ.get("STARVED_CONSECUTIVE_DAYS", "14")),
        monitor_sweep_hour_cet=int(os.environ.get("MONITOR_SWEEP_HOUR_CET", "6")),
        weekly_report_day=os.environ.get("WEEKLY_REPORT_DAY", "monday"),
        weekly_report_hour_cet=int(os.environ.get("WEEKLY_REPORT_HOUR_CET", "8")),
        hitl_timeout_business_hours=int(
            os.environ.get("HITL_TIMEOUT_BUSINESS_HOURS", "4")),
        per_lead_budget_usd=float(os.environ.get("PER_LEAD_BUDGET_USD", "2.0")),
        budget_circuit_break_pct=float(
            os.environ.get("BUDGET_CIRCUIT_BREAK_PCT", "0.8")),
        as_of_date=as_of,
    )
