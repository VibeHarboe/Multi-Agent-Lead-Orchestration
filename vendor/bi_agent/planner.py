"""The Query Planning Agent — a Claude tool-use loop over the semantic layer."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import anthropic

from .catalog import Catalog
from .config import Config
from .guardrails import check_reconciliation
from .semantic_layer import MetricFlowClient
from .tools import QUERY_TOOL, execute_query_tool
from .visualization import extract_visualization

_SYSTEM_HEADER = """You are the Query Planning Agent for a self-querying BI system built on the \
Ageras KPI semantic layer (six markets: DK, NO, SE, DE, NL, US).

Your job: translate a business question into one or more structured queries \
against the semantic layer using the `query_semantic_layer` tool, then explain \
the answer in plain language.

RULES
- You may ONLY use metric and dimension names from the CATALOG below. Never invent names.
- For "why did X change" questions, plan MULTIPLE queries: the value in question, \
a baseline/comparison period, and a breakdown by a relevant sub-dimension.
- If a question is ambiguous (e.g. an unclear time range), ask ONE clarifying \
question instead of guessing.
- Every metric carries a definition, a target, and a definition_version. Your \
final answer MUST cite the definition(s) you used, including any v1/v2 limitation \
noted in the description — answer honestly about precision.
- Country values are ISO-2 codes: DK, NO, SE, DE, NL, US.
- For time filters use field "metric_time" with a grain (month/quarter/year).
- Each tool result includes a `guardrails` report. If it has warnings or errors, \
surface them honestly in your answer — never present a flagged number as clean.

When you have enough data, stop calling tools and write the final answer:
1. A direct, narrative answer to the question.
2. The numbers that support it.
3. A "Definitions used" section citing each metric (name, what it means, target, version).
4. 1-3 suggested drill-down questions.
5. A visualization spec — end with a fenced ```viz block containing JSON:
   {"chart_type": "bar|line|table|none", "title": "...", "x": "<dimension column>",
    "y": "<metric name>", "series": "<optional dimension column>"}
   Use "line" for a time series, "bar" for comparing categories, "table" for a
   detailed breakdown, "none" for a single-number answer. x and y are column
   names from the query result. Never write plotting code — only the spec.
"""


@dataclass
class AgentResult:
    question: str
    narrative: str
    visualization: dict | None = None
    queries: list[dict] = field(default_factory=list)
    turns: int = 0
    stopped_early: bool = False
    # Post-pass reconciliation between any breakdown + ungrouped-total queries
    # the planner happened to issue (additive metrics only). One entry per
    # detected pair; non-additive metrics surface as a warning.
    reconciliation: list[dict] = field(default_factory=list)


def find_reconcilable_pairs(
    executed: list[dict],
) -> list[tuple[dict, dict, str]]:
    """Detect (breakdown_query, total_query, metric) triples from a run.

    A breakdown is any successful query with a non-empty `group_by`; a total is
    a successful query sharing the metric but with no `group_by`. Returns every
    such pair so check_reconciliation can run on each. Filtering out
    non-additive metrics is left to check_reconciliation, which downgrades them
    to a warning rather than erroring. Pure function — unit-testable without
    touching the LLM or the warehouse.
    """
    ok = [q for q in executed if q.get("ok")]
    by_metric: dict[str, dict[str, list[dict]]] = {}
    for q in ok:
        tool_input = q.get("tool_input") or {}
        bucket = "breakdown" if tool_input.get("group_by") else "total"
        for metric in (tool_input.get("metrics") or []):
            by_metric.setdefault(metric, {"breakdown": [], "total": []})[bucket].append(q)
    triples: list[tuple[dict, dict, str]] = []
    for metric, queries in by_metric.items():
        for breakdown in queries["breakdown"]:
            for total in queries["total"]:
                triples.append((breakdown, total, metric))
    return triples


def _reconcile_executed(executed: list[dict], catalog: Catalog) -> list[dict]:
    """Run check_reconciliation for every breakdown/total pair in `executed`."""
    out: list[dict] = []
    for breakdown, total, metric in find_reconcilable_pairs(executed):
        report = check_reconciliation(
            breakdown.get("rows") or [],
            total.get("rows") or [],
            metric,
            catalog,
        )
        out.append({
            "metric": metric,
            "breakdown_group_by": (breakdown.get("tool_input") or {}).get("group_by") or [],
            "passed": report.passed,
            "warnings": report.warnings,
            "errors": report.errors,
        })
    return out


def build_system_prompt(catalog: Catalog, metric_subset: list[str] | None = None) -> str:
    lines = [_SYSTEM_HEADER, "\n=== CATALOG: METRICS ===\n"]

    if metric_subset is not None:
        lines.append(
            "(Showing the metrics most relevant to the question - not the full "
            "catalog. If you need a metric that is not listed, say so.)\n"
        )
        metrics = [catalog.metrics[n] for n in metric_subset if n in catalog.metrics]
    else:
        metrics = list(catalog.metrics.values())

    by_level: dict[str, list] = {}
    for m in metrics:
        by_level.setdefault(str(m.meta.get("level", "other")), []).append(m)

    for level in sorted(by_level):
        lines.append(f"\n## level: {level}")
        for m in sorted(by_level[level], key=lambda x: x.name):
            target = m.meta.get("target")
            version = m.meta.get("definition_version", "")
            tags = " ".join(
                t for t in [
                    f"[type={m.type}]",
                    f"[target={target}]" if target else "",
                    f"[version={version}]" if version else "",
                ] if t
            )
            lines.append(f"- {m.name} — {m.label} {tags}")
            lines.append(f"    {m.description}")

    lines.append("\n=== CATALOG: DIMENSIONS (group_by / filter fields) ===\n")
    lines.append(
        "Dimension names are '<entity>__<dimension>'. A model's own dimensions "
        "use its primary entity. Dimensions reached through a join chain the "
        "entity names, e.g. 'customer__partner__partner_name'.\n"
    )
    for sm in sorted(catalog.semantic_models.values(), key=lambda x: x.name):
        dims = [catalog.dimensions[d] for d in sm.dimension_names]
        dim_strs = [f"{d.qualified_name} ({d.type})" for d in dims]
        entity_str = f"  entities: {', '.join(sm.entities)}" if sm.entities else ""
        lines.append(f"\n## {sm.name}{entity_str}")
        for s in dim_strs:
            lines.append(f"- {s}")

    lines.append(
        "\nNOTE: dimensions from one semantic model can be used with metrics "
        "from another when the models share an entity — e.g. group nps_score "
        "(from nps_surveys) by customer__country, because nps_surveys joins to "
        "customers via the `customer` entity."
    )
    return "\n".join(lines)


def _text_blocks(content) -> str:
    return "\n".join(b.text for b in content if getattr(b, "type", None) == "text").strip()


def answer_question(
    question: str,
    config: Config,
    catalog: Catalog,
    mf: MetricFlowClient,
) -> AgentResult:
    client = anthropic.Anthropic(api_key=config.anthropic_api_key)

    # With retrieval off (default), the whole catalog is static — cache it so
    # repeated questions only pay for it once. With retrieval on, the prompt
    # varies per question, so caching the catalog block would not help.
    if config.retrieval_k > 0:
        from .retrieval import MetricRetriever

        subset = MetricRetriever(catalog, config.chroma_dir).retrieve(
            question, config.retrieval_k
        )
        system_block = {"type": "text", "text": build_system_prompt(catalog, subset)}
    else:
        system_block = {
            "type": "text",
            "text": build_system_prompt(catalog),
            "cache_control": {"type": "ephemeral"},
        }
    system = [system_block]
    messages: list[dict] = [{"role": "user", "content": question}]
    executed: list[dict] = []

    for turn in range(1, config.max_agent_turns + 1):
        response = client.messages.create(
            model=config.planner_model,
            max_tokens=2048,
            system=system,
            tools=[QUERY_TOOL],
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            text = _text_blocks(response.content) or "(no answer produced)"
            narrative, visualization = extract_visualization(text)
            return AgentResult(
                question=question,
                narrative=narrative,
                visualization=visualization,
                queries=executed,
                turns=turn,
                reconciliation=_reconcile_executed(executed, catalog),
            )

        tool_results = []
        for block in response.content:
            if getattr(block, "type", None) != "tool_use":
                continue
            result = execute_query_tool(block.input, catalog, mf, as_of=config.as_of_date)
            executed.append(result)
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result, default=str),
                    "is_error": not result.get("ok", False),
                }
            )
        messages.append({"role": "user", "content": tool_results})

    return AgentResult(
        question=question,
        narrative="(stopped — reached MAX_AGENT_TURNS without a final answer)",
        queries=executed,
        turns=config.max_agent_turns,
        stopped_early=True,
        reconciliation=_reconcile_executed(executed, catalog),
    )
