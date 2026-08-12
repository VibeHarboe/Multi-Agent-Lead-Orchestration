"""DuckDB-backed data access layer.

Week 2: the Matching Agent reads partner-capacity directly from here (fixture
mode). Week 3: `partner_capacity_mcp` wraps these same queries as MCP tools.
The queries stay the same either way — only the transport changes.

All functions take an explicit warehouse `Path` so tests can point at a
fixture DB.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

import duckdb


@dataclass
class Candidate:
    """One row returned by list_candidates. Rebuild 3's structured partner
    view — richer than the raw stg_partner_capacity row because it joins in
    engagement + specialization + status."""
    partner_id: str
    partner_name: str
    country: str
    is_under_soft_cap: bool
    is_under_hard_cap: bool
    active_deals_count: int
    soft_cap: int
    hard_cap: int
    specialization_strength: float          # 0-100
    close_rate_signal: float                # 0-100
    partner_roi_pct: float
    partner_roi_trend: str                  # "up" | "flat" | "down"
    net_monthly_value: float
    churn_risk_score: float                 # 0-100
    response_latency_p50_hours: float
    partner_status: str                     # "active" | "inactive"


def _connect(warehouse_path: Path) -> duckdb.DuckDBPyConnection:
    # Read-write by default: DuckDB refuses to open the same file with
    # different configurations in one process, and crm_mock_mcp needs a
    # writer against the same file. This module itself only ever SELECTs.
    return duckdb.connect(str(warehouse_path))


def list_candidates(
    warehouse_path: Path,
    *,
    country: str,
    service_type: Optional[str],
    industry: Optional[str],
    as_of: date,
) -> list[Candidate]:
    """Return active partners in the market whose capacity is not at hard cap
    and whose specialisations cover the (industry, service_type) if provided.

    Server-side hard filters (per ARCHITECTURE §3.2):
    - status = 'active'
    - active_deals_count < hard_cap on `as_of`
    - specialization covers (industry, service_type) if both provided

    Rank signals (returned; the caller does the actual rank sort):
    - specialization_strength · churn_risk_score · partner_roi_pct + trend
    - response_latency_p50_hours · close_rate_signal
    """
    # For Week 2: use the seeded engagement rollup as the ROI + churn source
    # of truth. Match on latest available snapshot on/before as_of.
    where_svc = "AND ps.service_type = ?" if service_type else ""
    where_ind = "AND ps.industry = ?" if industry else ""
    params: list = [country, as_of.isoformat()]
    if service_type:
        params.append(service_type)
    if industry:
        params.append(industry)
    params.append(as_of.isoformat())      # for engagement snapshot
    params.append(as_of.isoformat())      # for capacity snapshot

    sql = f"""
        WITH latest_capacity AS (
            SELECT partner_id, active_deals_count, soft_cap, hard_cap,
                   is_under_soft_cap, is_under_hard_cap
            FROM main_staging.stg_partner_capacity
            WHERE snapshot_date = ?
        ),
        latest_engagement AS (
            SELECT partner_id,
                   partner_roi_pct_snapshot,
                   churn_risk_score,
                   response_latency_p50_hours,
                   net_monthly_value_snapshot
            FROM main_staging.stg_partner_engagement_daily
            WHERE snapshot_date = ?
        ),
        roi_trend AS (
            -- crude trend: compare latest ROI to 30-day-earlier value
            SELECT
                l.partner_id,
                CASE
                    WHEN l.partner_roi_pct_snapshot > COALESCE(e.partner_roi_pct_snapshot, 0) + 3 THEN 'up'
                    WHEN l.partner_roi_pct_snapshot < COALESCE(e.partner_roi_pct_snapshot, 0) - 3 THEN 'down'
                    ELSE 'flat'
                END AS trend
            FROM latest_engagement l
            LEFT JOIN main_staging.stg_partner_engagement_daily e
                ON e.partner_id = l.partner_id
                AND e.snapshot_date = (SELECT snapshot_date FROM latest_engagement LIMIT 1) - INTERVAL 30 DAY
        ),
        spec_agg AS (
            SELECT ps.partner_id,
                   MAX(ps.strength_score) AS specialization_strength
            FROM main_staging.stg_partner_specializations ps
            WHERE 1=1 {where_svc} {where_ind}
            GROUP BY ps.partner_id
        )
        SELECT
            p.partner_id,
            p.partner_name,
            p.country,
            COALESCE(c.is_under_soft_cap, TRUE) AS is_under_soft_cap,
            COALESCE(c.is_under_hard_cap, TRUE) AS is_under_hard_cap,
            CAST(COALESCE(c.active_deals_count, 0)              AS INT) AS active_deals_count,
            CAST(COALESCE(c.soft_cap, 10)                       AS INT) AS soft_cap,
            CAST(COALESCE(c.hard_cap, 12)                       AS INT) AS hard_cap,
            CAST(COALESCE(s.specialization_strength, 50.0)      AS DOUBLE) AS specialization_strength,
            CAST(COALESCE(s.specialization_strength, 50.0)      AS DOUBLE) AS close_rate_signal,
            CAST(COALESCE(e.partner_roi_pct_snapshot, 0.0)      AS DOUBLE) AS partner_roi_pct,
            COALESCE(t.trend, 'flat')                                     AS partner_roi_trend,
            CAST(COALESCE(e.net_monthly_value_snapshot, 0.0)    AS DOUBLE) AS net_monthly_value,
            CAST(COALESCE(e.churn_risk_score, 0.0)              AS DOUBLE) AS churn_risk_score,
            CAST(COALESCE(e.response_latency_p50_hours, 24.0)   AS DOUBLE) AS response_latency_p50_hours,
            p.status                                                       AS partner_status
        FROM main_staging.stg_partners p
        LEFT JOIN latest_capacity c USING (partner_id)
        LEFT JOIN latest_engagement e USING (partner_id)
        LEFT JOIN roi_trend t USING (partner_id)
        LEFT JOIN spec_agg s USING (partner_id)
        WHERE p.country = ?
          AND p.status = 'active'
          AND COALESCE(c.is_under_hard_cap, TRUE) = TRUE
        ORDER BY p.partner_id
    """
    # Note the params order: service, industry, latest_engagement date, latest_capacity date, country
    # But WHERE is at the end — DuckDB positional. Let me reorder:
    ordered_params: list = []
    ordered_params.append(as_of.isoformat())        # latest_capacity snapshot_date
    ordered_params.append(as_of.isoformat())        # latest_engagement snapshot_date
    if service_type:
        ordered_params.append(service_type)
    if industry:
        ordered_params.append(industry)
    ordered_params.append(country)                  # p.country

    with _connect(warehouse_path) as con:
        rows = con.execute(sql, ordered_params).fetchall()
        columns = [d[0] for d in con.description]

    return [Candidate(**dict(zip(columns, r))) for r in rows]


def list_dormant_partners(
    warehouse_path: Path,
    *,
    country: str,
    service_type: Optional[str],
    industry: Optional[str],
) -> list[dict]:
    """Partners with status='inactive' that would otherwise match — used from
    the manager console (§7.1 Q5) to surface reactivation candidates."""
    where_svc = "AND ps.service_type = ?" if service_type else ""
    where_ind = "AND ps.industry = ?" if industry else ""
    # Positional params MUST follow the order placeholders appear in the SQL:
    # spec_agg's filters (service, industry) come before the outer country.
    params: list = []
    if service_type:
        params.append(service_type)
    if industry:
        params.append(industry)
    params.append(country)

    sql = f"""
        WITH spec_agg AS (
            SELECT ps.partner_id,
                   MAX(ps.strength_score) AS specialization_strength
            FROM main_staging.stg_partner_specializations ps
            WHERE 1=1 {where_svc} {where_ind}
            GROUP BY ps.partner_id
        )
        SELECT
            p.partner_id,
            p.partner_name,
            p.country,
            p.partner_type,
            COALESCE(s.specialization_strength, 50.0) AS specialization_strength
        FROM main_staging.stg_partners p
        LEFT JOIN spec_agg s USING (partner_id)
        WHERE p.country = ? AND p.status = 'inactive'
        ORDER BY specialization_strength DESC
    """
    with _connect(warehouse_path) as con:
        rows = con.execute(sql, params).fetchall()
        columns = [d[0] for d in con.description]
    return [dict(zip(columns, r)) for r in rows]


def get_partner_engagement(
    warehouse_path: Path,
    *,
    partner_id: str,
    window_days: int,
    as_of: date,
) -> dict:
    """Roll-up of the partner's recent engagement — used by the Monitor
    Agent (Week 5) and the manager console (§3.5)."""
    sql = """
        SELECT
            partner_id,
            COUNT(*)                            AS days_observed,
            AVG(response_latency_p50_hours)     AS avg_p50_hours,
            AVG(partner_roi_pct_snapshot)       AS avg_roi_pct,
            AVG(churn_risk_score)               AS avg_churn_risk,
            MAX(churn_risk_score)               AS max_churn_risk,
            SUM(accept_count)                   AS total_accepts,
            SUM(decline_count)                  AS total_declines,
            SUM(no_response_count)              AS total_no_responses,
            SUM(cancellation_count)             AS total_cancellations
        FROM main_staging.stg_partner_engagement_daily
        WHERE partner_id = ?
          AND snapshot_date > (?::DATE - CAST(? AS INT) * INTERVAL 1 DAY)
          AND snapshot_date <= ?::DATE
        GROUP BY partner_id
    """
    with _connect(warehouse_path) as con:
        row = con.execute(sql, [partner_id, as_of.isoformat(), window_days,
                                as_of.isoformat()]).fetchone()
        columns = [d[0] for d in con.description]
    return dict(zip(columns, row)) if row else {}


def get_referral_lead_by_id(
    warehouse_path: Path, referral_lead_id: str
) -> Optional[dict]:
    """Load a seeded referral lead for testing / graph replay."""
    with _connect(warehouse_path) as con:
        row = con.execute(
            "SELECT * FROM main_staging.stg_referral_leads WHERE referral_lead_id = ?",
            [referral_lead_id],
        ).fetchone()
        columns = [d[0] for d in con.description] if row else []
    return dict(zip(columns, row)) if row else None


def get_referring_partner_engagement(
    warehouse_path: Path,
    *,
    referring_partner_id: str,
    window_days: int,
    as_of: date,
) -> dict:
    """Ambassador-side rollup — symmetrical to get_partner_engagement."""
    sql = """
        SELECT
            referring_partner_id,
            COUNT(*)                              AS days_observed,
            SUM(leads_sent_count)                 AS total_leads_sent,
            AVG(conversion_rate_pct)              AS avg_conversion_rate_pct,
            AVG(avg_deal_size)                    AS avg_deal_size,
            AVG(cancellation_rate_pct)            AS avg_cancellation_rate_pct,
            AVG(inquiry_response_latency_hours)   AS avg_response_latency_hours,
            AVG(churn_risk_score)                 AS avg_churn_risk,
            MAX(churn_risk_score)                 AS max_churn_risk
        FROM main_staging.stg_referring_partner_engagement_daily
        WHERE referring_partner_id = ?
          AND snapshot_date > (?::DATE - CAST(? AS INT) * INTERVAL 1 DAY)
          AND snapshot_date <= ?::DATE
        GROUP BY referring_partner_id
    """
    with _connect(warehouse_path) as con:
        row = con.execute(sql, [referring_partner_id, as_of.isoformat(),
                                window_days, as_of.isoformat()]).fetchone()
        columns = [d[0] for d in con.description] if row else []
    return dict(zip(columns, row)) if row else {}


def find_referral_lead(
    warehouse_path: Path,
    *,
    country: str | None = None,
    status: str | None = None,
    limit: int = 1,
) -> list[dict]:
    """Find seeded referral leads by criteria — used by the dry-run CLI and
    tests to pick a deterministic subject (ordered by id)."""
    clauses, params = [], []
    if country:
        clauses.append("country = ?")
        params.append(country)
    if status:
        clauses.append("referral_status = ?")
        params.append(status)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = f"""
        SELECT * FROM main_staging.stg_referral_leads
        {where}
        ORDER BY referral_lead_id
        LIMIT {int(limit)}
    """
    with _connect(warehouse_path) as con:
        rows = con.execute(sql, params).fetchall()
        columns = [d[0] for d in con.description]
    return [dict(zip(columns, r)) for r in rows]
