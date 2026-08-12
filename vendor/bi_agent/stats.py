"""Robust statistical helpers shared by the guardrails and the proactive monitor.

The modified z-score (median + MAD) is the project's outlier statistic of choice:
it stays robust when there are only a handful of values, where the classic
mean/stdev z-score is broken by a single outlier inflating the stdev enough to
mask itself. Iglewicz-Hoaglin's standard threshold for flagging is |z| >= 3.5.

`guardrails.check_result` calls `modified_z_scores` to flag a row that stands out
from its siblings within one query result. `monitor.scan` uses `median_mad`
directly to compare the latest period to its own trailing baseline. Same
statistic, two different axes — one implementation.
"""

from __future__ import annotations

import statistics

# 0.6745 = the inverse of the normal CDF at 0.75, so MAD ~ 0.6745 * stdev under
# a normal distribution. With this constant the modified z-score is comparable
# to a classic z-score.
_MOD_Z_CONSTANT = 0.6745


def median_mad(values: list[float]) -> tuple[float, float]:
    """Median and median-absolute-deviation. MAD = median(|x - median(x)|)."""
    median = statistics.median(values)
    mad = statistics.median([abs(x - median) for x in values])
    return median, mad


def modified_z_scores(values: list[float]) -> list[float]:
    """Modified z-score for each value relative to the rest of the list.

    When MAD = 0 (>= half the values are identical) the modified z-score gives
    no signal, but the series may still have real outliers — falls back to the
    standard stdev-based z-score in that case. If stdev is also 0 (truly
    constant) every value returns 0.
    """
    median, mad = median_mad(values)
    if mad > 0:
        return [_MOD_Z_CONSTANT * (x - median) / mad for x in values]
    return _stdev_z_scores(values, median)


def latest_vs_baseline_z(latest: float, baseline: list[float]) -> tuple[float, float]:
    """Modified z-score of `latest` against the `baseline` series' median + MAD.

    Returns (baseline_median, mod_z). When the baseline's MAD is zero (heavily
    clustered values), falls back to a stdev-based z-score so a latest value
    dramatically different from a near-constant baseline still gets flagged —
    a real failure mode of the bare modified-z when the series is discrete.
    Used by monitor.scan to compare the most recent period to its history.
    """
    median, mad = median_mad(baseline)
    if mad > 0:
        return median, _MOD_Z_CONSTANT * (latest - median) / mad
    stdev = _pstdev_safe(baseline)
    if stdev > 0:
        return median, (latest - median) / stdev
    # Truly constant baseline. Any deviation is by definition an outlier; emit
    # a high marker so the |z| >= threshold check trips.
    if latest != median:
        return median, 10.0 if latest > median else -10.0
    return median, 0.0


def _pstdev_safe(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    return statistics.pstdev(values)


def _stdev_z_scores(values: list[float], center: float) -> list[float]:
    stdev = _pstdev_safe(values)
    if stdev > 0:
        return [(x - center) / stdev for x in values]
    return [0.0] * len(values)
