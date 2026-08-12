"""Monitor Agent — Station 4. The scheduled watcher (§3.4).

Three responsibilities, all deterministic (no LLM in the sweep loop —
narrative text in the weekly report is the only pluggable LLM surface):

  (a) SLA sweep (Q8): booked deals without resolution within 30 days are
      severity-3 breaches. Each produces an escalation record, a CRM note,
      and a *re-injection order* — the lead re-enters the graph as a fresh
      intake with the breaching partner excluded (§3.4a dynamic post-booking
      re-routing). The accept-SLA (48h) needs an accept-timestamp column that
      the simulation does not model; it is documented as the production hook.

  (b) Partner interventions (Q9): three v1 rules over the engagement rollups:
        churn_risk      — churn_risk_score ≥ 65 sustained ≥ 7 consecutive
                          days (fulfillment + referring, symmetrical)
        roi_decline     — partner_roi_pct < 0 sustained ≥ 7 consecutive days
                          (catches the unprofitable-but-friendly partner whose
                          engagement looks healthy)
        volume_collapse — referring partner's 28-day lead volume down > 60%
                          vs the prior 28 days (catches the quiet-fade Billy
                          long before any score threshold trips)

  (c) Cross-market imbalance (Q10): avg market utilisation ≥ 90% for 7
      consecutive days → saturated; ≤ 20% for 14 consecutive days → starved.

  (d) Weekly per-partner report (Q12): deals, latency vs peers, SLA breaches,
      ROI + NMV + churn trends, and a narrative line per partner — Template
      narrator by default, Claude narrator opt-in.

Schedules (Q11/Q12: daily 06:00 CET · Mondays 08:00 CET) are the cron
contract — the CLI exposes `--scan` and `--weekly-report` for the scheduler
to invoke; nothing in here sleeps or self-schedules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Protocol

import duckdb

from ..config import Config


# ── result shapes ───────────────────────────────────────────────────────────

@dataclass
class SlaBreach:
    referral_lead_id: str
    country: str
    booked_at: date
    days_since_booking: int
    breaching_partner_id: str | None
    severity: int = 3


@dataclass
class Intervention:
    kind: str                    # churn_risk | roi_decline | volume_collapse
    partner_id: str
    partner_side: str            # fulfillment | referring
    detail: str
    metric_now: float
    threshold: float
    sustained_days: int = 0


@dataclass
class MarketAlert:
    kind: str                    # saturated | starved
    country: str
    avg_util_pct: float
    consecutive_days: int


@dataclass
class SweepReport:
    as_of: date
    sla_breaches: list[SlaBreach] = field(default_factory=list)
    interventions: list[Intervention] = field(default_factory=list)
    market_alerts: list[MarketAlert] = field(default_factory=list)

    @property
    def has_findings(self) -> bool:
        return bool(self.sla_breaches or self.interventions or self.market_alerts)


# ── the sweep ───────────────────────────────────────────────────────────────

def _connect(warehouse_path: Path):
    return duckdb.connect(str(warehouse_path))


def sweep_sla(warehouse_path: Path, *, as_of: date,
              close_sla_days: int) -> list[SlaBreach]:
    """(a) Close-SLA: booked, unresolved, older than the SLA window. The
    breaching partner is read from the deal's booked event."""
    sql = """
        SELECT rl.referral_lead_id, rl.country, rl.booked_at,
               date_diff('day', rl.booked_at, ?::DATE) AS age_days,
               (SELECT de.partner_id FROM main_staging.stg_deal_events de
                WHERE de.referral_lead_id = rl.referral_lead_id
                  AND de.event_type = 'booked'
                ORDER BY de.event_at DESC LIMIT 1)     AS breaching_partner
        FROM main_staging.stg_referral_leads rl
        WHERE rl.referral_status = 'booked'
          AND rl.resolved_at IS NULL
          AND rl.booked_at IS NOT NULL
          AND date_diff('day', rl.booked_at, ?::DATE) > ?
        ORDER BY rl.referral_lead_id
    """
    with _connect(warehouse_path) as con:
        rows = con.execute(sql, [as_of.isoformat(), as_of.isoformat(),
                                 close_sla_days]).fetchall()
    return [SlaBreach(referral_lead_id=r[0], country=r[1],
                      booked_at=r[2], days_since_booking=int(r[3]),
                      breaching_partner_id=r[4] or None)
            for r in rows]


def _sustained_streak(values: list[tuple[date, float]],
                      predicate, as_of: date) -> int:
    """Consecutive days (ending at the latest snapshot ≤ as_of) where
    predicate(value) holds."""
    ordered = sorted((d, v) for d, v in values if d <= as_of)
    streak = 0
    for _, v in reversed(ordered):
        if predicate(v):
            streak += 1
        else:
            break
    return streak


# Jitter tolerance for "sustained" (Q9): a partner oscillating around the
# threshold (65, 60, 70, 65…) IS in sustained trouble — a strict
# consecutive-day streak is brittle to daily noise, the same lesson the
# BI Agent's modified z-score learned about MAD=0 on discrete series. The
# v1 reading of "sustained N days": predicate holds on ≥ N of the last
# N + _SUSTAINED_JITTER_TOLERANCE daily snapshots.
_SUSTAINED_JITTER_TOLERANCE = 3


def _sustained_days_within(values: list[tuple[date, float]],
                           predicate, as_of: date,
                           sustained_days: int) -> int:
    """How many of the last (sustained_days + tolerance) snapshots satisfy
    the predicate. Fire when the return value ≥ sustained_days."""
    window = sustained_days + _SUSTAINED_JITTER_TOLERANCE
    ordered = sorted((d, v) for d, v in values if d <= as_of)
    tail = ordered[-window:]
    return sum(1 for _, v in tail if predicate(v))


def sweep_interventions(warehouse_path: Path, *, as_of: date,
                        churn_threshold: float,
                        sustained_days: int) -> list[Intervention]:
    """(b) The three intervention rules over both partner sides."""
    out: list[Intervention] = []
    window_start = (as_of - timedelta(days=120)).isoformat()

    with _connect(warehouse_path) as con:
        # fulfillment side: churn_risk + roi_decline
        rows = con.execute(
            "SELECT partner_id, snapshot_date, churn_risk_score, "
            "       partner_roi_pct_snapshot "
            "FROM main_staging.stg_partner_engagement_daily "
            "WHERE snapshot_date BETWEEN ? AND ? ORDER BY partner_id, snapshot_date",
            [window_start, as_of.isoformat()]).fetchall()
        by_partner: dict[str, list] = {}
        for pid, d, risk, roi in rows:
            by_partner.setdefault(pid, []).append((d, float(risk), float(roi)))

        for pid, series in by_partner.items():
            risk_series = [(d, r) for d, r, _ in series]
            roi_series = [(d, roi) for d, _, roi in series]
            latest_risk = risk_series[-1][1]
            latest_roi = roi_series[-1][1]

            churn_days = _sustained_days_within(
                risk_series, lambda v: v >= churn_threshold, as_of,
                sustained_days)
            if churn_days >= sustained_days:
                out.append(Intervention(
                    kind="churn_risk", partner_id=pid,
                    partner_side="fulfillment",
                    detail=(f"churn-risk {latest_risk:.0f} ≥ {churn_threshold:.0f} "
                            f"on {churn_days} of the last "
                            f"{sustained_days + _SUSTAINED_JITTER_TOLERANCE} days "
                            f"— propose check-in; ranking already deprioritises"),
                    metric_now=latest_risk, threshold=churn_threshold,
                    sustained_days=churn_days))

            roi_days = _sustained_days_within(
                roi_series, lambda v: v < 0.0, as_of, sustained_days)
            if roi_days >= sustained_days:
                out.append(Intervention(
                    kind="roi_decline", partner_id=pid,
                    partner_side="fulfillment",
                    detail=(f"ROI {latest_roi:+.1f}% negative on {roi_days} of "
                            f"the last {sustained_days + _SUSTAINED_JITTER_TOLERANCE} "
                            f"days despite healthy engagement — review "
                            f"commercial terms"),
                    metric_now=latest_roi, threshold=0.0,
                    sustained_days=roi_days))

        # referring side: churn_risk (symmetrical) + volume_collapse
        rows = con.execute(
            "SELECT referring_partner_id, snapshot_date, churn_risk_score, "
            "       leads_sent_count "
            "FROM main_staging.stg_referring_partner_engagement_daily "
            "WHERE snapshot_date BETWEEN ? AND ? "
            "ORDER BY referring_partner_id, snapshot_date",
            [window_start, as_of.isoformat()]).fetchall()
        by_ref: dict[str, list] = {}
        for rid, d, risk, sent in rows:
            by_ref.setdefault(rid, []).append((d, float(risk), int(sent)))

        for rid, series in by_ref.items():
            risk_series = [(d, r) for d, r, _ in series]
            churn_days = _sustained_days_within(
                risk_series, lambda v: v >= churn_threshold, as_of,
                sustained_days)
            if churn_days >= sustained_days:
                out.append(Intervention(
                    kind="churn_risk", partner_id=rid,
                    partner_side="referring",
                    detail=(f"ambassador churn-risk ≥ {churn_threshold:.0f} on "
                            f"{churn_days} of the last "
                            f"{sustained_days + _SUSTAINED_JITTER_TOLERANCE} days"),
                    metric_now=risk_series[-1][1], threshold=churn_threshold,
                    sustained_days=churn_days))

            recent = sum(s for d, _, s in series
                         if as_of - timedelta(days=28) < d <= as_of)
            prior = sum(s for d, _, s in series
                        if as_of - timedelta(days=56) < d <= as_of - timedelta(days=28))
            if prior >= 20 and recent < prior * 0.4:      # > 60% drop
                drop_pct = (1 - recent / prior) * 100
                out.append(Intervention(
                    kind="volume_collapse", partner_id=rid,
                    partner_side="referring",
                    detail=(f"lead volume collapsed {drop_pct:.0f}%: "
                            f"{prior} → {recent} referrals per 28d — the "
                            f"quiet-fade signal; check in with the ambassador"),
                    metric_now=float(recent), threshold=prior * 0.4))
    return out


def sweep_market_imbalance(warehouse_path: Path, *, as_of: date,
                           saturated_pct: float, saturated_days: int,
                           starved_pct: float, starved_days: int
                           ) -> list[MarketAlert]:
    """(c) Q10 — per-market daily avg utilisation, consecutive-day streaks."""
    window_start = (as_of - timedelta(days=45)).isoformat()
    sql = """
        SELECT p.country, c.snapshot_date,
               AVG(c.utilization_pct) AS util
        FROM main_staging.stg_partner_capacity c
        JOIN main_staging.stg_partners p USING (partner_id)
        WHERE c.snapshot_date BETWEEN ? AND ?
        GROUP BY 1, 2
        ORDER BY 1, 2
    """
    with _connect(warehouse_path) as con:
        rows = con.execute(sql, [window_start, as_of.isoformat()]).fetchall()
    by_market: dict[str, list] = {}
    for country, d, util in rows:
        by_market.setdefault(country, []).append((d, float(util)))

    alerts: list[MarketAlert] = []
    for country, series in by_market.items():
        latest = series[-1][1]
        sat = _sustained_streak(series, lambda v: v >= saturated_pct, as_of)
        if sat >= saturated_days:
            alerts.append(MarketAlert("saturated", country, round(latest, 1), sat))
        starv = _sustained_streak(series, lambda v: v <= starved_pct, as_of)
        if starv >= starved_days:
            alerts.append(MarketAlert("starved", country, round(latest, 1), starv))
    return alerts


def sweep(warehouse_path: Path, config: Config, *, as_of: date,
          recorder=None) -> SweepReport:
    """The daily sweep — all three responsibilities in one pass. With a
    recorder, each responsibility is its own span under a monitor.sweep
    trace (the §10 reference shape for scheduled runs)."""
    from contextlib import nullcontext

    trace_cm = (recorder.trace(f"monitor.sweep:{as_of.isoformat()}")
                if recorder else nullcontext())
    span = (recorder.span if recorder
            else (lambda name, **kw: nullcontext()))

    report = SweepReport(as_of=as_of)
    with trace_cm:
        with span("monitor.sla_sweep"):
            report.sla_breaches = sweep_sla(
                warehouse_path, as_of=as_of,
                close_sla_days=config.post_booking_close_sla_days)
        with span("monitor.interventions"):
            report.interventions = sweep_interventions(
                warehouse_path, as_of=as_of,
                churn_threshold=config.churn_risk_threshold,
                sustained_days=config.churn_risk_sustained_days)
        with span("monitor.market_imbalance"):
            report.market_alerts = sweep_market_imbalance(
                warehouse_path, as_of=as_of,
                saturated_pct=config.saturated_util_pct,
                saturated_days=config.saturated_consecutive_days,
                starved_pct=config.starved_util_pct,
                starved_days=config.starved_consecutive_days)
    return report


def escalate_breaches(report: SweepReport, stack) -> list[dict]:
    """Persist each SLA breach as a CRM note (deal_events, agent=monitor) so
    the audit spine shows the escalation even before re-injection runs."""
    results = []
    for b in report.sla_breaches:
        r = stack.call("crm", "attach_note",
                       referral_lead_id=b.referral_lead_id,
                       note=(f"SLA breach severity-{b.severity}: booked "
                             f"{b.days_since_booking}d ago "
                             f"({b.booked_at}) without resolution — "
                             f"re-injection ordered, excluding "
                             f"{b.breaching_partner_id or 'unknown partner'}"),
                       agent_name="monitor",
                       as_of=report.as_of.isoformat())
        results.append(r)
    return results


def post_digest(report: SweepReport, stack) -> dict:
    """The daily operational digest → Slack channel (rate-limit note: one
    digest per sweep; per-partner severity-3 dedup is handled by the sweep
    running daily, not per-event)."""
    lines = [f"Daily digest {report.as_of.isoformat()} — "
             f"{len(report.sla_breaches)} SLA breach(es), "
             f"{len(report.interventions)} intervention(s), "
             f"{len(report.market_alerts)} market alert(s)"]
    for b in report.sla_breaches:
        lines.append(f"• SLA: {b.referral_lead_id} ({b.country}) "
                     f"{b.days_since_booking}d unresolved — partner "
                     f"{b.breaching_partner_id}")
    for i in report.interventions:
        lines.append(f"• {i.kind}: {i.partner_id} ({i.partner_side}) — {i.detail}")
    for m in report.market_alerts:
        lines.append(f"• market {m.kind}: {m.country} at {m.avg_util_pct}% "
                     f"for {m.consecutive_days}d")
    return stack.call("slack", "post_to_channel",
                      channel="#nordledger-ops", text="\n".join(lines))


# ── weekly report (d) ───────────────────────────────────────────────────────

class Narrator(Protocol):
    def narrate(self, partner_row: dict, peers: list[dict]) -> str: ...


class TemplateNarrator:
    """Deterministic one-liner per partner — no LLM."""

    def narrate(self, row: dict, peers: list[dict]) -> str:
        peer_roi = (sum(p["avg_roi_pct"] for p in peers) / len(peers)) if peers else 0.0
        rel = "above" if row["avg_roi_pct"] >= peer_roi else "below"
        risk = ("elevated churn-risk" if row["max_churn_risk"] >= 65
                else "healthy engagement")
        return (f"{row['partner_id']}: ROI {row['avg_roi_pct']:+.1f}% "
                f"({rel} peer avg {peer_roi:+.1f}%), {risk}, "
                f"p50 response {row['avg_p50_hours']:.1f}h.")


class ClaudeNarrator:
    """LLM narrative — opt-in (llm-marked tests / live runs only)."""

    def __init__(self, config: Config):
        import anthropic                       # lazy on purpose
        self._client = anthropic.Anthropic(api_key=config.anthropic_api_key)
        self._model = config.planner_model

    def narrate(self, row: dict, peers: list[dict]) -> str:
        response = self._client.messages.create(
            model=self._model, max_tokens=200,
            system=("One crisp sentence summarising this partner's week vs "
                    "peers for an ops manager. No fluff."),
            messages=[{"role": "user",
                       "content": f"partner: {row}\npeers: {peers}"}])
        return "".join(b.text for b in response.content
                       if getattr(b, "type", None) == "text").strip()


def build_weekly_report(warehouse_path: Path, config: Config, *, as_of: date,
                        narrator: Narrator | None = None) -> dict:
    """Per-partner weekly rollup with ROI + churn trends and a narrative
    line. Rendered every Monday 08:00 CET by the scheduler; deterministic
    given (warehouse, as_of)."""
    narrator = narrator or TemplateNarrator()
    week_start = (as_of - timedelta(days=7)).isoformat()
    prior_start = (as_of - timedelta(days=14)).isoformat()

    sql = """
        WITH this_week AS (
            SELECT partner_id,
                   AVG(partner_roi_pct_snapshot)  AS avg_roi_pct,
                   AVG(net_monthly_value_snapshot) AS avg_nmv,
                   AVG(response_latency_p50_hours) AS avg_p50_hours,
                   MAX(churn_risk_score)           AS max_churn_risk,
                   SUM(accept_count)               AS accepts,
                   SUM(decline_count)              AS declines,
                   SUM(no_response_count)          AS no_responses,
                   SUM(cancellation_count)         AS cancellations
            FROM main_staging.stg_partner_engagement_daily
            WHERE snapshot_date > ? AND snapshot_date <= ?
            GROUP BY 1
        ),
        prior_week AS (
            SELECT partner_id,
                   AVG(partner_roi_pct_snapshot)  AS prior_roi_pct,
                   MAX(churn_risk_score)           AS prior_churn_risk
            FROM main_staging.stg_partner_engagement_daily
            WHERE snapshot_date > ? AND snapshot_date <= ?
            GROUP BY 1
        )
        SELECT t.partner_id,
               CAST(t.avg_roi_pct AS DOUBLE)      AS avg_roi_pct,
               CAST(t.avg_nmv AS DOUBLE)          AS avg_nmv,
               CAST(t.avg_p50_hours AS DOUBLE)    AS avg_p50_hours,
               CAST(t.max_churn_risk AS DOUBLE)   AS max_churn_risk,
               CAST(t.accepts AS INT)             AS accepts,
               CAST(t.declines AS INT)            AS declines,
               CAST(t.no_responses AS INT)        AS no_responses,
               CAST(t.cancellations AS INT)       AS cancellations,
               CAST(COALESCE(p.prior_roi_pct, t.avg_roi_pct) AS DOUBLE)
                                                  AS prior_roi_pct,
               CAST(COALESCE(p.prior_churn_risk, t.max_churn_risk) AS DOUBLE)
                                                  AS prior_churn_risk
        FROM this_week t
        LEFT JOIN prior_week p USING (partner_id)
        ORDER BY t.partner_id
    """
    with _connect(warehouse_path) as con:
        rows = con.execute(sql, [week_start, as_of.isoformat(),
                                 prior_start, week_start]).fetchall()
        cols = [d[0] for d in con.description]
    partner_rows = [dict(zip(cols, r)) for r in rows]

    for row in partner_rows:
        row["roi_trend"] = ("up" if row["avg_roi_pct"] > row["prior_roi_pct"] + 1
                            else "down" if row["avg_roi_pct"] < row["prior_roi_pct"] - 1
                            else "flat")
        row["churn_trend"] = ("worsening"
                              if row["max_churn_risk"] > row["prior_churn_risk"] + 5
                              else "improving"
                              if row["max_churn_risk"] < row["prior_churn_risk"] - 5
                              else "stable")
    for row in partner_rows:
        peers = [p for p in partner_rows if p["partner_id"] != row["partner_id"]]
        row["narrative"] = narrator.narrate(row, peers)

    return {"as_of": as_of.isoformat(), "week_start": week_start,
            "partners": partner_rows}


def post_weekly_report(report: dict, stack) -> dict:
    lines = [f"Weekly partner report — week ending {report['as_of']}"]
    for row in report["partners"]:
        lines.append(f"• {row['narrative']} "
                     f"(ROI trend {row['roi_trend']}, churn {row['churn_trend']}, "
                     f"{row['accepts']}A/{row['declines']}D/{row['no_responses']}N)")
    return stack.call("slack", "post_to_channel",
                      channel="#nordledger-weekly", text="\n".join(lines))
