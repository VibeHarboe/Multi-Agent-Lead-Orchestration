"""Decision-dashboard data layer (§3.5 / §9).

Everything the manager console renders is computed HERE, UI-free — so the
Week 6 exit gate ("a paused lead resolves through the console with the
dashboard rendering real partner/market/financial metrics") is testable with
pytest, and `app/console.py` stays a thin Streamlit skin.

Four dashboard sections + the paused-leads listing:
  partner_health     — engagement + ROI + capacity per candidate/dormant partner
  market_health      — active-under-cap count, 7d avg util, 30d conversion,
                       30d SLA breaches for the lead's market
  financial_context  — the market's NordLedger financials (active/churned MRR,
                       overdue rate) straight from the shared core tables the
                       Self-Querying BI Agent also reads
  lead_vs_book       — this lead's deal size vs the referring partner's book
                       and the market average
  list_paused_leads  — every thread in the checkpoint DB with a pending
                       interrupt, payload included
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

import duckdb


def _connect(warehouse_path: Path):
    return duckdb.connect(str(warehouse_path))


# ── partner health ──────────────────────────────────────────────────────────

def partner_health(warehouse_path: Path, partner_id: str, *,
                   as_of: date, window_days: int = 30) -> dict:
    """The per-partner card: engagement, ROI (+trend vs the prior 30 days),
    capacity. The window is deliberately 30 days — a health card shows
    *current* condition; a 90-day average would dilute a December collapse
    with a healthy October (exactly what happened to P010 in testing)."""
    with _connect(warehouse_path) as con:
        row = con.execute("""
            SELECT AVG(response_latency_p50_hours), AVG(partner_roi_pct_snapshot),
                   AVG(net_monthly_value_snapshot), MAX(churn_risk_score),
                   SUM(accept_count), SUM(decline_count), SUM(no_response_count),
                   SUM(cancellation_count)
            FROM main_staging.stg_partner_engagement_daily
            WHERE partner_id = ?
              AND snapshot_date > (?::DATE - CAST(? AS INT) * INTERVAL 1 DAY)
              AND snapshot_date <= ?::DATE
        """, [partner_id, as_of.isoformat(), window_days,
              as_of.isoformat()]).fetchone()
        prior = con.execute("""
            SELECT AVG(partner_roi_pct_snapshot)
            FROM main_staging.stg_partner_engagement_daily
            WHERE partner_id = ?
              AND snapshot_date > (?::DATE - 60 * INTERVAL 1 DAY)
              AND snapshot_date <= (?::DATE - 30 * INTERVAL 1 DAY)
        """, [partner_id, as_of.isoformat(), as_of.isoformat()]).fetchone()
        cap = con.execute("""
            SELECT active_deals_count, soft_cap, hard_cap
            FROM main_staging.stg_partner_capacity
            WHERE partner_id = ? AND snapshot_date = ?
        """, [partner_id, as_of.isoformat()]).fetchone()
        meta = con.execute(
            "SELECT partner_name, country, status FROM main_staging.stg_partners "
            "WHERE partner_id = ?", [partner_id]).fetchone()

    if row is None or row[0] is None:
        return {"partner_id": partner_id, "available": False}
    roi_now = float(row[1])
    roi_prior = float(prior[0]) if prior and prior[0] is not None else roi_now
    return {
        "partner_id": partner_id, "available": True,
        "partner_name": meta[0] if meta else partner_id,
        "country": meta[1] if meta else "?",
        "status": meta[2] if meta else "?",
        "avg_p50_hours": round(float(row[0]), 1),
        "avg_roi_pct": round(roi_now, 1),
        "roi_trend": ("up" if roi_now > roi_prior + 1 else
                      "down" if roi_now < roi_prior - 1 else "flat"),
        "avg_nmv": round(float(row[2]), 0),
        "max_churn_risk": round(float(row[3]), 0),
        "accepts": int(row[4]), "declines": int(row[5]),
        "no_responses": int(row[6]), "cancellations": int(row[7]),
        "active_deals": int(cap[0]) if cap else None,
        "soft_cap": int(cap[1]) if cap else None,
        "hard_cap": int(cap[2]) if cap else None,
    }


# ── market health ───────────────────────────────────────────────────────────

def market_health(warehouse_path: Path, country: str, *, as_of: date) -> dict:
    d7 = (as_of - timedelta(days=7)).isoformat()
    d30 = (as_of - timedelta(days=30)).isoformat()
    with _connect(warehouse_path) as con:
        under_cap = con.execute("""
            SELECT COUNT(*) FROM main_staging.stg_partner_capacity c
            JOIN main_staging.stg_partners p USING (partner_id)
            WHERE p.country = ? AND p.status = 'active'
              AND c.snapshot_date = ? AND c.is_under_hard_cap
        """, [country, as_of.isoformat()]).fetchone()[0]
        total_active = con.execute(
            "SELECT COUNT(*) FROM main_staging.stg_partners "
            "WHERE country = ? AND status = 'active'", [country]).fetchone()[0]
        util = con.execute("""
            SELECT AVG(c.utilization_pct)
            FROM main_staging.stg_partner_capacity c
            JOIN main_staging.stg_partners p USING (partner_id)
            WHERE p.country = ? AND c.snapshot_date > ? AND c.snapshot_date <= ?
        """, [country, d7, as_of.isoformat()]).fetchone()[0]
        conv = con.execute("""
            SELECT COUNT(*) FILTER (WHERE referral_status = 'booked') * 100.0
                   / NULLIF(COUNT(*), 0)
            FROM main_staging.stg_referral_leads
            WHERE country = ? AND referred_at > ? AND referred_at <= ?
        """, [country, d30, as_of.isoformat()]).fetchone()[0]
        breaches = con.execute("""
            SELECT COUNT(*) FROM main_staging.stg_referral_leads
            WHERE country = ? AND referral_status = 'booked'
              AND resolved_at IS NULL AND booked_at IS NOT NULL
              AND date_diff('day', booked_at, ?::DATE) > 30
        """, [country, as_of.isoformat()]).fetchone()[0]
    return {
        "country": country,
        "partners_under_cap": int(under_cap),
        "partners_active_total": int(total_active),
        "avg_util_7d_pct": round(float(util), 1) if util is not None else None,
        "conversion_rate_30d_pct": round(float(conv), 1) if conv is not None else None,
        "sla_breaches_open": int(breaches),
    }


# ── financial context (the shared NordLedger core) ─────────────────────────

def financial_context(warehouse_path: Path, country: str, *,
                      as_of: date) -> dict:
    """The market's financial picture from the SAME core tables the
    Self-Querying BI Agent's semantic layer is built on (Q1/Q18 cohesion)."""
    with _connect(warehouse_path) as con:
        mrr = con.execute("""
            SELECT SUM(mrr) FILTER (WHERE is_active),
                   SUM(mrr) FILTER (WHERE is_churned)
            FROM main_staging.stg_subscriptions WHERE country = ?
        """, [country]).fetchone()
        overdue = con.execute("""
            SELECT COUNT(*) FILTER (WHERE status = 'overdue') * 100.0
                   / NULLIF(COUNT(*), 0)
            FROM main_staging.stg_invoices WHERE country = ?
        """, [country]).fetchone()[0]
        churn = con.execute("""
            SELECT COUNT(*) FILTER (WHERE is_churned) * 100.0
                   / NULLIF(COUNT(*), 0)
            FROM main_staging.stg_subscriptions WHERE country = ?
        """, [country]).fetchone()[0]
    return {
        "country": country,
        "active_mrr": float(mrr[0] or 0),
        "churned_mrr": float(mrr[1] or 0),
        "churn_rate_pct": round(float(churn or 0), 1),
        "overdue_rate_pct": round(float(overdue or 0), 1),
    }


# ── this lead vs the book ───────────────────────────────────────────────────

def lead_vs_book(warehouse_path: Path, *, referral_lead_id: str) -> dict:
    with _connect(warehouse_path) as con:
        row = con.execute("""
            SELECT deal_size_estimate, referring_partner_id, country
            FROM main_staging.stg_referral_leads WHERE referral_lead_id = ?
        """, [referral_lead_id]).fetchone()
        if row is None:
            return {"referral_lead_id": referral_lead_id, "available": False}
        deal, ref_id, country = (float(row[0] or 0), row[1], row[2])
        book_avg = con.execute("""
            SELECT AVG(deal_size_estimate) FROM main_staging.stg_referral_leads
            WHERE referring_partner_id = ? AND deal_size_estimate > 0
        """, [ref_id]).fetchone()[0]
        market_avg = con.execute("""
            SELECT AVG(deal_size_estimate) FROM main_staging.stg_referral_leads
            WHERE country = ? AND deal_size_estimate > 0
        """, [country]).fetchone()[0]
    book_avg = float(book_avg or 0)
    market_avg = float(market_avg or 0)
    return {
        "referral_lead_id": referral_lead_id, "available": True,
        "deal_size": deal,
        "referring_partner_id": ref_id,
        "book_avg": round(book_avg, 0),
        "market_avg": round(market_avg, 0),
        "vs_book_pct": (round((deal / book_avg - 1) * 100, 0)
                        if book_avg else None),
        "vs_market_pct": (round((deal / market_avg - 1) * 100, 0)
                          if market_avg else None),
    }


# ── paused-leads listing (from the checkpoint DB) ──────────────────────────

def list_paused_lead_ids(checkpoint_dir: Path) -> list[str]:
    """Distinct thread ids in the checkpointer — candidates for the paused
    list. (Whether each is actually paused is answered by get_paused.)"""
    db = Path(checkpoint_dir) / "leads.sqlite"
    if not db.exists():
        return []
    with sqlite3.connect(str(db)) as con:
        rows = con.execute(
            "SELECT DISTINCT thread_id FROM checkpoints").fetchall()
    return sorted(r[0] for r in rows)


def list_paused_leads(stack, config, *, as_of: date,
                      checkpoint_dir: Path) -> list[dict]:
    """Every lead with a pending interrupt, payload included — the console's
    sidebar. Uses the same open_graph/get_paused machinery as the CLI."""
    from .graph import get_paused, open_graph

    paused = []
    thread_ids = list_paused_lead_ids(checkpoint_dir)
    if not thread_ids:
        return paused
    with open_graph(stack, config, as_of=as_of,
                    checkpoint_dir=checkpoint_dir) as graph:
        for tid in thread_ids:
            payload = get_paused(graph, tid)
            if payload is not None:
                paused.append(payload)
    return paused


# ── the full dashboard payload for one paused lead ─────────────────────────

def build_dashboard(warehouse_path: Path, pause_payload: dict, *,
                    as_of: date) -> dict:
    """Everything §3.5 says the manager sees — assembled from the pause
    payload + live warehouse reads. This is the exit-gate artifact."""
    lead_id = pause_payload["referral_lead_id"]
    country = None
    for f in pause_payload["intake"]["fields"]:
        if f["field"] == "country":
            country = f["value"]
    candidate_ids = [c["partner_id"]
                     for c in pause_payload["matching"]["candidates"]]
    dormant_ids = [d["partner_id"]
                   for d in pause_payload.get("dormant_partners", [])]

    dashboard = {
        "referral_lead_id": lead_id,
        "destination": pause_payload["destination"],
        "hitl_reason": pause_payload["hitl_reason"],
        "partners": {pid: partner_health(warehouse_path, pid, as_of=as_of)
                     for pid in candidate_ids + dormant_ids},
        "lead_vs_book": lead_vs_book(warehouse_path,
                                     referral_lead_id=lead_id),
    }
    if country:
        dashboard["market"] = market_health(warehouse_path, country,
                                            as_of=as_of)
        dashboard["financial"] = financial_context(warehouse_path, country,
                                                   as_of=as_of)
    return dashboard
