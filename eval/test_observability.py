"""Week 5 observability tests — the trace tree matches §10's reference shape.

Pure tests (recorder mechanics) run in the default suite; the reference-shape
assertions against a real graph run are warehouse-marked.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from src.observability import TraceRecorder

AS_OF = date(2024, 12, 31)


# ── recorder mechanics (pure) ───────────────────────────────────────────────

def test_spans_nest_under_current_context():
    rec = TraceRecorder()
    with rec.trace("t"):
        with rec.span("a"):
            with rec.span("a1"):
                pass
        with rec.span("b"):
            pass
    assert rec.shape() == [{"t": [{"a": ["a1"]}, "b"]}]


def test_span_durations_recorded():
    rec = TraceRecorder()
    with rec.trace("t"):
        with rec.span("a"):
            pass
    t = rec.last_trace()
    assert t.duration_ms is not None and t.duration_ms >= 0
    assert t.children[0].duration_ms is not None


def test_save_writes_json(tmp_path):
    rec = TraceRecorder(out_dir=tmp_path)
    with rec.trace("lead_run:RL1"):
        with rec.span("x"):
            pass
    paths = rec.save()
    assert len(paths) == 1
    data = json.loads(paths[0].read_text(encoding="utf-8"))
    assert data["name"] == "lead_run:RL1"
    assert data["children"][0]["name"] == "x"


def test_orphan_span_becomes_its_own_trace():
    rec = TraceRecorder()
    with rec.span("standalone"):
        pass
    assert rec.shape() == ["standalone"]


# ── the §10 reference shape: a booked lead's trace tree ────────────────────

@pytest.mark.warehouse
@pytest.mark.scenarios
def test_lead_run_trace_matches_reference_shape(warehouse_path,
                                                warehouse_cleaner,
                                                tmp_path, monkeypatch):
    """Exit gate: run a lead through the graph with the recorder attached and
    assert the nested span tree has the §10 reference structure —
    node spans at level 1, mcp.* child spans inside them."""
    from src.config import load_config
    from src.data.warehouse import find_referral_lead
    from src.dry_run import lead_state_from_seed
    from src.graph import open_graph, run_lead
    from src.mcp_stack import MCPStack

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-placeholder")
    config = load_config(require_api_key=False)
    rec = TraceRecorder(out_dir=tmp_path)

    seed = find_referral_lead(warehouse_path, country="SE",
                              status="pending", limit=1)[0]
    warehouse_cleaner.snapshot_lead(seed["referral_lead_id"])
    lead = lead_state_from_seed(seed)

    stack = MCPStack(warehouse_path, recorder=rec)
    with open_graph(stack, config, as_of=AS_OF,
                    checkpoint_dir=tmp_path / "ckpt", recorder=rec) as graph:
        run_lead(graph, lead, recorder=rec)

    trace = rec.last_trace()
    assert trace.name == f"lead_run:{lead.referral_lead_id}"

    node_names = [c.name for c in trace.children]
    assert node_names == ["intake_gate", "match", "negotiate"], node_names

    match_span = trace.children[1]
    match_children = [c.name for c in match_span.children]
    assert match_children == ["mcp.partner_capacity.list_candidates"]

    neg_span = trace.children[2]
    neg_children = [c.name for c in neg_span.children]
    assert neg_children == [
        "mcp.partner_capacity.hold_capacity",
        "mcp.slack.send_dm",
        "mcp.slack.wait_for_reply",
        "mcp.crm.upsert_lead",
        "mcp.crm.update_stage",
    ], neg_children


@pytest.mark.warehouse
def test_monitor_sweep_trace_shape(warehouse_path, monkeypatch):
    from src.config import load_config
    from src.agents.monitor import sweep

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-placeholder")
    config = load_config(require_api_key=False)
    rec = TraceRecorder()
    sweep(warehouse_path, config, as_of=AS_OF, recorder=rec)

    trace = rec.last_trace()
    assert trace.name == f"monitor.sweep:{AS_OF.isoformat()}"
    assert [c.name for c in trace.children] == [
        "monitor.sla_sweep", "monitor.interventions",
        "monitor.market_imbalance"]
