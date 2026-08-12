"""Generate synthetic seed CSVs for the NordLedger Marketplace simulation.

NordLedger Marketplace is a fictional Nordic SMB<->accountant marketplace — a
dummy stand-in for the real Ageras BI rebuild this project is modelled on. All
data here is synthetic: 6 markets (DK, NO, SE, DE, NL, US), customer signup
cohorts from 2022-01, and 24 months of monthly event history (2023-01..2024-12)
for leads, invoices, NPS surveys and upsells.

Deterministic — one seeded RNG — so `dbt build` and the eval suite are
reproducible. Four anomalies are deliberately injected into the latest month
(2024-12); they are listed in ANOMALIES below and documented in
warehouse/ANOMALIES.md. The proactive anomaly monitor (src/monitor.py) scans the
latest period, and eval/test_monitor.py asserts it surfaces exactly these.

Rates (churn / conversion / overdue / NPS mix) are assigned by *count* per
(market, month) cell rather than per-row coin flips — this keeps the baseline
months low-noise so the injected anomalies stand out cleanly under the monitor's
modified z-score, while a small per-cell jitter keeps the series from being flat.

Usage:  python warehouse/scripts/generate_seeds.py
"""

from __future__ import annotations

import bisect
import csv
import random
from datetime import date, timedelta
from pathlib import Path

RANDOM_SEED = 42
SEEDS_DIR = Path(__file__).resolve().parent.parent / "seeds"
ANOMALIES_MD = Path(__file__).resolve().parent.parent / "ANOMALIES.md"

MARKETS = ["DK", "NO", "SE", "DE", "NL", "US"]
CURRENCY = {"DK": "DKK", "NO": "NOK", "SE": "SEK", "DE": "EUR", "NL": "EUR", "US": "USD"}

# Customer signup cohorts start here; monthly event history spans EVENT_*.
COHORT_START = date(2022, 1, 1)
EVENT_START = date(2023, 1, 1)
EVENT_END = date(2024, 12, 1)   # last month of the simulation

CUSTOMERS_PER_MARKET_MONTH = 22
SURVEYS_PER_MARKET_MONTH = 18
LEADS_PER_MARKET_MONTH = 22
INVOICES_PER_MARKET_MONTH = 24
UPSELLS_PER_MARKET_MONTH = 6

# --- injected anomalies — single source of truth, mirrored into ANOMALIES.md --
# Each is one (metric, market) cell in the latest month. Every other market is
# left within normal variance for that metric, so a correct monitor flags these
# four and nothing else.
ANOMALIES = [
    {
        "metric": "churn_rate", "market": "DE", "month": "2024-12", "kind": "spike",
        "baseline": "~12%", "anomalous": "~46%",
        "detail": "The German subscription cohort that started in Dec 2024 churns "
                  "at nearly 4x the usual rate — a retention failure in that cohort.",
    },
    {
        "metric": "overdue_rate", "market": "NL", "month": "2024-12", "kind": "spike",
        "baseline": "~10%", "anomalous": "~36%",
        "detail": "Dutch invoices issued in Dec 2024 go overdue at ~3.5x baseline — "
                  "a collections breakdown in the Netherlands.",
    },
    {
        "metric": "nps_score", "market": "SE", "month": "2024-12", "kind": "drop",
        "baseline": "~+42", "anomalous": "~-16",
        "detail": "Swedish NPS collapses from strongly positive to negative — a "
                  "satisfaction crisis surfacing in the Dec 2024 survey wave.",
    },
    {
        "metric": "conversion_rate", "market": "US", "month": "2024-12", "kind": "drop",
        "baseline": "~50%", "anomalous": "~17%",
        "detail": "US lead conversion falls to roughly a third of baseline — a "
                  "demand-quality or sales-capacity problem in the US market.",
    },
]
# (metric, market, month_key) -> anomalous rate / target used by the generators.
_ANOMALY_RATE = {
    ("churn_rate", "DE", "2024-12"): 0.46,
    ("overdue_rate", "NL", "2024-12"): 0.36,
    ("conversion_rate", "US", "2024-12"): 0.17,
    ("nps_score", "SE", "2024-12"): -16.0,   # target NPS, not a fraction
}

# Reference data --------------------------------------------------------------
SEGMENTS = ["SMB", "MID", "ENT"]
SEGMENT_WEIGHTS = [0.60, 0.30, 0.10]
PLAN_BY_SEGMENT = {
    "SMB": (["basic", "premium"], [0.72, 0.28]),
    "MID": (["basic", "premium", "enterprise"], [0.18, 0.67, 0.15]),
    "ENT": (["premium", "enterprise"], [0.25, 0.75]),
}
MRR_RANGE = {"basic": (450, 800), "premium": (1000, 2800), "enterprise": (3800, 8200)}
SERVICE_TYPES = ["accounting", "tax_advisory", "bookkeeping", "audit", "payroll"]
LEAD_SOURCES = ["organic", "paid", "partner", "referral"]
SALES_REPS = [f"SR{n:02d}" for n in range(1, 13)]
PARTNER_TYPES = ["accounting_firm", "audit_firm", "advisory_firm", "bookkeeping_service"]
CHURN_REASONS = ["price_too_high", "competitor", "not_using"]
ADD_ONS = ["tax_advisory_add_on", "audit_add_on", "bookkeeping_add_on", "payroll_add_on"]

# Customer-name building blocks per market (purely cosmetic — customer_id is PK).
_SURNAMES = {
    "DK": ["Andersen", "Nielsen", "Hansen", "Christensen", "Larsen", "Sørensen",
           "Jensen", "Pedersen", "Mortensen", "Holm"],
    "NO": ["Berg", "Olsen", "Hansen", "Johansen", "Larsen", "Andersen",
           "Nilsen", "Pettersen", "Bakke", "Haugen"],
    "SE": ["Carlsson", "Lindqvist", "Svensson", "Bergström", "Andersson",
           "Johansson", "Nilsson", "Larsson", "Eklund", "Holm"],
    "DE": ["Müller", "Weber", "Bauer", "Schmidt", "Fischer", "Wagner",
           "Becker", "Hoffmann", "Schäfer", "Koch"],
    "NL": ["Van den Berg", "De Vries", "Hendriks", "Jansen", "Bakker",
           "Visser", "Smit", "Meijer", "De Boer", "Mulder"],
    "US": ["Smith", "Johnson", "Williams", "Brown", "Miller", "Davis",
           "Wilson", "Anderson", "Taylor", "Thomas"],
}
_BIZ_WORDS = {
    "DK": ["Regnskab", "Bogføring", "Revision", "Økonomi", "Consulting"],
    "NO": ["Regnskap", "Consulting", "Revisjon", "Økonomi", "Rådgivning"],
    "SE": ["Redovisning", "Revision", "Konsult", "Ekonomi", "Rådgivning"],
    "DE": ["Steuerberatung", "Buchhaltung", "Consulting", "Wirtschaftsprüfung", "Treuhand"],
    "NL": ["Advies", "Fiscaal", "Boekhouding", "Accountancy", "Consultancy"],
    "US": ["Advisory", "Tax", "Consulting", "Bookkeeping", "Partners"],
}
_LEGAL = {"DK": "ApS", "NO": "AS", "SE": "AB", "DE": "GmbH", "NL": "BV", "US": "LLC"}

# Localised survey comments — a few non-ASCII to exercise the utf-8 write path.
_COMMENTS = {
    "promoter": {
        "DK": "Fremragende service og hurtig respons",
        "NO": "Veldig fornøyd med tjenesten",
        "SE": "Utmärkt service, rekommenderar starkt",
        "DE": "Hervorragende Qualität und Service",
        "NL": "Uitstekende service, zeer tevreden",
        "US": "Excellent support team, very happy",
    },
    "passive": {
        "DK": "God service, men kan forbedres",
        "NO": "Grei tjeneste totalt sett",
        "SE": "Helt ok men kan förbättras",
        "DE": "Insgesamt zufriedenstellender Service",
        "NL": "Gemiddeld tevreden over het geheel",
        "US": "Decent, but room for improvement",
    },
    "detractor": {
        "DK": "Ikke tilfreds med serviceniveauet",
        "NO": "Skuffet over responstiden",
        "SE": "Inte nöjd, för långsam respons",
        "DE": "Zu langsam und unzuverlässig",
        "NL": "Teleurgesteld in de kwaliteit",
        "US": "Not satisfied with the service level",
    },
}


# Helpers ---------------------------------------------------------------------
def months_between(start: date, end: date) -> list[date]:
    """First-of-month dates from start to end inclusive."""
    out, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append(date(y, m, 1))
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def rand_day_in_month(rng: random.Random, m: date) -> date:
    # Cap at day 28 so every month behaves the same — the exact day is cosmetic.
    return m + timedelta(days=rng.randint(0, 27))


def iso(d: date | None) -> str:
    return d.isoformat() if d else ""


def weighted(rng: random.Random, choices: list, weights: list):
    return rng.choices(choices, weights=weights, k=1)[0]


def split_counts(n: int, fractions: list[float]) -> list[int]:
    """Split n into integer buckets by fractions; remainder lands in the last."""
    counts = [round(n * f) for f in fractions[:-1]]
    counts.append(n - sum(counts))
    if counts[-1] < 0:                       # fractions summed > 1 after rounding
        counts[-1] = 0
    return counts


# Generators ------------------------------------------------------------------
def generate_partners(rng: random.Random) -> list[dict]:
    rows, n = [], 0
    for market in MARKETS:
        for _ in range(3):
            n += 1
            rows.append({
                "partner_id": f"P{n:03d}",
                "partner_name": f"{rng.choice(_SURNAMES[market])} "
                                f"{rng.choice(['Group', 'Partners', 'Associates', 'Firm'])}",
                "country": market,
                "partner_type": rng.choice(PARTNER_TYPES),
                "contract_start_date": iso(date(rng.randint(2017, 2021),
                                                rng.randint(1, 12), rng.randint(1, 28))),
                "monthly_fee": rng.randint(8000, 22000),
                "commission_rate": round(rng.uniform(0.06, 0.18), 2),
                "status": "active",
            })
    for idx in rng.sample(range(len(rows)), 2):    # a couple of inactive partners
        rows[idx]["status"] = "inactive"
    return rows


def generate_customers_and_subscriptions(
    rng: random.Random, partners: list[dict]
) -> tuple[list[dict], list[dict]]:
    """Customers (1 per row) and their 1:1 subscriptions.

    Churn is assigned by count per (market, signup-cohort-month): a baseline
    fraction with small jitter, overridden for the injected anomaly cell.
    """
    partners_by_market: dict[str, list[str]] = {m: [] for m in MARKETS}
    for p in partners:
        partners_by_market[p["country"]].append(p["partner_id"])

    customers: list[dict] = []
    subscriptions: list[dict] = []
    cust_n = sub_n = 0
    cohort_months = months_between(COHORT_START, EVENT_END)

    for market in MARKETS:
        for cohort in cohort_months:
            mkey = month_key(cohort)
            n = CUSTOMERS_PER_MARKET_MONTH
            # Baseline range is intentionally wider than "realistic" jitter so
            # that with ~22 subs/cohort, the resulting rate hits ~4 distinct
            # discrete values (k/n) rather than 2 — the modified-z baseline
            # needs a non-zero MAD, and a bimodal series has MAD=0.
            churn_frac = _ANOMALY_RATE.get(
                ("churn_rate", market, mkey), round(rng.uniform(0.05, 0.20), 3)
            )
            n_churn = round(churn_frac * n)
            churn_flags = [True] * n_churn + [False] * (n - n_churn)
            rng.shuffle(churn_flags)

            for is_churned in churn_flags:
                cust_n += 1
                sub_n += 1
                cid = f"C{cust_n:05d}"
                sid = f"S{sub_n:05d}"
                signup = rand_day_in_month(rng, cohort)
                segment = weighted(rng, SEGMENTS, SEGMENT_WEIGHTS)
                plans, plan_weights = PLAN_BY_SEGMENT[segment]
                plan = weighted(rng, plans, plan_weights)
                mrr = rng.randint(*MRR_RANGE[plan])
                has_partner = rng.random() < 0.55 and partners_by_market[market]
                partner_id = rng.choice(partners_by_market[market]) if has_partner else ""
                name = (f"{rng.choice(_SURNAMES[market])} "
                        f"{rng.choice(_BIZ_WORDS[market])} {_LEGAL[market]}")

                customers.append({
                    "customer_id": cid,
                    "customer_name": name,
                    "country": market,
                    "segment": segment,
                    "signup_date": iso(signup),
                    "plan_type": plan,
                    "monthly_revenue": mrr,
                    "partner_id": partner_id,
                })

                churn_date = end_date = reason = ""
                if is_churned:
                    cd = signup + timedelta(days=rng.randint(20, 230))
                    churn_date = end_date = iso(cd)
                    reason = rng.choice(CHURN_REASONS)
                subscriptions.append({
                    "subscription_id": sid,
                    "customer_id": cid,
                    "plan_type": plan,
                    "start_date": iso(signup),
                    "end_date": end_date,
                    "churn_date": churn_date,
                    "churn_reason": reason,
                    "mrr": mrr,
                    "country": market,
                })
    return customers, subscriptions


def _customer_pool(customers: list[dict]) -> dict[str, tuple[list[date], list[str]]]:
    """Per market: signup dates (sorted) and the parallel customer_id list, so an
    event in month M can pick a customer that already existed by then."""
    pool: dict[str, list[tuple[date, str]]] = {m: [] for m in MARKETS}
    for c in customers:
        pool[c["country"]].append((date.fromisoformat(c["signup_date"]), c["customer_id"]))
    out: dict[str, tuple[list[date], list[str]]] = {}
    for market, pairs in pool.items():
        pairs.sort()
        out[market] = ([p[0] for p in pairs], [p[1] for p in pairs])
    return out


def _pick_customer(rng: random.Random, pool, market: str, as_of: date) -> str:
    dates, ids = pool[market]
    hi = bisect.bisect_right(dates, as_of)
    if hi == 0:                               # no customer that early — shouldn't happen
        hi = len(ids)
    return ids[rng.randrange(hi)]


def generate_nps_surveys(rng: random.Random, pool) -> list[dict]:
    rows, n = [], 0
    for market in MARKETS:
        for m in months_between(EVENT_START, EVENT_END):
            mkey = month_key(m)
            target = _ANOMALY_RATE.get(
                ("nps_score", market, mkey), round(rng.uniform(34.0, 50.0), 1)
            )
            # NPS = (p_prom - p_det) * 100 with p_passive fixed at 0.30.
            p_prom = max(0.0, min(0.70, 0.35 + target / 200.0))
            p_det = max(0.0, min(0.70, 0.35 - target / 200.0))
            count = SURVEYS_PER_MARKET_MONTH
            n_prom, n_det = round(p_prom * count), round(p_det * count)
            n_pass = max(0, count - n_prom - n_det)
            categories = (["promoter"] * n_prom + ["detractor"] * n_det
                          + ["passive"] * n_pass)
            rng.shuffle(categories)
            for cat in categories:
                n += 1
                d = rand_day_in_month(rng, m)
                if cat == "promoter":
                    nps, csat = rng.randint(9, 10), rng.randint(4, 5)
                elif cat == "passive":
                    nps, csat = rng.randint(7, 8), rng.randint(3, 4)
                else:
                    nps, csat = rng.randint(0, 6), rng.randint(1, 3)
                rows.append({
                    "survey_id": f"SV{n:05d}",
                    "customer_id": _pick_customer(rng, pool, market, d),
                    "survey_date": iso(d),
                    "nps_score": nps,
                    "csat_score": csat,
                    "country": market,
                    "comment": _COMMENTS[cat][market],
                })
    return rows


def generate_leads(rng: random.Random, pool) -> list[dict]:
    rows, n = [], 0
    for market in MARKETS:
        for m in months_between(EVENT_START, EVENT_END):
            mkey = month_key(m)
            conv = _ANOMALY_RATE.get(
                ("conversion_rate", market, mkey), round(rng.uniform(0.44, 0.56), 3)
            )
            count = LEADS_PER_MARKET_MONTH
            n_conv = round(conv * count)
            rest = count - n_conv
            # Of the non-converted: pending / unassigned / lost.
            n_pending, n_unassigned, n_lost = split_counts(rest, [0.40, 0.22, 0.38])
            statuses = (["converted"] * n_conv + ["pending"] * n_pending
                        + ["unassigned"] * n_unassigned + ["lost"] * n_lost)
            rng.shuffle(statuses)
            for status in statuses:
                n += 1
                lead_date = rand_day_in_month(rng, m)
                assigned_at = converted_at = None
                if status == "unassigned":
                    pass
                else:
                    # ~18% of assigned leads breach the 24h SLA (>= 2 days late).
                    delay = rng.randint(2, 6) if rng.random() < 0.18 else rng.randint(0, 1)
                    assigned_at = lead_date + timedelta(days=delay)
                    if status == "converted":
                        converted_at = assigned_at + timedelta(days=rng.randint(3, 25))
                rows.append({
                    "lead_id": f"L{n:05d}",
                    "customer_id": _pick_customer(rng, pool, market, lead_date),
                    "country": market,
                    "service_type": rng.choice(SERVICE_TYPES),
                    "lead_date": iso(lead_date),
                    "assigned_at": iso(assigned_at),
                    "converted_at": iso(converted_at),
                    "lead_status": status,
                    "lead_source": rng.choice(LEAD_SOURCES),
                })
    return rows


def generate_invoices(rng: random.Random, pool) -> list[dict]:
    rows, n = [], 0
    for market in MARKETS:
        for m in months_between(EVENT_START, EVENT_END):
            mkey = month_key(m)
            # Wider baseline jitter — see generate_customers_and_subscriptions
            # for the discrete-rate / MAD rationale.
            overdue = _ANOMALY_RATE.get(
                ("overdue_rate", market, mkey), round(rng.uniform(0.04, 0.18), 3)
            )
            count = INVOICES_PER_MARKET_MONTH
            n_overdue = round(overdue * count)
            rest = count - n_overdue
            n_paid_late, n_paid = split_counts(rest, [0.17, 0.83])
            statuses = (["overdue"] * n_overdue + ["paid_late"] * n_paid_late
                        + ["paid"] * n_paid)
            rng.shuffle(statuses)
            for status in statuses:
                n += 1
                issue = rand_day_in_month(rng, m)
                due = issue + timedelta(days=30)
                if status == "paid":
                    paid = issue + timedelta(days=rng.randint(5, 28))
                elif status == "paid_late":
                    paid = due + timedelta(days=rng.randint(3, 40))
                else:
                    paid = None
                rows.append({
                    "invoice_id": f"INV{n:05d}",
                    "customer_id": _pick_customer(rng, pool, market, issue),
                    "issue_date": iso(issue),
                    "due_date": iso(due),
                    "paid_date": iso(paid),
                    "amount": rng.randint(400, 8200),
                    "currency": CURRENCY[market],
                    "status": status,
                    "country": market,
                })
    return rows


def generate_upsell_events(rng: random.Random, pool) -> list[dict]:
    rows, n = [], 0
    for market in MARKETS:
        for m in months_between(EVENT_START, EVENT_END):
            for _ in range(UPSELLS_PER_MARKET_MONTH):
                n += 1
                d = rand_day_in_month(rng, m)
                if rng.random() < 0.60:                       # plan upgrade
                    from_plan, to_plan = rng.choice(
                        [("basic", "premium"), ("premium", "enterprise")])
                    add_on, revenue = "", rng.randint(400, 3500)
                else:                                          # add-on sale
                    from_plan = to_plan = rng.choice(["basic", "premium", "enterprise"])
                    add_on, revenue = rng.choice(ADD_ONS), rng.randint(250, 900)
                rows.append({
                    "upsell_id": f"U{n:05d}",
                    "customer_id": _pick_customer(rng, pool, market, d),
                    "upsell_date": iso(d),
                    "from_plan": from_plan,
                    "to_plan": to_plan,
                    "add_on": add_on,
                    "add_on_revenue": revenue,
                    "sales_rep": rng.choice(SALES_REPS),
                    "country": market,
                })
    return rows


# Validation ------------------------------------------------------------------
def validate(tables: dict[str, list[dict]]) -> None:
    """Catch every dbt schema-test failure before dbt ever runs."""
    errors: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            errors.append(msg)

    customers = tables["customers"]
    cust_ids = {c["customer_id"] for c in customers}
    check(len(cust_ids) == len(customers), "duplicate customer_id")
    for c in customers:
        check(c["country"] in MARKETS, f"bad country {c['country']}")
        check(c["segment"] in SEGMENTS, f"bad segment {c['segment']}")
        check(c["plan_type"] in MRR_RANGE, f"bad plan_type {c['plan_type']}")
        check(int(c["monthly_revenue"]) > 0, "monthly_revenue must be > 0")

    subs = tables["subscriptions"]
    check(len({s["subscription_id"] for s in subs}) == len(subs), "duplicate subscription_id")
    for s in subs:
        check(s["customer_id"] in cust_ids, f"subscription FK miss {s['customer_id']}")
        check(s["plan_type"] in MRR_RANGE, f"bad plan_type {s['plan_type']}")
        check(int(s["mrr"]) > 0, "mrr must be > 0")
        if s["churn_reason"]:
            check(s["churn_reason"] in CHURN_REASONS, f"bad churn_reason {s['churn_reason']}")

    for sv in tables["nps_surveys"]:
        check(0 <= int(sv["nps_score"]) <= 10, "nps_score out of 0-10")
        check(1 <= int(sv["csat_score"]) <= 5, "csat_score out of 1-5")
        check(sv["country"] in MARKETS, f"bad survey country {sv['country']}")

    for ld in tables["leads"]:
        check(ld["country"] in MARKETS, f"bad lead country {ld['country']}")
        check(ld["lead_status"] in ("converted", "pending", "unassigned", "lost"),
              f"bad lead_status {ld['lead_status']}")

    for inv in tables["invoices"]:
        check(inv["customer_id"] in cust_ids, f"invoice FK miss {inv['customer_id']}")
        check(int(inv["amount"]) > 0, "invoice amount must be > 0")
        check(inv["status"] in ("paid", "overdue", "paid_late"), f"bad status {inv['status']}")

    for up in tables["upsell_events"]:
        check(up["customer_id"] in cust_ids, f"upsell FK miss {up['customer_id']}")
        check(int(up["add_on_revenue"]) >= 0, "add_on_revenue must be >= 0")

    for p in tables["partners"]:
        check(0.0 <= float(p["commission_rate"]) <= 1.0, "commission_rate out of 0-1")
        check(p["status"] in ("active", "inactive"), f"bad partner status {p['status']}")

    if errors:
        raise ValueError("seed validation failed:\n  " + "\n  ".join(errors[:20]))


# Output ----------------------------------------------------------------------
HEADERS = {
    "customers": ["customer_id", "customer_name", "country", "segment",
                  "signup_date", "plan_type", "monthly_revenue", "partner_id"],
    "subscriptions": ["subscription_id", "customer_id", "plan_type", "start_date",
                      "end_date", "churn_date", "churn_reason", "mrr", "country"],
    "nps_surveys": ["survey_id", "customer_id", "survey_date", "nps_score",
                    "csat_score", "country", "comment"],
    "leads": ["lead_id", "customer_id", "country", "service_type", "lead_date",
              "assigned_at", "converted_at", "lead_status", "lead_source"],
    "invoices": ["invoice_id", "customer_id", "issue_date", "due_date",
                 "paid_date", "amount", "currency", "status", "country"],
    "upsell_events": ["upsell_id", "customer_id", "upsell_date", "from_plan",
                      "to_plan", "add_on", "add_on_revenue", "sales_rep", "country"],
    "partners": ["partner_id", "partner_name", "country", "partner_type",
                 "contract_start_date", "monthly_fee", "commission_rate", "status"],
    # Rebuild 3 orchestration extension
    "referring_partners": ["referring_partner_id", "referring_partner_name", "country",
                           "referring_partner_type", "contract_start_date",
                           "monthly_fee_paid", "status"],
    "referral_leads": ["referral_lead_id", "referring_partner_id", "customer_id",
                       "country", "service_type", "industry", "referred_at",
                       "matched_at", "booked_at", "resolved_at", "referral_status",
                       "deal_size_estimate", "urgency"],
    "partner_capacity": ["partner_id", "snapshot_date", "active_deals_count",
                         "soft_cap", "hard_cap"],
    "partner_specializations": ["partner_id", "industry", "service_type",
                                "strength_score"],
    "deal_events": ["event_id", "referral_lead_id", "partner_id", "event_type",
                    "event_at", "agent_name", "rationale"],
    "partner_engagement_daily": ["partner_id", "snapshot_date",
                                 "response_latency_p50_hours", "response_latency_p90_hours",
                                 "accept_count", "decline_count", "no_response_count",
                                 "cancellation_count", "capacity_utilization_pct",
                                 "partner_roi_pct_snapshot", "net_monthly_value_snapshot",
                                 "churn_risk_score"],
    "referring_partner_engagement_daily": ["referring_partner_id", "snapshot_date",
                                           "leads_sent_count", "conversion_rate_pct",
                                           "avg_deal_size", "cancellation_rate_pct",
                                           "inquiry_response_latency_hours",
                                           "churn_risk_score"],
    "partner_status_events": ["event_id", "partner_id", "prior_status", "new_status",
                              "event_at", "reason", "changed_by"],
}


def write_csv(name: str, rows: list[dict]) -> None:
    path = SEEDS_DIR / f"{name}.csv"
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=HEADERS[name])
        writer.writeheader()
        writer.writerows(rows)
    print(f"  {name}.csv — {len(rows)} rows")


def write_anomalies_md() -> None:
    lines = [
        "# Injected Anomalies — NordLedger Marketplace simulation",
        "",
        "The synthetic dataset (`warehouse/scripts/generate_seeds.py`) plants four",
        "anomalies in the **latest month of data (2024-12)** — one per metric, each in",
        "a single market. Every other market stays within normal variance for that",
        "metric, so a correct proactive monitor flags exactly these four and nothing",
        "else.",
        "",
        "`src/monitor.py` scans the latest period against each segment's trailing",
        "baseline (modified z-score, median/MAD); `eval/test_monitor.py` asserts every",
        "row below is surfaced. This file is generated from the `ANOMALIES` list in",
        "`generate_seeds.py` — that list is the single source of truth.",
        "",
        "| Metric | Market | Month | Direction | Baseline | Anomalous | What it represents |",
        "|---|---|---|---|---|---|---|",
    ]
    for a in ANOMALIES:
        lines.append(
            f"| `{a['metric']}` | {a['market']} | {a['month']} | {a['kind']} | "
            f"{a['baseline']} | {a['anomalous']} | {a['detail']} |"
        )
    lines += [
        "",
        "All four are deliberately placed in the most recent month so the default",
        "`--scan` (which checks the latest period) catches them. The baseline months",
        "carry a small per-cell jitter — the series is not flat — but rates are",
        "assigned by *count* per (market, month), so baseline noise stays well below",
        "the injected signal.",
        "",
    ]
    ANOMALIES_MD.write_text("\n".join(lines), encoding="utf-8")
    print(f"  ANOMALIES.md — {len(ANOMALIES)} anomalies documented")


# =============================================================================
# Rebuild 3 orchestration extension — 8 new tables + SCENARIOS.md
# =============================================================================

SCENARIOS_MD = Path(__file__).resolve().parent.parent / "SCENARIOS.md"

# Enums for orchestration data
ORCH_INDUSTRIES = ["retail", "hospitality", "professional_services", "tech", "other"]
ORCH_SERVICE_TYPES = ["accounting", "tax_advisory", "bookkeeping", "audit", "payroll"]
REFERRING_PARTNER_TYPES = ["accounting_firm", "tax_advisor", "bookkeeping_service"]
REFERRAL_URGENCY = ["low", "medium", "high"]

# The 9 injected orchestration scenarios — single source of truth, mirrored to
# SCENARIOS.md. eval/test_e2e.py will assert every scenario is caught end-to-end.
ORCH_SCENARIOS = [
    {
        "id": "cold-start-market", "market": "DK", "months": ["2024-12"],
        "detail": "All 3 DK fulfillment partners at hard-cap for the whole month; "
                  "any DK lead should escalate to HITL — no candidates under capacity.",
        "asserts_via": "stg_partner_capacity (P001..P003 in 2024-12 with active_deals_count = hard_cap)"
    },
    {
        "id": "ambiguous-inquiry", "market": "*", "months": ["2024-08", "2024-10", "2024-12"],
        "detail": "3-5 referral_leads with industry/urgency/service_type all null — "
                  "Intake Agent should surface these to HITL rather than guess.",
        "asserts_via": "stg_referral_leads (industry is null AND service_type is null)"
    },
    {
        "id": "negotiation-stall", "market": "SE", "months": ["2024-11"],
        "detail": "2 referrals stuck in status='negotiating' with 3 rounds of "
                  "outreach_sent + reply_received but no booked event.",
        "asserts_via": "stg_deal_events (3+ outreach_sent per referral_lead_id, referral_status still 'negotiating')"
    },
    {
        "id": "sla-breach", "market": "NL", "months": ["2024-09", "2024-10"],
        "detail": "3 booked deals with no resolved_at after 45+ days — the Monitor "
                  "Agent should re-inject them into the graph for re-routing.",
        "asserts_via": "stg_referral_leads (referral_status='booked' AND resolved_at is null AND booked_at < today - 45 days)"
    },
    {
        "id": "cross-market-imbalance", "market": "DE+DK", "months": ["2024-11"],
        "detail": "DE all 3 partners at max capacity; DK all 3 partners at <20% utilization.",
        "asserts_via": "stg_partner_capacity (DE utilisation ≥95% AND DK ≤20% for 2024-11)"
    },
    {
        "id": "slow-churn-partner", "market": "DE", "months": ["2024-07..2024-12"],
        "detail": "Fulfillment partner P010 shows declining engagement (response_latency "
                  "climbing from 4h → 48h) AND declining ROI (+30% → -5%) over 6 months.",
        "asserts_via": "stg_partner_engagement_daily (P010 trends over 2024-07..2024-12)"
    },
    {
        "id": "unprofitable-but-friendly", "market": "US", "months": ["2024-10..2024-12"],
        "detail": "Fulfillment partner P016 keeps response times low + high accept rate "
                  "but ROI trends to -15% over Q4 2024.",
        "asserts_via": "stg_partner_engagement_daily (P016 partner_roi_pct_snapshot decline, response_latency stable)"
    },
    {
        "id": "slow-referring-ambassador", "market": "SE", "months": ["2024-10..2024-12"],
        "detail": "Referring partner R009 (Swedish) lead volume drops from ~30/mo to "
                  "~5/mo over Q4 2024 without explanation.",
        "asserts_via": "stg_referring_partner_engagement_daily (R009 leads_sent_count decline)"
    },
    {
        "id": "reactivate-inactive-partner", "market": "US", "months": ["2024-08..2024-12"],
        "detail": "Fulfillment partner P018 deactivated 2024-08-01. When a US lead arrives "
                  "in 2024-12 that no active US partner can serve, manager should be able "
                  "to reactivate P018 via the console (assert set_partner_status flow works).",
        "asserts_via": "stg_partner_status_events (P018 deactivation), stg_partners (P018 status='inactive')"
    },
]

# Volume knobs for orchestration
REFERRING_PARTNERS_PER_MARKET = 4          # 4 × 6 markets = 24 total
REFERRAL_LEADS_PER_MARKET_MONTH = 15       # 15 × 6 × 24 = 2160 total
SPECIALIZATION_COMBOS_PER_PARTNER = (2, 4) # each partner covers 2-4 industry×service_type combos


def generate_referring_partners(rng: random.Random) -> list[dict]:
    """~4 referring partners per market. Includes R009 (Sweden) — the slow-referring
    ambassador scenario partner."""
    rows, n = [], 0
    for market in MARKETS:
        for _ in range(REFERRING_PARTNERS_PER_MARKET):
            n += 1
            rows.append({
                "referring_partner_id": f"R{n:03d}",
                "referring_partner_name": f"{rng.choice(_SURNAMES[market])} "
                                          f"{rng.choice(['Advisors', 'Bureau', 'Group', 'Firm'])}",
                "country": market,
                "referring_partner_type": weighted(rng, REFERRING_PARTNER_TYPES, [0.60, 0.25, 0.15]),
                "contract_start_date": iso(date(rng.randint(2019, 2023),
                                                rng.randint(1, 12), rng.randint(1, 28))),
                "monthly_fee_paid": rng.choice([0, 0, 0, 500, 1000, 2000]),   # mostly pay-per-lead
                "status": "active",
            })
    # Set one inactive
    rows[rng.randrange(len(rows))]["status"] = "inactive"
    return rows


def generate_partner_specializations(rng: random.Random, partners: list[dict]) -> list[dict]:
    """Each fulfillment partner covers 2-4 (industry × service_type) combos."""
    rows = []
    for p in partners:
        n_combos = rng.randint(*SPECIALIZATION_COMBOS_PER_PARTNER)
        # Sample without replacement for diversity
        combos = set()
        while len(combos) < n_combos:
            combos.add((rng.choice(ORCH_INDUSTRIES), rng.choice(ORCH_SERVICE_TYPES)))
        for ind, svc in combos:
            rows.append({
                "partner_id": p["partner_id"],
                "industry": ind,
                "service_type": svc,
                "strength_score": rng.randint(40, 95),
            })
    return rows


def generate_referral_leads(
    rng: random.Random,
    referring_partners: list[dict],
    pool: dict,
) -> list[dict]:
    """~15 referrals per market per month across the 24-month event window.

    Injects scenarios: ambiguous-inquiry, negotiation-stall, sla-breach.
    """
    referring_by_market: dict[str, list[str]] = {m: [] for m in MARKETS}
    for r in referring_partners:
        if r["status"] == "active":
            referring_by_market[r["country"]].append(r["referring_partner_id"])

    rows, n = [], 0
    ambiguous_slots = {"2024-08": 2, "2024-10": 2, "2024-12": 1}    # scenario #2
    stall_slots_se = 2     # scenario #3 — 2 stalls in SE 2024-11
    sla_breach_slots_nl = 3   # scenario #4 — 3 booked-but-unresolved in NL 2024-09/10

    for market in MARKETS:
        for m in months_between(EVENT_START, EVENT_END):
            mkey = month_key(m)
            for _ in range(REFERRAL_LEADS_PER_MARKET_MONTH):
                n += 1
                d = rand_day_in_month(rng, m)
                cust = _pick_customer(rng, pool, market, d)
                referring = rng.choice(referring_by_market[market] or [""])

                # -- ambiguous-inquiry scenario ---------------------------------
                if ambiguous_slots.get(mkey, 0) > 0 and rng.random() < 0.3:
                    ambiguous_slots[mkey] -= 1
                    industry, service_type = None, None
                    urgency = None
                    deal_size = None
                    status = "lost"        # unresolvable, ends up lost after HITL
                    matched_at = booked_at = resolved_at = None
                    customer_id = None
                else:
                    industry = rng.choice(ORCH_INDUSTRIES)
                    service_type = rng.choice(ORCH_SERVICE_TYPES)
                    urgency = weighted(rng, REFERRAL_URGENCY, [0.30, 0.55, 0.15])
                    deal_size = rng.randint(3000, 45000)
                    # baseline pipeline
                    status_roll = rng.random()
                    if status_roll < 0.50:
                        status = "booked"
                        matched_at = d + timedelta(days=rng.randint(1, 3))
                        booked_at = matched_at + timedelta(days=rng.randint(2, 10))
                        resolved_at = booked_at + timedelta(days=rng.randint(5, 25))
                        customer_id = cust
                    elif status_roll < 0.70:
                        status = "lost"
                        matched_at = d + timedelta(days=rng.randint(1, 3))
                        booked_at = None
                        resolved_at = matched_at + timedelta(days=rng.randint(5, 15))
                        customer_id = None
                    else:
                        # still in pipeline
                        status = rng.choice(["pending", "matching", "negotiating"])
                        matched_at = d + timedelta(days=rng.randint(1, 3)) if status != "pending" else None
                        booked_at = None
                        resolved_at = None
                        customer_id = None

                # -- negotiation-stall scenario (SE 2024-11) --------------------
                if market == "SE" and mkey == "2024-11" and stall_slots_se > 0:
                    stall_slots_se -= 1
                    status = "negotiating"
                    matched_at = d + timedelta(days=1)
                    booked_at = None
                    resolved_at = None
                    customer_id = None

                # -- sla-breach scenario (NL 2024-09..10) -----------------------
                if market == "NL" and mkey in ("2024-09", "2024-10") and sla_breach_slots_nl > 0:
                    sla_breach_slots_nl -= 1
                    status = "booked"
                    matched_at = d + timedelta(days=2)
                    booked_at = matched_at + timedelta(days=5)
                    resolved_at = None    # <-- the breach: booked but never resolved
                    customer_id = cust

                rows.append({
                    "referral_lead_id": f"RL{n:05d}",
                    "referring_partner_id": referring,
                    "customer_id": customer_id or "",
                    "country": market,
                    "service_type": service_type or "",
                    "industry": industry or "",
                    "referred_at": iso(d),
                    "matched_at": iso(matched_at),
                    "booked_at": iso(booked_at),
                    "resolved_at": iso(resolved_at),
                    "referral_status": status,
                    "deal_size_estimate": deal_size or 0,
                    "urgency": urgency or "",
                })
    return rows


def generate_deal_events(
    rng: random.Random,
    referral_leads: list[dict],
    partners: list[dict],
) -> list[dict]:
    """Historical event stream per referral — mirrors what the graph writes at
    runtime. For seeds: derive events from each referral's terminal state."""
    partners_by_market: dict[str, list[str]] = {m: [] for m in MARKETS}
    for p in partners:
        partners_by_market[p["country"]].append(p["partner_id"])

    rows, n = [], 0
    for lead in referral_leads:
        market = lead["country"]
        matched_partner = rng.choice(partners_by_market[market] or [""])
        referred_at = date.fromisoformat(lead["referred_at"])

        # every lead gets a classify event from intake
        n += 1
        rows.append({
            "event_id": f"EV{n:06d}",
            "referral_lead_id": lead["referral_lead_id"],
            "partner_id": "",
            "event_type": "classified",
            "event_at": iso(referred_at),
            "agent_name": "intake",
            "rationale": "" if lead["industry"] else "low_confidence_ambiguous",
        })

        if lead["matched_at"]:
            n += 1
            rows.append({
                "event_id": f"EV{n:06d}",
                "referral_lead_id": lead["referral_lead_id"],
                "partner_id": matched_partner,
                "event_type": "matched",
                "event_at": lead["matched_at"],
                "agent_name": "matching",
                "rationale": f"top-3 by capacity+ROI, ranked {matched_partner} #1",
            })

        # negotiation-stall gets 3 outreach + reply cycles
        if lead["referral_status"] == "negotiating" and lead["matched_at"]:
            matched_d = date.fromisoformat(lead["matched_at"])
            for round_i in range(3):
                round_offset = timedelta(days=(round_i * 2) + 1)
                n += 1
                rows.append({
                    "event_id": f"EV{n:06d}",
                    "referral_lead_id": lead["referral_lead_id"],
                    "partner_id": matched_partner,
                    "event_type": "outreach_sent",
                    "event_at": iso(matched_d + round_offset),
                    "agent_name": "negotiation",
                    "rationale": f"round {round_i + 1}",
                })
                n += 1
                rows.append({
                    "event_id": f"EV{n:06d}",
                    "referral_lead_id": lead["referral_lead_id"],
                    "partner_id": matched_partner,
                    "event_type": "reply_received",
                    "event_at": iso(matched_d + round_offset + timedelta(days=1)),
                    "agent_name": "negotiation",
                    "rationale": "propose_alternative",
                })

        if lead["booked_at"]:
            n += 1
            rows.append({
                "event_id": f"EV{n:06d}",
                "referral_lead_id": lead["referral_lead_id"],
                "partner_id": matched_partner,
                "event_type": "booked",
                "event_at": lead["booked_at"],
                "agent_name": "negotiation",
                "rationale": "partner accepted; capacity held",
            })

        if lead["resolved_at"]:
            n += 1
            rows.append({
                "event_id": f"EV{n:06d}",
                "referral_lead_id": lead["referral_lead_id"],
                "partner_id": matched_partner,
                "event_type": "resolved" if lead["referral_status"] == "booked" else "lost",
                "event_at": lead["resolved_at"],
                "agent_name": "monitor",
                "rationale": "closed successfully" if lead["referral_status"] == "booked" else "no fit / declined",
            })

        # sla-breach scenario: emit escalation event when booked but not resolved
        if lead["referral_status"] == "booked" and not lead["resolved_at"] and lead["booked_at"]:
            booked_d = date.fromisoformat(lead["booked_at"])
            n += 1
            rows.append({
                "event_id": f"EV{n:06d}",
                "referral_lead_id": lead["referral_lead_id"],
                "partner_id": matched_partner,
                "event_type": "escalated_to_hitl",
                "event_at": iso(booked_d + timedelta(days=45)),
                "agent_name": "monitor",
                "rationale": "SLA breach — 45 days without resolution",
            })
    return rows


def generate_partner_capacity(rng: random.Random, partners: list[dict]) -> list[dict]:
    """Daily capacity snapshot per partner. Injects cold-start (DK 2024-12) and
    cross-market-imbalance (DE saturated + DK starved 2024-11) scenarios."""
    rows = []
    all_days = []
    d = EVENT_START
    while d <= date(2024, 12, 31):
        all_days.append(d)
        d += timedelta(days=1)

    for p in partners:
        if p["status"] == "inactive":
            continue    # inactive partners don't have capacity snapshots
        soft_cap = rng.randint(6, 12)
        hard_cap = soft_cap + rng.randint(2, 4)

        for day in all_days:
            mkey = f"{day.year:04d}-{day.month:02d}"
            market = p["country"]

            # baseline utilisation ~40-70%
            active = rng.randint(int(soft_cap * 0.3), int(soft_cap * 0.75))

            # cold-start-market scenario: DK 2024-12 → all DK partners at hard_cap
            if market == "DK" and mkey == "2024-12":
                active = hard_cap
            # cross-market-imbalance scenario: DE saturated + DK starved 2024-11
            if market == "DE" and mkey == "2024-11":
                active = hard_cap - rng.randint(0, 1)     # ≥95% util
            if market == "DK" and mkey == "2024-11":
                active = max(0, int(soft_cap * 0.15))     # ≤20% util

            rows.append({
                "partner_id": p["partner_id"],
                "snapshot_date": iso(day),
                "active_deals_count": active,
                "soft_cap": soft_cap,
                "hard_cap": hard_cap,
            })
    return rows


def generate_partner_engagement_daily(
    rng: random.Random,
    partners: list[dict],
) -> list[dict]:
    """Rolling daily engagement + ROI snapshot per partner.

    Injects slow-churn (P010, 2024-07..12) and unprofitable-but-friendly (P016,
    2024-10..12) scenarios."""
    rows = []
    all_days = []
    d = EVENT_START
    while d <= date(2024, 12, 31):
        all_days.append(d)
        d += timedelta(days=1)

    for p in partners:
        if p["status"] == "inactive":
            continue
        pid = p["partner_id"]

        # baseline partner ROI (random per-partner constant)
        baseline_roi = rng.uniform(15.0, 45.0)
        baseline_nmv = rng.uniform(5000.0, 25000.0)
        baseline_p50 = rng.uniform(2.0, 6.0)     # response latency p50 hours
        baseline_p90 = baseline_p50 * rng.uniform(2.5, 4.0)

        for day in all_days:
            mkey = f"{day.year:04d}-{day.month:02d}"

            roi = baseline_roi + rng.gauss(0, 2.0)
            nmv = baseline_nmv + rng.gauss(0, 800.0)
            p50 = max(0.5, baseline_p50 + rng.gauss(0, 0.5))
            p90 = max(p50 + 1.0, baseline_p90 + rng.gauss(0, 1.5))
            capacity_util = rng.uniform(35.0, 75.0)

            # slow-churn-partner scenario: P010, degrade over 2024-07..12
            if pid == "P010" and mkey >= "2024-07":
                month_offset = (day.year - 2024) * 12 + (day.month - 7)   # 0..5
                degrade = month_offset / 5.0                              # 0..1
                roi = baseline_roi * (1 - degrade * 1.15)                 # +30% → -5%
                p50 = baseline_p50 * (1 + degrade * 11.0)                 # 4h → 48h
                p90 = p50 * 3.5

            # unprofitable-but-friendly scenario: P016, Q4 2024
            if pid == "P016" and mkey >= "2024-10":
                month_offset = day.month - 10   # 0..2
                degrade = month_offset / 2.0    # 0..1
                roi = baseline_roi * (1 - degrade * 1.65)      # ~+30% → -15%
                # response latency STAYS healthy

            accept = rng.randint(3, 12)
            decline = rng.randint(0, 3)
            no_response = rng.randint(0, 2)
            cancellations = 0 if rng.random() > 0.05 else 1

            # crude churn-risk score (0-100)
            risk = 0.0
            risk += min(40, max(0, (p50 - 4) * 2.5))              # latency signal
            risk += min(25, no_response * 5)                        # ghosting
            risk += min(20, max(0, (baseline_roi - roi) * 0.8))    # roi decline
            risk += cancellations * 8
            churn_risk = max(0.0, min(100.0, risk))

            rows.append({
                "partner_id": pid,
                "snapshot_date": iso(day),
                "response_latency_p50_hours": round(p50, 2),
                "response_latency_p90_hours": round(p90, 2),
                "accept_count": accept,
                "decline_count": decline,
                "no_response_count": no_response,
                "cancellation_count": cancellations,
                "capacity_utilization_pct": round(capacity_util, 2),
                "partner_roi_pct_snapshot": round(roi, 2),
                "net_monthly_value_snapshot": round(nmv, 2),
                "churn_risk_score": round(churn_risk, 2),
            })
    return rows


def generate_referring_partner_engagement_daily(
    rng: random.Random,
    referring_partners: list[dict],
) -> list[dict]:
    """Symmetrical rollup for the ambassador side. Injects slow-referring-ambassador
    scenario (R009, Q4 2024)."""
    rows = []
    all_days = []
    d = EVENT_START
    while d <= date(2024, 12, 31):
        all_days.append(d)
        d += timedelta(days=1)

    for r in referring_partners:
        if r["status"] == "inactive":
            continue
        rid = r["referring_partner_id"]
        baseline_leads = rng.uniform(15.0, 30.0)   # per day
        baseline_conv = rng.uniform(45.0, 65.0)
        baseline_size = rng.uniform(8000.0, 25000.0)
        baseline_resp = rng.uniform(4.0, 12.0)

        for day in all_days:
            mkey = f"{day.year:04d}-{day.month:02d}"

            leads_sent = max(0, int(baseline_leads * 0.15 + rng.gauss(2.0, 1.0)))
            conv = max(0.0, min(100.0, baseline_conv + rng.gauss(0, 5)))
            size = max(1000.0, baseline_size + rng.gauss(0, 2000))
            cancel = max(0.0, min(100.0, rng.uniform(5.0, 15.0)))
            resp = max(0.5, baseline_resp + rng.gauss(0, 2.0))

            # slow-referring-ambassador scenario: R009, Q4 2024
            if rid == "R009" and mkey >= "2024-10":
                month_offset = day.month - 10   # 0..2
                degrade = month_offset / 2.0
                leads_sent = max(0, int(leads_sent * (1 - degrade * 0.83)))   # 30 → 5

            # crude churn-risk score
            risk = 0.0
            risk += min(35, max(0, (baseline_leads * 0.15 - leads_sent) * 4))
            risk += min(25, max(0, (baseline_conv - conv) * 0.8))
            risk += min(15, max(0, (resp - baseline_resp) * 1.5))
            churn_risk = max(0.0, min(100.0, risk))

            rows.append({
                "referring_partner_id": rid,
                "snapshot_date": iso(day),
                "leads_sent_count": leads_sent,
                "conversion_rate_pct": round(conv, 2),
                "avg_deal_size": round(size, 2),
                "cancellation_rate_pct": round(cancel, 2),
                "inquiry_response_latency_hours": round(resp, 2),
                "churn_risk_score": round(churn_risk, 2),
            })
    return rows


def generate_partner_status_events(
    rng: random.Random,
    partners: list[dict],
) -> list[dict]:
    """Sparse audit trail. Injects reactivate-inactive-partner scenario (P018)."""
    rows = []
    # scenario #9: P018 deactivation on 2024-08-01
    rows.append({
        "event_id": "SE00001",
        "partner_id": "P018",
        "prior_status": "active",
        "new_status": "inactive",
        "event_at": "2024-08-01",
        "reason": "auto-deactivation after 90d capacity-underutilisation",
        "changed_by": "system",
    })
    return rows


def apply_partner_deactivation(partners: list[dict]) -> None:
    """Mutate the partners list so P018 shows as inactive from the seed
    (matches partner_status_events)."""
    for p in partners:
        if p["partner_id"] == "P018":
            p["status"] = "inactive"
            return


def validate_orchestration(tables: dict[str, list[dict]]) -> None:
    """Additional validation for the 8 orchestration tables."""
    errors = []
    def check(cond, msg):
        if not cond:
            errors.append(msg)

    referring_ids = {r["referring_partner_id"] for r in tables["referring_partners"]}
    partner_ids = {p["partner_id"] for p in tables["partners"]}
    referral_ids = {rl["referral_lead_id"] for rl in tables["referral_leads"]}

    for rl in tables["referral_leads"]:
        if rl["referring_partner_id"]:
            check(rl["referring_partner_id"] in referring_ids,
                  f"referral_lead {rl['referral_lead_id']} references unknown referring_partner {rl['referring_partner_id']}")

    for ps in tables["partner_specializations"]:
        check(ps["partner_id"] in partner_ids,
              f"partner_specialization references unknown partner {ps['partner_id']}")
        check(0 <= int(ps["strength_score"]) <= 100, "strength_score out of 0-100")

    for pc in tables["partner_capacity"]:
        check(pc["partner_id"] in partner_ids,
              f"partner_capacity references unknown partner {pc['partner_id']}")

    for de in tables["deal_events"]:
        check(de["referral_lead_id"] in referral_ids,
              f"deal_event references unknown referral {de['referral_lead_id']}")

    for pe in tables["partner_engagement_daily"]:
        check(pe["partner_id"] in partner_ids,
              f"partner_engagement references unknown partner {pe['partner_id']}")
        check(0.0 <= float(pe["churn_risk_score"]) <= 100.0, "churn_risk out of range")

    for re in tables["referring_partner_engagement_daily"]:
        check(re["referring_partner_id"] in referring_ids,
              f"referring_engagement references unknown referring_partner {re['referring_partner_id']}")

    for se in tables["partner_status_events"]:
        check(se["partner_id"] in partner_ids,
              f"partner_status_event references unknown partner {se['partner_id']}")

    if errors:
        raise ValueError("orchestration seed validation failed:\n  " + "\n  ".join(errors[:20]))


def write_scenarios_md() -> None:
    lines = [
        "# Injected Scenarios — NordLedger Multi-Agent Lead Orchestration",
        "",
        "The synthetic dataset (`warehouse/scripts/generate_seeds.py`) plants **9",
        "scenarios** across the referral + orchestration tables. Every one is",
        "asserted end-to-end by `eval/test_e2e.py` — the graph must reach the",
        "expected terminal state for each, or the test fails.",
        "",
        "This file is generated from the `ORCH_SCENARIOS` list in the generator —",
        "that list is the single source of truth. If you change scenarios, edit the",
        "generator and re-run.",
        "",
        "| # | Scenario | Market | Months | What we assert against |",
        "|---|---|---|---|---|",
    ]
    for i, s in enumerate(ORCH_SCENARIOS, 1):
        months = ", ".join(s["months"])
        lines.append(f"| {i} | **{s['id']}** — {s['detail']} | {s['market']} | {months} | `{s['asserts_via']}` |")
    lines += [
        "",
        "## Notes",
        "",
        "- All scenarios land in the latest ~6 months of the 24-month window so the",
        "  Monitor's rolling baseline has plenty of clean history.",
        "- `slow-churn`, `unprofitable-but-friendly` and `slow-referring-ambassador`",
        "  are *gradual* — no single-day anomaly. The eval suite asserts the trend",
        "  is detected, not a single point.",
        "- `cold-start-market`, `cross-market-imbalance`, `negotiation-stall`,",
        "  `sla-breach` and `reactivate-inactive-partner` are *event-shaped* —",
        "  binary presence/absence in the seeded data.",
        "",
    ]
    SCENARIOS_MD.write_text("\n".join(lines), encoding="utf-8")
    print(f"  SCENARIOS.md — {len(ORCH_SCENARIOS)} orchestration scenarios documented")


def main() -> None:
    rng = random.Random(RANDOM_SEED)
    print(f"Generating NordLedger Marketplace seeds (seed={RANDOM_SEED})...")

    # -- NordLedger core (BI Agent compatible) ------------------------------
    partners = generate_partners(rng)
    customers, subscriptions = generate_customers_and_subscriptions(rng, partners)
    pool = _customer_pool(customers)
    nps_surveys = generate_nps_surveys(rng, pool)
    leads = generate_leads(rng, pool)
    invoices = generate_invoices(rng, pool)
    upsell_events = generate_upsell_events(rng, pool)

    # -- Rebuild 3 orchestration extension ----------------------------------
    referring_partners = generate_referring_partners(rng)
    partner_specializations = generate_partner_specializations(rng, partners)
    referral_leads = generate_referral_leads(rng, referring_partners, pool)
    deal_events = generate_deal_events(rng, referral_leads, partners)
    partner_capacity = generate_partner_capacity(rng, partners)
    partner_engagement_daily = generate_partner_engagement_daily(rng, partners)
    referring_partner_engagement_daily = generate_referring_partner_engagement_daily(
        rng, referring_partners)
    partner_status_events = generate_partner_status_events(rng, partners)

    # Apply P018 deactivation (scenario #9) to the partners seed itself
    apply_partner_deactivation(partners)

    tables = {
        "customers": customers,
        "subscriptions": subscriptions,
        "nps_surveys": nps_surveys,
        "leads": leads,
        "invoices": invoices,
        "upsell_events": upsell_events,
        "partners": partners,
        # orchestration
        "referring_partners": referring_partners,
        "referral_leads": referral_leads,
        "partner_capacity": partner_capacity,
        "partner_specializations": partner_specializations,
        "deal_events": deal_events,
        "partner_engagement_daily": partner_engagement_daily,
        "referring_partner_engagement_daily": referring_partner_engagement_daily,
        "partner_status_events": partner_status_events,
    }
    validate(tables)
    validate_orchestration(tables)
    for name, rows in tables.items():
        write_csv(name, rows)
    write_anomalies_md()
    write_scenarios_md()
    print("Done.")


if __name__ == "__main__":
    main()
