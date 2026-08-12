# Showcase

Client-facing presentation artifacts for **Multi-Agent Lead Orchestration** —
built for LinkedIn, portfolio and freelance profiles (UpWork, Worksome, DINQ).

They demonstrate the *experience* of the system — not the implementation. No
code, no prompts, no architecture internals.

## ▶️ View it live

<!--
TODO when the portfolio pages are published:
  1. Replace the URL placeholders below with the live links.
  2. Delete this comment.
-->

**[VIBE Group portfolio →](https://YOUR-PORTFOLIO-URL-HERE)**

GitHub doesn't render HTML in the browser — the landing page, the interactive
prototype and the one-pager are hosted live at the link above. That's the
version to share and the version to send to prospects.

### What lives there

| Page | What it is | Best for |
|---|---|---|
| [Landing page →](https://YOUR-LANDING-PAGE-URL) | The hub piece — the four agents + HITL safety net, the graph walkthrough, production principles, proof | Portfolio site · the page linked from the LinkedIn profile |
| [Interactive prototype →](https://YOUR-PROTOTYPE-URL) | Pick a scenario, watch the graph run — happy path, negotiation stall + manager override, capacity crunch + reactivation, the Monitor's sweep | "Run it yourself" link in a LinkedIn post · recruiter demo |
| [One-pager →](https://YOUR-ONE-PAGER-URL) | Single-page case overview: problem → solution → measured proof → CTA, A4-printable | UpWork / Worksome / DINQ profiles — open and "Save as PDF" at A4 |

## Note

The live demo runs on **scripted, deterministic scenarios** through the REAL
graph — it mirrors the production system's behaviour without spending LLM
tokens. The actual working build (LangGraph + 4 HITL interrupts, three MCP
servers, the Monitor's interventions, 61 warehouse-marked tests, the
DuckDB-backed NordLedger simulation) lives in the parent repository.

---

*VIBE Group · [vibegroup.dk](https://vibegroup.dk) — Not demos. Not theory.
Production AI agents, grounded in 20+ years of operational experience.*
