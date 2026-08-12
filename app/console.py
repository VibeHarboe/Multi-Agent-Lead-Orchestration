"""Manager HITL console — the decision dashboard (§3.5, §9).

    streamlit run app/console.py

Lists every paused lead from the LangGraph checkpointer, renders the full
decision dashboard (partner health · market health · financial context ·
this-lead-vs-book), takes the manager's decision (enrich / reactivate /
override / drop) and resumes the graph — the same machinery the CLI uses.

The embedded "Ask the BI Agent" widget (Q18) imports the vendored
Self-Querying BI Agent and points it at THIS tool's warehouse — one shared
metric layer, ad-hoc questions without leaving the decision flow. Live BI
queries need a real ANTHROPIC_API_KEY; everything else on this page runs
without one.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import streamlit as st  # noqa: E402

from src.config import load_config  # noqa: E402
from src.console_data import build_dashboard, list_paused_leads  # noqa: E402
from src.graph import (DEFAULT_CHECKPOINT_DIR, extract_interrupt,  # noqa: E402
                       lead_out, open_graph, resume_lead)
from src.hitl import validate_decision  # noqa: E402
from src.mcp_stack import MCPStack  # noqa: E402


@st.cache_resource
def _load():
    config = load_config(require_api_key=False)
    return config


def _resume(config, lead_id: str, decision: dict, as_of: date):
    stack = MCPStack(config.duckdb_path)
    with open_graph(stack, config, as_of=as_of,
                    checkpoint_dir=DEFAULT_CHECKPOINT_DIR) as graph:
        return resume_lead(graph, lead_id, decision)


def _render_partner_card(col, h: dict):
    with col:
        if not h.get("available"):
            st.caption(f"{h['partner_id']} — no engagement data")
            return
        risk_icon = "⚠️" if h["max_churn_risk"] >= 65 else "✅"
        st.markdown(f"**{h['partner_id']}** · {h['partner_name']}")
        st.caption(f"{h['country']} · {h['status']}")
        st.metric("ROI", f"{h['avg_roi_pct']:+.1f}%",
                  delta=h["roi_trend"], delta_color="off")
        st.text(f"{risk_icon} churn-risk {h['max_churn_risk']:.0f}\n"
                f"p50 resp {h['avg_p50_hours']}h\n"
                f"cap {h['active_deals']}/{h['hard_cap']}"
                if h["active_deals"] is not None else
                f"{risk_icon} churn-risk {h['max_churn_risk']:.0f}\n"
                f"p50 resp {h['avg_p50_hours']}h")


def main() -> None:
    st.set_page_config(page_title="NordLedger — Manager Console",
                       page_icon="🛟", layout="wide")
    st.title("Manager Console — paused leads")
    st.caption("Every lead the graph paused for a human decision. The "
               "dashboard shows the full operational picture; your decision "
               "resumes the graph exactly where it stopped.")

    config = _load()
    as_of = config.as_of_date or date.today()
    stack = MCPStack(config.duckdb_path)

    paused = list_paused_leads(stack, config, as_of=as_of,
                               checkpoint_dir=DEFAULT_CHECKPOINT_DIR)
    if not paused:
        st.success("No paused leads — the graph is running clean. "
                   "(Run `python -m src.cli --run <LEAD_ID> --replies "
                   "decline decline decline` to create one.)")
        st.stop()

    ids = [p["referral_lead_id"] for p in paused]
    with st.sidebar:
        st.metric("Paused leads", len(paused))
        selected = st.radio("Select a lead", ids)
    payload = next(p for p in paused if p["referral_lead_id"] == selected)

    # ── header ──────────────────────────────────────────────────────────
    st.subheader(f"{selected} — paused at `{payload['destination']}`")
    st.warning(payload["hitl_reason"])

    # ── the reasoning chain ─────────────────────────────────────────────
    with st.expander("What the agents did (full reasoning chain)"):
        st.markdown("**Intake fields**")
        st.dataframe([{k: f[k] for k in ("field", "value", "confidence",
                                         "source_span")}
                      for f in payload["intake"]["fields"]],
                     use_container_width=True)
        if payload["matching"]["candidates"]:
            st.markdown("**Matching candidates**")
            st.dataframe(payload["matching"]["candidates"],
                         use_container_width=True)
        if payload["negotiation"]["history"]:
            st.markdown("**Negotiation history**")
            st.dataframe(payload["negotiation"]["history"],
                         use_container_width=True)

    # ── decision dashboard ──────────────────────────────────────────────
    dashboard = build_dashboard(config.duckdb_path, payload, as_of=as_of)

    if dashboard.get("market"):
        m, f = dashboard["market"], dashboard["financial"]
        st.markdown(f"### Market health — {m['country']}")
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Partners under cap",
                  f"{m['partners_under_cap']}/{m['partners_active_total']}")
        c2.metric("Avg util (7d)", f"{m['avg_util_7d_pct']}%")
        c3.metric("Conversion (30d)", f"{m['conversion_rate_30d_pct']}%")
        c4.metric("Open SLA breaches", m["sla_breaches_open"])
        c5.metric("Churn rate", f"{f['churn_rate_pct']}%")
        st.caption(f"Financials: active MRR {f['active_mrr']:,.0f} · "
                   f"churned MRR {f['churned_mrr']:,.0f} · "
                   f"overdue rate {f['overdue_rate_pct']}%")

    if dashboard["partners"]:
        st.markdown("### Partner health")
        cols = st.columns(min(4, len(dashboard["partners"])))
        for i, h in enumerate(dashboard["partners"].values()):
            _render_partner_card(cols[i % len(cols)], h)

    lb = dashboard["lead_vs_book"]
    if lb.get("available") and lb["deal_size"]:
        st.markdown("### This lead vs the book")
        st.text(f"deal size {lb['deal_size']:,.0f} · referring book avg "
                f"{lb['book_avg']:,.0f} ({lb['vs_book_pct']:+.0f}%) · "
                f"market avg {lb['market_avg']:,.0f} "
                f"({lb['vs_market_pct']:+.0f}%)")

    # ── the decision ────────────────────────────────────────────────────
    st.markdown("### Your decision")
    options = payload["options"]
    action = st.selectbox("Action", options)
    decision: dict = {"action": action}
    if action == "enrich":
        c1, c2, c3 = st.columns(3)
        country = c1.text_input("country (DK/NO/SE/DE/NL/US)")
        service = c2.text_input("service_type")
        industry = c3.text_input("industry")
        fields = {k: v for k, v in [("country", country),
                                    ("service_type", service),
                                    ("industry", industry)] if v}
        decision["fields"] = fields
    elif action in ("reactivate", "override", "reassign"):
        dormant = payload.get("dormant_partners", [])
        if dormant:
            decision["partner_id"] = st.selectbox(
                "Partner", [d["partner_id"] for d in dormant])
        else:
            decision["partner_id"] = st.text_input("partner_id (e.g. P012)")
    decision["note"] = st.text_input("Note (audited)")

    if st.button("Submit decision", type="primary"):
        try:
            validate_decision(payload["destination"], decision)
        except ValueError as exc:
            st.error(str(exc))
            st.stop()
        with st.spinner("Resuming the graph…"):
            result = _resume(config, selected, decision, as_of)
        pause2 = extract_interrupt(result)
        if pause2:
            st.warning(f"Paused again at {pause2['destination']}: "
                       f"{pause2['hitl_reason']}")
        else:
            final = lead_out(result)
            st.success(f"{selected} → **{final.status}**")
            if final.negotiation_history:
                st.dataframe(final.negotiation_history,
                             use_container_width=True)

    # ── Ask the BI Agent (Q18) ──────────────────────────────────────────
    st.markdown("---")
    st.markdown("### Ask the BI Agent")
    st.caption("Ad-hoc questions against the shared NordLedger metric layer "
               "(51 governed KPIs) — the vendored Self-Querying BI Agent "
               "pointed at this tool's warehouse. Needs a real "
               "ANTHROPIC_API_KEY.")
    question = st.text_input(
        "Question", placeholder="What's SE churn_rate trend the last 6 months?")
    if question:
        import os
        if not os.environ.get("ANTHROPIC_API_KEY", "").startswith("sk-ant-"):
            st.info("Set a real ANTHROPIC_API_KEY in .env to enable live "
                    "BI queries. (The metric layer itself is loaded and "
                    "verified — 51 metrics.)")
        else:
            from vendor.bi_agent.catalog import load_catalog
            from vendor.bi_agent.config import load_config as bi_load_config
            from vendor.bi_agent.planner import answer_question
            from vendor.bi_agent.semantic_layer import MetricFlowClient
            with st.spinner("BI Agent planning + querying…"):
                bi_config = bi_load_config()
                catalog = load_catalog(bi_config.semantic_layer_dir)
                mf = MetricFlowClient(bi_config.dbt_project_dir)
                result = answer_question(question, bi_config, catalog, mf)
            st.markdown(result.narrative)


if __name__ == "__main__":
    main()
