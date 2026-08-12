"""The `query_semantic_layer` tool: schema, compilation, and execution.

This is the constrained-generation boundary. The LLM emits a structured query
(metrics + dimensions + structured filters) — it never writes SQL. Unknown
metrics are rejected before anything touches the warehouse; the error is fed
back so the planner can re-plan.
"""

from __future__ import annotations

from datetime import date

from .catalog import Catalog
from .guardrails import check_freshness, check_result
from .semantic_layer import MetricFlowClient, MetricFlowError, QuerySpec

_FILTER_OPERATORS = ["=", "!=", ">", ">=", "<", "<=", "in"]
_TIME_GRAINS = ["day", "week", "month", "quarter", "year"]

QUERY_TOOL = {
    "name": "query_semantic_layer",
    "description": (
        "Run a structured query against the Ageras KPI semantic layer. "
        "You select metrics and dimensions from the catalog — you never write "
        "SQL. The semantic layer compiles and executes the query. Returns the "
        "compiled SQL and the result rows."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "metrics": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Metric names from the catalog. At least one.",
            },
            "group_by": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Dimensions to break the result down by, named "
                    "'<entity>__<dimension>', e.g. 'subscription__country' or "
                    "'customer__segment'. Use 'metric_time' for time."
                ),
            },
            "filters": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "field": {
                            "type": "string",
                            "description": "Dimension name, or 'metric_time' for a time filter.",
                        },
                        "operator": {"type": "string", "enum": _FILTER_OPERATORS},
                        "value": {
                            "description": "Scalar value, or a list when operator is 'in'.",
                        },
                        "grain": {
                            "type": "string",
                            "enum": _TIME_GRAINS,
                            "description": "Required for time filters (field = a time dimension).",
                        },
                    },
                    "required": ["field", "operator", "value"],
                },
            },
            "order_by": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Metrics or dimensions to sort by. Prefix with '-' for descending.",
            },
            "limit": {"type": "integer", "description": "Max rows to return."},
        },
        "required": ["metrics"],
    },
}


def _quote(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "\\'") + "'"


def _is_time_field(field: str, catalog: Catalog) -> bool:
    if field == "metric_time" or field.startswith("metric_time__"):
        return True
    if field in catalog.dimensions:
        return catalog.dimensions[field].type == "time"
    return field.split("__")[-1] in catalog.time_dimension_raw_names


def compile_filter(flt: dict, catalog: Catalog) -> str:
    """Compile a structured filter dict to a MetricFlow Jinja filter string.

    Exposed for `monitor.py`, which builds time-window filters directly without
    going through the LLM. Reuses the same TimeDimension/Dimension logic the
    planner's tool calls go through.
    """
    field = flt["field"]
    operator = flt["operator"]
    value = flt["value"]

    if _is_time_field(field, catalog):
        grain = flt.get("grain", "day")
        ref = f"TimeDimension('{field}', '{grain}')"
    else:
        ref = f"Dimension('{field}')"

    jinja = "{{ " + ref + " }}"
    if operator == "in":
        if not isinstance(value, list):
            raise ValueError("operator 'in' requires a list value")
        return f"{jinja} IN ({', '.join(_quote(v) for v in value)})"
    return f"{jinja} {operator} {_quote(value)}"


def compile_query(tool_input: dict, catalog: Catalog) -> tuple[QuerySpec | None, str | None]:
    """Validate tool input and compile it to a QuerySpec.

    Returns (spec, None) on success or (None, error_message) on a validation
    failure — the error is meant to be handed back to the planner.
    """
    metrics = tool_input.get("metrics") or []
    if not metrics:
        return None, "No metrics provided. Pick at least one metric from the catalog."

    unknown = [m for m in metrics if not catalog.is_known_metric(m)]
    if unknown:
        return None, (
            f"Unknown metric(s): {', '.join(unknown)}. "
            f"Use only metrics from the catalog."
        )

    group_by = tool_input.get("group_by") or []
    bad_group_by = [g for g in group_by if not catalog.is_known_group_by(g)]
    if bad_group_by:
        return None, (
            f"Unrecognised group_by field(s): {', '.join(bad_group_by)}. "
            f"Use dimensions from the catalog or 'metric_time'."
        )

    where_parts = []
    for flt in tool_input.get("filters") or []:
        try:
            where_parts.append(compile_filter(flt, catalog))
        except (KeyError, ValueError) as exc:
            return None, f"Invalid filter {flt!r}: {exc}"

    spec = QuerySpec(
        metrics=metrics,
        group_by=group_by,
        where=" AND ".join(where_parts) if where_parts else None,
        order_by=tool_input.get("order_by") or [],
        limit=tool_input.get("limit"),
    )
    return spec, None


def _metric_time_column(group_by: list[str]) -> str | None:
    """Return the first metric_time grouping in `group_by`, if any."""
    for g in group_by:
        if g == "metric_time" or g.startswith("metric_time__"):
            return g
    return None


def execute_query_tool(
    tool_input: dict,
    catalog: Catalog,
    mf: MetricFlowClient,
    as_of: date | None = None,
) -> dict:
    """Validate, compile, explain, and run a query. Always returns a dict —
    never raises — so the result (success or error) can go back to the planner.

    `as_of` flows in from Config.as_of_date for the freshness check; defaults
    to "today" when omitted.
    """
    spec, error = compile_query(tool_input, catalog)
    if error:
        return {"ok": False, "error": error, "tool_input": tool_input}

    result: dict = {"ok": True, "tool_input": tool_input, "query_spec": spec.to_mf_args()}
    try:
        result["compiled_sql"] = mf.explain(spec)
    except MetricFlowError as exc:
        return {
            "ok": False,
            "error": f"MetricFlow rejected the query: {exc}",
            "tool_input": tool_input,
        }

    try:
        rows = mf.run(spec)
    except MetricFlowError as exc:
        return {
            "ok": False,
            "error": f"Query failed at execution: {exc}",
            "tool_input": tool_input,
            "compiled_sql": result["compiled_sql"],
        }

    result["rows"] = rows
    result["row_count"] = len(rows)

    report = check_result(tool_input, rows, catalog)
    time_col = _metric_time_column(tool_input.get("group_by") or [])
    if time_col:
        fresh = check_freshness(rows, time_col, as_of=as_of)
        report.warnings.extend(fresh.warnings)
        report.errors.extend(fresh.errors)
        if not fresh.passed:
            report.passed = False
    result["guardrails"] = report.as_dict()
    return result
