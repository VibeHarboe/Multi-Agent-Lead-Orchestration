"""Matching Agent — Station 2.

Rebuild 3 §3.2: Given a classified LeadState, rank fulfillment partners
against four factors and return top-K with a written rationale per pick.

Week 2 (this file): pure-function ranking + a fixture data path that reads
from the DuckDB warehouse directly (`src/data/warehouse.py`). Week 3: replaced
by an MCP tool call (`partner_capacity_mcp.list_candidates`), but the ranking
logic here stays identical.

Hard rules enforced server-side in `warehouse.list_candidates`:
- inactive partners excluded
- over-hard-cap partners excluded

Ranking factors (all soft — influence order, don't hard-filter):
- Historical close rate for the (industry × service_type) combination
- Partner ROI % with trend
- Churn-risk score (partners the Monitor is watching drop in ranking)
- Response-latency percentile
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Optional

from ..config import Config
from ..data.warehouse import Candidate, list_candidates
from ..state import LeadState, MatchedCandidate


def _composite_score(c: Candidate) -> float:
    """Combined ranking score. Higher = better.

    - specialization_strength (0-100): how well the partner covers this
      (industry × service_type) combination
    - partner_roi_pct: soft signal; +30 ROI beats -5 ROI at same skill
    - churn_risk_score: 100 - risk (partners at risk drop)
    - response_latency: lower latency scores better (converted via inverse)
    """
    roi_component = max(0.0, min(100.0, 50.0 + c.partner_roi_pct))          # -50..+50 -> 0..100
    churn_component = 100.0 - c.churn_risk_score
    # latency: 0h -> 100 pts, 24h -> 50 pts, 48h -> 0 pts (linear)
    latency_component = max(0.0, min(100.0, 100.0 - (c.response_latency_p50_hours * (100.0 / 48.0))))

    return round(
        0.40 * c.specialization_strength +
        0.30 * roi_component +
        0.20 * churn_component +
        0.10 * latency_component,
        2,
    )


def _build_rationale(c: Candidate, score: float, rank: int) -> str:
    """One-line explanation the manager / Negotiation Agent can read."""
    parts = [f"#{rank} · score {score:.1f}"]
    parts.append(f"specialisation {c.specialization_strength:.0f}/100")
    trend_word = {"up": "trending up", "flat": "stable", "down": "trending down"}[c.partner_roi_trend]
    parts.append(f"ROI {c.partner_roi_pct:+.1f}% ({trend_word})")
    if c.churn_risk_score >= 65:
        parts.append(f"⚠ churn-risk {c.churn_risk_score:.0f}")
    else:
        parts.append(f"churn-risk {c.churn_risk_score:.0f}")
    parts.append(f"resp {c.response_latency_p50_hours:.1f}h")
    parts.append(f"cap {c.active_deals_count}/{c.hard_cap}")
    return " · ".join(parts)


def rank_candidates(
    candidates: list[Candidate],
    *,
    top_k: int,
) -> list[MatchedCandidate]:
    """Pure function — given raw candidates from the data layer, produce a
    ranked top-K MatchedCandidate list.

    Filters out anyone flagged as *not* under hard cap (defence-in-depth on
    top of the server-side filter) and inactive partners.
    """
    eligible = [
        c for c in candidates
        if c.is_under_hard_cap and c.partner_status == "active"
    ]
    scored = [(c, _composite_score(c)) for c in eligible]
    scored.sort(key=lambda cs: cs[1], reverse=True)

    return [
        MatchedCandidate(
            partner_id=c.partner_id,
            rank=i + 1,
            close_rate_signal=c.close_rate_signal,
            partner_roi_pct=c.partner_roi_pct,
            partner_roi_trend=c.partner_roi_trend,
            churn_risk_score=c.churn_risk_score,
            response_latency_p50_hours=c.response_latency_p50_hours,
            composite_score=score,
            rationale=_build_rationale(c, score, i + 1),
        )
        for i, (c, score) in enumerate(scored[:top_k])
    ]


def _guard_essentials(lead: LeadState) -> LeadState | None:
    """Shared pre-check: Matching can't run without country + service_type."""
    missing = lead.missing_essential_fields()
    if missing:
        return lead.model_copy(update={
            "status": "hitl_intake",
            "hitl_reason": f"cannot match — missing {', '.join(missing)}",
        })
    return None


def _apply_ranking(lead: LeadState, raw: list[Candidate], *,
                   top_k: int, as_of: date) -> LeadState:
    """Shared post-step: rank raw candidates and update the LeadState.
    Used identically by the fixture path and the MCP path — so both paths
    are guaranteed to produce the same result on the same inputs."""
    ranked = rank_candidates(raw, top_k=top_k)
    if not ranked:
        return lead.model_copy(update={
            "status": "hitl_capacity",
            "hitl_reason": (
                f"no active partner under hard-cap in {lead.country} "
                f"for {lead.service_type} on {as_of.isoformat()} — surface "
                f"reactivation candidates via list_dormant_partners"
            ),
        })
    return lead.model_copy(update={
        "matched_candidates": ranked,
        "status": "matched",
    })


def match(
    lead: LeadState,
    *,
    warehouse_path: Path,
    top_k: int,
    as_of: date,
) -> LeadState:
    """Fixture-path matching: reads candidates straight from the DuckDB layer.
    Kept for fast deterministic tests; the runtime graph uses match_mcp."""
    guarded = _guard_essentials(lead)
    if guarded is not None:
        return guarded
    assert lead.country and lead.service_type       # narrowed by guard

    raw = list_candidates(
        warehouse_path=warehouse_path,
        country=lead.country,
        service_type=lead.service_type,
        industry=lead.industry,
        as_of=as_of,
    )
    return _apply_ranking(lead, raw, top_k=top_k, as_of=as_of)


def match_mcp(
    lead: LeadState,
    *,
    stack,                    # MCPStack — untyped to avoid an import cycle
    top_k: int,
    as_of: date,
    exclude_partner_ids: list[str] | None = None,
) -> LeadState:
    """MCP-path matching: candidates come through partner_capacity_mcp's
    list_candidates tool — the same route the production graph uses. The
    ranking itself is the same pure function as the fixture path, so the two
    paths produce identical results on identical warehouse state (asserted
    by the Week 3 parity test).

    `exclude_partner_ids` is the Monitor's re-injection lever (§3.4a): a deal
    re-entering the graph after an SLA breach must never be re-routed to the
    partner that breached it."""
    guarded = _guard_essentials(lead)
    if guarded is not None:
        return guarded
    assert lead.country and lead.service_type

    payload = stack.call(
        "partner_capacity", "list_candidates",
        country=lead.country,
        service_type=lead.service_type,
        industry=lead.industry,
        as_of=as_of.isoformat(),
    )
    excluded = set(exclude_partner_ids or ())
    raw = [
        Candidate(**{k: v for k, v in c.items()
                     if k not in ("live_holds", "headroom")})
        for c in payload["candidates"]
        if c["partner_id"] not in excluded
    ]
    return _apply_ranking(lead, raw, top_k=top_k, as_of=as_of)


def match_with_config(lead: LeadState, config: Config) -> LeadState:
    """Convenience: read all knobs from Config."""
    as_of = config.as_of_date or date.today()
    return match(
        lead,
        warehouse_path=config.duckdb_path,
        top_k=config.matching_top_k,
        as_of=as_of,
    )
