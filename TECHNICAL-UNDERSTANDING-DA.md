# Teknisk Forståelse — Multi-Agent Lead Orchestration

**Formål:** én samlet visuel, trin-for-trin A-Z reference for hvordan systemet
virker. Læs top-til-bund for den fulde mentale model; hop pr. sektion for et
specifikt emne.

**Følgesvend til:**
- `ARCHITECTURE.md` — design-recorden (hvorfor vi valgte som vi gjorde)
- `PROJECT-OVERVIEW.md` — gennemgangen i almindeligt sprog
- `README.md` — den udadvendte fortælling

Dette dokument er den **tekniske mentale model** — hvordan delene passer
sammen, hvad hver enkelt gør, og hvordan et rigtigt leads rejse ser ud i
praksis.

*Sprognote: al forklarende tekst er på dansk; tekniske termer, diagrammer og
kodeblokke er bevaret på engelsk, så de matcher koden og de øvrige dokumenter
1:1.*

---

## Indhold

1. [Systemet på 30 sekunder](#1-systemet-på-30-sekunder)
2. [Hvad systemet kan](#2-hvad-systemet-kan)
3. [Den komplette graf](#3-den-komplette-graf)
4. [Et leads rejse (happy path)](#4-et-leads-rejse-happy-path)
5. [De fire agenter i dybden](#5-de-fire-agenter-i-dybden)
6. [LeadState — objektet der bærer alt](#6-leadstate--objektet-der-bærer-alt)
7. [MCP-grænsen](#7-mcp-grænsen)
8. [De fire HITL-destinationer](#8-de-fire-hitl-destinationer)
9. [Decision dashboard + BI Agent-integration](#9-decision-dashboard--bi-agent-integration)
10. [De ni scenarier — gennemgået](#10-de-ni-scenarier--gennemgået)
11. [Et fuldt gennemarbejdet eksempel](#11-et-fuldt-gennemarbejdet-eksempel)
12. [Observability + audit](#12-observability--audit)
13. [Datamodellen](#13-datamodellen)
14. [Sikkerhedsgarantier — hvad der IKKE kan ske](#14-sikkerhedsgarantier--hvad-der-ikke-kan-ske)
15. [Byggekortet over de seks uger](#15-byggekortet-over-de-seks-uger)

---

## 1. Systemet på 30 sekunder

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

**Fem dele. Ét typed state-objekt flyder igennem. Alle side-effekter går
gennem et typed tool. Ingen agent opfinder; ingen agent gætter; intet der
pauser går tabt.**

---

## 2. Hvad systemet kan

Tolv konkrete kapabiliteter systemet leverer. Hver enkelt er en
*førsteklasses* feature (bygget og testbar), ikke et strækmål.

| # | Kapabilitet | Hvor den bor | Kører når |
|---|---|---|---|
| 1 | Klassificere en referral i fritekst til 12 typede felter med per-felt confidence + source span | Intake Agent | Hver indkommende |
| 2 | Ranke fulfillment-partnere mod capacity + specialisering + ROI + churn-risk | Matching Agent | Hvert klassificeret lead |
| 3 | Nægte at route til en over-capacity partner (hårdt, i kode — ikke et hint) | `partner_capacity_mcp` | Hvert match-kald |
| 4 | Nægte at route til en inaktiv partner (hard filter) | `partner_capacity_mcp` | Hvert match-kald |
| 5 | Automatisk åbne Slack-forhandlinger med top-kandidaten, begrænset til 3 runder × 3 kandidater | Negotiation Agent | Hvert matchet lead |
| 6 | Droppe en partner og prøve den næste når de ikke svarer inden 24h ("dynamic in-loop re-routing") | Negotiation Agent | Live |
| 7 | Re-route en booket deal der misser sin 48h-accept eller 30d-close SLA ("dynamic post-booking re-routing") | Monitor Agent | Dagligt sweep |
| 8 | Score hver partners engagement + ROI-trend på et rullende 90d-vindue; fyre et interventions-playbook når scoren krydser 65 i 7 dage ("predictive churn intervention") | Monitor Agent | Dagligt sweep |
| 9 | Opdage cross-market ubalancer (et marked saturated 7d ≥90% util, eller starved 14d ≤20% util) | Monitor Agent | Dagligt sweep |
| 10 | Udsende en mandag-morgen weekly per-partner performance-rapport med LLM-skrevet narrativ | Monitor Agent | Ugentlig cron |
| 11 | Pause ved usikkerhed og DM'e on-call manageren med den fulde reasoning chain — og genoptage på deres beslutning | HITL-laget | 4 interrupt-destinationer |
| 12 | Besvare ad-hoc BI-spørgsmål inde i manager-konsollen via den embeddede Self-Querying BI Agent | Console + vendored BI Agent | On demand |

Hver af disse mapper til et specifikt sted i grafen — se [§3](#3-den-komplette-graf).

---

## 3. Den komplette graf

Hele systemet er en LangGraph state machine. Hver node er en pure function
over `LeadState`. Hver edge er et betinget prædikat over `LeadState`. Alle
side-effekter er MCP-kald.

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

**Forklaring af de fire HITL-destinationer:**

| # | Trigger | Manageren gør |
|---|---|---|
| ① `HITL_INTAKE` | Tvetydig referral — kan ikke udtrække country/service | Tilføjer manglende kontekst, eller dropper |
| ② `HITL_CAPACITY` | Ingen under-capacity aktiv partner i markedet | Reaktiverer en dormant partner, eller venter |
| ③ `HITL_NEGOTIATE` | Round budget opbrugt / alle kandidater afviste | Overrider til en specifik partner, eller dropper |
| ④ `HITL_MONITOR` | Severity-3 SLA breach | Om-tildeler til en anden partner |

Hvert interrupt persisterer LeadState via LangGraphs checkpointer. Manageren
genoptager grafen ved præcis samme node — intet arbejde går tabt.

---

## 4. Et leads rejse (happy path)

Følg én referral fra ankomst til resolution — hvert felt der skrives, hver
transition, hver side-effekt.

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

**Total ende-til-ende:** 32 dage fra referral → resolution.
**Total menneske-tid:** 0 minutter (ingen HITL fyrede).
**Total omkostning:** ~$0.35 i API-tokens over ~10 LLM-kald.
**Total audit trail:** 8 deal_events-rækker, 1 Langfuse trace tree.

---

## 5. De fire agenter i dybden

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

**Med andre ord:** Intake-agenten læser den rå henvendelse og udtrækker et
struktureret billede. Hvert faktum bærer det præcise tekstudsnit det kom fra
og en confidence-score. Står et faktum ikke i teksten, forbliver feltet tomt
— agenten opfinder aldrig. Er Sonnets samlede confidence for lav, prøves
Opus én gang; er resultatet stadig tvetydigt, går sagen til HITL i stedet
for at blive gættet videre på.

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

**Med andre ord:** Matching-agenten finder den rette fulfillment-partner. De
hårde grænser (capacity, aktiv status) håndhæves server-side i MCP'en — de
kan ikke over-tales. ROI og churn-risk er derimod bløde signaler: de trækker
en partner ned i rankingen, men manageren kan stadig vælge dem bevidst via
HITL hvis der er en strategisk grund.

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

**Med andre ord:** Negotiation-agenten kontakter top-kandidaten på Slack og
håndterer svaret. Loopet er hårdt begrænset — max 3 runder pr. kandidat, max
3 kandidater, altså 9 forsøg i alt. Svarer en partner ikke inden 24 timer,
frigives den holdte capacity og næste kandidat prøves med det samme — leadet
ligger aldrig og venter på nogen der er gået på ferie. Runde 2+ eskaleres
til Opus, fordi en counter-proposal kræver hårdere judgment end en første
henvendelse.

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

**Med andre ord:** Monitor-agenten er uafhængig af de enkelte leads. Den
sweeper dagligt kl. 06:00 CET: fanger SLA-brud og re-router dealen (med den
brydende partner ekskluderet), scorer hver partners engagement + ROI-trend
til en churn-risk score, og opdager markeds-ubalancer. Hver mandag kl. 08:00
udsendes en strategisk per-partner rapport. Alt er rate-limited så managerens
indbakke aldrig floodes under et udfald.

---

## 6. LeadState — objektet der bærer alt

Hver agent læser fra og skriver til det samme typede objekt. Det er "state
graph"-delen i "LangGraph state graph".

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

**Hvem skriver hvad:**

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

## 7. MCP-grænsen

LLM'en kalder aldrig en rå API. Alle side-effekter går gennem et
Model-Context-Protocol tool. Tre MCP-servere, én kontrakt pr. eksternt
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

**Hvorfor MCP og ikke bare en Python-funktion?**

| Grund | Hvorfor det betyder noget |
|---|---|
| Enumerable tools | LLM'en ser et *lukket sæt* af typede tools — ingen opfindelser |
| Swappable | mock → rigtig Slack = config-ændring, ikke en kode-ændring |
| Contract-testable | Hver server har sin egen contract test-suite |
| Cross-language | Enhver MCP-server (Python, TS, Rust) virker med enhver klient |

---

## 8. De fire HITL-destinationer

Hvert interrupt er en graf-node med sin egen trigger-betingelse, sin egen
Slack DM-skabelon og sin egen resume-sti.

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

**Fælles HITL resume-maskineri** (på tværs af alle fire destinationer):

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

## 9. Decision dashboard + BI Agent-integration

Når en manager klikker på et HITL Slack-link, lander de i manager-konsollen —
en Streamlit-app der viser det fulde operationelle billede *og* embedder den
Self-Querying BI Agent til ad-hoc forespørgsler.

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

**Sådan virker BI Agent-widget'en** (Q18):

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

**Med andre ord:** Manageren beslutter aldrig i blinde. Dashboard'et viser
partnerens sundhed, markedets sundhed, den finansielle kontekst og hvordan
dette lead ligger i forhold til partnerens normale bog — alt sammen læst fra
det samme NordLedger warehouse som BI Agenten bruger. Og kan manageren ikke
finde svaret i dashboard'et, stiller de bare spørgsmålet direkte til den
embeddede BI Agent uden at forlade konsollen.

---

## 10. De ni scenarier — gennemgået

Hvert scenarie er et specifikt data-mønster som eval-suiten asserter imod.

### Scenarie #1 — cold-start-market (DK, 2024-12)

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

### Scenarie #2 — ambiguous-inquiry

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

### Scenarie #3 — negotiation-stall (SE, 2024-11)

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

### Scenarie #4 — sla-breach (NL, 2024-09/10)

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

### Scenarie #5 — cross-market-imbalance (DE saturated, DK starved, Nov 2024)

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

### Scenarie #6 — slow-churn-partner (P010, DE, 2024-07 → 2024-12)

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

### Scenarie #7 — unprofitable-but-friendly (P016, US, Q4 2024)

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

### Scenarie #8 — slow-referring-ambassador (R009, SE, Q4 2024)

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

### Scenarie #9 — reactivate-inactive-partner (P018, US)

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

## 11. Et fuldt gennemarbejdet eksempel

**Referral-teksten (ankommer 15. dec 2024 via Billys Slack —
systemet kører English-only per Q3):**

> Hi team,
>
> Referring a client: Bauer & Söhne GmbH — a mid-market software company
> in Munich, Germany. Frank Bauer (CEO, frank@bauer-soehne.de) is looking
> for a new tax_advisory partner for their 2024 books. Revenue base is
> around EUR 2M, stack is DATEV + Salesforce. Wants to onboard within 4
> weeks. Budget already approved.
>
> — Karsten (Nordic Partners AB)

**Trin 1: Intake Agent udtrækker (Sonnet, ~1.4k tokens, $0.006):**

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

**intake_confidence = 0.92** → langt over threshold, ingen Opus-retry nødvendig.

**Trin 2: Matching Agent ranker (Sonnet + MCP, ~800ms, $0.005):**

Kald: `partner_capacity_mcp.list_candidates(country="DE", service_type="tax_advisory", industry="tech", as_of="2024-12-15")`

Returnerer 3 DE tax advisors, ranket efter composite score:

| Rank | Partner | Spec | ROI | Trend | Churn | Latency | Cap | Score | Rationale |
|---|---|---|---|---|---|---|---|---|---|
| 1 | P011 | 88 | +28% | up | 22 | 4.1h | 5/12 | 82.7 | Stærk specialisering · sund udvikling |
| 2 | P012 | 74 | +19% | flat | 31 | 6.3h | 8/13 | 69.4 | God dækning · nærmer sig soft cap |
| 3 | P010 | 92 | -3.5% | **down** | **78** ⚠ | 52h | 3/11 | 42.1 | ⚠ Forhøjet churn-risk · faldende ROI |

Slow-churn partneren P010 har den højeste rå specialiserings-score, men dens
churn-risk på 78 + faldende ROI skubber den ned til #3.

**Trin 3: Negotiation Agent — Runde 1 med P011 (Sonnet, $0.004):**

```
19:31:03  hold_capacity(P011, RL02901, ttl=48h) → OK
19:31:04  slack_mock_mcp.send_dm(P011_contact, blocks=[...])
          → message_id = "msg_a7f21"
          → deal_events += outreach_sent
```

**Trin 4: 6 timer senere — svar modtaget:**

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

**LeadState nu:**
```
referral_lead_id: "RL02901"
status: "booked"
matched_candidates: [P011, P012, P010]  # kept for audit
negotiation_history: [
    {round: 1, partner: "P011", outcome: "accept", elapsed_hours: 6.1}
]
tokens_spent_usd: 0.015     # 0.75% of $2 budget
```

**Trin 5: 28 dage senere — monitor sweep på dag 28:**

```
06:00 CET  Monitor sweeps active deals
           - RL02901 · booked_at 2024-12-15 · accept_at 2024-12-16 (day+1)
             · in_progress
             · 28 days since booking → within 30d close SLA
             → OK

           status stays "booked"
```

**Trin 6: 32 dage senere — P011 melder deal afsluttet:**

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

Hver LangGraph-node wrappes som en Langfuse span. Hvert MCP-kald er en child
span. Trace-træet for det gennemarbejdede eksempel ser sådan ud:

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

**Hvad Langfuse giver os:**

| Rollup | Besvarer |
|---|---|
| Pr. lead | Total omkostning, total elapsed, agent-fordeling |
| Pr. agent | Latency-percentiler, tokens/min, fejlrate |
| Pr. MCP-kald | Failure rate, timeout rate, retry count |
| Pr. partner | Hvor meget LLM-arbejde pr. succesfuld booking |
| Pr. marked | Cost-per-booked-lead |
| Pr. time | Peak API-forbrug — til rate limit-planlægning |

**Uafhængig audit-rygrad: `deal_events`-tabellen**

Hvert event har:
```
(event_id, referral_lead_id, partner_id, event_type, event_at, agent_name, rationale)
```

Kombineret med Langfuse trace ID kan ethvert spørgsmål i stil med "hvorfor
routede vi RL02901 til P011?" besvares med én JOIN:

```sql
SELECT event_at, event_type, agent_name, rationale
FROM stg_deal_events
WHERE referral_lead_id = 'RL02901'
ORDER BY event_at;
```

+ Langfuse-link for den fulde LLM reasoning chain pr. node.

---

## 13. Datamodellen

Warehouse'et har **15 tabeller**: 7 NordLedger core (delt med den
Self-Querying BI Agent) + 8 Rebuild 3 orchestration-extension.

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

**Hvem læser hvad** (agenter ↔ tabeller):

| Tabel | Intake | Matching | Negotiation | Monitor | Console |
|---|:---:|:---:|:---:|:---:|:---:|
| stg_partners | | ✓ | | | ✓ |
| partner_capacity | | ✓ | ✓ (hold) | | ✓ |
| partner_specializations | | ✓ | | | ✓ |
| partner_engagement_daily | | ✓ (via list_candidates) | | ✓ | ✓ |
| referring_partner_engagement_daily | | | | ✓ | ✓ |
| partner_status_events | | | | | ✓ (skriver reactivations) |
| referral_leads | | ✓ (læser seneste) | | | ✓ |
| deal_events | ✓ skriver | ✓ skriver | ✓ skriver | ✓ skriver | ✓ læser |
| NordLedger core (customers, subs, osv.) | | | | ✓ (LTV-signaler) | ✓ (BI Agent) |

---

## 14. Sikkerhedsgarantier — hvad der IKKE kan ske

Fejltilstande og hvordan de er strukturelt forhindret — ikke "vi bad modellen
pænt", men "umuligt by construction".

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

## 15. Byggekortet over de seks uger

Hvor hver kapabilitet kommer online.

```
Week 1    DATA + SIMULATION
──────    ─────────────────
          ✅ NordLedger warehouse (15 tables, 70k rows)
          ✅ 9 injected scenarios in SCENARIOS.md
          ✅ BI Agent schema compatibility verified
          Exit: 150/150 dbt tests green


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

## Ordliste

| Term | Definition |
|---|---|
| **Referral** | En SMB-henvendelse sendt til marketplacen af en referring partner (Billy-analog) |
| **Fulfillment partner** | Bogholder / revisor / skatterådgiver på marketplacen som udfører det henviste arbejde |
| **Referring partner** | Ambassadøren der sender henvisningen (fx Billy) |
| **LeadState** | Typed Pydantic-objekt der flyder gennem LangGraphen og bærer hvert felt, hver beslutning, hvert audit-spor |
| **Provenance** | Per-felt extraction-metadata: confidence 0-1 + source_span |
| **MCP** | Model Context Protocol — tool-grænsen mellem grafen og eksterne systemer |
| **HITL** | Human-in-the-Loop — de fire interrupt-destinationer hvor grafen pauser og venter på managerens beslutning |
| **Composite score** | Matchings rank-signal: 0.4 × spec + 0.3 × ROI + 0.2 × (100−churn) + 0.1 × latency |
| **Churn risk score** | 0-100 beregnet fra engagement + ROI-trend; threshold 65 sustained 7d fyrer en intervention |
| **Cross-market imbalance** | Saturated ≥90% i 7d ELLER starved ≤20% i 14d |
| **Decision dashboard** | Streamlit-viewet en manager åbner fra et HITL Slack-link; viser lead + partner-sundhed + markeds-sundhed + BI Agent-widget |
| **Langfuse trace** | Nested span-træ pr. lead — hver node, hvert MCP-kald, cost, latency |
| **deal_events** | Audit-rygraden — én række pr. graf-transition pr. lead — forankret til Langfuse trace ID |

---

*Vibe Harboe Christensen — AI Automation Engineer | vibegroup.dk*

*Dette dokument er den danske læseversion af TECHNICAL-UNDERSTANDING.md.
Diagrammer, kodeblokke og tekniske termer er bevaret på engelsk så de matcher
koden 1:1. Opdater begge versioner når arkitekturen ændrer sig;
`ARCHITECTURE.md` §15 forbliver source of truth for design-beslutninger.*
