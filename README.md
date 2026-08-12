# Multi-Agent Lead Orchestration

![status](https://img.shields.io/badge/status-complete-brightgreen)
![agents](https://img.shields.io/badge/agents-4%20%2B%20HITL-A855F7)
![framework](https://img.shields.io/badge/framework-LangGraph-7C3AED)
![mcp](https://img.shields.io/badge/tool%20boundary-MCP-7C3AED)
![observability](https://img.shields.io/badge/observability-Langfuse-C084FC)
![python](https://img.shields.io/badge/python-3.12-blue)
![llm](https://img.shields.io/badge/LLM-Claude-blueviolet)

**An autonomous multi-agent tool for cross-market partner-lead orchestration —
inspired by [Business Case #7 (Ageras Ambassador Program)](https://sites.google.com/view/vibegroup-dk/business-cases).**

> **The problem I know inside-out (2023–2024):** I built the ambassador-referral
> pipeline at Ageras — partner accountants (Billy et al.) saw SMB inquiries
> first, tagged them into Slack, an ops team routed them into the Marketplace
> where bookkeepers, auditors and tax advisors picked them up. Result:
> **−40% lead processing time, −20% churn, scalable across markets.** But
> routing was rule-based, capacity was tracked in a spreadsheet, and every new
> market meant more Slack channels, more spreadsheets, and more people-hours
> to keep the machine oiled. The value was proven — the bottleneck was
> **glue-work.**
>
> **What this tool does (2026):** Same business problem, AI-native. Four
> cooperating agents take a referral from raw text to a matched, negotiated,
> monitored deal — hand off to a human *only* when the system genuinely can't
> decide. Routing runs against a live partner-capacity feed via **MCP**;
> every step is traced in **Langfuse**; every escalation carries its full
> reasoning chain.

This isn't another CRM agent demo. It's a production-shaped tool that
demonstrates all three of 2026's hottest engineering trends in one system —
**multi-agent orchestration + MCP tool-boundary + human-in-the-loop
governance** — grounded in a real operational problem I've delivered against.

---

## What it does

A referral inquiry arrives; a LangGraph state machine walks it through four
agents and hands off to a human only at explicit interrupt destinations:

```
             INBOUND referral
                     │
                     ▼
                ┌───────────┐
                │  INTAKE   │  extract 12+ fields · confidence per field
                └─────┬─────┘
                      │  LeadState (typed, validated)
                      ▼
                ┌───────────┐   MCP:
                │ MATCHING  │──▶ partner_capacity_mcp
                │           │◀── ranked candidates + capacity + churn-risk
                └─────┬─────┘
         ┌────────────┴─────────────┐
         ▼                          ▼
   HITL(no capacity)        ┌───────────────┐   MCP:
                            │ NEGOTIATION   │──▶ slack + crm
                            │  bounded loop │
                            └─────┬─────────┘
                                  │
                       ┌──────────┴───────────┐
                       ▼                      ▼
                    BOOKED             HITL(round budget /
                       │                all declined)
                       ▼
                ┌───────────┐
                │  MONITOR  │  SLA sweep · churn scoring · weekly reports
                │  (scheduled)
                └─────┬─────┘
                      │
                      ▼
             re-enter graph on breach ─▶ dynamic re-routing
             fire intervention        ─▶ manager DM + auto-deprioritise
             emit weekly report       ─▶ Slack (Monday morning)

                          ▼
                     LANGFUSE
                trace + cost per lead
```

**The core principle is structured state.** The four agents do not "chat"
with each other. They read from and write to a typed `LeadState` object that
flows through a LangGraph. Each transition is a testable pure function.
Every side-effect — Slack post, CRM write, capacity query — is a call to an
**MCP server**, not a raw API. So the model never invents a partner, never
overbooks a rep, and never sends an email the graph can't trace.

1. **Intake Agent** — Claude with structured extraction. Reads the referral
   text and produces a typed `LeadState` with 12+ fields, each carrying its
   source span and a confidence score. No evidence → the field stays empty.
2. **Matching Agent** — Claude with the `partner_capacity_mcp` tool. Ranks
   candidate partners against capacity, specialization, close-rate history,
   **partner ROI %**, and churn-risk score. An over-capacity partner cannot
   be picked — the hard limit lives in code, not in the model. ROI and
   churn-risk are soft signals: they influence ranking, they don't
   hard-filter — so a manager can still route to a struggling partner via
   HITL when there's strategic reason to.
3. **Negotiation Agent** — Claude with `slack_mock_mcp` + `crm_mock_mcp`. A
   bounded loop (max N rounds × M candidates). Non-response within **24
   hours** = *drop this candidate and try the next* — dynamic in-loop
   re-routing.
4. **Monitor Agent** — runs on a schedule. Three responsibilities: SLA sweep
   with post-booking re-routing, partner-engagement scoring with **predictive
   churn intervention**, and **weekly per-partner performance reports**.
5. **HITL Gate** — first-class LangGraph node. Four explicit interrupt
   destinations; every pause DMs the on-call manager with the entire
   reasoning chain to that point. The manager decides inside a **decision
   dashboard** — KPI + BI metrics per partner, per country, per revenue,
   churn and debt — reading from the same NordLedger warehouse the
   Self-Querying BI Agent uses, with the BI Agent's natural-language
   interface embedded for ad-hoc questions. The override happens in full
   operational context, not blind.

---

## What makes it production-shaped, not a demo

| Concern | How it's handled |
|---|---|
| **Hallucinated partners / fields** | Intake outputs a Pydantic-validated `LeadState` with per-field confidence; unfilled fields stay empty, never guessed. Matching picks from an MCP-returned enumerable set — the model can't name a partner that doesn't exist. |
| **Overbooking a partner** | Capacity is a *hard limit* in `partner_capacity_mcp` — the tool filters server-side. The agent's ranking runs on a pre-filtered set. |
| **Loops that never converge** | Hard `MAX_NEGOTIATION_ROUNDS` × `MAX_CANDIDATES` caps. On cap → HITL, not a silent drop. |
| **Safe degradation** | LangGraph checkpointer holds state across MCP failures — the graph resumes when tools recover. HITL is a *destination*, not a fallback. |
| **Non-response, silently** | Both in-loop (Negotiation §3.3) and post-booking (Monitor §3.4a). Non-response is treated the same as a decline: release capacity, drop the partner, re-route. |
| **Partner churn caught late** | Rolling 90-day engagement rollup + a churn-risk score (weighting engagement signals **and partner ROI trend**) exposed to Matching. The Monitor fires an intervention playbook when the score is sustained past threshold — pre-churn, not post-mortem. |
| **Unprofitable routing** | Matching ranks on ROI too, not just close-rate. A partner with high response speed but declining ROI drops in the ranking automatically — the graph won't route new deals to unprofitable relationships. Manager can still override for strategic reasons. |
| **Audit trail** | Every LangGraph node is a Langfuse span; every MCP call is a child span. "Why did we route L00042 to P019?" is a single trace link away. |
| **Cost per lead** | Langfuse rolls token cost per span → per-lead, per-market, per-partner cost dashboards. A per-lead budget short-circuits to HITL on breach. |
| **Runnable end-to-end locally** | Bundled DuckDB simulation of the NordLedger Marketplace referral universe (§5). No Slack account, no CRM account, no credentials — the MCP mocks handle everything. |

---

## Status

**Built end to end — all six weeks complete.** 61 warehouse-marked + 46
deterministic tests green; every scenario in `warehouse/SCENARIOS.md` lands
at its expected terminal state; a paused lead resolves through the manager
console with the decision dashboard rendering real partner/market/financial
metrics.

The full design record lives in [`ARCHITECTURE.md`](ARCHITECTURE.md) — 14
sections covering components, tech-stack rationale, data model, state
machine, MCP boundary, failure modes, HITL escalation model, observability,
evaluation strategy, and the 6-week build plan.

The plain-language walkthrough is in [`PROJECT-OVERVIEW.md`](PROJECT-OVERVIEW.md).

| Week | Scope | Exit gate |
|---|---|---|
| 1 | Data model + `warehouse/` simulation (15 tables, 70k rows, 9 scenarios) | ✅ 150/150 dbt tests |
| 2 | `LeadState` schema + Intake & Matching Agents | ✅ All 9 scenarios green at classify+rank |
| 3 | Three MCP servers + `MCPStack` + `--dry-run` | ✅ Full walk, 0 LLM calls, parity test green |
| 4 | Negotiation 3×3 loop + 4 HITL interrupts + SQLite checkpointer | ✅ Cross-instance disk resume proven |
| 5 | Monitor Agent (SLA re-injection · churn/ROI/volume interventions · imbalance · weekly) + observability | ✅ All scenarios land; trace tree matches §10 |
| 6 | Manager console w/ decision dashboard + BI widget (Q18) + demo + docs + GitHub | ✅ Console e2e resolution test green |

---

## Showcase

Once built, client-facing presentation artifacts (landing page, interactive
prototype, one-pager) will live online — see
[`showcase/README.md`](showcase/README.md) for the live links (coming in Week 6).

---

## Setup

*(Forward-looking — this section will be finalised as each week's exit gate
is met.)*

```bash
# 1. Install (Python 3.12) — LangGraph + Anthropic SDK + MCP + Langfuse + DuckDB
pip install -r requirements.txt

# 2. Set ANTHROPIC_API_KEY (the bundled simulation needs no other credentials)
cp .env.example .env        # then edit ANTHROPIC_API_KEY

# 3. Build the local DuckDB warehouse from the bundled NordLedger simulation
python warehouse/scripts/generate_seeds.py
cd warehouse && dbt build   # or plain SQL runner — decision Week 1
cd ..
```

```bash
# Deterministic tests (free, run on every push)
pytest

# End-to-end scenario suite against the built warehouse
pytest -m warehouse

# Run a single lead through the graph
python -m src.cli --run L00042

# Resume a paused (HITL) lead from disk
python -m src.cli --resume L00042 --decision accept --note "..."

# Proactive Monitor sweep
python -m src.cli --scan

# The Streamlit demo + the manager HITL console
streamlit run app/streamlit_demo.py
streamlit run app/console.py
```

---

## Project layout

```
src/
  state.py              LeadState Pydantic schema
  agents/               intake · matching · negotiation · monitor
  graph.py              LangGraph nodes + edges + HITL interrupts
  hitl.py               interrupt handlers + resume machinery
  observability.py      Langfuse tracing hooks
  cli.py                entry point — --run / --resume / --scan
mcp_servers/            three MCP servers: partner_capacity · slack_mock · crm_mock
app/
  streamlit_demo.py     customer-facing demo (scripted scenarios)
  console.py            manager HITL console
eval/                   deterministic + scenario + live-eval markers
warehouse/              bundled DuckDB simulation + SCENARIOS.md
```

Full repo tree in [`ARCHITECTURE.md` §12](ARCHITECTURE.md).

---

## Tech stack

**LangGraph** state machine — the only framework with native interrupt/resume
for HITL · **Model Context Protocol (MCP)** as the tool boundary, three
custom servers (`partner_capacity`, `slack_mock`, `crm_mock`), each a
swap-out target for a real integration · **Claude** (Anthropic SDK), Sonnet
by default, Opus configurable for hard negotiation turns · **Langfuse**
(self-hostable) for trace-per-lead observability · **DuckDB** for the
bundled runnable simulation · **Streamlit** for the demo + the manager
console.

Full design — architecture, failure modes, HITL model — is in
[`ARCHITECTURE.md`](ARCHITECTURE.md). A plain-language walkthrough is in
[`PROJECT-OVERVIEW.md`](PROJECT-OVERVIEW.md).

---

## About

Built by **Vibe Harboe Christensen** — AI Automation Engineer, founder of
VIBE Group. 20+ years of operational, data and process-engineering
experience. This is one of a series of AI tools I've built for real
operational problems — each grounded in years of running that kind of
operation myself, and each engineered to hold in production, not just to
demo.

*Not demos. Not theory. Production AI agents, grounded in 20+ years of
operational experience.* — [vibegroup.dk](https://vibegroup.dk) ·
[LinkedIn](https://linkedin.com/in/v-h-c) · [GitHub](https://github.com/VibeHarboe)
