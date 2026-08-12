"""The LangGraph — nodes, edges and the four HITL interrupt destinations.

Design rules (§6 + Week 4 decisions):

- Every worker node (intake_gate, match, negotiate) is side-effect-bearing;
  every HITL node contains NOTHING before its `interrupt()` call. LangGraph
  re-runs a node from the top on resume — putting the interrupt in its own
  side-effect-free node means resuming never re-sends a DM or re-books.
- Routing is by `state.lead.status` — cold, deterministic predicates.
- The checkpointer (SqliteSaver) persists GraphState between interrupt and
  resume; `thread_id` = referral_lead_id, so one lead = one thread.

Graph shape:

    START → intake_gate ──ok──▶ match ──matched──▶ negotiate ──booked──▶ END
                │ low conf /            │ no capacity      │ exhausted
                ▼                       ▼                  ▼
            hitl_intake             hitl_capacity      hitl_negotiate
             │ enrich → match        │ reactivate → match │ override → negotiate
             │ drop   → END          │ drop → END          │ drop → END
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Optional

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel

from .agents.matching import match_mcp
from .agents.negotiation import Drafter, negotiate
from .config import Config
from .hitl import (apply_enrichment, build_interrupt_payload, mark_lost,
                   validate_decision)
from .mcp_stack import MCPStack
from .state import LeadState


class GraphState(BaseModel):
    lead: LeadState
    override_partner_id: Optional[str] = None
    exclude_partner_ids: list[str] = []


def build_graph(stack: MCPStack, config: Config, *,
                as_of: date, drafter: Drafter | None = None,
                recorder=None):
    """Wire the state machine. `stack`, `config`, `as_of`, `drafter` and the
    optional TraceRecorder live in node closures — only GraphState is
    checkpointed. With a recorder, every node run becomes a span (and the
    MCP calls made inside it become child spans via the stack's recorder)."""

    # ── worker nodes ────────────────────────────────────────────────────
    def intake_gate(state: GraphState) -> dict:
        """Structural gate only. The *confidence* judgment (Sonnet→Opus
        escalation, ambiguity threshold) belongs to the Intake Agent
        (classify_lead), which sets status='hitl_intake' itself. This gate
        enforces the invariant Matching cannot run without — country +
        service_type — and honors an upstream HITL flag."""
        lead = state.lead
        missing = lead.missing_essential_fields()
        if lead.status == "hitl_intake" or missing:
            reason = lead.hitl_reason if lead.status == "hitl_intake" else (
                f"missing essentials: {', '.join(missing)}")
            return {"lead": lead.model_copy(update={
                "status": "hitl_intake", "hitl_reason": reason})}
        return {"lead": lead.model_copy(update={"status": "classified"})}

    def match_node(state: GraphState) -> dict:
        lead = match_mcp(state.lead.model_copy(update={"status": "classified"}),
                         stack=stack, top_k=config.matching_top_k, as_of=as_of,
                         exclude_partner_ids=state.exclude_partner_ids)
        return {"lead": lead}

    def negotiate_node(state: GraphState) -> dict:
        lead = negotiate(state.lead, stack=stack, config=config, as_of=as_of,
                         drafter=drafter,
                         override_partner_id=state.override_partner_id)
        # the override is single-use — clear it after the attempt
        return {"lead": lead, "override_partner_id": None}

    # ── HITL nodes: interrupt FIRST, side-effects only after resume ────
    def hitl_intake(state: GraphState) -> Command:
        decision = interrupt(build_interrupt_payload("hitl_intake", state.lead))
        validate_decision("hitl_intake", decision)
        if decision["action"] == "drop":
            return Command(goto=END,
                           update={"lead": mark_lost(state.lead, decision.get("note"))})
        enriched = apply_enrichment(state.lead, decision["fields"])
        return Command(goto="match", update={"lead": enriched})

    def hitl_capacity(state: GraphState) -> Command:
        dormant = stack.call("partner_capacity", "list_dormant_partners",
                             country=state.lead.country,
                             service_type=state.lead.service_type,
                             industry=state.lead.industry)
        decision = interrupt(build_interrupt_payload(
            "hitl_capacity", state.lead,
            dormant_partners=dormant.get("dormant", [])))
        validate_decision("hitl_capacity", decision)
        if decision["action"] == "drop":
            return Command(goto=END,
                           update={"lead": mark_lost(state.lead, decision.get("note"))})
        stack.call("partner_capacity", "set_partner_status",
                   partner_id=decision["partner_id"], status="active",
                   reason=(decision.get("note")
                           or f"HITL reactivation for {state.lead.referral_lead_id}"),
                   changed_by="hitl")
        return Command(goto="match", update={})

    def hitl_negotiate(state: GraphState) -> Command:
        decision = interrupt(build_interrupt_payload("hitl_negotiate", state.lead))
        validate_decision("hitl_negotiate", decision)
        if decision["action"] == "drop":
            return Command(goto=END,
                           update={"lead": mark_lost(state.lead, decision.get("note"))})
        return Command(goto="negotiate",
                       update={"override_partner_id": decision["partner_id"]})

    # NOTE on hitl_capacity: list_dormant_partners runs BEFORE the interrupt
    # and therefore re-runs on resume. It is a pure read (no side-effect), so
    # re-execution is safe — and it means the manager always decides against
    # a fresh dormant-list.

    # ── routing predicates ──────────────────────────────────────────────
    def route_after_intake(state: GraphState) -> str:
        return "hitl_intake" if state.lead.status == "hitl_intake" else "match"

    def route_after_match(state: GraphState) -> str:
        return {"matched": "negotiate",
                "hitl_capacity": "hitl_capacity",
                "hitl_intake": "hitl_intake"}[state.lead.status]

    def route_after_negotiate(state: GraphState) -> str:
        return END if state.lead.status == "booked" else "hitl_negotiate"

    # ── wiring (nodes wrapped as observability spans) ───────────────────
    def _traced(name, fn):
        if recorder is None:
            return fn
        def wrapped(state):
            with recorder.span(name):
                return fn(state)
        return wrapped

    builder = StateGraph(GraphState)
    builder.add_node("intake_gate", _traced("intake_gate", intake_gate))
    builder.add_node("match", _traced("match", match_node))
    builder.add_node("negotiate", _traced("negotiate", negotiate_node))
    builder.add_node("hitl_intake", _traced("hitl_intake", hitl_intake))
    builder.add_node("hitl_capacity", _traced("hitl_capacity", hitl_capacity))
    builder.add_node("hitl_negotiate", _traced("hitl_negotiate", hitl_negotiate))

    builder.add_edge(START, "intake_gate")
    builder.add_conditional_edges("intake_gate", route_after_intake,
                                  ["hitl_intake", "match"])
    builder.add_conditional_edges("match", route_after_match,
                                  ["negotiate", "hitl_capacity", "hitl_intake"])
    builder.add_conditional_edges("negotiate", route_after_negotiate,
                                  ["hitl_negotiate", END])
    # HITL nodes route via Command(goto=...) — no static edges needed.
    return builder


# ── runner helpers (the CLI + tests use these) ──────────────────────────────

DEFAULT_CHECKPOINT_DIR = Path(".langgraph_checkpoints")


@contextmanager
def open_graph(stack: MCPStack, config: Config, *, as_of: date,
               drafter: Drafter | None = None,
               checkpoint_dir: Path = DEFAULT_CHECKPOINT_DIR,
               recorder=None):
    """Compile the graph with a SqliteSaver checkpointer on disk."""
    checkpoint_dir.mkdir(exist_ok=True)
    db = checkpoint_dir / "leads.sqlite"
    with SqliteSaver.from_conn_string(str(db)) as saver:
        yield build_graph(stack, config, as_of=as_of, drafter=drafter,
                          recorder=recorder).compile(checkpointer=saver)


def _thread(lead_id: str) -> dict:
    return {"configurable": {"thread_id": lead_id}}


def lead_out(result: dict) -> LeadState:
    """Normalize invoke()'s ['lead'] value to a LeadState. Depending on the
    checkpoint serde path it comes back as either a dict (fresh run) or a
    reconstructed LeadState (resume) — callers shouldn't care."""
    lead = result["lead"]
    return lead if isinstance(lead, LeadState) else LeadState.model_validate(lead)


def extract_interrupt(result: dict) -> dict | None:
    """The pending interrupt payload from an invoke() result, if any."""
    interrupts = result.get("__interrupt__") or []
    if not interrupts:
        return None
    first = interrupts[0]
    return getattr(first, "value", first)


def run_lead(graph, lead: LeadState, *, recorder=None,
             exclude_partner_ids: list[str] | None = None) -> dict:
    """Start (or restart) a lead through the graph. Returns the raw invoke
    result — check `extract_interrupt()` for a pause, else `lead_out()`.
    With a recorder, the whole run is wrapped in a `lead_run:<id>` trace."""
    state = GraphState(lead=lead,
                       exclude_partner_ids=exclude_partner_ids or [])
    cfg = _thread(lead.referral_lead_id)
    if recorder is None:
        return graph.invoke(state.model_dump(), config=cfg)
    with recorder.trace(f"lead_run:{lead.referral_lead_id}",
                        lead_id=lead.referral_lead_id):
        return graph.invoke(state.model_dump(), config=cfg)


def resume_lead(graph, lead_id: str, decision: dict) -> dict:
    """Resume a paused lead with the manager's decision."""
    return graph.invoke(Command(resume=decision), config=_thread(lead_id))


def get_paused(graph, lead_id: str) -> dict | None:
    """The pending interrupt payload for a lead, read from the checkpointer —
    None if the lead isn't paused."""
    snapshot = graph.get_state(_thread(lead_id))
    if snapshot is None:
        return None
    for task in getattr(snapshot, "tasks", ()):
        for intr in getattr(task, "interrupts", ()):
            return getattr(intr, "value", intr)
    return None
