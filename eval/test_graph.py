"""Week 4 exit-gate tests — the LangGraph with HITL interrupts + checkpointer.

The keystone assertions:
  1. A paused lead resumes CLEANLY FROM DISK — proven by using two separate
     compiled graph instances over the same SQLite file (simulating two CLI
     processes).
  2. Scenario #2 (ambiguous → enrich-resume) and scenario #9 (capacity →
     reactivate-resume) complete end-to-end through the graph.
"""

from __future__ import annotations

from datetime import date

import pytest

from src.config import load_config
from src.data.warehouse import find_referral_lead
from src.dry_run import lead_state_from_seed
from src.graph import (extract_interrupt, get_paused, lead_out,
                       open_graph, resume_lead, run_lead)
from src.mcp_stack import MCPStack

AS_OF = date(2024, 12, 31)


@pytest.fixture()
def config(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-placeholder")
    return load_config(require_api_key=False)


@pytest.fixture()
def checkpoint_dir(tmp_path):
    return tmp_path / "checkpoints"


def _seed_lead(warehouse_path, **criteria):
    rows = find_referral_lead(warehouse_path, **criteria)
    assert rows, f"no seeded lead for {criteria}"
    return lead_state_from_seed(rows[0])


# ── happy path through the graph ────────────────────────────────────────────

@pytest.mark.warehouse
def test_graph_happy_path_books(warehouse_path, warehouse_cleaner, config,
                                checkpoint_dir):
    lead = _seed_lead(warehouse_path, country="SE", status="pending")
    warehouse_cleaner.snapshot_lead(lead.referral_lead_id)
    stack = MCPStack(warehouse_path)
    with open_graph(stack, config, as_of=AS_OF,
                    checkpoint_dir=checkpoint_dir) as graph:
        result = run_lead(graph, lead)
    assert extract_interrupt(result) is None
    assert lead_out(result).status == "booked"


# ── exit gate: pause → separate instance → resume from disk ─────────────────

@pytest.mark.warehouse
@pytest.mark.scenarios
def test_paused_lead_resumes_from_disk_across_instances(
        warehouse_path, warehouse_cleaner, config, checkpoint_dir):
    """THE Week 4 exit gate. Instance A pauses at hitl_negotiate and dies.
    Instance B (fresh graph, fresh MCPStack — a new 'process') reads the
    interrupt from SQLite and resumes to booked."""
    lead = _seed_lead(warehouse_path, country="NL", status="pending")
    warehouse_cleaner.snapshot_lead(lead.referral_lead_id)

    # -- instance A: exhaust the negotiation budget, then die --------------
    stack_a = MCPStack(warehouse_path)
    stack_a.prime_slack_replies(["counter"] * 9)
    with open_graph(stack_a, config, as_of=AS_OF,
                    checkpoint_dir=checkpoint_dir) as graph_a:
        result_a = run_lead(graph_a, lead)
        pause = extract_interrupt(result_a)
    assert pause is not None
    assert pause["destination"] == "hitl_negotiate"
    assert len(pause["negotiation"]["history"]) == 9

    # -- instance B: fresh everything, resume from the SQLite checkpoint ---
    stack_b = MCPStack(warehouse_path)         # auto-reply: accept
    with open_graph(stack_b, config, as_of=AS_OF,
                    checkpoint_dir=checkpoint_dir) as graph_b:
        persisted = get_paused(graph_b, lead.referral_lead_id)
        assert persisted is not None, "interrupt must be readable from disk"
        assert persisted["destination"] == "hitl_negotiate"

        result_b = resume_lead(graph_b, lead.referral_lead_id,
                               {"action": "override", "partner_id": "P004",
                                "note": "manager knows P004 has bandwidth"})
    assert extract_interrupt(result_b) is None
    final_b = lead_out(result_b)
    assert final_b.status == "booked"
    assert final_b.negotiation_history[-1]["partner_id"] == "P004"


# ── scenario #2: ambiguous → enrich-resume ──────────────────────────────────

@pytest.mark.warehouse
@pytest.mark.scenarios
def test_scenario_2_ambiguous_enrich_resume(warehouse_path, warehouse_cleaner,
                                            config, checkpoint_dir):
    import duckdb
    with duckdb.connect(str(warehouse_path)) as con:
        row = con.execute(
            "SELECT * FROM main_staging.stg_referral_leads "
            "WHERE industry IS NULL AND service_type IS NULL "
            "ORDER BY referral_lead_id LIMIT 1").fetchone()
        cols = [d[0] for d in con.description]
    seed = dict(zip(cols, row))
    lead = lead_state_from_seed(seed)
    warehouse_cleaner.snapshot_lead(lead.referral_lead_id)

    stack = MCPStack(warehouse_path)
    with open_graph(stack, config, as_of=AS_OF,
                    checkpoint_dir=checkpoint_dir) as graph:
        result = run_lead(graph, lead)
        pause = extract_interrupt(result)
        assert pause is not None and pause["destination"] == "hitl_intake"
        assert "service_type" in pause["intake"]["missing_essentials"]

        # manager supplies the missing context (SE market has capacity)
        result2 = resume_lead(graph, lead.referral_lead_id,
                              {"action": "enrich",
                               "fields": {"country": "SE",
                                          "service_type": "bookkeeping",
                                          "industry": "retail"},
                               "note": "clarified with Billy on the phone"})
    assert extract_interrupt(result2) is None
    final = lead_out(result2)
    assert final.status == "booked"
    # manager-supplied fields carry provenance "(manager)" at confidence 1.0
    assert final.provenance["service_type"].confidence == 1.0
    assert final.provenance["service_type"].source_span == "(manager)"


# ── scenario #9: capacity → reactivate-resume ──────────────────────────────

@pytest.mark.warehouse
@pytest.mark.scenarios
def test_scenario_9_reactivate_resume_books_via_p018(
        warehouse_path, warehouse_cleaner, config, checkpoint_dir):
    """Force US into a capacity crunch by deactivating every active US
    partner, run a US lead → hitl_capacity with P018 listed dormant →
    manager reactivates P018 → graph re-matches and books it."""
    import duckdb
    with duckdb.connect(str(warehouse_path)) as con:
        us_active = [r[0] for r in con.execute(
            "SELECT partner_id FROM main.partners "
            "WHERE country = 'US' AND status = 'active'").fetchall()]
    stack = MCPStack(warehouse_path)
    for pid in us_active:
        warehouse_cleaner.snapshot_partner(pid)
        stack.call("partner_capacity", "set_partner_status",
                   partner_id=pid, status="inactive",
                   reason="test-induced capacity crunch", changed_by="system")
    warehouse_cleaner.snapshot_partner("P018")

    lead = _seed_lead(warehouse_path, country="US", status="pending")
    warehouse_cleaner.snapshot_lead(lead.referral_lead_id)

    with open_graph(stack, config, as_of=AS_OF,
                    checkpoint_dir=checkpoint_dir) as graph:
        result = run_lead(graph, lead)
        pause = extract_interrupt(result)
        assert pause is not None and pause["destination"] == "hitl_capacity"
        dormant_ids = [d["partner_id"] for d in pause["dormant_partners"]]
        assert "P018" in dormant_ids

        result2 = resume_lead(graph, lead.referral_lead_id,
                              {"action": "reactivate", "partner_id": "P018",
                               "note": "P018 confirmed they want back in"})
    assert extract_interrupt(result2) is None
    final = lead_out(result2)
    assert final.status == "booked"
    assert final.negotiation_history[-1]["partner_id"] == "P018"


# ── drop decisions ──────────────────────────────────────────────────────────

@pytest.mark.warehouse
def test_drop_decision_marks_lost(warehouse_path, warehouse_cleaner, config,
                                  checkpoint_dir):
    lead = _seed_lead(warehouse_path, country="NO", status="pending")
    warehouse_cleaner.snapshot_lead(lead.referral_lead_id)
    stack = MCPStack(warehouse_path)
    stack.prime_slack_replies(["decline"] * 3)
    with open_graph(stack, config, as_of=AS_OF,
                    checkpoint_dir=checkpoint_dir) as graph:
        result = run_lead(graph, lead)
        assert extract_interrupt(result)["destination"] == "hitl_negotiate"
        result2 = resume_lead(graph, lead.referral_lead_id,
                              {"action": "drop", "note": "duplicate referral"})
    final = lead_out(result2)
    assert final.status == "lost"
    assert "duplicate referral" in final.hitl_reason


# ── decision validation ─────────────────────────────────────────────────────

def test_validate_decision_rejects_malformed():
    from src.hitl import validate_decision
    with pytest.raises(ValueError):
        validate_decision("hitl_intake", {"action": "override"})
    with pytest.raises(ValueError):
        validate_decision("hitl_intake", {"action": "enrich"})       # no fields
    with pytest.raises(ValueError):
        validate_decision("hitl_capacity", {"action": "reactivate"})  # no partner
    validate_decision("hitl_negotiate",
                      {"action": "override", "partner_id": "P001"})   # ok


def test_enrichment_rejects_unknown_field():
    from src.hitl import apply_enrichment
    from src.state import EXTRACTED_FIELDS, build_lead_state
    extraction = {f: None for f in EXTRACTED_FIELDS}
    lead = build_lead_state(
        referral_lead_id="RL_x", raw_text="…", received_at=AS_OF,
        extraction=extraction,
        provenance={f: {"confidence": 0.0, "source_span": None}
                    for f in EXTRACTED_FIELDS})
    with pytest.raises(ValueError):
        apply_enrichment(lead, {"nonsense_field": "x"})
