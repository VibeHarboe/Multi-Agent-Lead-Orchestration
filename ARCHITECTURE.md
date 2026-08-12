# Multi-Agent Lead Orchestration — Architecture

**Author:** Vibe Harboe Christensen — AI Automation Engineer | vibegroup.dk
**Date:** May 2026
**Status:** Built — all six weeks complete. Data + simulation, LeadState +
Intake/Matching, three MCP servers, the LangGraph with four HITL interrupts +
SQLite checkpointer, the Monitor Agent (SLA re-injection · churn/ROI
interventions · market imbalance · weekly reports), Langfuse-compatible
observability, the manager console with decision dashboard + embedded BI
Agent (Q18), and the Streamlit demo. 61 warehouse-marked + 46 deterministic
tests green; every scenario in `SCENARIOS.md` lands at its expected terminal
state.
**Inspired by:** Business Case #7 (Ageras Ambassador Program).
**Universe:** the fictional `NordLedger Marketplace`, shared with my
Self-Querying BI Agent tool.

> **This document is the design + build record.** It captures the architecture as
> designed, and — from Week 1 onward — the state as built with validation findings
> and deliberate deviations noted inline. The plain-language version will live in
> `PROJECT-OVERVIEW.md`.

---

## 1. Purpose & context

### Then (Ageras Ambassador Program, 2023–2024)
I built the ambassador-referral pipeline: partner accountants (Billy et al.) saw
SMB inquiries first, tagged them into Slack, and a human ops team routed them
into the Ageras Marketplace where fulfillment partners (bookkeepers, auditors,
tax advisors) picked them up. Result: **−40% lead processing time, −20% churn,
scalable across markets.** But routing was rule-based, capacity was tracked by
hand in a spreadsheet, and the Slack-tag → CRM handoff was manual glue.

The honest constraint: the *value* was proven — cross-market ambassador routing
works. The bottleneck was **glue-work**. Every new market meant more Slack
channels, more spreadsheets, and more people-hours to keep the machine oiled.

### Now (this tool)
Same business problem, AI-native. A referral inquiry arrives; four cooperating
agents take it from raw text to a matched, negotiated, monitored deal — and
hand off to a human *only* when the system genuinely can't decide. The routing
runs against a live partner-capacity feed via **MCP**; every step is traced;
every escalation carries its full reasoning chain.

**What this build proves:** I understand the difference between "a routing
system that works because people fill the gaps" and "a routing system that
degrades gracefully when the gap is unclear" — and the engineering discipline
(structured state, hard capacity limits, HITL as a first-class node) that makes
autonomous orchestration trustworthy.

### Scope decisions

| Choice | Decision | Why |
|---|---|---|
| Framework | LangGraph (explicit state graph) | Pattern proved in my Agentic CRM tool; native interrupt/resume for HITL is the killer feature here |
| Tool boundary | Model Context Protocol (MCP) | The three connectors (partner-capacity, Slack, CRM) each become a swappable MCP server — real integrations drop in behind the same interface |
| Observability | Langfuse (self-hostable) | Trace-per-lead is the audit surface; open-source means no vendor lock-in for a portfolio project |
| HITL | First-class graph node with structured pause/resume | Not a "fallback" — a *destination* the graph explicitly routes to |
| Data | DuckDB-backed simulation of NordLedger's referral + fulfillment tables | Continues the pattern from my Self-Querying BI Agent tool; runs locally with no credentials |
| Universe | NordLedger Marketplace (fictional) | Reused from the Self-Querying BI Agent for portfolio cohesion — different subsystem, same fictional company |

---

## 2. System overview

```
                              INBOUND
                                  │
                                  ▼
                       ┌──────────────────────┐
                       │   INTAKE  AGENT      │  classify · extract 12+ fields
                       │   Claude + schema    │  confidence per field
                       └──────────┬───────────┘
                                  │  LeadState (structured)
                                  ▼
                       ┌──────────────────────┐   MCP tool call:
                       │  MATCHING  AGENT     │──▶ partner_capacity_mcp
                       │  Claude + tool use   │◀── ranked partners + capacity
                       └──────────┬───────────┘
                                  │  candidate list
                     ┌────────────┴─────────────┐
                     ▼                          ▼
              HITL escalation           NEGOTIATION AGENT
              (uncertain? no cap?)      Claude + tools ─┐
                     │                                  │
                     │                          MCP tool calls:
                     │                          ┌─────────────────┐
                     │                          │ slack_mock_mcp  │
                     │                          │ crm_mock_mcp    │
                     │                          └─────────────────┘
                     │                                  │  outcome
                     │                                  ▼
                     │                          ┌──────────────────┐
                     └─────────────────────────▶│  MONITOR AGENT   │
                                                │  SLA + escalate  │
                                                └────────┬─────────┘
                                                         │
                                                         ▼
                                                  Slack digest +
                                                  CRM stage update
                                                         │
                                                         ▼
                                                    LANGFUSE
                                                    trace per lead
```

**Design principle — structured state, explicit routing.** The four agents do
not "chat" with each other. They read from and write to a typed `LeadState`
object that flows through a LangGraph. Each transition is a testable pure
function of the current state. Every side-effect (Slack post, CRM write) is a
tool call to an MCP server — the model never invokes a raw API.

---

## 3. Components + responsibilities

### 3.1 Intake Agent (Station 1)
Classifies the inbound referral into `LeadState`. Extracts ~12 structured
fields (industry, country, service type, urgency, referring-partner ID,
inferred deal size, budget signals, tech stack, timeline, contact, contact
role, source). Every field carries the exact text span it came from and a
confidence score — a field with no evidence stays empty, never guessed.

Output schema is validated with Pydantic before the state advances. A field
with confidence < threshold flags the whole lead for HITL review at Station 3's
gate, not at Station 1 — so the routing agent still gets to try.

### 3.2 Matching Agent (Station 2)
The routing brain. Given the classified `LeadState`, it queries the
`partner_capacity_mcp` server for candidates — the tool returns fulfillment
partners whose specialization matches the industry, capacity is under limit,
and country coverage includes the lead's market. The agent ranks the candidates
against four factors and outputs a **top-K** with a written rationale per pick.
Default `MATCHING_TOP_K = 3` — configurable via env var so a larger-market
deployment can expand to top-5 without a code change:

1. **Historical close rate** for similar leads (industry × service_type)
2. **Partner ROI %** and net-monthly-value trend — a partner with high
   close-rate but declining ROI is deprioritised; avoids routing to
   unprofitable relationships
3. **Churn-risk score** (from §3.4) — partners the Monitor is watching drop
   in ranking automatically, so pre-churn deprioritisation happens without
   the manager having to intervene
4. **Response-latency percentile** — recent behaviour, not just aggregate
   history

**Hard rules (server-side, in `partner_capacity_mcp.list_candidates`):**
- **Over-capacity partner cannot be picked** — capacity is a boundary, not a
  suggestion.
- **Inactive partner cannot be picked** — `partner_status_events` is the
  source of truth; the candidate list only contains partners with
  `status = 'active'` as of the query time. But an inactive partner *can* be
  brought back into circulation via HITL: when Matching returns zero
  candidates, the manager can reactivate a dormant partner from the console
  (§3.5) and resume the graph.

ROI and churn-risk are *soft* signals — they influence ranking, they don't
hard-filter — so the manager can still route to a struggling partner via
HITL override when there's strategic reason to.

### 3.3 Negotiation Agent (Station 3)
Given the top-K candidate list, it drafts an initial outreach to the top pick
(via `slack_mock_mcp` in the demo, a real Slack MCP in production), waits for a
reply (accept / decline / propose-alternative), and either books the deal (via
`crm_mock_mcp`) or falls through to the next candidate. It runs bounded —
**`MAX_NEGOTIATION_ROUNDS = 3` per candidate × `MAX_CANDIDATES = 3`** (9
outreach attempts max per lead) — after which it escalates to HITL with the
full negotiation trail attached. With the 24h response SLA below, 9 attempts
covers roughly one business week end-to-end.

**Dynamic in-loop re-routing.** *Non-response* is a first-class outcome. If a
partner doesn't reply within `PARTNER_RESPONSE_SLA_HOURS` (**default 24h** —
covers overnight + next-day handling, realistic for async B2B partner
communication), the agent doesn't wait — it releases the held capacity slot,
drops that partner to the bottom of the ranking, and starts outreach to the
next candidate. The non-response counter is written to the partner's
engagement rollup (§3.4) — repeated non-responses feed the churn-risk score
and eventually deprioritise that partner in future `list_candidates` calls.

### 3.4 Monitor Agent (Station 4)
Runs independently of any single lead. **Daily sweep at 06:00 CET** (before
business day begins — manager reads the digest with morning coffee); **weekly
report Mondays at 08:00 CET**. Three responsibilities: SLA sweep, partner-
engagement scoring with predictive churn intervention, and periodic
performance reporting.

**(a) SLA sweep — dynamic re-routing on breach.** Sweeps active deals against
two SLAs: **48 hours to accept** (partner files acceptance in CRM after
verbally agreeing) and **30 days to close** (deal moves to `resolved`).
When a breach crosses severity threshold it doesn't just alert — it
re-enters the deal into the LangGraph as a fresh intake with the breach
context attached. Downstream, the Matching Agent runs again on the same
lead, now excluding the breaching partner. This is the post-booking
counterpart to Negotiation's in-loop re-routing (§3.3) — non-response and
post-booking silence are treated as the same class of failure.

**Cross-market imbalance detection.** During the daily sweep, the Monitor
computes per-market average capacity utilisation over rolling windows and
fires an alert when either threshold trips:
- **Saturated:** avg market util ≥ **90%** for **7 consecutive days**
- **Starved:** avg market util ≤ **20%** for **14 consecutive days**

Both trip on the Nov-2024 DE / DK seed scenario (§5.2 #5) — the seeded data
is the eval contract.

**(b) Partner-engagement scoring + predictive churn intervention.** Maintains
a rolling 90-day engagement rollup per partner (`partner_engagement_daily` —
see §5.1): response-latency percentiles, accept vs decline vs no-response
rates, cancellation frequency, capacity-utilisation trend, **partner ROI %
and net-monthly-value trend**. A composite **churn-risk score** (0–100) is
derived nightly, weighting engagement signals *and* ROI trajectory together —
a partner going quiet is one signal; a partner going quiet *while* ROI is
declining is a much stronger one. When the score crosses the intervention
threshold (default 65, sustained 7 days) or either trajectory (engagement
or ROI) inflects sharply, an intervention playbook fires: a manager DM with
the partner's engagement + ROI chart, a proposed check-in template, and a
routing-weight adjustment recommendation (deprioritise in ranking until both
signals recover). The score and the ROI trend are exposed to the Matching
Agent through `list_candidates` (§7.1), so pre-churn deprioritisation
happens automatically for new leads while the manager decides whether to
intervene.

**(c) Weekly per-partner performance reports.** Every Monday morning the
Monitor emits a per-partner report to a private Slack channel: deals matched
/ negotiated / booked / closed / cancelled, response-latency percentiles vs
peer partners, SLA breach count, **partner ROI % + trend, net-monthly-value
+ trend**, churn-risk trend, and a short LLM-generated narrative summary.
This is the strategic report — distinct from the *daily* operational digest,
which is deal-flow only. Both are rate-limited (max 1 severity-3 escalation
per partner per 12h in the daily; the weekly is strictly scheduled) so the
manager's inbox never gets flooded during an outage.

### 3.5 HITL Gate (cross-cutting)
Not an agent — a LangGraph interrupt. Triggered when:
- Intake confidence below threshold (nothing extractable);
- Matching returns zero candidates under capacity in the lead's market;
- Negotiation exhausts its round budget without a booking;
- Monitor escalates a severity-3 breach.

The interrupt pauses graph execution on that lead, DMs the on-call manager on
Slack with **the entire reasoning chain to that point**, and waits for a
resume signal (accept / override / drop). All prior work is preserved —
the human never starts from scratch.

**Decision dashboard.** The Slack DM contains a link to the manager console
(`app/console.py`). When the manager clicks through, they don't just see the
paused lead's reasoning trail — they see a full **decision dashboard** so
the override happens in operational context, not blind. The dashboard reads
from the same NordLedger DuckDB warehouse that the Self-Querying BI Agent
queries, and shows:

- **Partner health (this partner)** — active MRR contribution, churn-risk
  score + trend, response-latency percentiles, capacity utilisation, historical
  close-rate for this industry, `partner_roi_pct` and `partner_net_monthly_value`
- **Market health (this country)** — active partners under capacity, lead
  volume rolling window, `conversion_rate`, `sla_breach_rate`, supply-demand gap
- **Financial context (this partner's customers)** — `churned_mrr`, active_mrr
  by segment, `overdue_rate`, `dso_days`, aging brackets — signals a partner
  whose fulfilment quality is dropping *before* churn hits
- **This lead's implied deal size** — vs. the partner's book average, vs. the
  country's average — flags outliers

An **embedded natural-language widget** ("Ask the BI Agent…") lets the
manager type ad-hoc questions — the Self-Querying BI Agent handles them
against the same metric layer, so any question ("what's this partner's
churn trend over the last 6 months?") stays inside the console. Cross-tool
integration: same universe, one shared warehouse, one shared metric layer.

The manager's decision — accept / override / drop plus a free-text note —
becomes the HITL node's output, and the graph continues.

---

## 4. Tech stack — choices & rationale

| Layer | Choice | Rationale |
|---|---|---|
| Orchestration | **LangGraph** state graph | Explicit typed state; native interrupt/resume for HITL; testable pure-function nodes; same pattern proved out in my Agentic CRM tool |
| LLM | Claude (Anthropic SDK), `claude-sonnet-4-6` default | Strong structured output + tool use; Sonnet is the sensible default for a frequent, cost-sensitive per-lead cost. **Explicit Opus escalation:** `claude-opus-4-7` for Negotiation rounds ≥ 2 (harder judgment when partner has counter-proposed) and for Intake when extracted-fields confidence < 0.5 (give ambiguous inquiries extra effort before HITL). Intake / Matching / Monitor stay on Sonnet — routine, high-volume work |
| Tool boundary | **Model Context Protocol** (MCP) | Each of the three external systems (partner capacity, Slack, CRM) is an MCP server the graph calls — swapping the mock for a real Slack MCP is a config change, not a code change |
| MCP transport | stdio (mock/dev), HTTP+SSE (production seam) | stdio matches Claude Desktop's local dev flow; HTTP+SSE is what a hosted MCP looks like |
| State store | LangGraph checkpointer (SQLite locally, Postgres seam) | Persistence for pause/resume; also the audit spine |
| Observability | **Langfuse** (self-hostable) | Trace per lead, cost per lead, per-agent latency, tool-call inspector; open-source keeps it deployable in any environment |
| Data | DuckDB warehouse (bundled), Python data-generator | Same pattern as my Self-Querying BI Agent tool — the simulation is runnable end-to-end without credentials |
| Frontend | Streamlit | Solo build; the same UI plays the role of the "manager console" for the HITL flow |
| Runtime | Python 3.12 | Aligned with the Self-Querying BI Agent's stack (dbt 1.11, LangGraph latest) |

**Deliberately not chosen:** CrewAI (roleplay-oriented; loses the explicit
state guarantee LangGraph gives); AutoGen (Microsoft-flavoured, less
production-shaped for HITL); free-form "agent tools an LLM invents on the fly"
(the MCP boundary is the whole point — the tools are enumerable and typed).

---

## 5. Data model — leads, partners, capacity

The simulation lives in `warehouse/` (DuckDB + a Python data generator, same
pattern as my Self-Querying BI Agent tool's NordLedger seeds). Same fictional
company; a different subsystem (referral pipeline, not KPI reporting).

**Own physical warehouse copy.** This tool ships its own
`warehouse/nordledger.duckdb` — not a shared file with the Self-Querying BI
Agent. The schemas are deliberately compatible: all NordLedger tables the BI
Agent knows (`stg_customers`, `stg_subscriptions`, `mart_roi_partnerships`,
the whole semantic layer) live here too, extended with the extra referral +
orchestration tables listed in §5.1. Cleanest for development (no
cross-project file dependencies) and cleanest for portfolio storytelling
(each tool is self-contained). The BI Agent's embedded widget in the console
(§3.5) imports the BI Agent's Python code but points its `DBT_PROJECT_DIR`
at *this* tool's warehouse — same code, this tool's data.

**Live warehouse.** The `crm_mock_mcp` server persists booked deals directly
into `deal_events` and `partner_engagement_daily` in this warehouse. So the
decision dashboard reads real-time flow — a lead booked five minutes ago
shows up in the manager's console immediately. Not a static snapshot from
Week 1 seeds.

**Referral language.** All referral text in the simulator is **English
only** — a deliberate simplification for portability. Multi-language intake
(DA/NO/SE/DE/NL) is a natural v2 addition; the Intake Agent's schema doesn't
change, only the training/prompt does.

### 5.1 Core tables

| Table | Grain | Purpose |
|---|---|---|
| `referring_partners` | 1 row per ambassador (accountant/advisor firm that sends inquiries) | The "Billy" analog — where leads originate |
| `leads` | 1 row per inbound referral | The event stream the graph processes |
| `fulfillment_partners` | 1 row per bookkeeper / auditor / tax advisor on the marketplace | Match targets — the graph picks from here |
| `partner_capacity` | 1 row per (partner, day) | The live capacity feed served by `partner_capacity_mcp` — updated as deals accept/reject |
| `partner_specializations` | 1 row per (partner, industry, service_type) | The join dimension the matching agent filters on |
| `deal_events` | 1 row per graph transition per lead — **written live by the graph and `crm_mock_mcp`**, not just seeded | The audit spine — plan / match / negotiate / book / escalate — anchored to the Langfuse trace ID. Live-writing means the decision dashboard shows real-time flow |
| `partner_engagement_daily` | 1 row per (partner, day) | Rolling engagement rollup for **fulfillment** partners: response-latency percentiles, accept / decline / no-response counts, cancellation count, active-capacity utilisation, **partner_roi_pct + net_monthly_value snapshots** (joined from the NordLedger `mart_roi_partnerships` — the same table the Self-Querying BI Agent queries). The churn-risk score is computed on top of this table |
| `referring_partner_engagement_daily` | 1 row per (referring_partner, day) | Rolling engagement rollup for **referring** partners (the Billy-analogs). Tracks lead volume sent, conversion rate of referred leads, avg deal size, cancellation rate, and time-to-respond to marketplace inquiries. Fires an intervention when a Billy starts sending fewer / worse leads — a churn precursor on the ambassador side, symmetrical to the fulfillment side |
| `partner_status_events` | 1 row per status change per partner | Audit trail for activate / deactivate / reactivate — including HITL-driven reactivations (see §3.5 + §7.1) |

### 5.2 Injected scenarios (documented in `warehouse/SCENARIOS.md`)

The simulation is deterministic (`RANDOM_SEED=42`) and plants a handful of
scenarios in the seed data so the eval suite has hard cases to assert on:

- **Cold-start market** — a new market (LU, say) with two partners, both
  over-capacity for the whole simulation window
- **Ambiguous inquiry** — a two-line referral with no industry, no country, no
  budget signal
- **Negotiation stall** — a partner that always replies "propose alternative"
  three rounds deep
- **SLA breach** — a booked deal where the partner never files acceptance in
  the CRM within the 24-hour SLA (asserts §3.4a re-routing kicks in)
- **Cross-market imbalance** — DE market saturated, DK market starved
- **Slow-churn partner** — a partner whose response latency, decline rate,
  cancellation count *and* ROI % all creep up / down over an 8-week window
  without a single hard SLA breach (asserts §3.4b intervention fires before
  the partner actually churns, and that Matching's ranking deprioritises them
  on the combined engagement + ROI signal)
- **Unprofitable-but-friendly partner** — a partner who answers on time and
  never declines but whose ROI has been trending negative for 12 weeks
  (asserts ROI alone can trigger deprioritisation in Matching even when
  engagement looks healthy)
- **Slow-referring ambassador** — a referring partner (Billy-analog) whose
  lead volume drops from ~30/month to ~5/month over an 8-week window with
  no explanation (asserts `referring_partner_engagement_daily` fires an
  intervention on the ambassador side, symmetrical to the fulfillment side)
- **Reactivate an inactive partner** — a fulfillment partner marked
  `status = 'inactive'` for 4 months. A lead arrives that no active partner
  can serve; graph pauses at HITL for capacity, manager reactivates the
  partner via the console (writes to `partner_status_events`), graph
  resumes and routes to the newly-active partner (asserts §7.1
  `set_partner_status` + audit trail work end-to-end)

Each scenario is asserted by `eval/test_e2e.py` — the correct behaviour is
already documented before the graph is built.

---

## 6. State machine — the LangGraph

The graph is defined once in `src/graph.py`. Nodes are pure functions of
`LeadState`. Edges are conditional on `LeadState.status`.

```
             classify(intake)
                    │
                    ▼
      ┌── low confidence ──▶ HITL_intake ──▶ END(paused)
      │
      ▼
      match(matching, MCP: partner_capacity)
      │
      ├── no candidates under cap ──▶ HITL_capacity ──▶ END(paused)
      │
      ▼
      negotiate(negotiation, MCP: slack + crm)
      │
      ├── booked ──▶ monitor_arm(register with monitor) ──▶ END(active)
      │
      ├── all candidates declined ──▶ HITL_negotiate ──▶ END(paused)
      │
      └── round budget exceeded ──▶ HITL_negotiate ──▶ END(paused)


    (independently, on schedule)
      monitor.sweep_active_deals()
      │
      ├── all healthy ──▶ digest
      │
      └── SLA breach severity 3 ──▶ re-enter graph as new intake
                                    (with breach context attached)
```

Every node emits a `deal_event` row before returning. The Langfuse trace ID for
the graph run is attached to every event — so any question ("why did lead
L00042 land with partner P019?") is a single SQL join away from the answer.

---

## 7. The MCP boundary

Three MCP servers, one contract per external system. Each is a small Python
package under `mcp_servers/` that speaks stdio (locally) and is trivially
promotable to HTTP+SSE (hosted). Each exposes 2–4 typed tools.

### 7.1 `partner_capacity_mcp`
- `list_candidates(industry, country, service_type, as_of)` → ranked partners with capacity, specialization strength, historical close rate, **partner_roi_pct + trend**, **net_monthly_value + trend**, **churn-risk score + trend**. **Server-side hard filter: only `status = 'active'` partners are returned.**
- `list_dormant_partners(industry, country, service_type)` → partners with `status = 'inactive'` that would otherwise match the lead's need. Called from the manager console when Matching's active-list is empty — surfaces reactivation candidates
- `set_partner_status(partner_id, status, reason)` → activate / deactivate; writes to `partner_status_events` for audit. **Restricted to the HITL flow** — called from the manager console only, never by the graph autonomously
- `get_partner_load(partner_id, as_of)` → current active deals + soft/hard cap
- `hold_capacity(partner_id, lead_id, ttl_hours)` → reserve a slot during negotiation
- `release_capacity(partner_id, lead_id)` → free the reservation on decline/timeout
- `get_partner_engagement(partner_id, window_days)` → the 90-day rollup + churn-risk score + intervention-status flag + **ROI snapshot with trend** (called by Monitor for the weekly report and by the manager console)
- `get_referring_partner_engagement(referring_partner_id, window_days)` → symmetrical rollup for the referring / ambassador side (§5.1 `referring_partner_engagement_daily`)

### 7.2 `slack_mock_mcp`
- `post_to_channel(channel, blocks)` → returns the message timestamp
- `send_dm(user_id, blocks)` → returns the DM ID
- `wait_for_reply(message_id, timeout_seconds)` → polls a mocked inbox
- (In production, this is Slack's real MCP surface — same interface)

### 7.3 `crm_mock_mcp`
- `upsert_lead(lead_state)` → creates or updates the lead record
- `update_stage(lead_id, stage, rationale)` → writes the stage + a reasoning breadcrumb
- `attach_note(lead_id, note)` → free-text notes on the record

**Persistence:** unlike a real Salesforce/HubSpot MCP that pushes to a remote
system, `crm_mock_mcp` writes directly into this tool's warehouse — every
`upsert_lead` and `update_stage` produces a row in `deal_events` and updates
the per-partner rollups. The consequence: the decision dashboard (§3.5) and
the BI Agent's embedded widget see the same live state — a deal booked five
minutes ago shows up in both immediately. In production, a real CRM MCP
would still emit these events to the warehouse in parallel with the CRM
write, so the same read-consistency holds.

**Why MCP and not "just a Python function"?** Because the LLM only sees
enumerable, typed tools; because swapping the mock for a real Slack MCP is a
config-only change; and because MCP is the protocol layer the industry landed
on in 2025–2026 — using it *for a real integration surface* is the whole
portfolio signal.

---

## 8. Failure modes + mitigations

| # | Failure mode | Mitigation |
|---|---|---|
| 1 | Intake agent invents a country / industry | Structured extraction with per-field confidence; low-confidence field → left empty, never guessed |
| 2 | Matching agent proposes an over-capacity partner | Impossible by construction — the MCP tool filters capacity server-side; the agent's ranking runs on a pre-filtered set |
| 3 | Negotiation loop that never converges | Hard `MAX_NEGOTIATION_ROUNDS` cap per candidate + `MAX_CANDIDATES` per lead; on cap → HITL, not silent drop |
| 4 | Slack / CRM MCP server down mid-flow | LangGraph checkpointer holds the state; the graph resumes on the same lead once the tool call retries succeed |
| 5 | Monitor misses an SLA breach | Deterministic sweep on a schedule; the eval suite asserts every scenario in `SCENARIOS.md` is caught |
| 6 | Monitor floods Slack during a live outage | Rate-limit the digest (max 1 severity-3 escalation per partner per 12h); collapse repeats into a single notification |
| 7 | HITL timeout — no manager response | After N hours the interrupt escalates to the next-on-call (round-robin); state stays paused |
| 8 | Cost runaway (agent stuck in a subtle loop) | **Per-lead budget = $2** (Langfuse tracked); graph short-circuits to HITL at **80% consumption ($1.60)** — 3–10× headroom over typical ~$0.20–$0.60 per lead spend, catches loops before they eat the API budget |
| 9 | Two graph runs try to book the same partner concurrently | `hold_capacity` is the source of truth — the second `hold_capacity` call fails and forces re-ranking |
| 10 | An old lead re-enters the graph via monitor (SLA breach) and confuses downstream nodes | Every graph run has a `lead_run_id`; downstream nodes key on that, not on the raw lead id — replays are safe |
| 11 | Churn-risk intervention fires a false positive (a partner briefly unresponsive during holiday but not actually churning) | The score requires the threshold to be sustained ≥ 7 days before the playbook fires; every intervention is manager-confirmable rather than automatic; the routing-weight adjustment is capped and time-bound (auto-reverts on score recovery) |
| 12 | Weekly report noise — same partner flagged for churn every week without action | Report is *comparative* against peer partners on the same score, not absolute; if the score doesn't move week-over-week the section collapses to a one-liner |

**Throughline:** the system routes autonomously when it *can* be autonomous
and pauses cleanly when it *shouldn't* — never guesses onward.

---

## 9. HITL escalation model

HITL is a graph node, not an exception handler. The graph has four HITL
interrupt destinations (§6). Each interrupt:

1. **Persists** the current `LeadState` via LangGraph's checkpointer.
2. **Sends a Slack DM** to the on-call manager. **Routing is fixed per
   market** — each country's ops manager is on-call for their own market's
   leads. If no reply within **4 hours of business time** (08:00–17:00 CET,
   Mon–Fri; the countdown pauses overnight and on weekends), the interrupt
   escalates to a **round-robin fallback** across all on-call managers.
   The DM contains:
   - Lead summary + confidence per extracted field
   - What each agent tried (matching candidates considered, negotiation rounds)
   - The reason the graph paused (which HITL destination, which threshold)
   - Two buttons — **accept the recommendation** / **override** — plus a
     link to the decision dashboard.
3. **Waits** for a `resume(decision, note)` call on the LangGraph server.
4. **Resumes** at the exact same node — the human's decision becomes the
   node's output, and the graph continues.

The manager console (`app/console.py`) is where the manager actually
decides. It filters to paused leads only, and — as described in §3.5 —
surfaces a full decision dashboard (partner health, market health, financial
context, this-lead's implied deal size) reading from the shared NordLedger
warehouse, plus an embedded natural-language widget backed by the
Self-Querying BI Agent's metric layer. The override happens with the
operational picture in view, not from a bare "accept / decline" prompt.

---

## 10. Observability + audit trail

Every LangGraph node is wrapped with a Langfuse span. Every MCP tool call is a
child span. The trace tree for a booked deal typically looks like:

```
lead_run:L00042
├── intake.classify         (450ms, 1.2k tokens, $0.006)
├── match.rank
│   ├── mcp:partner_capacity.list_candidates   (12ms)
│   └── claude.rank                            (820ms, 2.4k tokens, $0.012)
├── negotiate.round_1
│   ├── mcp:slack.send_dm                      (140ms)
│   ├── mcp:slack.wait_for_reply               (paused, 4h32m)
│   └── claude.parse_reply                     (380ms, 0.9k tokens, $0.004)
├── negotiate.book
│   ├── mcp:crm.upsert_lead                    (24ms)
│   └── mcp:partner_capacity.hold_capacity     (11ms)
└── monitor.arm                                (3ms)
```

**What this gives us:**
- Cost per lead (rollable to cost per market, per partner-tier, per hour of day)
- Which agent is the latency floor
- Which tool call fails most often
- Full reasoning chain for any downstream question — "why did we route L00042
  to P019?" is one Langfuse link away

**Audit-ready by construction:** the trace tree *is* the audit trail. No
separate "explain this decision" endpoint needed.

---

## 11. Evaluation strategy

Three tiers, mirroring the pattern from my Self-Querying BI Agent tool.

- **Structural (deterministic, always runs):** each agent's output schema
  validates against Pydantic; every LangGraph edge condition is a pure
  function tested with synthetic `LeadState` objects; every MCP tool has a
  contract test.
- **Scenario suite (`eval/test_e2e.py`):** every entry in
  `warehouse/SCENARIOS.md` is asserted end-to-end against the built graph
  with a mocked MCP layer. The graph *must* land each scenario at the correct
  terminal state (booked / paused-at-hitl-capacity / etc.).
- **Live eval (`pytest -m llm`):** opt-in run against Claude with the full MCP
  mock stack. Asserts the model doesn't regress on the scenario suite — costs
  real tokens, gated behind a marker.

**Warehouse tests (`pytest -m warehouse`):** load the DuckDB simulation, replay
the day-1 lead stream, assert Langfuse traces match the expected shape.

---

## 12. Repo structure

```
multi-agent-lead-orchestration/
├── README.md                 then-vs-now narrative + setup
├── ARCHITECTURE.md            this document
├── PROJECT-OVERVIEW.md        plain-language status
├── requirements.txt
├── .env.example
├── src/
│   ├── config.py             env loading + validation
│   ├── state.py              LeadState Pydantic schema
│   ├── agents/
│   │   ├── intake.py         extraction + confidence per field
│   │   ├── matching.py       candidate ranking against capacity
│   │   ├── negotiation.py    bounded negotiation loop
│   │   └── monitor.py        SLA sweep + escalation
│   ├── graph.py              LangGraph wiring — nodes + edges + HITL interrupts
│   ├── hitl.py               interrupt handlers + resume machinery
│   ├── observability.py      Langfuse tracing hooks
│   └── cli.py                entry point — --scan / --run / --resume
├── mcp_servers/
│   ├── partner_capacity/
│   ├── slack_mock/
│   └── crm_mock/
├── app/
│   ├── streamlit_demo.py     the customer-facing demo (scripted scenarios)
│   └── console.py            the manager HITL console (approve / override)
├── eval/
│   ├── test_agents.py        pure-function tests per agent
│   ├── test_graph.py         edge-condition + interrupt tests
│   ├── test_mcp.py           MCP contract tests
│   └── test_e2e.py           scenario suite (opt-in `warehouse` marker)
├── warehouse/                bundled DuckDB simulation
│   ├── SCENARIOS.md          the injected-scenario contract
│   ├── dbt_project.yml       NordLedger-compatible schema + orchestration extension
│   ├── models/marts/         mart_roi_partnerships (fulfillment) + mart_referring_partner_roi (Week 5, Q20)
│   ├── seeds/                generated CSVs — partners, leads, capacity
│   └── scripts/generate_seeds.py
├── vendor/                   Week 6 addition
│   └── bi_agent/             import target for the console's embedded BI Agent widget (Q18); vendored or pip-installed — decided in Week 6
├── pytest.ini
└── showcase/                 README pointing to live portfolio pages (same pattern as my other tools)
```

---

## 13. Timeline & milestones — 6-week plan

The roadmap estimates 6–8 weeks for this tool vs 4–6 for the Self-Querying BI
Agent. Compressed via AI-assisted build, target is 6 weeks with two hard exit
gates (end of Week 1: simulation runs; end of Week 3: MCP stack works
end-to-end without the LLM).

| Week | Scope | Exit gate |
|---|---|---|
| 1 | Data model + `warehouse/` simulation of NordLedger's referral universe: own warehouse copy (compatible schemas + extra referral/orchestration tables §5.1); `referring_partner_engagement_daily` **and** `partner_engagement_daily` tables; `partner_status_events` audit; **English-only** referral text. Deterministic generator + `SCENARIOS.md` (7 scenarios incl. Billy-slowdown + partner reactivation). | `python warehouse/scripts/generate_seeds.py` runs clean; `dbt build` green (or plain SQL runner); the scenario contract is committed; the schema is verified compatible with the Self-Querying BI Agent's semantic layer (BI Agent widget will work in Week 6) |
| 2 | `LeadState` schema + Intake Agent + Matching Agent skeleton; per-field confidence extraction; ranking against a fixture partner-capacity table | Deterministic tests green on all 5 scenarios' *classification* + *ranking* stages |
| 3 | ✅ **Built & validated.** Three MCP servers (`partner_capacity` 8 tools, `slack_mock` 3+2, `crm_mock` 3) on MCP SDK v2 (`MCPServer`), stdio-capable + in-process via the `MCPStack` sync facade. Matching re-wired (`match_mcp`); in-memory capacity holds as the concurrency guard; crm live-writes (`RT*` events). | ✅ Met: `python -m src.cli --dry-run RL00722` walks classify → match → hold → DM → reply → book against mocks with **no API key in the environment** — 6 MCP calls, 0 LLM calls. 29 warehouse-marked tests green incl. the fixture-vs-MCP parity test |
| 4 | ✅ **Built & validated.** Negotiation Agent full 3×3 loop (Q7) with pluggable drafters (TemplateDrafter deterministic · ClaudeDrafter with Q16 Sonnet→Opus tier routing, recorded per round). LangGraph (`src/graph.py`) with `intake_gate → match → negotiate` + three side-effect-free HITL interrupt nodes routing via `Command(goto=…)`; SqliteSaver checkpointer; `src/hitl.py` payload builders + decision validation (enrich / reactivate / override / drop). CLI: `--run` / `--paused` / `--resume`. | ✅ Met: pause at `hitl_negotiate` in one graph instance, resume `--action override` from a **separate instance over the same SQLite file** → booked. Scenario #2 (ambiguous → enrich-resume, manager provenance at confidence 1.0) and scenario #9 (capacity crunch → reactivate P018 → booked) pass e2e. 41 warehouse + 40 deterministic tests green |
| 5 | ✅ **Built & validated.** Monitor Agent (`src/agents/monitor.py`): close-SLA sweep with re-injection (exclude-breaching-partner via graph `exclude_partner_ids`), three intervention rules (churn_risk ≥65 jitter-tolerant "N of last N+3 days" · roi_decline · referring volume_collapse), Q10 market imbalance, weekly per-partner report with ROI/churn trends + pluggable narrator. `mart_referring_partner_roi` (Q20). Langfuse-compatible `TraceRecorder` (local span trees, optional live forward) wired through MCPStack + graph nodes. CLI: `--scan [--reinject --post-digest]` / `--weekly-report`. | ✅ Met: all 9 scenarios land at expected terminal state (3 NL breaches → re-booked with P015 excluded · P010 churn+roi · P016 roi-only, **not** churn · R009 volume collapse 96% · DE saturated + DK starved Nov · DK saturation echo Dec); trace tree matches the §10 reference shape (node spans → mcp.* child spans); weekly report renders 16 partners with peer-comparative narratives. 55 warehouse + 44 deterministic tests green |
| 6 | ✅ **Built & validated.** `src/console_data.py` (UI-free dashboard layer: partner health 30d-window cards · market health · financial context from the shared NordLedger core · lead-vs-book · paused-leads listing from the checkpoint DB) + `app/console.py` (manager console: reasoning chain, dashboard, decision form, embedded BI widget) + `app/streamlit_demo.py` (4 scripted scenarios through the REAL graph + Monitor sweep section) + `vendor/bi_agent/` (Q18: Rebuild 2's planner-stack pointed at this warehouse — 51 metrics verified loading). Docs + mirror + GitHub push. | ✅ Met: `test_paused_lead_resolves_through_console_flow` — pause → console listing → dashboard renders real partner/market/financial metrics → override decision → booked → paused list empty. 61 warehouse + 46 deterministic tests green |

### Validated stack (pre-build snapshot)
LangGraph latest · Anthropic SDK · MCP Python SDK · Langfuse OSS · Pydantic v2
· DuckDB 1.5 · Streamlit 1.57 · Python 3.12. No live-service dependencies —
the same stack that runs the Self-Querying BI Agent covers all of this.

---

## 14. Out of scope & future extensions

**Not in the initial build:**
- Real Slack integration (using Slack's real MCP server) — the `slack_mock_mcp`
  is a swap-out target, not the eventual endpoint
- Real CRM integration (Salesforce / HubSpot MCPs exist; same swap-out)
- Multi-tenant capacity (one NordLedger instance in the simulation)
- Per-manager routing preferences (HITL currently DMs a single on-call)
- Cost-based re-ranking (currently ranks on close-rate; a v2 could weight by
  historic partner-margin)

**Natural next steps:**
- Add a `preferences_mcp` server per manager so overrides ("skip P019 for
  DACH leads") persist and inform future ranking
- Replay historic Self-Querying BI Agent `warehouse/nordledger.duckdb`
  snapshots as seeded lead streams — cross-tool storytelling in the same
  fictional universe
- A/B test the negotiation-prompt against a baseline via Langfuse's built-in
  variant hooks

---

## 15. Open questions log

Every design decision locked — the questions we've asked, the answers we
committed to, and anything still deliberately deferred. This is the
source-of-truth log: if a section elsewhere in the doc disagrees with a
decision here, this table wins.

### Resolved before Week 1 (data-model decisions)

| Q | Question | Decision |
|---|---|---|
| Q1 | Shared warehouse file or own? | **Own** — this tool ships `warehouse/nordledger.duckdb`; schemas compatible with the Self-Querying BI Agent |
| Q2 | Track engagement on the referring / ambassador side too? | **Yes** — `referring_partner_engagement_daily` symmetrical to the fulfillment side |
| Q3 | Multi-language referral text in the simulator? | **No** — English only, universal, v2 candidate |
| Q4 | Does `crm_mock_mcp` persist deals live to the warehouse? | **Yes** — live-writing; dashboards see real-time flow |
| Q5 | Can HITL reactivate an inactive fulfillment partner? | **Yes** — `set_partner_status` via the manager console, audited in `partner_status_events` |

### Resolved during Week 1 (tuning + operational decisions)

| Q | Question | Decision |
|---|---|---|
| Q6 | Top-K in Matching | **Top-3**, configurable via `MATCHING_TOP_K` env var (default 3) |
| Q7 | `MAX_NEGOTIATION_ROUNDS` × `MAX_CANDIDATES` | **3 × 3 = 9** outreach attempts max per lead |
| Q8 | Post-booking SLA | **48h to accept · 30d to close** — B2B accounting industry standard |
| Q9 | Churn-risk intervention threshold | **Score ≥ 65 sustained 7 days**; symmetrical for `partner_engagement_daily` and `referring_partner_engagement_daily`. Verified against P010's slow-churn seed data — threshold trips 2 months before partner collapse |
| Q10 | Cross-market imbalance detection rule | **Saturated:** avg market util ≥ 90% for 7 consecutive days. **Starved:** avg market util ≤ 20% for 14 consecutive days. Both trip on the Nov-2024 DE / DK seed scenario |
| Q11 | Monitor sweep frequency | **Daily at 06:00 CET** — before business day begins; hourly is overkill for a process where partner response takes 24h |
| Q12 | Weekly report timezone + time | **Mondays at 08:00 CET** — aligned with the daily sweep and manager inbox habits |
| Q13 | HITL on-call routing | **Fixed per market** (each country's ops manager is on-call for their market) with **round-robin fallback** after Q14 timeout |
| Q14 | HITL timeout N | **4h business hours** (08:00–17:00 CET, Mon–Fri). Nights and weekends are skipped — the countdown pauses |
| Q15 | Per-lead token budget | **$2 per lead**; graph short-circuits to HITL at 80% consumption ($1.60). 3–10× headroom over typical spend, catches runaway loops before they eat the API budget |
| Q16 | Sonnet → Opus escalation | **Explicit gate:** Sonnet for Intake / Matching / Monitor (routine, high volume). **Opus** for Negotiation rounds ≥ 2 (harder judgment when partner has counter-proposed) *and* for Intake when the extracted-fields confidence is < 0.5 (give ambiguous inquiries extra effort before HITL) |
| Q17 | Simulation scale | **Resolved by Week 1 build:** 2,160 referral leads, ~24,000 engagement rollup rows — enough for meaningful Langfuse trend analysis. No further tuning needed |
| Q18 | BI Agent embedded widget — integration | **Import + reuse.** Console imports the BI Agent's Python code (`from bi_agent.planner import answer_question`), sets `DBT_PROJECT_DIR` to this tool's warehouse. Vendored under `vendor/bi_agent/` (or pip-installed from the BI Agent's GitHub repo — decide in Week 6) — no subprocess, no HTTP call, no duplicated planner logic |
| Q20 | Referring-partner ROI equivalent | **Yes — computed in Week 5's Monitor** as `referring_partner_net_value` = LTV-of-converted-leads − referring-fees-paid. Materialised in a new `mart_referring_partner_roi` model. Exact formula finalised in Week 5 |

### Deferred to v2

| Q | Question | Note |
|---|---|---|
| Q19 | Per-manager preferences (persistent overrides — "skip P019 for DACH leads") | Out-of-scope §14; every override in v1 is a per-lead decision the manager makes in the console. Persistence is a v2 concern once we see whether managers actually want it |

---

*Vibe Harboe Christensen — AI Automation Engineer | vibegroup.dk*
