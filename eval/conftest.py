"""Shared fixtures + opt-in markers for the Multi-Agent Lead Orchestration
tests. Mirrors the Self-Querying BI Agent's pattern: deterministic tests run
free; `warehouse` and `llm` markers are opt-in for cost/setup reasons.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_WAREHOUSE_DIR = _REPO_ROOT / "warehouse"
_DUCKDB_FILE = _WAREHOUSE_DIR / "nordledger.duckdb"


def pytest_collection_modifyitems(config, items):
    """Skip opt-in markers unless explicitly selected."""
    markexpr = config.option.markexpr or ""

    skip_warehouse = pytest.mark.skip(
        reason="warehouse test — run with: pytest -m warehouse "
               "(after `cd warehouse && dbt build`)"
    )
    skip_llm = pytest.mark.skip(
        reason="live LLM test — run with: pytest -m llm (costs API tokens)"
    )
    for item in items:
        if "warehouse" in item.keywords and "warehouse" not in markexpr:
            item.add_marker(skip_warehouse)
        if "llm" in item.keywords and "llm" not in markexpr:
            item.add_marker(skip_llm)


@pytest.fixture(scope="session")
def warehouse_path() -> Path:
    """Path to the built DuckDB. Skips if the warehouse isn't built."""
    if not _DUCKDB_FILE.exists():
        pytest.skip(
            f"DuckDB warehouse not built ({_DUCKDB_FILE.name} missing). "
            f"Run: cd warehouse && dbt deps && dbt seed && dbt build"
        )
    return _DUCKDB_FILE


@pytest.fixture(scope="session")
def as_of() -> date:
    """The as-of date the seed data uses — pinned to the simulation's horizon."""
    return date(2024, 12, 31)


@pytest.fixture()
def warehouse_cleaner(warehouse_path):
    """Snapshot-and-restore for tests that mutate the warehouse (crm writes,
    partner status changes). Restores every touched referral_leads row and
    partners row, and purges runtime ('RT…') deal_events / status_events."""
    import duckdb

    lead_snapshots: dict[str, tuple] = {}
    partner_snapshots: dict[str, str] = {}

    class Cleaner:
        def snapshot_lead(self, referral_lead_id: str) -> None:
            with duckdb.connect(str(warehouse_path)) as con:
                row = con.execute(
                    "SELECT referral_status, booked_at, resolved_at "
                    "FROM main.referral_leads WHERE referral_lead_id = ?",
                    [referral_lead_id]).fetchone()
            if row is not None:
                lead_snapshots.setdefault(referral_lead_id, row)

        def snapshot_partner(self, partner_id: str) -> None:
            with duckdb.connect(str(warehouse_path)) as con:
                row = con.execute(
                    "SELECT status FROM main.partners WHERE partner_id = ?",
                    [partner_id]).fetchone()
            if row is not None:
                partner_snapshots.setdefault(partner_id, row[0])

    cleaner = Cleaner()
    yield cleaner

    with duckdb.connect(str(warehouse_path)) as con:
        for lead_id, (status, booked_at, resolved_at) in lead_snapshots.items():
            con.execute(
                "UPDATE main.referral_leads SET referral_status = ?, "
                "booked_at = ?, resolved_at = ? WHERE referral_lead_id = ?",
                [status, booked_at, resolved_at, lead_id])
        for partner_id, status in partner_snapshots.items():
            con.execute("UPDATE main.partners SET status = ? WHERE partner_id = ?",
                        [status, partner_id])
        con.execute("DELETE FROM main.deal_events WHERE event_id LIKE 'RT%'")
        con.execute("DELETE FROM main.partner_status_events WHERE event_id LIKE 'RT%'")


@pytest.fixture()
def sample_referral_texts() -> dict[str, str]:
    """A hand-authored sample per scenario type. Used by both the ambiguity
    logic tests (fixture-only) and the live `-m llm` intake tests."""
    return {
        "clear_swedish": (
            "Hi — I'm Anders Lindqvist, Head of Finance at Nordic Retail Group AB, "
            "a mid-sized retail chain based in Sweden. We're looking for a new "
            "bookkeeping partner starting next quarter. Budget approved, tech "
            "stack is Fortnox + shopify. Timeline: onboarding within 6 weeks."
        ),
        "clear_dutch_urgent": (
            "Van den Berg Hospitality BV — restaurant group in the Netherlands. "
            "We need an auditor for our 2024 books urgently, our previous auditor "
            "quit. Contact: Marieke van den Berg, CFO, mvdb@vdbergrestaurants.nl. "
            "We use Exact Online."
        ),
        "ambiguous": (
            "Hi, we might be interested in some accounting help. Can someone "
            "reach out? Thanks."
        ),
        "very_ambiguous": (
            "hey — partnership?"
        ),
        "clear_german_tech": (
            "Bauer & Söhne GmbH — a mid-market software company in Munich, "
            "Germany. We need help with tax_advisory for a EUR 2M revenue base. "
            "Timeline: as soon as possible. Frank Bauer, CEO, frank@bauer-soehne.de. "
            "Stack: DATEV + Salesforce."
        ),
    }
