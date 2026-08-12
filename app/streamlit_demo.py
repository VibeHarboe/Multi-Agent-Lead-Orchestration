"""Customer-facing demo — scripted scenarios through the REAL graph.

    streamlit run app/streamlit_demo.py

Unlike a mockup, every run here executes the actual LangGraph against the
actual MCP servers and the actual DuckDB warehouse — only the partner replies
are scripted (via slack_mock's primed FIFO) so each scenario is
deterministic. No LLM, no API key: intake is the seed-fixture and outreach
uses the TemplateDrafter.

Five scenarios, mirroring SCENARIOS.md:
  happy path · negotiation stall (+HITL override) · ambiguous inquiry ·
  cold-start capacity crunch · the Monitor's daily sweep
"""

from __future__ import annotations

import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import streamlit as st  # noqa: E402

from src.config import load_config  # noqa: E402
from src.data.warehouse import find_referral_lead  # noqa: E402
from src.dry_run import lead_state_from_seed  # noqa: E402
from src.graph import (extract_interrupt, lead_out, open_graph,  # noqa: E402
                       resume_lead, run_lead)
from src.mcp_stack import MCPStack  # noqa: E402
from src.observability import TraceRecorder  # noqa: E402

SCENARIOS = {
    "Happy path — accepted in round 1": {
        "country": "SE", "replies": [],
        "blurb": "A clean Swedish bookkeeping referral: matched, first "
                 "partner accepts, booked. Zero human involvement.",
    },
    "Negotiation stall — 9 counters, manager override": {
        "country": "NL", "replies": ["counter"] * 9,
        "resume": {"action": "override", "partner_id": "P004",
                   "note": "manager knows P004 has bandwidth"},
        "blurb": "Every candidate counter-proposes three times — the Q7 "
                 "budget (3×3) exhausts, the graph pauses at HITL, and the "
                 "manager overrides to a specific partner.",
    },
    "Dynamic re-routing — first partner silent 24h": {
        "country": "US", "replies": ["no_reply"],
        "blurb": "The top pick never replies. After the 24h SLA the "
                 "candidate is dropped and the next one contacted — no "
                 "lead ever waits on a silent partner.",
    },
    "Cold start — DK capacity crunch (Dec 2024)": {
        "country": "DK", "replies": [], "as_of": date(2024, 12, 15),
        "blurb": "Every DK partner is at hard-cap. Matching returns zero "
                 "candidates and the graph pauses at HITL_CAPACITY with the "
                 "dormant-partner list ready for the manager.",
    },
}


@st.cache_resource
def _config():
    return load_config(require_api_key=False)


def main() -> None:
    st.set_page_config(page_title="NordLedger — Lead Orchestration demo",
                       page_icon="🔀", layout="wide")
    st.title("Multi-Agent Lead Orchestration — live demo")
    st.caption("Every run executes the real LangGraph against the real MCP "
               "servers and warehouse. Partner replies are scripted for "
               "determinism; no LLM tokens are spent.")

    config = _config()
    name = st.selectbox("Scenario", list(SCENARIOS))
    scenario = SCENARIOS[name]
    st.info(scenario["blurb"])

    if st.button("▶ Run scenario", type="primary"):
        as_of = scenario.get("as_of") or config.as_of_date or date.today()
        recorder = TraceRecorder()
        stack = MCPStack(config.duckdb_path, recorder=recorder)
        if scenario["replies"]:
            stack.prime_slack_replies(scenario["replies"])

        seed = find_referral_lead(config.duckdb_path,
                                  country=scenario["country"],
                                  status="pending", limit=1)[0]
        lead = lead_state_from_seed(seed)
        ckpt = Path(tempfile.mkdtemp(prefix="demo_ckpt_"))

        with st.status("Running the graph…", expanded=True) as status:
            with open_graph(stack, config, as_of=as_of,
                            checkpoint_dir=ckpt, recorder=recorder) as graph:
                result = run_lead(graph, lead, recorder=recorder)
                pause = extract_interrupt(result)

                if pause:
                    st.write(f"⏸ paused at **{pause['destination']}** — "
                             f"{pause['hitl_reason']}")
                    if pause.get("dormant_partners"):
                        st.write("dormant candidates: " + ", ".join(
                            d["partner_id"] for d in pause["dormant_partners"]))
                    if scenario.get("resume"):
                        st.write(f"👤 manager decision: "
                                 f"`{scenario['resume']['action']}` "
                                 f"→ {scenario['resume'].get('partner_id', '')}")
                        result = resume_lead(graph, lead.referral_lead_id,
                                             scenario["resume"])
                        pause = extract_interrupt(result)

            if pause:
                status.update(label=f"Paused at {pause['destination']} — "
                                    f"manager decision needed",
                              state="error")
            else:
                final = lead_out(result)
                status.update(label=f"Outcome: {final.status}",
                              state="complete")
                if final.negotiation_history:
                    st.dataframe(final.negotiation_history,
                                 use_container_width=True)

        st.markdown("#### The trace (every node, every MCP call)")
        st.json(recorder.shape())

        st.markdown("#### MCP call log")
        st.dataframe([{"server": c["server"], "tool": c["tool"]}
                      for c in stack.call_log], use_container_width=True)

    # ── the Monitor's daily sweep ───────────────────────────────────────
    st.markdown("---")
    st.markdown("### The Monitor's daily sweep")
    st.caption("SLA breaches · partner interventions (churn / ROI / volume) "
               "· market imbalances — across the whole seeded book.")
    if st.button("▶ Run the sweep (as-of 2024-12-31)"):
        from src.agents.monitor import sweep
        report = sweep(config.duckdb_path, config, as_of=date(2024, 12, 31))
        c1, c2, c3 = st.columns(3)
        c1.metric("SLA breaches", len(report.sla_breaches))
        c2.metric("Interventions", len(report.interventions))
        c3.metric("Market alerts", len(report.market_alerts))
        for b in report.sla_breaches:
            st.error(f"SLA: {b.referral_lead_id} ({b.country}) "
                     f"{b.days_since_booking}d unresolved — partner "
                     f"{b.breaching_partner_id}")
        for i in report.interventions:
            st.warning(f"{i.kind}: {i.partner_id} ({i.partner_side}) — "
                       f"{i.detail}")
        for m in report.market_alerts:
            st.info(f"market {m.kind}: {m.country} at {m.avg_util_pct}% "
                    f"for {m.consecutive_days}d")


if __name__ == "__main__":
    main()
