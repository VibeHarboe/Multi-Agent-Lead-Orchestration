# Injected Anomalies — NordLedger Marketplace simulation

The synthetic dataset (`warehouse/scripts/generate_seeds.py`) plants four
anomalies in the **latest month of data (2024-12)** — one per metric, each in
a single market. Every other market stays within normal variance for that
metric, so a correct proactive monitor flags exactly these four and nothing
else.

`src/monitor.py` scans the latest period against each segment's trailing
baseline (modified z-score, median/MAD); `eval/test_monitor.py` asserts every
row below is surfaced. This file is generated from the `ANOMALIES` list in
`generate_seeds.py` — that list is the single source of truth.

| Metric | Market | Month | Direction | Baseline | Anomalous | What it represents |
|---|---|---|---|---|---|---|
| `churn_rate` | DE | 2024-12 | spike | ~12% | ~46% | The German subscription cohort that started in Dec 2024 churns at nearly 4x the usual rate — a retention failure in that cohort. |
| `overdue_rate` | NL | 2024-12 | spike | ~10% | ~36% | Dutch invoices issued in Dec 2024 go overdue at ~3.5x baseline — a collections breakdown in the Netherlands. |
| `nps_score` | SE | 2024-12 | drop | ~+42 | ~-16 | Swedish NPS collapses from strongly positive to negative — a satisfaction crisis surfacing in the Dec 2024 survey wave. |
| `conversion_rate` | US | 2024-12 | drop | ~50% | ~17% | US lead conversion falls to roughly a third of baseline — a demand-quality or sales-capacity problem in the US market. |

All four are deliberately placed in the most recent month so the default
`--scan` (which checks the latest period) catches them. The baseline months
carry a small per-cell jitter — the series is not flat — but rates are
assigned by *count* per (market, month), so baseline noise stays well below
the injected signal.
