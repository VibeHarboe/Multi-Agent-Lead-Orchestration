# Technical Understanding — Multi-Agent Lead Orchestration

**Purpose:** a single visual, step-by-step, A-Z reference for how this system
works. Read top-to-bottom for a full mental model; jump by section for a
specific topic.

**Companion to:**
- `ARCHITECTURE.md` — the design record (why we chose what we chose)
- `PROJECT-OVERVIEW.md` — the plain-language walkthrough
- `README.md` — the public-facing narrative

This doc is the **technical mental model** — how the pieces fit together, what
each one does, and what a real lead's journey looks like in practice.

---

## Contents

1. [The system in 30 seconds](#1-the-system-in-30-seconds)
2. [What the system can do](#2-what-the-system-can-do)
3. [The complete graph](#3-the-complete-graph)
4. [A lead's journey (happy path)](#4-a-leads-journey-happy-path)
5. [The four agents in depth](#5-the-four-agents-in-depth)
6. [LeadState — the object that carries everything](#6-leadstate--the-object-that-carries-everything)
7. [The MCP boundary](#7-the-mcp-boundary)
8. [The four HITL destinations](#8-the-four-hitl-destinations)
9. [The decision dashboard + BI Agent integration](#9-the-decision-dashboard--bi-agent-integration)
10. [The nine scenarios — walked through](#10-the-nine-scenarios--walked-through)
11. [A fully worked example](#11-a-fully-worked-example)
12. [Observability + audit](#12-observability--audit)
13. [The data model](#13-the-data-model)
14. [Safety guarantees — what CAN'T happen](#14-safety-guarantees--what-cant-happen)
15. [The six-week build map](#15-the-six-week-build-map)

---

## 1. The system in 30 seconds

```
   ┌────────────────────────────────────────────────────────────────┐
   │                                                                │
   │   A referral inquiry arrives (email, Slack, form).             │
   │                                                                │
   │   Four AI agents take it from raw text to a booked deal —      │
   │   handing off to a human only when the system genuinely        │
   │   can't decide.                                                │
   │                                                                │
   │   Every decision is traced, every side-effect goes through     │
   │   MCP, every escalation carries its full reasoning chain.      │
   │                                                                │
   └────────────────────────────────────────────────────────────────┘

           INBOUND                                    OUTBOUND
   raw referral text                          booked & monitored deal
           │                                            ▲
           ▼                                            │
   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
   │   INTAKE     │──▶│  MATCHING    │──▶│ NEGOTIATION  │──▶│   MONITOR    │
   │  (Reader)    │   │  (Matcher)   │   │ (Negotiator) │   │  (Watcher)   │
   └──────┬───────┘   └──────┬───────┘   └──────┬───────┘   └──────┬───────┘
          │                  │                  │                  │
          └──────────────────┴──────────────────┴──────────────────┘
                                     │
                     when uncertain / no capacity /
                     stall / SLA breach → pauses at
                                     │
                                     ▼
                             ┌──────────────┐
                             │  HITL GATE   │  ── DM to on-call manager
                             │ (Safety net) │     with full reasoning
                             └──────────────┘
```

**Five parts. One typed state object flows through. Every side-effect goes
through a typed tool. No agent invents; no agent guesses; nothing that pauses
gets lost.**

---

## 2. What the system can do

Twelve concrete capabilities the system exposes. Each is a *first-class*
feature (built and testable), not a stretch goal.

| # | Capability | Where it lives | Runs when |
|---|---|---|---|
| 1 | Classify a plain-language referral into 12 typed fields with per-field confidence + source span | Intake Agent | Every inbound |
| 2 | Rank fulfilment partners against capacity + specialisation + ROI + churn-risk | Matching Agent | Every classified lead |
| 3 | Refuse to route to an over-capacity partner (hard, in-code, not a hint) | `partner_capacity_mcp` | Every match call |
| 4 | Refuse to route to an inactive partner (hard filter) | `partner_capacity_mcp` | Every match call |
| 5 | Auto-open Slack negotiations with the top pick, bounded to 3 rounds × 3 candidates | Negotiation Agent | Every matched lead |
| 6 | Drop a partner and try the next when they don't reply within 24h ("dynamic in-loop re-routing") | Negotiation Agent | Live |
| 7 | Re-route a booked deal that misses its 48h-accept or 30d-close SLA ("dynamic post-booking re-routing") | Monitor Agent | Daily sweep |
| 8 | Score every partner's engagement + ROI trend on a rolling 90d window; fire an intervention playbook when the score crosses 65 for 7d ("predictive churn intervention") | Monitor Agent | Daily sweep |
| 9 | Detect cross-market imbalances (a market saturated 7d ≥90% util, or starved 14d ≤20% util) | Monitor Agent | Daily sweep |
| 10 | Emit a Monday-morning weekly per-partner performance report with LLM-written narrative | Monitor Agent | Weekly cron |
| 11 | Pause on uncertainty and DM the on-call manager with the full reasoning chain, then resume on their decision | HITL layer | 4 interrupt destinations |
| 12 | Answer ad-hoc BI questions inside the manager console via the embedded Self-Querying BI Agent | Console + vendored BI Agent | On demand |

Each of these maps to a specific spot in the graph — see [§3](#3-the-complete-graph).

---

## 3. The complete graph

The whole system is a LangGraph state machine. Every node is a pure function
of `LeadState`. Every edge is a conditional predicate over `LeadState`. Every
side-effect is an MCP call.

```
                              ┌─────────┐
                              │   NEW   │  incoming referral text
                              └────┬────┘
                                   │  classify(intake)
                                   ▼
                          ┌────────────────┐
                          │   CLASSIFIED   │  LeadState fully typed
                          └────┬───────────┘
                               │
                  ┌────────────┴─────────────┐
                  │                          │
       intake_confidence               country + service_type
       ≥ 0.5 AND essentials            missing / < 0.5
       present                              │
                  │                         ▼
                  │                 ┌────────────────┐
                  │                 │  HITL_INTAKE   │◄── ①
                  │                 └────────────────┘
                  ▼
                  match(matching, MCP: partner_capacity)
                  │
       ┌──────────┴──────────┐
       │                     │
       │  top-K ≥ 1          │  no candidate under hard-cap
       ▼                     ▼
   ┌───────────┐        ┌────────────────┐
   │  MATCHED  │        │ HITL_CAPACITY  │◄── ②
   └─────┬─────┘        └────────────────┘
         │
         │  negotiate(negotiation, MCP: slack + crm)
         │
     ┌───┴────────────────────────────────┐
     │                                     │
     │  round budget OK                    │  round budget exceeded
     │  ▼                                  │  OR all candidates declined
     │                                     ▼
   attempt outreach                 ┌────────────────┐
     │                              │HITL_NEGOTIATE  │◄── ③
     │  reply?                      └────────────────┘
     │
     ┌───────┬───────────┬──────────┐
     │       │           │          │
   accept  decline    no-reply    counter
     │       │       (24h SLA)     │
     ▼       │           │         ▼
   ┌───┐     │           │       another round
   │BOOK│    │           │       (if round<3)
   └─┬─┘     │           │
     │       ▼           ▼
     │    next candidate (if any) — ELSE HITL_NEGOTIATE
     ▼
   ┌────────────┐
   │  BOOKED    │  capacity held, crm_mock persisted, deal live
   └─────┬──────┘
         │
         │  monitor_arm  (register with the monitor)
         │
         ▼
      TERMINAL (deal in progress; monitor watches)


  ┌──────────────── monitor.sweep_active_deals (daily 06:00 CET) ─────────────┐
  │                                                                          │
  │  For each active deal:                                                   │
  │    ├─ accept_at > 48h from booking?  → severity-3 breach                 │
  │    ├─ close_at > 30d from booking?   → severity-3 breach                 │
  │    │                                                                    │
  │    └─ on breach: re-inject into graph as new CLASSIFIED                  │
  │                  (Matching runs again, excluding breaching partner)      │
  │                                                                          │
  │  For each partner:                                                       │
  │    ├─ churn-risk ≥ 65 sustained 7d?  → intervention playbook             │
  │    │                                                                    │
  │    └─ severity-3? → HITL_MONITOR ◄── ④                                   │
  │                                                                          │
  │  Every Monday 08:00 CET:                                                 │
  │    └─ per-partner performance report to Slack                            │
  └──────────────────────────────────────────────────────────────────────────┘


  Terminal states:  BOOKED · RESOLVED · LOST
  Paused states:    HITL_INTAKE · HITL_CAPACITY · HITL_NEGOTIATE · HITL_MONITOR
```

**Legend of the four HITL destinations:**

| # | Trigger | Manager does |
|---|---|---|
| ① `HITL_INTAKE` | Ambiguous referral — can't extract country/service | Add missing context, or drop |
| ② `HITL_CAPACITY` | No under-capacity active partner in the market | Reactivate a dormant partner, or wait |
| ③ `HITL_NEGOTIATE` | Round budget exceeded / all candidates declined | Override to a specific partner, or drop |
| ④ `HITL_MONITOR` | Severity-3 SLA breach | Reassign to a different partner |

Each interrupt persists LeadState via LangGraph's checkpointer. Manager
resumes the graph at the exact same node — no work lost.

---

## 4. A lead's journey (happy path)

Follow one referral from arrival to resolution — every field written, every
transition, every side-effect.

```
┌── T = 0s ─── REFERRAL ARRIVES ────────────────────────────────────────────┐
│                                                                          │
│ Trigger:  new row in stg_referral_leads                                   │
│           referral_lead_id = "RL02345"                                    │
│           referring_partner_id = "R009"  (Nordic Partners AB)             │
│           raw_text = "Hi — I'm Anders Lindqvist, Head of Finance at ..."  │
│                                                                          │
│ LeadState: status="new", all extraction fields = None                    │
└──────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌── T + 1.2s ── INTAKE AGENT ──────────────────────────────────────────────┐
│                                                                          │
│  Claude (Sonnet) called with tool_choice = extract_lead_fields.          │
│  Model must call the tool — no free-form text output.                    │
│                                                                          │
│  Tool response:                                                          │
│    country: "SE"          (conf 0.95, span "based in Sweden")            │
│    industry: "retail"     (conf 0.90, span "mid-sized retail chain")     │
│    service_type: "bookkeeping" (conf 0.95, span "bookkeeping partner")   │
│    urgency: "medium"      (conf 0.75, span "starting next quarter")      │
│    deal_size_estimate: 25000 (conf 0.60, span inferred)                  │
│    company_name: "Nordic Retail Group AB" (conf 1.0, span exact)         │
│    contact_name: "Anders Lindqvist"       (conf 1.0)                     │
│    contact_role: "Head of Finance"        (conf 1.0)                     │
│    contact_email: null    (conf 0.0)                                     │
│    timeline: "6 weeks"    (conf 0.9, span "onboarding within 6 weeks")   │
│    tech_stack: "Fortnox + shopify" (conf 0.85)                           │
│    budget_signal: "Budget approved" (conf 0.9)                           │
│                                                                          │
│  intake_confidence = 0.72 (mean across all 12 fields)                    │
│                                                                          │
│  Cost: ~$0.008  (Sonnet, ~1500 tokens)                                   │
│                                                                          │
│ LeadState: status="classified", all fields typed, provenance populated   │
│                                                                          │
│  deal_events += classified                                               │
└──────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌── T + 1.4s ── MATCHING AGENT ────────────────────────────────────────────┐
│                                                                          │
│  Call: partner_capacity_mcp.list_candidates(                             │
│           country="SE", service_type="bookkeeping",                      │
│           industry="retail", as_of=today)                                │
│                                                                          │
│  Server-side hard filter: status='active', under-hard-cap.               │
│  Returns 3 candidates:                                                   │
│    P007  spec 92 · ROI +32% up · churn 18 · resp 3.2h · cap 4/12         │
│    P008  spec 78 · ROI +21% flat · churn 24 · resp 5.1h · cap 7/12       │
│    P009  spec 65 · ROI +15% flat · churn 45 · resp 9.8h · cap 6/11       │
│                                                                          │
│  rank_candidates() composite score:                                      │
│    P007  score 88.4  #1                                                  │
│    P008  score 74.3  #2                                                  │
│    P009  score 60.8  #3                                                  │
│                                                                          │
│  Rationale for each pick auto-generated.                                 │
│                                                                          │
│ LeadState: status="matched", matched_candidates = [P007, P008, P009]    │
│                                                                          │
│  deal_events += matched (agent=matching, partner=P007)                   │
└──────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌── T + 1.5s ── NEGOTIATION AGENT — ROUND 1, CANDIDATE 1 ─────────────────┐
│                                                                          │
│  Call: partner_capacity_mcp.hold_capacity(P007, RL02345, ttl_hours=48)  │
│  → capacity slot reserved                                                │
│                                                                          │
│  Claude (Sonnet — round 1, so still Sonnet per Q16) drafts outreach:    │
│  Call: slack_mock_mcp.send_dm(user=P007_contact, blocks=...)             │
│  → message_id captured                                                   │
│                                                                          │
│  Call: slack_mock_mcp.wait_for_reply(message_id, timeout=24h)            │
│  → graph awaits (checkpointer persists state)                            │
│                                                                          │
│  deal_events += outreach_sent                                            │
└──────────────────────────────────────────────────────────────────────────┘
                                    │
                                    │  (LangGraph pauses here — durable)
                                    │
                                    ▼
┌── T + 18h ── REPLY RECEIVED — ACCEPT ────────────────────────────────────┐
│                                                                          │
│  slack_mock_mcp delivers reply: "Yes, we can take this. Kickoff 15/12."  │
│                                                                          │
│  Negotiation Agent (Sonnet) parses reply → outcome="accept"              │
│                                                                          │
│  Call: crm_mock_mcp.upsert_lead(LeadState) → CRM lead created            │
│  Call: crm_mock_mcp.update_stage("booked", rationale="P007 accepted")    │
│  Call: partner_capacity_mcp.hold_capacity confirmed (not released)       │
│                                                                          │
│ LeadState: status="booked"                                               │
│                                                                          │
│  deal_events += reply_received, booked                                   │
└──────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌── T + 18h + 1s ── MONITOR ARM ──────────────────────────────────────────┐
│                                                                          │
│  monitor.arm(RL02345) registers the deal for tracking.                   │
│  SLA countdowns start:                                                   │
│    - accept SLA: 48h (partner files formal acceptance in CRM)           │
│    - close SLA: 30d (deal must reach 'resolved' status)                 │
│                                                                          │
│  LangGraph run for this lead terminates. Deal is live.                  │
└──────────────────────────────────────────────────────────────────────────┘
                                    │
                                    │  (Monitor now watches independently)
                                    │
                                    ▼
┌── Daily 06:00 CET ── MONITOR SWEEP ─────────────────────────────────────┐
│                                                                          │
│  Day 1  (T+42h):  accept_at filed → OK                                  │
│  Day 8  (T+8d):   status='in_progress' → OK                             │
│  Day 25 (T+25d):  status='in_progress' → OK                             │
│  Day 30 (T+30d):  close SLA at threshold, no breach yet → OK            │
│  Day 32 (T+32d):  status='resolved' → close SLA respected               │
│                                                                          │
│  deal_events += resolved                                                  │
│                                                                          │
│ LeadState: status="resolved" — terminal.                                 │
└──────────────────────────────────────────────────────────────────────────┘
```

**Total end-to-end:** 32 days from referral → resolution.
**Total human time:** 0 minutes (no HITL fired).
**Total cost:** ~$0.35 in API tokens across ~10 LLM calls.
**Total audit trail:** 8 deal_events rows, 1 Langfuse trace tree.

---

## 5. The four agents in depth

### 5.1 Intake Agent — the Reader

```
┌──────────────────────────────────────────────────────────────────────────┐
│                              INTAKE AGENT                                │
│                                                                          │
│  INPUT                          OUTPUT                                   │
│  ─────                          ──────                                   │
│  raw_text: str                  LeadState with:                          │
│  received_at: date               • 12 typed fields                       │
│  referring_partner_id: str?      • provenance{field: {conf, span}}       │
│                                  • intake_confidence (mean)              │
│                                  • status: classified / hitl_intake      │
│                                                                          │
│  ── Decisions the agent makes ──                                         │
│                                                                          │
│  ┌─── Model choice (Q16) ───────────────────────────────────────────┐    │
│  │                                                                  │    │
│  │   Start on SONNET                                                │    │
│  │                                                                  │    │
│  │   IF intake_confidence < 0.5:                                    │    │
│  │       retry on OPUS                                              │    │
│  │       keep the higher-confidence version                         │    │
│  │                                                                  │    │
│  │   IF still ambiguous after Opus:                                 │    │
│  │       status ← "hitl_intake"                                     │    │
│  │       hitl_reason ← "confidence 0.32 < threshold 0.5 —           │    │
│  │                       missing essentials: country, service_type" │    │
│  └──────────────────────────────────────────────────────────────────┘    │
│                                                                          │
│  ── Safety rule ──                                                       │
│                                                                          │
│  Constrained generation via tool_use.                                    │
│  Model MUST call extract_lead_fields — no free-form text output.         │
│  Enum-constrained values for country (ISO-2), service_type, industry,   │
│    urgency → invalid values are impossible.                              │
│  IF a fact isn't in the text: value=null, confidence=0.0.                │
│  NEVER guessed.                                                          │
│                                                                          │
│  ── Audit ──                                                             │
│                                                                          │
│  deal_events += classified                                               │
│  Langfuse span: intake.classify (model, tokens, latency, cost)           │
└──────────────────────────────────────────────────────────────────────────┘
```

### 5.2 Matching Agent — the Matcher

```
┌──────────────────────────────────────────────────────────────────────────┐
│                             MATCHING AGENT                               │
│                                                                          │
│  INPUT                          OUTPUT                                   │
│  ─────                          ──────                                   │
│  LeadState (classified)         LeadState with:                          │
│                                  • matched_candidates: top-K             │
│                                    (partner_id, rank, composite_score,   │
│                                     rationale, roi, churn_risk, ...)     │
│                                  • status: matched / hitl_capacity /     │
│                                            hitl_intake (if essentials    │
│                                            missing)                      │
│                                                                          │
│  ── Steps ──                                                             │
│                                                                          │
│  1. Guard: if country or service_type missing → hitl_intake              │
│                                                                          │
│  2. Call: partner_capacity_mcp.list_candidates(                          │
│              country, service_type, industry, as_of)                     │
│      Server-side hard filters (in MCP, not model):                       │
│         • status = 'active'                                              │
│         • under hard cap                                                 │
│         • specialization covers (industry, service_type)                 │
│                                                                          │
│  3. rank_candidates() — pure function:                                   │
│      composite = 0.40 × specialization                                   │
│                + 0.30 × ROI-normalised                                   │
│                + 0.20 × (100 − churn_risk)                              │
│                + 0.10 × latency-normalised                              │
│      Sort desc, take top-K (MATCHING_TOP_K env, default 3)               │
│                                                                          │
│  4. Build rationale line per pick:                                       │
│     "#1 · score 88.4 · specialisation 92/100 · ROI +32% trending up      │
│      · churn-risk 18 · resp 3.2h · cap 4/12"                             │
│                                                                          │
│  5. If top-K empty → status="hitl_capacity"                              │
│                                                                          │
│  ── Safety rules ──                                                      │
│                                                                          │
│  • Over-capacity partner CAN'T be picked (hard, MCP-enforced)            │
│  • Inactive partner CAN'T be picked (hard, MCP-enforced)                 │
│  • ROI + churn are SOFT signals — influence ranking, not filter          │
│    (so manager can override for strategic reasons via HITL)              │
│                                                                          │
│  ── Audit ──                                                             │
│                                                                          │
│  deal_events += matched (partner_id = top_candidate)                     │
│  Langfuse span: match.rank (candidates_considered, top_score, rationale) │
└──────────────────────────────────────────────────────────────────────────┘
```

### 5.3 Negotiation Agent — the Negotiator

```
┌──────────────────────────────────────────────────────────────────────────┐
│                           NEGOTIATION AGENT                              │
│                                                                          │
│  INPUT                          OUTPUT                                   │
│  ─────                          ──────                                   │
│  LeadState (matched)            LeadState with:                          │
│                                  • negotiation_history[] (each round)    │
│                                  • status: booked / lost /               │
│                                            hitl_negotiate                │
│                                                                          │
│  ── The bounded loop (Q7) ──                                             │
│                                                                          │
│  FOR candidate in matched_candidates[:MAX_CANDIDATES]:  # 3              │
│      hold_capacity(candidate.partner_id, ttl_hours=48)                   │
│                                                                          │
│      FOR round in 1..MAX_NEGOTIATION_ROUNDS:  # 3                        │
│                                                                          │
│          # Q16: escalate to Opus on hard rounds                          │
│          model = OPUS if round >= 2 else SONNET                          │
│                                                                          │
│          slack_mock_mcp.send_dm(candidate.contact, outreach_body)        │
│                                                                          │
│          reply = slack_mock_mcp.wait_for_reply(                          │
│                   timeout_hours=24)  # Q7 dynamic re-routing threshold   │
│                                                                          │
│          IF reply is None:  # 24h passed, no reply                       │
│              release_capacity(candidate.partner_id)                      │
│              # drop candidate; try next (in-loop re-routing)             │
│              BREAK inner loop                                            │
│                                                                          │
│          IF reply == "accept":                                           │
│              crm_mock_mcp.upsert_lead(state)                             │
│              crm_mock_mcp.update_stage("booked", rationale)              │
│              status ← "booked"                                           │
│              RETURN                                                      │
│                                                                          │
│          IF reply == "decline":                                          │
│              release_capacity(candidate.partner_id)                      │
│              BREAK inner loop  # try next candidate                      │
│                                                                          │
│          IF reply == "counter":                                          │
│              # continue the loop, another round with same candidate      │
│              CONTINUE                                                    │
│                                                                          │
│  # All candidates exhausted without a booking                            │
│  status ← "hitl_negotiate"                                               │
│  hitl_reason ← "3 candidates × 3 rounds — no booking"                    │
│                                                                          │
│  ── Safety rules ──                                                      │
│                                                                          │
│  • Bounded loop — MAX 9 outreach attempts per lead                       │
│  • Every outreach has a hold_capacity call — no phantom bookings         │
│  • Every timeout triggers release_capacity — no leaked reservations      │
│  • Round budget breach → HITL, never silent drop                         │
│                                                                          │
│  ── Audit ──                                                             │
│                                                                          │
│  deal_events += outreach_sent, reply_received, booked/lost               │
│  Langfuse spans: negotiate.round_N per attempt                           │
└──────────────────────────────────────────────────────────────────────────┘
```

### 5.4 Monitor Agent — the Watcher

```
┌──────────────────────────────────────────────────────────────────────────┐
│                             MONITOR AGENT                                │
│                                                                          │
│  Runs INDEPENDENTLY of any single lead. Two schedules:                   │
│    - Daily sweep at 06:00 CET                                            │
│    - Weekly report Monday at 08:00 CET                                   │
│                                                                          │
│  ── Three responsibilities ──                                            │
│                                                                          │
│  ┌─── (a) SLA sweep — dynamic re-routing on breach (Q8) ──────────┐      │
│  │                                                                │      │
│  │   FOR each active booked deal:                                 │      │
│  │       accept_age = now - booked_at                             │      │
│  │       close_age  = now - booked_at                             │      │
│  │                                                                │      │
│  │       IF accept_age > 48h AND status not accepted:             │      │
│  │           severity = 3 → re-inject as new intake               │      │
│  │           excluding breaching partner                          │      │
│  │                                                                │      │
│  │       IF close_age > 30d AND status != resolved:               │      │
│  │           severity = 3 → HITL_MONITOR                          │      │
│  │                                                                │      │
│  └────────────────────────────────────────────────────────────────┘      │
│                                                                          │
│  ┌─── (b) Churn scoring + intervention (Q9) ──────────────────────┐      │
│  │                                                                │      │
│  │   FOR each partner (fulfillment + referring):                  │      │
│  │       score = f(response_latency, accept_rate, decline_rate,   │      │
│  │                 no_response_rate, cancellation_rate,           │      │
│  │                 ROI_trend, capacity_utilisation)               │      │
│  │                                                                │      │
│  │       IF score >= 65 SUSTAINED for 7 days:                     │      │
│  │           intervention_playbook:                               │      │
│  │             - DM manager with engagement chart                 │      │
│  │             - propose check-in template                        │      │
│  │             - deprioritise in ranking (via list_candidates)    │      │
│  │             - status → HITL_MONITOR if severity 3              │      │
│  │                                                                │      │
│  │   The score IS exposed to Matching via list_candidates,        │      │
│  │   so pre-churn deprioritisation is automatic while             │      │
│  │   manager decides.                                             │      │
│  └────────────────────────────────────────────────────────────────┘      │
│                                                                          │
│  ┌─── (c) Cross-market imbalance (Q10) ────────────────────────────┐     │
│  │                                                                 │     │
│  │   FOR each market:                                              │     │
│  │       util_7d  = avg market util last 7 days                    │     │
│  │       util_14d = avg market util last 14 days                   │     │
│  │                                                                 │     │
│  │       IF util_7d >= 90%:  saturated → alert                     │     │
│  │       IF util_14d <= 20%: starved   → alert                     │     │
│  └─────────────────────────────────────────────────────────────────┘     │
│                                                                          │
│  ┌─── (d) Weekly report (Q12) — Mondays 08:00 CET ─────────────────┐     │
│  │                                                                 │     │
│  │   For each partner:                                             │     │
│  │     - deals matched / negotiated / booked / closed / cancelled  │     │
│  │     - response-latency percentiles vs peers                     │     │
│  │     - SLA breach count                                          │     │
│  │     - partner_roi_pct + trend                                   │     │
│  │     - net_monthly_value + trend                                 │     │
│  │     - churn_risk_score + trend                                  │     │
│  │     - short LLM-written narrative comparing to peers            │     │
│  │                                                                 │     │
│  │   Rate-limited (max 1 severity-3 escalation / partner / 12h in  │     │
│  │   the daily; weekly is scheduled) so inbox never floods.        │     │
│  └─────────────────────────────────────────────────────────────────┘     │
│                                                                          │
│  ── Audit ──                                                             │
│                                                                          │
│  deal_events += escalated_to_hitl / resolved                             │
│  partner_status_events += (from HITL reactivations)                      │
│  Langfuse spans: monitor.sweep, monitor.weekly_report                    │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## 6. LeadState — the object that carries everything

Every agent reads and writes to the same typed object. This is the "state
graph" in "LangGraph state graph".

```
┌──────────────────────────────────────────────────────────────────────────┐
│                              LeadState                                   │
│                                                                          │
│  ─── Identity (set at arrival) ─────────────────────────────────         │
│  referral_lead_id: str          # "RL02345"                              │
│  raw_text: str                  # the original inquiry                   │
│  received_at: date              # when the referral hit the queue        │
│  referring_partner_id: str?     # the Billy who sent it                  │
│                                                                          │
│  ─── Extracted values (Intake writes; None if not extractable) ──        │
│  country: Country?              # DK | NO | SE | DE | NL | US            │
│  industry: Industry?            # retail | hospitality | ...             │
│  service_type: ServiceType?     # accounting | audit | ...               │
│  urgency: Urgency?              # low | medium | high                    │
│  deal_size_estimate: int?       # in local currency                      │
│  company_name: str?                                                      │
│  contact_name: str?                                                      │
│  contact_role: str?                                                      │
│  contact_email: str?                                                     │
│  timeline: str?                                                          │
│  tech_stack: str?                                                        │
│  budget_signal: str?                                                     │
│                                                                          │
│  ─── Provenance (Intake writes) ──                                       │
│  provenance: dict[str, {confidence: 0-1, source_span: str?}]             │
│  intake_confidence: float       # mean over 12 fields                    │
│                                                                          │
│  ─── Matching output ────────────                                        │
│  matched_candidates: list[{                                              │
│      partner_id: str,                                                    │
│      rank: 1..K,                                                         │
│      composite_score: float,                                             │
│      partner_roi_pct: float,                                             │
│      partner_roi_trend: "up" | "flat" | "down",                          │
│      churn_risk_score: 0..100,                                           │
│      response_latency_p50_hours: float,                                  │
│      close_rate_signal: 0..100,                                          │
│      rationale: str            # one-line explanation for manager        │
│  }]                                                                      │
│                                                                          │
│  ─── Negotiation history (Week 4) ──                                     │
│  negotiation_history: list[{                                             │
│      round: int,                                                         │
│      partner_id: str,                                                    │
│      message_id: str,                                                    │
│      outcome: "accept" | "decline" | "counter" | "no_reply",             │
│      elapsed_hours: float                                                │
│  }]                                                                      │
│                                                                          │
│  ─── Lifecycle ──────────────────────                                    │
│  status: LeadStatus                                                      │
│     new | classified | matched | negotiating                             │
│     booked | resolved | lost                                             │
│     hitl_intake | hitl_capacity | hitl_negotiate | hitl_monitor          │
│                                                                          │
│  hitl_reason: str?                # why the graph paused                 │
│                                                                          │
│  ─── Cost tracking (Q15) ──                                              │
│  tokens_spent_usd: float          # rolling total; hits 80% → circuit    │
│                                                                          │
│  ─── Convenience methods ──                                              │
│  is_ambiguous(threshold=0.5) → bool                                      │
│  field_confidence(field) → float                                         │
│  extracted_fields_count() → int                                          │
│  missing_essential_fields() → list[str]  # country + service_type        │
│  use_opus_for_intake(threshold) → bool                                   │
└──────────────────────────────────────────────────────────────────────────┘
```

**Who writes what:**

```
       Intake        Matching       Negotiation      Monitor        HITL
        │              │                 │              │             │
        ▼              ▼                 ▼              ▼             ▼
  raw_text           matched_          negotiation_    (reads         status
  received_at        candidates        history         all,           (from paused)
  country                              status          writes          hitl_reason
  industry                             tokens_spent    to
  service_type                                         deal_events)
  urgency
  deal_size...
  company_name
  contact_*
  timeline
  tech_stack
  budget_signal
  provenance
  intake_confidence
  status = classified
```

---

## 7. The MCP boundary

The LLM never invokes a raw API. Every side-effect goes through a
Model-Context-Protocol tool. Three MCP servers, one contract per external
system.

```
                    ┌─────────────────────────────────────────┐
                    │       LangGraph (in this repo)          │
                    │                                         │
                    │   INTAKE ── MATCHING ── NEGOTIATION     │
                    │      │         │            │           │
                    │      ▼         ▼            ▼           │
                    │   (LLM       (LLM         (LLM          │
                    │  tool_use)  tool_use)    tool_use)      │
                    └──────┬─────────┬────────────┬───────────┘
                           │         │            │
                           │         │            │  MCP stdio (dev)
                           │         │            │  MCP HTTP+SSE (prod)
                           ▼         ▼            ▼
   ┌───────────────────────────────────────────────────────────┐
   │                                                           │
   │          partner_capacity_mcp                              │
   │          ┌───────────────────────────────────────────┐    │
   │          │ list_candidates(country, service_type,    │    │
   │          │                 industry, as_of)          │    │
   │          │   → filtered + rank-signal-attached list  │    │
   │          │ list_dormant_partners(...)                │    │
   │          │ set_partner_status(pid, status, reason)   │    │
   │          │ hold_capacity(pid, lead_id, ttl_hours)    │    │
   │          │ release_capacity(pid, lead_id)            │    │
   │          │ get_partner_load(pid, as_of)              │    │
   │          │ get_partner_engagement(pid, window_days)  │    │
   │          │ get_referring_partner_engagement(...)     │    │
   │          └───────────────────────────────────────────┘    │
   │                       │                                   │
   │                       ▼                                   │
   │            NordLedger DuckDB warehouse                    │
   │                                                           │
   │──────────────────────────────────────────────────────────│
   │                                                           │
   │          slack_mock_mcp    (real Slack MCP in prod)       │
   │          ┌───────────────────────────────────────────┐    │
   │          │ post_to_channel(channel, blocks)          │    │
   │          │ send_dm(user_id, blocks)                  │    │
   │          │ wait_for_reply(message_id, timeout)       │    │
   │          └───────────────────────────────────────────┘    │
   │                                                           │
   │──────────────────────────────────────────────────────────│
   │                                                           │
   │          crm_mock_mcp      (real Salesforce MCP in prod)  │
   │          ┌───────────────────────────────────────────┐    │
   │          │ upsert_lead(lead_state)                   │    │
   │          │ update_stage(lead_id, stage, rationale)   │    │
   │          │ attach_note(lead_id, note)                │    │
   │          └───────────────────────────────────────────┘    │
   │                       │                                   │
   │                       ▼                                   │
   │            NordLedger DuckDB warehouse                    │
   │            (writes to deal_events + partner_               │
   │             engagement_daily live)                        │
   └───────────────────────────────────────────────────────────┘
```

**Why MCP and not a Python function?**

| Reason | Why it matters |
|---|---|
| Enumerable tools | The LLM sees a *closed set* of typed tools — no invention |
| Swappable | mock → real Slack = config change, not a code change |
| Contract-testable | Each server has its own contract test suite |
| Cross-language | Any MCP server (Python, TS, Rust) works with any client |

---

## 8. The four HITL destinations

Each interrupt is a graph node with its own trigger condition, its own Slack
DM template, and its own resume path.

### ① HITL_INTAKE

```
Trigger:   intake_confidence < 0.5  AND  missing essentials (country
           OR service_type), after both Sonnet and Opus retries.

Slack DM:
   ┌──────────────────────────────────────────────────────────────┐
   │  🚨 Ambiguous referral — needs your call                     │
   │                                                              │
   │  Referral: RL02901 (from Billy — Nordic Partners AB)          │
   │  Received: 2024-12-15 14:32                                   │
   │                                                              │
   │  Raw text: "Hi, we might need some accounting help."         │
   │                                                              │
   │  What we extracted:                                           │
   │    country: null (confidence 0.0)                             │
   │    service_type: "accounting" (confidence 0.6)                │
   │    everything else: null                                      │
   │                                                              │
   │  Overall confidence: 0.11                                     │
   │                                                              │
   │  [ Ask for clarification ]  [ Drop this lead ]                │
   │  🔗 Open in decision dashboard                                │
   └──────────────────────────────────────────────────────────────┘

Manager resume:
   ├─ "Add context: SE, retail, 5-week timeline" → graph re-classifies
   ├─ "Drop this" → status = lost
   └─ (no reply 4h business time) → escalate to next on-call
```

### ② HITL_CAPACITY

```
Trigger:   list_candidates returns 0 rows for this (country, service_type).

Slack DM:
   ┌──────────────────────────────────────────────────────────────┐
   │  ⚠ No active partner under capacity in DK for bookkeeping     │
   │                                                              │
   │  Referral: RL02876 (Nordic Retail Group AB)                   │
   │  Received: 2024-12-15                                         │
   │                                                              │
   │  Situation:                                                   │
   │    All 3 DK bookkeeping partners at hard-cap:                 │
   │      P001 (12/12)  P002 (16/16)  P003 (9/9)                   │
   │                                                              │
   │  Dormant partners in DK that specialise in bookkeeping:       │
   │    P044 (deactivated 2024-06-01, spec-strength 78)            │
   │                                                              │
   │  [ Reactivate P044 ]  [ Queue for tomorrow ]  [ Drop ]         │
   │  🔗 Open in decision dashboard                                │
   └──────────────────────────────────────────────────────────────┘

Manager resume:
   ├─ "Reactivate P044" → set_partner_status(P044, active, "HITL reactivation")
   │                     → graph re-runs Matching, includes P044, resumes negotiation
   ├─ "Queue for tomorrow" → status pending, retry at next sweep
   └─ "Drop" → status = lost
```

### ③ HITL_NEGOTIATE

```
Trigger:   3 candidates × 3 rounds exhausted, no booking.

Slack DM:
   ┌──────────────────────────────────────────────────────────────┐
   │  🔀 Negotiation stalled after 9 attempts                      │
   │                                                              │
   │  Referral: RL02543 (Bergström Advisors AB — SE)               │
   │  Started: 2024-11-08                                          │
   │  Attempts:                                                    │
   │    P007 — declined (busy Q1)                                  │
   │    P008 — 3 rounds no reply → timeout                         │
   │    P009 — 2 rounds "propose alternative" → decline            │
   │                                                              │
   │  All 3 top SE partners exhausted.                             │
   │                                                              │
   │  [ Override: pick another partner ]  [ Escalate to CEO ]       │
   │  [ Drop this lead ]                                          │
   │  🔗 Open in decision dashboard                                │
   └──────────────────────────────────────────────────────────────┘
```

### ④ HITL_MONITOR

```
Trigger:   severity-3 SLA breach OR sustained churn-risk intervention.

Slack DM:
   ┌──────────────────────────────────────────────────────────────┐
   │  ⚠ P010 churn-risk elevated for 8 days — intervention needed  │
   │                                                              │
   │  Partner: P010 Bauer Consulting GmbH (DE)                     │
   │  Churn-risk trajectory:                                       │
   │    2024-11-15: 42                                             │
   │    2024-11-22: 58                                             │
   │    2024-11-29: 71                                             │
   │    2024-12-06: 78  ← now, threshold 65 crossed 8d ago         │
   │                                                              │
   │  Root drivers:                                                │
   │    - response_latency p50: 4h → 52h (13× worse)               │
   │    - ROI trend: +30% → -3.5% (12 weeks)                       │
   │    - active_deals: 8 → 3 (backing off)                        │
   │                                                              │
   │  Auto-actions taken:                                          │
   │    - deprioritised in ranking until score recovers            │
   │                                                              │
   │  Proposed check-in message:                                   │
   │    "Hi Frank — noticed response times slipping. Everything    │
   │     OK? Happy to talk about workload."                        │
   │                                                              │
   │  [ Send the message ]  [ Deactivate P010 ]  [ Ignore 30d ]     │
   │  🔗 Open in decision dashboard                                │
   └──────────────────────────────────────────────────────────────┘
```

**Common HITL resume machinery** (across all four destinations):

```
                                Interrupt fires
                                        │
                                        ▼
                              persist LeadState via LangGraph checkpointer
                                        │
                                        ▼
                              slack DM to on-call manager for market
                                        │
                                        ▼
                              ┌────── Wait ──────┐
                              │                  │
                        4h business time    manager clicks button
                              │                  │
                              ▼                  ▼
                    escalate to round-robin   resume(decision, note)
                                                 │
                                                 ▼
                              LangGraph resumes at the SAME node —
                              decision becomes the node's output,
                              graph continues from there.
                              (No work lost. Ever.)
```

---

## 9. The decision dashboard + BI Agent integration

When a manager clicks a HITL Slack link, they land in the manager console — a
Streamlit app that surfaces the full operational picture *and* embeds the
Self-Querying BI Agent for ad-hoc queries.

```
┌────────────────────────────────────────────────────────────────────────┐
│  MANAGER CONSOLE — RL02901 paused at HITL_CAPACITY                     │
├────────────────────────────────────────────────────────────────────────┤
│                                                                        │
│  ── Lead context ─────────────────────────────────────────────         │
│  Referral: Nordic Retail Group AB (SE, bookkeeping)                    │
│  Received: 2024-12-15 14:32 by Anders Lindqvist, Head of Finance       │
│  Timeline: 6 weeks · Deal size est. 25 000 SEK/mo · Tech: Fortnox      │
│                                                                        │
│  ── What the graph tried ────────────────────────────────────          │
│  Matching considered 4 SE partners:                                    │
│    P007 · at hard-cap (12/12) — no room                                │
│    P008 · at hard-cap (16/16) — no room                                │
│    P009 · at hard-cap (9/9) — no room                                  │
│    P044 · INACTIVE since 2024-06-01                                    │
│                                                                        │
│  Dormant candidates (would qualify if reactivated):                    │
│    P044 · spec 78 · was_active_until 2024-06-01 · reason: low volume   │
│                                                                        │
│  ── Partner health snapshot ─────────────────────────────────          │
│  ┌─── P007 ──────────┐  ┌─── P008 ──────────┐  ┌─── P009 ──────────┐  │
│  │ ROI  +32% up      │  │ ROI  +21% flat    │  │ ROI  +15% flat    │  │
│  │ NMV  15k          │  │ NMV  11k          │  │ NMV  7k           │  │
│  │ Churn 18 ✓        │  │ Churn 24 ✓        │  │ Churn 45 ⚠        │  │
│  │ Resp 3.2h         │  │ Resp 5.1h         │  │ Resp 9.8h         │  │
│  └───────────────────┘  └───────────────────┘  └───────────────────┘  │
│                                                                        │
│  ── SE market health ────────────────────────────────────────         │
│  ┌────────────────────────────────────────────────────────────────┐    │
│  │ Active partners under cap:  0 / 3                              │    │
│  │ Avg capacity util (7d):    102% ⚠ (saturated 6d in a row)      │    │
│  │ Conversion rate (30d):      48%                                │    │
│  │ SLA breach count (30d):      2                                 │    │
│  │                                                                │    │
│  │ [Chart: SE utilisation last 90 days — trending up sharply]     │    │
│  └────────────────────────────────────────────────────────────────┘    │
│                                                                        │
│  ── This lead vs Nordic Retail's book ───────────────────────         │
│  Deal size:  25k SEK/mo                                                │
│  Book avg:   18k SEK/mo (this is 40% above their average)              │
│  → Non-trivial opportunity                                             │
│                                                                        │
│  ── Ask the BI Agent (embedded) ─────────────────────────────         │
│  ┌────────────────────────────────────────────────────────────────┐    │
│  │ Q: What's SE overdue_rate trend last 6 months?                 │    │
│  │                                                                │    │
│  │ (BI Agent runs against the shared NordLedger warehouse and     │    │
│  │  returns cited answer + chart + suggested drill-downs)         │    │
│  └────────────────────────────────────────────────────────────────┘    │
│                                                                        │
│  ── Your decision ────────────────────────────────────────────         │
│                                                                        │
│  ┌───────────────────────────┐  ┌───────────────────────────┐         │
│  │  ✅ Reactivate P044        │  │  ⏸ Queue for tomorrow      │         │
│  │  (was inactive since       │  │  (retry at 06:00 sweep)   │         │
│  │  2024-06-01)              │  │                          │         │
│  └───────────────────────────┘  └───────────────────────────┘         │
│                                                                        │
│  ┌───────────────────────────┐  Notes: ___________________________     │
│  │  ❌ Drop this lead         │                                        │
│  └───────────────────────────┘  [ Submit ]                             │
│                                                                        │
└────────────────────────────────────────────────────────────────────────┘

Behind the scenes, on manager clicking "Reactivate P044":
   1. console calls partner_capacity_mcp.set_partner_status(
          P044, "active", "HITL reactivation for RL02901")
      → new row in partner_status_events
      → stg_partners.status updated
   2. LangGraph.resume(RL02901, decision="reactivate_and_match",
                        note="P044 reactivated")
   3. Graph re-runs Matching (P044 now in candidate pool)
   4. Negotiation proceeds with P044 as top pick
```

**How the BI Agent widget works** (Q18):

```
   Console (Streamlit app in Rebuild 3)
        │
        │   from bi_agent.planner import answer_question
        │   os.environ["DBT_PROJECT_DIR"] = "./warehouse"  # Rebuild 3's
        │
        │   answer = answer_question(user_question, config, catalog, mf)
        │
        ▼
   BI Agent's planner (imported as library, not subprocess)
        │
        │   Calls the SAME MetricFlow instance pointing at Rebuild 3's
        │   NordLedger DuckDB — the same 51 metrics, the same semantic
        │   layer.
        │
        ▼
   Query results with citations
        │
        ▼
   Rendered inside the console — the manager never leaves.
```

---

## 10. The nine scenarios — walked through

Each scenario is a specific data pattern that the eval suite asserts against.

### Scenario #1 — cold-start-market (DK, 2024-12)

```
Data pattern:                          Graph response:
────────────                          ────────────────
All 3 DK partners (P001-P003)    →    Matching runs
at hard-cap for the whole month       list_candidates returns []
                                      status → HITL_CAPACITY
                                      Console offers dormant candidates
                                      (list_dormant_partners)
                                      Manager decides
```

### Scenario #2 — ambiguous-inquiry

```
Data pattern:                          Graph response:
────────────                          ────────────────
"Hi, partnership?"               →    Intake tries Sonnet
                                      intake_confidence = 0.05
                                      Retries with Opus
                                      Still 0.05
                                      status → HITL_INTAKE
                                      Manager adds context OR drops
```

### Scenario #3 — negotiation-stall (SE, 2024-11)

```
Data pattern:                          Graph response:
────────────                          ────────────────
2 SE leads with 3 rounds of      →    Negotiation runs
outreach + reply cycles, no           Round 1 → "propose_alternative"
booked event                          Round 2 → "propose_alternative"
                                      Round 3 → "propose_alternative"
                                      → Next candidate
                                      All 3 exhausted
                                      status → HITL_NEGOTIATE
```

### Scenario #4 — sla-breach (NL, 2024-09/10)

```
Data pattern:                          Graph response:
────────────                          ────────────────
3 NL leads booked, no             →   Monitor daily sweep
resolved_at after 45+ days            Detects close_age > 30d
                                      severity-3 breach
                                      Re-injects as new intake
                                      Matching excludes original partner
                                      New candidate offered
```

### Scenario #5 — cross-market-imbalance (DE saturated, DK starved, Nov 2024)

```
Data pattern:                          Graph response:
────────────                          ────────────────
DE 3/3 at 100%+ util for 7d      →    Monitor cross-market sweep
DK 3/3 at 11% util for 14d            Fires both alerts:
                                        - DE saturated (7d ≥90%)
                                        - DK starved (14d ≤20%)
                                      Manager sees imbalance in
                                      weekly report + daily digest
```

### Scenario #6 — slow-churn-partner (P010, DE, 2024-07 → 2024-12)

```
Data pattern:                          Graph response:
────────────                          ────────────────
P010 latency 4h → 52h            →    Monitor computes churn_risk
P010 ROI +30% → -3.5%                 Score climbs 15 → 78 over 6 months
Over 6 months                         Threshold 65 crossed at 2024-10-15
                                      Sustained 7d confirmed 2024-10-22
                                      Intervention fires:
                                        - HITL_MONITOR DM sent
                                        - list_candidates deprioritises
                                          P010 automatically for new leads
```

### Scenario #7 — unprofitable-but-friendly (P016, US, Q4 2024)

```
Data pattern:                          Graph response:
────────────                          ────────────────
P016 fast response + high accept →   ROI-decline dominates score
P016 ROI +30% → -15% over Q4         churn_risk climbs even though
                                     engagement looks healthy
                                     Ranking deprioritises P016
                                     for new leads; Monitor DMs manager
                                     on ROI-trend
```

### Scenario #8 — slow-referring-ambassador (R009, SE, Q4 2024)

```
Data pattern:                          Graph response:
────────────                          ────────────────
R009 leads_sent: 30/mo → 5/mo    →    referring_partner_engagement_daily
Over Q4 2024                          computes ambassador churn_risk
                                      Threshold crossed
                                      HITL_MONITOR intervention
                                      Manager sees Billy-analog needs
                                      a check-in
```

### Scenario #9 — reactivate-inactive-partner (P018, US)

```
Data pattern:                          Graph response:
────────────                          ────────────────
P018 status = inactive since     →    Any US lead that Matching can't
2024-08-01                            place among active partners
                                      → HITL_CAPACITY
                                      Console lists dormant candidates
                                      (P018 with spec-strength shown)
                                      Manager clicks "Reactivate P018"
                                      → set_partner_status writes to
                                        partner_status_events
                                      → Graph re-runs Matching
                                      → P018 now in top-K
```

---

## 11. A fully worked example

**Referral text (arrives Dec 15, 2024 via Billy's Slack):**

> Hi team,
>
> Referring a client: Bauer & Söhne GmbH — a mid-market software company
> in Munich, Germany. Frank Bauer (CEO, frank@bauer-soehne.de) is looking
> for a new tax_advisory partner for their 2024 books. Revenue base is
> around EUR 2M, stack is DATEV + Salesforce. Wants to onboard within 4
> weeks. Budget already approved.
>
> — Karsten (Nordic Partners AB)

**Step 1: Intake Agent extracts (Sonnet, ~1.4k tokens, $0.006):**

```json
{
  "country":            {"value": "DE",          "confidence": 1.0,  "source_span": "in Munich, Germany"},
  "industry":           {"value": "tech",        "confidence": 0.95, "source_span": "software company"},
  "service_type":       {"value": "tax_advisory","confidence": 1.0,  "source_span": "tax_advisory partner"},
  "urgency":            {"value": "high",        "confidence": 0.75, "source_span": "within 4 weeks"},
  "deal_size_estimate": {"value": 24000,         "confidence": 0.65, "source_span": "EUR 2M revenue base (est. 1% for advisory)"},
  "company_name":       {"value": "Bauer & Söhne GmbH",   "confidence": 1.0},
  "contact_name":       {"value": "Frank Bauer", "confidence": 1.0},
  "contact_role":       {"value": "CEO",         "confidence": 1.0},
  "contact_email":      {"value": "frank@bauer-soehne.de", "confidence": 1.0},
  "timeline":           {"value": "4 weeks",     "confidence": 0.95, "source_span": "onboard within 4 weeks"},
  "tech_stack":         {"value": "DATEV + Salesforce",   "confidence": 0.95},
  "budget_signal":      {"value": "budget approved",      "confidence": 0.95}
}
```

**intake_confidence = 0.92** → far above threshold, no Opus retry needed.

**Step 2: Matching Agent ranks (Sonnet + MCP, ~800ms, $0.005):**

Call: `partner_capacity_mcp.list_candidates(country="DE", service_type="tax_advisory", industry="tech", as_of="2024-12-15")`

Returns 3 DE tax advisors, ranked by composite score:

| Rank | Partner | Spec | ROI | Trend | Churn | Latency | Cap | Score | Rationale |
|---|---|---|---|---|---|---|---|---|---|
| 1 | P011 | 88 | +28% | up | 22 | 4.1h | 5/12 | 82.7 | Strong specialisation · healthy trajectory |
| 2 | P012 | 74 | +19% | flat | 31 | 6.3h | 8/13 | 69.4 | Good coverage · nearing soft cap |
| 3 | P010 | 92 | -3.5% | **down** | **78** ⚠ | 52h | 3/11 | 42.1 | ⚠ Elevated churn-risk · declining ROI |

The slow-churn P010 has the highest raw specialisation score but its churn-risk of 78 + declining ROI drop it to #3.

**Step 3: Negotiation Agent — Round 1 with P011 (Sonnet, $0.004):**

```
19:31:03  hold_capacity(P011, RL02901, ttl=48h) → OK
19:31:04  slack_mock_mcp.send_dm(P011_contact, blocks=[...])
          → message_id = "msg_a7f21"
          → deal_events += outreach_sent
```

**Step 4: 6h later — reply received:**

```
01:24:17  slack_mock_mcp.wait_for_reply returns:
          "Yes, this fits our current portfolio. When can we kick off?"
19:24:18  Parse outcome = "accept"
19:24:19  crm_mock_mcp.upsert_lead(state)     → NL02901 in CRM
19:24:19  crm_mock_mcp.update_stage("booked", rationale="P011 accepted round 1")
19:24:20  partner_capacity_mcp.hold_capacity confirmed
19:24:21  monitor.arm(RL02901)
          → deal_events += reply_received, booked, monitor_armed
```

**LeadState now:**
```
referral_lead_id: "RL02901"
status: "booked"
matched_candidates: [P011, P012, P010]  # kept for audit
negotiation_history: [
    {round: 1, partner: "P011", outcome: "accept", elapsed_hours: 6.1}
]
tokens_spent_usd: 0.015     # 0.75% of $2 budget
```

**Step 5: 28 days later — monitor sweep on Day 28:**

```
06:00 CET  Monitor sweeps active deals
           - RL02901 · booked_at 2024-12-15 · accept_at 2024-12-16 (day+1)
             · in_progress
             · 28 days since booking → within 30d close SLA
             → OK

           status stays "booked"
```

**Step 6: 32 days later — P011 files resolution:**

```
2025-01-16 15:47  crm_mock_mcp receives resolution from P011:
                  "Delivered — kickoff successful, first quarterly review 2025-02-15"
                  → crm_mock_mcp.update_stage("resolved", rationale)
                  → deal_events += resolved
                  status → "resolved"

Total end-to-end: 32 days, 0 HITL fires, $0.015 in API tokens.

Full audit trail (7 deal_events rows):
   1. classified   (2024-12-15 14:32)
   2. matched      (2024-12-15 14:32)
   3. outreach_sent (2024-12-15 19:31)
   4. reply_received (2024-12-16 01:24)
   5. booked       (2024-12-16 01:24)
   6. monitor_armed (2024-12-16 01:24)
   7. resolved     (2025-01-16 15:47)
```

---

## 12. Observability + audit

Every LangGraph node is wrapped as a Langfuse span. Every MCP call is a child
span. The trace tree for the worked example looks like:

```
trace: lead_run RL02901                                    (elapsed 32d)
│
├── intake.classify                       (1.4s, 1420 tokens, $0.006, Sonnet)
│
├── match.rank
│   ├── mcp.partner_capacity.list_candidates    (12ms, 3 rows)
│   └── claude.rank                      (820ms, 940 tokens, $0.005, Sonnet)
│
├── negotiate.round_1
│   ├── mcp.partner_capacity.hold_capacity      (11ms)
│   ├── claude.draft_outreach            (620ms, 780 tokens, $0.004, Sonnet)
│   ├── mcp.slack.send_dm                (140ms)
│   └── mcp.slack.wait_for_reply         (paused, 6h)
│
├── negotiate.parse_reply                (380ms, 220 tokens, $0.001, Sonnet)
│
├── negotiate.book
│   ├── mcp.crm.upsert_lead              (24ms)
│   ├── mcp.crm.update_stage             (18ms)
│   └── mcp.partner_capacity.hold_capacity confirmed (8ms)
│
├── monitor.arm                          (3ms)
│
└── (32 days later, in a separate trace tree)
    monitor.sweep_active_deals
    └── RL02901 → status="in_progress", no action
    ...
    monitor.sweep_active_deals (day 32)
    └── RL02901 → status="resolved"
```

**What Langfuse gives us:**

| Rollup | Answers |
|---|---|
| Per lead | Total cost, total elapsed, agent breakdown |
| Per agent | Latency percentile, tokens/min, error rate |
| Per MCP call | Failure rate, timeout rate, retry count |
| Per partner | How much LLM effort per successful booking |
| Per market | Cost-per-booked-lead |
| Per hour | Peak API usage — for rate limit planning |

**Independent audit spine: `deal_events` table**

Every event has:
```
(event_id, referral_lead_id, partner_id, event_type, event_at, agent_name, rationale)
```

Combined with Langfuse trace ID, any question about "why did we route
RL02901 to P011?" is answered by one JOIN:

```sql
SELECT event_at, event_type, agent_name, rationale
FROM stg_deal_events
WHERE referral_lead_id = 'RL02901'
ORDER BY event_at;
```

+ Langfuse link for the full LLM reasoning chain per node.

---

## 13. The data model

The warehouse has **15 tables**: 7 NordLedger core (shared with the
Self-Querying BI Agent) + 8 Rebuild 3 orchestration extension.

```
                                     NORDLEDGER CORE
                                 (shared with BI Agent)
   ┌──────────────┐         ┌──────────────┐         ┌──────────────┐
   │  customers   │◄────────┤ subscriptions│         │  nps_surveys │
   │              │         │              │         │              │
   └──────┬───────┘         └──────────────┘         └──────────────┘
          │
          ├──── customer_id ────► ┌──────────────┐   ┌──────────────┐
          │                       │    leads     │   │   invoices   │
          │                       │              │   │              │
          │                       └──────────────┘   └──────────────┘
          │
          │                                          ┌──────────────┐
          │                                          │upsell_events │
          │                                          └──────────────┘
          │
          │  ┌──────────────┐    partner_id           ┌──────────────┐
          └─►│   partners   │◄──────────────────────► │mart_roi_     │
             │  (fulfill)   │                         │ partnerships │
             └──────┬───────┘                         └──────────────┘
                    │                                            (Rebuild 2's
                    │                                             semantic layer
                    │                                             is built here)
                    │
                    │
                    │       ─── REBUILD 3 ORCHESTRATION EXTENSION ───
                    │
                    │
                    │       ┌──────────────────────────┐
                    │       │  referring_partners      │  (Billy-analogs)
                    │       │                          │
                    │       └───────┬──────────────────┘
                    │               │
                    │               │ referring_partner_id
                    │               │
                    │               ▼
                    │       ┌──────────────────────────┐
                    │       │  referral_leads          │
                    │       │  (inbound event stream)  │
                    │       │                          │
                    │       └───────┬──────────────────┘
                    │               │
                    │               │ referral_lead_id
                    │               │
                    │               ▼
                    ├──────►┌──────────────────────────┐
                    │       │  deal_events             │
                    │       │  (audit spine)           │
                    │       └──────────────────────────┘
                    │
                    ├──────►┌──────────────────────────┐
                    │       │  partner_capacity        │
                    │       │  (1 row per partner/day) │
                    │       └──────────────────────────┘
                    │
                    ├──────►┌──────────────────────────┐
                    │       │  partner_specializations │
                    │       │  (industry × service)    │
                    │       └──────────────────────────┘
                    │
                    ├──────►┌──────────────────────────┐
                    │       │partner_engagement_daily  │
                    │       │(rolling rollup incl. ROI)│
                    │       └──────────────────────────┘
                    │
                    ├──────►┌──────────────────────────┐
                    │       │partner_status_events     │
                    │       │(activate/deactivate audit)│
                    │       └──────────────────────────┘
                    │
                    │
       (referring   │       ┌──────────────────────────┐
        partner)    └──────►│referring_partner_        │
                            │engagement_daily          │
                            │(ambassador-side rollup)  │
                            └──────────────────────────┘
```

**Who reads what** (agents ↔ tables):

| Table | Intake | Matching | Negotiation | Monitor | Console |
|---|:---:|:---:|:---:|:---:|:---:|
| stg_partners | | ✓ | | | ✓ |
| partner_capacity | | ✓ | ✓ (hold) | | ✓ |
| partner_specializations | | ✓ | | | ✓ |
| partner_engagement_daily | | ✓ (via list_candidates) | | ✓ | ✓ |
| referring_partner_engagement_daily | | | | ✓ | ✓ |
| partner_status_events | | | | | ✓ (writes reactivations) |
| referral_leads | | ✓ (reads recent) | | | ✓ |
| deal_events | ✓ writes | ✓ writes | ✓ writes | ✓ writes | ✓ reads |
| NordLedger core (customers, subs, etc.) | | | | ✓ (LTV signals) | ✓ (BI Agent) |

---

## 14. Safety guarantees — what CAN'T happen

Failure modes and how they're structurally prevented — not "we asked the
model nicely", but "impossible by construction".

```
   ┌──────────────────────────────────────────────────────────────────────┐
   │  ❌ CAN'T HAPPEN                    ✅ HOW IT'S PREVENTED             │
   ├──────────────────────────────────────────────────────────────────────┤
   │                                                                      │
   │  Model invents a partner            Constrained generation:          │
   │  that doesn't exist                 tool schema enumerates the       │
   │                                     partners; MCP returns the        │
   │                                     candidate list.                  │
   │                                                                      │
   │  Model books an over-capacity       Server-side hard filter in       │
   │  partner                            partner_capacity_mcp. The model  │
   │                                     literally cannot see over-cap    │
   │                                     partners in list_candidates.     │
   │                                                                      │
   │  Model routes to an inactive        Server-side hard filter: status  │
   │  partner                            = 'active' at query time.        │
   │                                                                      │
   │  Model fabricates a country /       Tool schema enum-constrained;    │
   │  service_type / industry            invalid values → tool call       │
   │                                     rejected, agent re-plans.        │
   │                                                                      │
   │  Model sends a Slack DM the         Slack MCP tool schema is typed;  │
   │  graph can't audit                  every send_dm returns a          │
   │                                     message_id logged to Langfuse    │
   │                                     + deal_events.                   │
   │                                                                      │
   │  Model writes a CRM record          Same — CRM MCP typed schema;     │
   │  the graph can't audit              every write logged.              │
   │                                                                      │
   │  Negotiation loops forever          MAX_NEGOTIATION_ROUNDS ×         │
   │                                     MAX_CANDIDATES = 9 hard cap.     │
   │                                     On exhaustion → HITL_NEGOTIATE.  │
   │                                                                      │
   │  Cost runaway (LLM stuck in         Per-lead budget $2, circuit-     │
   │  a subtle loop)                     break at 80% ($1.60). Graph      │
   │                                     short-circuits to HITL.          │
   │                                                                      │
   │  Ambiguous input → guessed          Intake retries on Opus if        │
   │  answer                             Sonnet confidence < 0.5. If      │
   │                                     still ambiguous → HITL_INTAKE.   │
   │                                     Field values never populated     │
   │                                     without provenance.              │
   │                                                                      │
   │  SLA breach silently ignored        Daily monitor sweep detects      │
   │                                     breaches; severity 3 → re-route  │
   │                                     or HITL_MONITOR.                 │
   │                                                                      │
   │  Partner churn caught too late      Rolling 90d engagement + ROI     │
   │                                     score; 65 sustained 7d fires     │
   │                                     intervention playbook BEFORE     │
   │                                     the partner actually leaves.     │
   │                                                                      │
   │  MCP server down mid-flow           LangGraph checkpointer holds     │
   │                                     state. Retries succeed → graph   │
   │                                     resumes at same node.            │
   │                                                                      │
   │  HITL timeout (manager doesn't      After 4h business time, escalate │
   │  respond)                           to round-robin next-on-call.     │
   │                                     Lead never lost; state stays     │
   │                                     paused.                          │
   │                                                                      │
   │  Manager overrides + re-route       partner_status_events audit      │
   │  unaudited                          trail. Every override includes   │
   │                                     manager ID + reason + timestamp. │
   │                                                                      │
   │  Concurrent bookings for the        hold_capacity is the source of   │
   │  same partner                       truth. Second concurrent         │
   │                                     hold_capacity fails → forces     │
   │                                     re-ranking.                      │
   └──────────────────────────────────────────────────────────────────────┘
```

---

## 15. The six-week build map

Where each capability comes online.

```
Week 1    DATA + SIMULATION
──────    ─────────────────
          ✅ NordLedger warehouse (15 tables, 70k rows)
          ✅ 9 injected scenarios in SCENARIOS.md
          ✅ BI Agent schema compatibility verified
          Exit: 150/150 dbt tests green                    ✅ DONE


Week 2    LeadState + INTAKE + MATCHING skeleton         ✅ DONE
──────    ────────────────────────────────
          ✅ LeadState Pydantic schema (12 fields + provenance)
          ✅ Intake Agent — Claude tool-use (Sonnet + Opus escalation)
          ✅ Matching Agent — pure ranking + DuckDB fixture layer
          Exit: All 9 scenarios pass classification+ranking  ✅ DONE


Week 3    3 MCP SERVERS wired end-to-end              ✅ DONE
──────    ──────────────────────────────
          partner_capacity_mcp (8 tools)
          slack_mock_mcp       (3 tools)
          crm_mock_mcp         (3 tools)
          Matching + Negotiation call REAL MCP endpoints, not fixtures
          Exit: `--dry-run RL02345` walks the graph against mocks
                without touching Claude — proves plumbing works


Week 4    NEGOTIATION LOOP + HITL INTERRUPTS          ✅ DONE
──────    ──────────────────────────────────
          Negotiation Agent full 3×3 bounded loop
          All 4 HITL interrupt destinations wired
          LangGraph checkpointer to SQLite
          Exit: Paused lead can resume from disk cleanly


Week 5    MONITOR + LANGFUSE                          ✅ DONE
──────    ──────────────────
          Monitor Agent — all 3 responsibilities (SLA + churn + weekly)
          Langfuse observability (trace per lead, cost tracking)
          `--scan` proactive sweep + re-injection on breach
          Exit: Every scenario in SCENARIOS.md lands at expected state,
                Langfuse trace tree matches reference shape


Week 6    STREAMLIT DEMO + HITL CONSOLE + DOCS        ✅ DONE
──────    ────────────────────────────────────
          Customer-facing Streamlit demo (scripted scenarios)
          Manager HITL console with decision dashboard
          Embedded Self-Querying BI Agent widget
          README / PROJECT-OVERVIEW / showcase docs updated
          Mirror sync + GitHub push
          Exit: Full end-to-end demo runs; git-mirror byte-identical
```

---

## Glossary

| Term | Definition |
|---|---|
| **Referral** | An SMB inquiry sent to the marketplace by a referring partner (Billy-analog) |
| **Fulfillment partner** | Bookkeeper / auditor / tax advisor on the marketplace who fulfils the referred work |
| **Referring partner** | Ambassador who sends the referral (e.g. Billy) |
| **LeadState** | Typed Pydantic object flowing through the LangGraph, carrying every field, every decision, every audit breadcrumb |
| **Provenance** | Per-field extraction metadata: confidence 0-1 + source_span |
| **MCP** | Model Context Protocol — the tool boundary between the graph and external systems |
| **HITL** | Human-in-the-Loop — the four interrupt destinations where the graph pauses for manager decision |
| **Composite score** | Matching's rank signal: 0.4 × spec + 0.3 × ROI + 0.2 × (100−churn) + 0.1 × latency |
| **Churn risk score** | 0-100 computed from engagement + ROI trend; threshold 65 sustained 7d fires intervention |
| **Cross-market imbalance** | Saturated ≥90% for 7d OR starved ≤20% for 14d |
| **Decision dashboard** | The Streamlit view a manager opens from a HITL Slack link; shows lead + partner health + market health + BI Agent widget |
| **Langfuse trace** | Nested span tree per lead — every node, every MCP call, cost, latency |
| **deal_events** | The audit spine — one row per graph transition per lead — anchored to Langfuse trace ID |

---

*Vibe Harboe Christensen — AI Automation Engineer | vibegroup.dk*

*This document is generated as part of the Multi-Agent Lead Orchestration
build. Update sections whenever the corresponding architecture changes;
`ARCHITECTURE.md` §15 remains the source of truth for design decisions.*
