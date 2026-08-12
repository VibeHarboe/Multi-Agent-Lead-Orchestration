# Project Overview: Multi-Agent Lead Orchestration

*Explained in plain language — no technical background needed.*

**Last updated:** May 2026
**Status:** Built — all six weeks complete and validated end to end.

---

## 1. What is this project?

It's an **AI tool for a problem I know inside-out — because I built the
manual version first, and it scaled only by adding people.**

A few years ago (2023–2024), Vibe built the *ambassador-referral pipeline*
for the company Ageras. Accountants that already served small-business
clients — Billy and similar partners — would spot an inquiry, tag it into
Slack, and an ops team would route it into the Ageras Marketplace where
fulfillment partners (bookkeepers, auditors, tax advisors) picked it up.

It worked. **−40% lead processing time, −20% churn, scalable across
markets.** That's "Business Case #7" in Vibe's portfolio.

But the *value* was proven with a lot of glue-work: Slack channels per
market, capacity tracked in spreadsheets, humans making sure the tags landed
in the right place. Every new market meant more Slack channels, more
spreadsheets, and more people-hours.

This tool answers a question I couldn't answer at the time: **"How would I
build this pipeline today — so a new market is a data change, not a
ten-Slack-channel change?"**

It's a **portfolio project**. It shows, concretely, that Vibe understands
the difference between "an operation that works because people fill the
gaps" and "an operation that fills its own gaps and only pauses when it
genuinely can't decide" — and the engineering discipline that makes the
second one trustworthy rather than a runaway.

---

## 2. What was the problem — then and now?

Imagine a marketplace with accountants on one side (referring SMB clients
in) and bookkeepers, auditors and tax advisors on the other (picking up the
work). Every day, hundreds of referrals arrive. Someone has to:

- read each referral and figure out *what* the client needs,
- find the right fulfillment partner — with the right specialization, in the
  right country, who isn't already overbooked,
- reach out on Slack, negotiate timing, book the deal,
- and follow up when things go quiet.

**How it worked then (the original solution):**
- Ops team members did all four steps by hand.
- Routing followed rules ("Germany → the German bucket").
- Partner capacity was tracked in a shared spreadsheet.
- Slack tags were the coordination protocol.
- Result: it scaled by adding more people per market.

**How it works in the new solution (2026):**
- Four AI agents handle intake, matching, negotiation, and monitoring.
- Every side-effect goes through a typed **MCP tool** — the agents can't
  invent a partner or send a rogue email.
- When the system is unsure — an ambiguous referral, no partner under
  capacity, a stalled negotiation — it **pauses cleanly** and hands the case
  to a manager with the full reasoning chain attached.
- Nothing is hard-glued; adding a new market is a data change, not a
  ten-Slack-channel change.

> An "agent" here is software with one specific job, that uses AI to
> understand a situation, decide what to do, and explain the decision —
> like a specialised digital colleague. "MCP" is the standard the industry
> landed on for how agents talk to real-world tools (Slack, CRM, databases)
> — it means the agent can only call *specific, typed* tools, not invent
> whatever it wants.

---

## 3. How does it work?

Think of it as an **assembly line with four stations and a safety net**. A
referral goes in one end; a matched, negotiated, monitored deal comes out
the other — and if anything is uncertain, the safety net catches it and
hands it to a human.

### Station 1 — "The Reader" (Intake Agent)
Reads the incoming referral and extracts a structured picture: what
industry, what country, what service is needed, how urgent, who's the
referring partner, what's the rough deal size. Every extracted fact comes
with the exact words it came from and a confidence score. **If a fact isn't
in the text, the field stays empty — never invented.**

### Station 2 — "The Matcher" (Matching Agent)
Looks at the picture Station 1 produced and finds the right fulfillment
partner. It queries a live capacity feed to see who's actually available,
ranks the top three against specialization, historical close rate,
**partner ROI %**, and **churn-risk score**, and writes a rationale for
each pick. **A partner who is over-capacity can never be picked** — that
limit lives in code, not in the model. ROI matters as a ranking signal
because the graph shouldn't be sending new deals to partners who cost the
marketplace more than they generate — even if they're friendly and
responsive.

### Station 3 — "The Negotiator" (Negotiation Agent)
Drafts an outreach to the top pick on Slack, waits for a reply, and either
books the deal or falls through to the next candidate. It runs bounded —
so many rounds per candidate, so many candidates per lead. **If a partner
doesn't reply within X hours, it doesn't wait — it drops the candidate and
moves on.** That's the "dynamic re-routing" from the roadmap: no silent
sitting on a lead because someone went to lunch.

### Station 4 — "The Watcher" (Monitor Agent)
Runs on its own — not tied to any one lead. Three responsibilities:

1. **Sweeps active deals for SLA breaches** ("partner didn't file
   acceptance within 24 hours") and, if a breach is severe enough,
   *re-enters* the deal back into the graph — the Matcher runs again,
   excluding the breaching partner. That's post-booking dynamic re-routing.
2. **Scores every partner's engagement patterns** — on *both* sides of the
   marketplace. For the fulfillment partners (bookkeepers, auditors, tax
   advisors): how fast they reply, how often they decline, how often they
   cancel, plus their ROI trend. For the referring partners (the Billy-
   analogs): how many leads they send, how well those leads convert,
   whether they respond when we ask them for context. Both sides get a
   rolling 90-day **churn-risk score**. When a partner's score trends the
   wrong way for a sustained period, an intervention playbook fires — a DM
   to the manager with the partner's engagement chart, a proposed check-in
   template, and an automatic routing-weight adjustment until the score
   recovers. That's the "predictive churn intervention" — catching churn
   *before* the partner has actually left, symmetrically on the fulfillment
   and ambassador side.
3. **Emits a weekly per-partner performance report** every Monday morning
   — deals matched, negotiated, booked, response-latency percentiles,
   SLA breach count, churn-risk trend, and a short LLM-written narrative
   comparing the partner to their peers. That's the "auto-generated
   weekly performance reports" from the roadmap.

### The Safety Net — "The Manager Console" (HITL)
Not a station — a *destination*. When any of the four stations reaches a
point where it can't decide (no partner under capacity, all candidates
declined, a churn intervention that needs manager confirmation, an
ambiguous referral), the graph pauses, DMs the on-call manager with the
entire reasoning chain to that point, and waits for a decision. Nothing is
lost.

But the manager doesn't decide blind — they open a **decision dashboard**
in the console with the full operational picture: this partner's health
(active revenue, churn risk, response speed, capacity, ROI), this
country's health (available partners, lead volume, conversion rate), the
financial context (which of this partner's customers are churning, which
are in debt, DSO trends), and how this specific lead compares to the
partner's usual book. All of this reads from the same NordLedger
warehouse the Self-Querying BI Agent uses — one shared model of the world,
one shared vocabulary — and the manager can ask the BI Agent ad-hoc
questions right there in the console ("what's this partner's churn trend
the last 6 months?") without ever leaving the decision flow. The
dashboard is *live* — a deal booked five minutes ago shows up immediately.
They confirm, correct, or override with the operational context in view —
never from scratch. And when Matching draws a blank (no active partner
under capacity in that market), the manager can even reactivate a dormant
partner right there in the console — no back-office ticket, no ops handoff,
audit-logged automatically.

---

## 4. What's planned to be built (six-week roadmap)

The project is planned to run over six weeks. Each week has a hard exit
gate that has to be met before the next week starts.

### Week 1 — Data + simulation
Build a realistic synthetic dataset of the NordLedger Marketplace referral
universe: referring partners, incoming leads, fulfillment partners, live
capacity table, a partner-engagement rollup. The same fictional company
used by my Self-Querying BI Agent tool — different subsystem, same
universe. Deterministic (same seed → same data). Injected scenarios
documented in `SCENARIOS.md` — the eval suite will assert against them
later.

**Exit gate:** the seed generator runs clean; the scenario contract is
committed.

### Week 2 — Intake + Matching agents (skeleton)
Build the `LeadState` schema (what every downstream step will read from and
write to). Build the Intake Agent (structured extraction with per-field
confidence) and the Matching Agent skeleton (ranking against a fixture
capacity table).

**Exit gate:** classification and ranking green on all 5 seeded scenarios.

### Week 3 — MCP servers wired
Build the three MCP servers — `partner_capacity_mcp`, `slack_mock_mcp`,
`crm_mock_mcp` — each with contract tests. Wire Matching and Negotiation to
real MCP calls instead of fixtures.

**Exit gate:** the whole graph walks through against MCP mocks *without the
LLM* — proving the plumbing is correct before spending API tokens.

### Week 4 — Negotiation loop + HITL
Complete the Negotiation Agent's full bounded loop. Implement all four HITL
interrupt destinations. Wire the LangGraph checkpointer so paused leads
persist to disk and resume cleanly.

**Exit gate:** a paused lead can be resumed from disk with the manager's
decision attached.

### Week 5 — Monitor + observability
Build the Monitor Agent's three responsibilities: SLA sweep (with dynamic
re-routing on breach), partner-engagement scoring with predictive churn
intervention, and weekly per-partner performance reports. Wire Langfuse for
trace-per-lead observability + cost tracking.

**Exit gate:** every scenario in `SCENARIOS.md` lands at its expected
terminal state — including the slow-churn scenario firing the intervention
*before* an actual breach.

### Week 6 — Demo + docs + ship
Build the customer-facing Streamlit demo (scripted, deterministic
scenarios) and the manager HITL console (approve / override / drop paused
leads). Finalise README, PROJECT-OVERVIEW, showcase artifacts. Push to
GitHub. Mirror sync.

**Exit gate:** full end-to-end demo runs; 6-week build documented; GitHub
repo live.

---

## 5. Where are we? (status in one table)

| Week | What | Status |
|---|---|---|
| 0 | Architecture drafted — `ARCHITECTURE.md`, `README.md`, `PROJECT-OVERVIEW.md` | ✅ Done |
| 1 | Data model + `warehouse/` NordLedger simulation | ✅ Built & validated |
| 2 | `LeadState` + Intake + Matching | ✅ Built & validated |
| 3 | Three MCP servers wired end-to-end | ✅ Built & validated |
| 4 | Full Negotiation loop + HITL interrupts + checkpointer | ✅ Built & validated |
| 5 | Monitor Agent (SLA + churn + weekly) + observability | ✅ Built & validated |
| 6 | Streamlit demo + HITL console + docs + GitHub push | ✅ Built & validated |

**Built end to end.** Every week's exit gate was met before the next week
started; the tool runs locally against the bundled simulation with no
credentials beyond an Anthropic key for the live-LLM paths.

---

## 6. Why is this a strong portfolio project?

- **It hits all three of 2026's hottest AI-engineering trends in one
  project.** Multi-agent orchestration. Model Context Protocol. Human-in-the
  loop governance. Not as three separate demos, but as a single system where
  each is load-bearing.
- **It's a real story.** It builds on a genuine success Vibe delivered —
  an ambassador program that scaled across markets — not an invented
  example.
- **"Then vs now" is a senior signal.** Most people applying for AI roles
  can show an AI demo. Far fewer built the *manual version* first and
  understand exactly what the AI layer does and doesn't change. That's the
  perspective the market is short on.
- **Failure modes are enumerated, not discovered in production.** The
  architecture doc lists 12 failure modes and how each is mitigated —
  before a line of code is written. That's the difference between a demo
  and a system a team can trust with its inbound pipeline.
- **The safety net is a first-class feature.** HITL isn't a fallback —
  it's a *destination* the graph explicitly routes to, with the entire
  reasoning chain preserved. The manager never starts cold.
- **It's part of a coherent portfolio universe.** My Self-Querying BI Agent
  tool uses the same fictional company — NordLedger Marketplace. Different
  subsystem, same world — signals that the portfolio is a *body of work*,
  not a collection of tutorials.

---

## Glossary

| Term | What it means here |
|---|---|
| **Agent** | Software with one specific job, using AI to understand, decide, and explain — a specialised digital colleague. |
| **LangGraph** | The framework the assembly line is built on — a *state graph* where every station is a testable function. |
| **MCP** | Model Context Protocol — the 2026 standard for how agents talk to real-world tools (Slack, CRM, databases). Means the agent can only call *specific, typed* tools, not invent whatever it wants. |
| **HITL** | Human-in-the-Loop — the safety net. When the system is unsure, it pauses and hands the case to a human, with all the reasoning attached. |
| **Decision dashboard** | The KPI + BI view the manager opens when a lead pauses at HITL — partner health, market health, financial context and this-lead's implied deal size. Reads from the shared NordLedger warehouse; the Self-Querying BI Agent is embedded for ad-hoc questions inside the same console. |
| **Referring partner** | The accountant (Billy-analog) who spots the SMB inquiry first and refers it into the marketplace. |
| **Fulfillment partner** | The bookkeeper, auditor or tax advisor on the marketplace who actually delivers the service. |
| **Capacity** | How many active deals a fulfillment partner has vs their cap. Live-tracked in the `partner_capacity_mcp` server. |
| **Churn risk** | A 0–100 score derived from a partner's engagement patterns *and* ROI trend over 90 days. Fires an intervention when sustained above threshold. |
| **Partner ROI** | Percentage return the marketplace gets on this partner — active MRR minus total monthly cost (fees + commission), as a percentage of cost. Used as a first-class KPI: in Matching's ranking, in the churn-risk score, in the weekly report, and in the decision dashboard. |
| **SLA** | Service-Level Agreement — the promised response time. A breach here re-enters the deal into the graph for re-routing. |
| **Langfuse** | The observability layer — one trace per lead, one span per agent call, one child span per MCP call. Cost and latency per span. |

---

*The full technical depth — architecture, failure modes, state machine,
HITL model, evaluation strategy — is in [`ARCHITECTURE.md`](ARCHITECTURE.md).
This document is the easy-to-read version.*

*Vibe Harboe Christensen — AI Automation Engineer | vibegroup.dk*
