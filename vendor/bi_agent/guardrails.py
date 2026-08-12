"""Post-execution guardrails — sanity-check query results before they reach the user.

This is the third validation tier. The first two — pre-execution name checks in
tools.py and MetricFlow's own compilation — run before the warehouse; this one
runs on the rows that come back.

`check_result` runs automatically on every query result: empty-result, null,
non-numeric, range, and statistical-outlier checks. `check_freshness` runs
automatically when the result has a `metric_time` grouping. `check_reconciliation`
is run by the planner after the tool-use loop, once a breakdown and a total
query for the same additive metric have both completed (parts-sum-to-whole).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from .catalog import Catalog, MetricInfo
from .stats import median_mad, modified_z_scores

# Plausible value ranges for metrics whose bounds are not simply "non-negative".
# Anything not listed defaults to >= 0 — correct for counts, amounts and
# durations, which is every other metric in the catalog.
_BOUNDS: dict[str, tuple[float | None, float | None]] = {
    # Rate / percentage metrics — defined as derived * 100, so they live on 0-100.
    "churn_rate": (0.0, 100.0),
    "conversion_rate": (0.0, 100.0),
    "assignment_rate": (0.0, 100.0),
    "sla_breach_rate": (0.0, 100.0),
    "overdue_rate": (0.0, 100.0),
    "recovery_rate": (0.0, 100.0),
    "csat_pct": (0.0, 100.0),
    # NPS ranges from -100 to +100.
    "nps_score": (-100.0, 100.0),
    # Commission rate is a 0-1 decimal, not a percentage.
    "avg_commission_rate": (0.0, 1.0),
    # ROI % and net value can legitimately be negative.
    "partner_roi_pct": (-100.0, None),
    "partner_net_monthly_value": (None, None),
}
_DEFAULT_BOUNDS: tuple[float | None, float | None] = (0.0, None)

# Metrics whose grouped parts do NOT sum to a total — rates, percentages,
# averages, ratios, per-unit figures. Everything else is additive.
_NON_ADDITIVE: frozenset[str] = frozenset({
    "churn_rate", "conversion_rate", "assignment_rate", "sla_breach_rate",
    "overdue_rate", "recovery_rate", "csat_pct", "nps_score",
    "avg_commission_rate", "partner_roi_pct",
    "avg_tenure_at_churn", "avg_hours_to_assign", "avg_days_to_convert",
    "avg_days_overdue", "avg_incremental_revenue",
    "dso_days", "partner_payback_months", "partner_cost_per_customer",
})

# Outlier detection uses the modified z-score (median + MAD), not the classic
# mean/stdev z-score: with only a handful of groups (e.g. 6 markets) a single
# outlier inflates the stdev enough to mask itself. Median/MAD is robust to
# that. 3.5 is the standard Iglewicz-Hoaglin threshold.
_OUTLIER_MOD_Z = 3.5
_MIN_ROWS_FOR_OUTLIER = 4  # fewer rows than this and the check is too noisy to trust


@dataclass
class GuardrailReport:
    passed: bool = True
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"passed": self.passed, "warnings": self.warnings, "errors": self.errors}


def _bounds_for(metric: MetricInfo) -> tuple[float | None, float | None]:
    return _BOUNDS.get(metric.name, _DEFAULT_BOUNDS)


def _is_additive(metric: MetricInfo) -> bool:
    return metric.name not in _NON_ADDITIVE


def _to_number(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _row_label(row: dict, group_by: list[str]) -> str:
    """A human-readable label for a row, built from its group-by values."""
    if not group_by:
        return "(overall)"
    return " / ".join(str(row.get(g, "?")) for g in group_by)


def _outlier_warnings(metric_name: str, labelled: list[tuple[str, float]]) -> list[str]:
    """Flag values that are outliers among the rows of a grouped result.

    Within-result form of the z-score check: "does one group stand out from its
    siblings?". The trailing-baseline form — comparing a value to its own
    history over time — lives in `monitor.py`. Both share the same modified
    z-score implementation in `stats.py`.
    """
    if len(labelled) < _MIN_ROWS_FOR_OUTLIER:
        return []
    nums = [v for _, v in labelled]
    median, mad = median_mad(nums)
    if mad == 0:
        return []  # too many identical values to judge outliers robustly
    z_scores = modified_z_scores(nums)
    warnings = []
    for (label, num), mod_z in zip(labelled, z_scores):
        if abs(mod_z) >= _OUTLIER_MOD_Z:
            direction = "above" if mod_z > 0 else "below"
            warnings.append(
                f"Metric '{metric_name}': '{label}' = {num:g} stands out as an "
                f"outlier, well {direction} the typical value ({median:g})."
            )
    return warnings


def check_result(tool_input: dict, rows: list[dict], catalog: Catalog) -> GuardrailReport:
    """Sanity-check a query result.

    Errors mean the result should not be trusted as-is; warnings mean it should
    be surfaced to the user with a caveat. Either way the report is attached to
    the tool result so the planner can react.
    """
    report = GuardrailReport()
    metric_names = tool_input.get("metrics") or []
    group_by = tool_input.get("group_by") or []

    if not rows:
        report.warnings.append(
            "Query returned no rows - there may be no data for these filters."
        )
        return report

    for metric_name in metric_names:
        info = catalog.metrics.get(metric_name)
        if info is None:
            continue  # unknown metrics are caught pre-execution
        if metric_name not in rows[0]:
            report.warnings.append(
                f"Metric '{metric_name}' is missing from the result columns."
            )
            continue

        low, high = _bounds_for(info)
        null_count = non_numeric = out_of_range = 0
        labelled: list[tuple[str, float]] = []
        for row in rows:
            raw = row.get(metric_name)
            num = _to_number(raw)
            if num is None:
                if raw in (None, ""):
                    null_count += 1
                else:
                    non_numeric += 1
                continue
            labelled.append((_row_label(row, group_by), num))
            if (low is not None and num < low) or (high is not None and num > high):
                out_of_range += 1

        total = len(rows)
        if non_numeric:
            report.errors.append(
                f"Metric '{metric_name}' has {non_numeric} non-numeric value(s)."
            )
        if null_count == total:
            report.warnings.append(f"Metric '{metric_name}' is null in every row.")
        elif null_count:
            report.warnings.append(
                f"Metric '{metric_name}' is null in {null_count} of {total} rows."
            )
        if out_of_range:
            rng = f"[{'-inf' if low is None else low}, {'inf' if high is None else high}]"
            report.errors.append(
                f"Metric '{metric_name}' has {out_of_range} value(s) outside the "
                f"plausible range {rng}."
            )

        # Outliers are noteworthy, not wrong — warnings only.
        for warning in _outlier_warnings(metric_name, labelled):
            report.warnings.append(warning)

    report.passed = not report.errors
    return report


def _parse_date(raw) -> date | None:
    if raw is None or raw == "":
        return None
    s = str(raw)
    try:
        # datetime.fromisoformat handles both "YYYY-MM-DD" and "YYYY-MM-DDTHH:MM:SS".
        return datetime.fromisoformat(s).date()
    except ValueError:
        return None


def check_freshness(
    rows: list[dict],
    time_column: str,
    max_age_days: int = 45,
    as_of: date | None = None,
) -> GuardrailReport:
    """Flag a result whose most-recent period is older than max_age_days.

    Pure function over rows — the caller decides when to invoke it. Typically
    `tools.execute_query_tool` invokes this automatically when the query is
    grouped by `metric_time__<grain>`, so any "the data is old" caveat surfaces
    to the planner alongside the value checks. With the NordLedger simulation
    `as_of` should be pinned to the data's end-date (2024-12-31), set via the
    AS_OF_DATE env var; against a live pipeline `as_of` defaults to today.
    """
    report = GuardrailReport()
    as_of = as_of or date.today()

    parse_errors = 0
    max_date: date | None = None
    found_any = False
    for row in rows:
        raw = row.get(time_column)
        if raw is None or raw == "":
            continue
        parsed = _parse_date(raw)
        if parsed is None:
            parse_errors += 1
            continue
        found_any = True
        if max_date is None or parsed > max_date:
            max_date = parsed

    if parse_errors:
        report.warnings.append(
            f"Could not parse {parse_errors} value(s) in '{time_column}' as dates - "
            f"freshness check is partial."
        )
    if not found_any or max_date is None:
        report.warnings.append(
            f"Cannot judge freshness - no parseable dates found in '{time_column}'."
        )
        return report

    age = (as_of - max_date).days
    if age > max_age_days:
        report.warnings.append(
            f"Data may be stale - latest '{time_column}' is {max_date.isoformat()}, "
            f"{age} days before as-of {as_of.isoformat()} (max age {max_age_days})."
        )
    return report


def check_reconciliation(
    breakdown_rows: list[dict],
    total_rows: list[dict],
    metric_name: str,
    catalog: Catalog,
    tolerance: float = 0.01,
) -> GuardrailReport:
    """Check that a grouped breakdown of an additive metric sums to its total.

    Standalone: the planner runs the breakdown and the total as separate
    queries, so this is called once both results are in. For non-additive
    metrics (rates, averages) it returns a warning rather than reconciling -
    their parts are not expected to sum.
    """
    report = GuardrailReport()
    info = catalog.metrics.get(metric_name)
    if info is None:
        report.warnings.append(f"Unknown metric '{metric_name}' - cannot reconcile.")
        return report
    if not _is_additive(info):
        report.warnings.append(
            f"Metric '{metric_name}' is a rate or average - its breakdown is not "
            f"expected to sum to a total, so reconciliation is skipped."
        )
        return report

    breakdown_sum = sum(
        (_to_number(r.get(metric_name)) or 0.0) for r in breakdown_rows
    )
    total = next(
        (t for t in (_to_number(r.get(metric_name)) for r in total_rows) if t is not None),
        None,
    )
    if total is None:
        report.warnings.append(
            f"No total value found for '{metric_name}' - cannot reconcile."
        )
        return report

    if abs(total) < tolerance:
        reconciled = abs(breakdown_sum) < tolerance
    else:
        reconciled = abs(breakdown_sum - total) / abs(total) <= tolerance
    if not reconciled:
        report.errors.append(
            f"Metric '{metric_name}': the breakdown sums to {breakdown_sum:g} but "
            f"the reported total is {total:g} - the parts do not reconcile."
        )

    report.passed = not report.errors
    return report
