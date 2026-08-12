# Injected Scenarios — NordLedger Multi-Agent Lead Orchestration

The synthetic dataset (`warehouse/scripts/generate_seeds.py`) plants **9
scenarios** across the referral + orchestration tables. Every one is
asserted end-to-end by `eval/test_e2e.py` — the graph must reach the
expected terminal state for each, or the test fails.

This file is generated from the `ORCH_SCENARIOS` list in the generator —
that list is the single source of truth. If you change scenarios, edit the
generator and re-run.

| # | Scenario | Market | Months | What we assert against |
|---|---|---|---|---|
| 1 | **cold-start-market** — All 3 DK fulfillment partners at hard-cap for the whole month; any DK lead should escalate to HITL — no candidates under capacity. | DK | 2024-12 | `stg_partner_capacity (P001..P003 in 2024-12 with active_deals_count = hard_cap)` |
| 2 | **ambiguous-inquiry** — 3-5 referral_leads with industry/urgency/service_type all null — Intake Agent should surface these to HITL rather than guess. | * | 2024-08, 2024-10, 2024-12 | `stg_referral_leads (industry is null AND service_type is null)` |
| 3 | **negotiation-stall** — 2 referrals stuck in status='negotiating' with 3 rounds of outreach_sent + reply_received but no booked event. | SE | 2024-11 | `stg_deal_events (3+ outreach_sent per referral_lead_id, referral_status still 'negotiating')` |
| 4 | **sla-breach** — 3 booked deals with no resolved_at after 45+ days — the Monitor Agent should re-inject them into the graph for re-routing. | NL | 2024-09, 2024-10 | `stg_referral_leads (referral_status='booked' AND resolved_at is null AND booked_at < today - 45 days)` |
| 5 | **cross-market-imbalance** — DE all 3 partners at max capacity; DK all 3 partners at <20% utilization. | DE+DK | 2024-11 | `stg_partner_capacity (DE utilisation ≥95% AND DK ≤20% for 2024-11)` |
| 6 | **slow-churn-partner** — Fulfillment partner P010 shows declining engagement (response_latency climbing from 4h → 48h) AND declining ROI (+30% → -5%) over 6 months. | DE | 2024-07..2024-12 | `stg_partner_engagement_daily (P010 trends over 2024-07..2024-12)` |
| 7 | **unprofitable-but-friendly** — Fulfillment partner P016 keeps response times low + high accept rate but ROI trends to -15% over Q4 2024. | US | 2024-10..2024-12 | `stg_partner_engagement_daily (P016 partner_roi_pct_snapshot decline, response_latency stable)` |
| 8 | **slow-referring-ambassador** — Referring partner R009 (Swedish) lead volume drops from ~30/mo to ~5/mo over Q4 2024 without explanation. | SE | 2024-10..2024-12 | `stg_referring_partner_engagement_daily (R009 leads_sent_count decline)` |
| 9 | **reactivate-inactive-partner** — Fulfillment partner P018 deactivated 2024-08-01. When a US lead arrives in 2024-12 that no active US partner can serve, manager should be able to reactivate P018 via the console (assert set_partner_status flow works). | US | 2024-08..2024-12 | `stg_partner_status_events (P018 deactivation), stg_partners (P018 status='inactive')` |

## Notes

- All scenarios land in the latest ~6 months of the 24-month window so the
  Monitor's rolling baseline has plenty of clean history.
- `slow-churn`, `unprofitable-but-friendly` and `slow-referring-ambassador`
  are *gradual* — no single-day anomaly. The eval suite asserts the trend
  is detected, not a single point.
- `cold-start-market`, `cross-market-imbalance`, `negotiation-stall`,
  `sla-breach` and `reactivate-inactive-partner` are *event-shaped* —
  binary presence/absence in the seeded data.
