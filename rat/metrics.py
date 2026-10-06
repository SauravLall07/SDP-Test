"""Pure metric algorithms behind the analytics endpoints.

Design notes
------------

* SQL owns *set-level* work (filtering, grouping, summation) so that every
  aggregation runs inside SQLite, next to the data and assisted by the
  schema indexes. Python owns *distribution-level* work (rankings,
  inequality, regression, association), which SQL cannot express cleanly
  and which stays easy to unit test in isolation.
* Every function is deterministic, dependency free, and documented with its
  asymptotic complexity. The heaviest functions sort once (O(n log n));
  everything else is linear or constant.
* Inputs are non-negative magnitudes (line counts, commit counts). Empty
  inputs return neutral values instead of raising, because the UI renders
  explicit "no data" states.
"""

from __future__ import annotations

from collections.abc import Sequence


def gini(values: Sequence[float]) -> float:
    """Gini coefficient of a non-negative distribution (0 = even, towards 1 = concentrated).

    Computed with the rank formula ``G = 2·Σ i·x_i / (n·Σ x_i) − (n + 1) / n``
    over ascending-sorted values: one sort dominates, O(n log n).
    """
    numbers = sorted(float(value) for value in values)
    total = sum(numbers)
    count = len(numbers)
    if count == 0 or total <= 0:
        return 0.0
    weighted = sum(rank * value for rank, value in enumerate(numbers, start=1))
    return max(0.0, (2 * weighted) / (count * total) - (count + 1) / count)


def hhi(values: Sequence[float]) -> float:
    """Herfindahl-Hirschman index over shares: 1/n when even, 1 for a single actor.

    A single linear pass (O(n)); shares are recomputed from the total, so
    callers do not have to normalize first.
    """
    total = sum(values)
    if total <= 0:
        return 0.0
    return sum((value / total) ** 2 for value in values)


def bus_factor(values: Sequence[float], threshold: float = 0.5) -> int:
    """Number of top-ranked values needed to cover ``threshold`` of the total.

    One descending sort (O(n log n)) followed by an O(n) prefix scan.
    Returns 0 for an empty or all-zero distribution.
    """
    total = sum(values)
    if total <= 0:
        return 0
    running = 0.0
    for count, value in enumerate(sorted(values, reverse=True), start=1):
        running += value
        if running >= threshold * total:
            return count
    return len(values)


def coverage(values: Sequence[float], threshold: float = 0.8) -> dict[str, object]:
    """Pareto headline: how few top items carry ``threshold`` of the mass.

    Returns the item count, the total item count, and the share of items
    involved (3 of 24 items -> share 0.125). One sort, O(n log n).
    """
    total = sum(values)
    count = len(values)
    if count == 0 or total <= 0:
        return {"items": 0, "total_items": count, "share": 0.0}
    running = 0.0
    for rank, value in enumerate(sorted(values, reverse=True), start=1):
        running += value
        if running >= threshold * total:
            return {"items": rank, "total_items": count, "share": rank / count}
    return {"items": count, "total_items": count, "share": 1.0}


def pareto_curve(values: Sequence[float], points: int = 64) -> list[list[float]]:
    """Sampled cumulative-share curve over values ranked descending.

    The x axis is the share of items consumed, the y axis the share of the
    total they contribute; knots are downsampled to at most ``points`` so
    the payload stays small. O(n log n) for the sort, O(points) output.
    """
    total = sum(values)
    count = len(values)
    if count == 0 or total <= 0:
        return [[0.0, 0.0], [1.0, 0.0]]
    ordered = sorted(values, reverse=True)
    stride = max(1, count // points)
    curve = [[0.0, 0.0]]
    running = 0.0
    for rank, value in enumerate(ordered, start=1):
        running += value
        if rank % stride == 0 or rank == count:
            curve.append([round(rank / count, 4), round(running / total, 4)])
    return curve


def linear_trend(values: Sequence[float]) -> dict[str, float]:
    """Least-squares fit over evenly spaced buckets (0, 1, 2, ...).

    Running sums only: O(n) time, O(1) extra memory, no intermediate
    arrays. ``slope`` is expressed per bucket; ``r2`` describes how much of
    the variance the trend explains (0 when the series is flat).
    """
    count = len(values)
    if count == 0:
        return {"slope": 0.0, "intercept": 0.0, "r2": 0.0, "n": 0}
    mean_x = (count - 1) / 2
    mean_y = sum(values) / count
    variance = sum((index - mean_x) ** 2 for index in range(count))
    covariance = sum((index - mean_x) * (value - mean_y) for index, value in enumerate(values))
    slope = covariance / variance if variance else 0.0
    intercept = mean_y - slope * mean_x
    residual = sum((value - (slope * index + intercept)) ** 2 for index, value in enumerate(values))
    spread = sum((value - mean_y) ** 2 for value in values)
    r2 = 1 - residual / spread if spread > 0 else 0.0
    return {"slope": slope, "intercept": intercept, "r2": r2, "n": count}


def association_metrics(together: int, commits_a: int, commits_b: int, total_commits: int) -> dict[str, float]:
    """Co-change association of two files, association-rule style. O(1).

    ``confidence`` is P(b | a): the share of a's commits that also touch b.
    ``lift`` normalizes confidence by P(b): 1.0 means independent files,
    above 1.0 means the pair co-changes more often than chance. ``support``
    is P(a and b) across all commits.
    """
    if total_commits <= 0 or commits_a <= 0 or commits_b <= 0 or together <= 0:
        return {"together": together, "confidence": 0.0, "lift": 0.0, "support": 0.0}
    return {
        "together": together,
        "confidence": together / commits_a,
        "lift": (together * total_commits) / (commits_a * commits_b),
        "support": together / total_commits,
    }


def concentration_report(values: Sequence[float]) -> dict[str, object]:
    """One-stop concentration summary for a distribution (authors, files, ...).

    Combines the sorted rankings (Gini, bus factor, 50/80% coverage) with
    the linear indices (HHI, top share) behind a single dict.
    """
    total = sum(values)
    top = max(values) if values else 0.0
    return {
        "items": len(values),
        "total": total,
        "gini": gini(values),
        "hhi": hhi(values),
        "bus_factor": bus_factor(values, 0.5),
        "top_share": top / total if total > 0 else 0.0,
        "coverage_50": coverage(values, 0.5),
        "coverage_80": coverage(values, 0.8),
    }


def hotspot_table(files: list[dict], limit: int = 12) -> list[dict]:
    """Rank files by a volatility risk score = churn/commit x frequency.

    Both factors are already normalized by the commit count, so the score
    is comparable across scopes. One sort, O(n log n).
    """
    scored = [
        {**item, "score": item["churn_rate"] * item["modification_frequency"]}
        for item in files
    ]
    scored.sort(key=lambda item: (item["score"], item["churn"]), reverse=True)
    return scored[:limit]
