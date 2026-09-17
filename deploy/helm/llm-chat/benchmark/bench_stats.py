# Copyright © Advanced Micro Devices, Inc., or its affiliates.
#
# SPDX-License-Identifier: MIT
"""Exact descriptive statistics for benchmark samples (stdlib only).

Percentiles use linear interpolation between closest ranks (Hyndman & Fan
type 7) -- the same definition as numpy.percentile's default and as
`vllm bench serve` -- so numbers are directly comparable. Verified against
numpy in test_bench_stats.py.
"""

import math
import random
from typing import Dict, List, Optional, Sequence

PERCENTILES = (50, 90, 95, 99)


def percentile(sorted_xs: Sequence[float], q: float) -> float:
    """q in [0, 100]; sorted_xs must be non-empty and ascending."""
    n = len(sorted_xs)
    if n == 1:
        return float(sorted_xs[0])
    h = (n - 1) * q / 100.0
    lo = math.floor(h)
    hi = min(lo + 1, n - 1)
    return sorted_xs[lo] + (h - lo) * (sorted_xs[hi] - sorted_xs[lo])


def min_samples_for(q: float) -> int:
    """Smallest n for which the q-th percentile is not just the sample max.

    With fewer than 1/(1-q) samples there is no observation above the
    percentile at all, so e.g. a "p99" from 20 samples is really the max.
    """
    return math.ceil(round(100.0 / (100.0 - q), 9))


def _bootstrap_ci(xs: List[float], stat, n_resamples: int, rng: random.Random):
    n = len(xs)
    estimates = []
    for _ in range(n_resamples):
        sample = sorted(xs[rng.randrange(n)] for _ in range(n))
        estimates.append(stat(sample))
    estimates.sort()
    return percentile(estimates, 2.5), percentile(estimates, 97.5)


def describe(
    values: Sequence[Optional[float]],
    bootstrap: bool = False,
    n_resamples: int = 1000,
    seed: int = 0,
) -> Dict[str, object]:
    xs = sorted(float(v) for v in values if v is not None and not math.isnan(v))
    n = len(xs)
    if n == 0:
        return {"n": 0}
    mean = sum(xs) / n
    std = math.sqrt(sum((x - mean) ** 2 for x in xs) / (n - 1)) if n > 1 else 0.0
    out: Dict[str, object] = {"n": n, "mean": mean, "std": std, "min": xs[0], "max": xs[-1]}
    for q in PERCENTILES:
        out[f"p{q}"] = percentile(xs, q)
        out[f"p{q}_reliable"] = n >= min_samples_for(q)
    # Bootstrap is O(n_resamples * n log n) in pure Python -- skipped for large
    # n, where the CI is narrow anyway and the cost isn't worth it.
    if bootstrap and 2 <= n <= 2000:
        rng = random.Random(seed)
        out["mean_ci95"] = _bootstrap_ci(xs, lambda s: sum(s) / len(s), n_resamples, rng)
        out["p50_ci95"] = _bootstrap_ci(xs, lambda s: percentile(s, 50), n_resamples, rng)
    return out


def time_weighted_concurrency(intervals: Sequence[tuple]) -> Dict[str, float]:
    """Mean and max number of in-flight requests over [first start, last end]."""
    if not intervals:
        return {"mean": 0.0, "max": 0}
    events = []
    for start, end in intervals:
        events.append((start, 1))
        events.append((end, -1))
    # Ends sort before starts at the same instant so back-to-back requests
    # don't count as overlapping.
    events.sort(key=lambda e: (e[0], e[1]))
    t0, t_end = events[0][0], events[-1][0]
    level = peak = 0
    area = 0.0
    prev_t = t0
    for t, delta in events:
        area += level * (t - prev_t)
        prev_t = t
        level += delta
        peak = max(peak, level)
    span = t_end - t0
    return {"mean": area / span if span > 0 else float(peak), "max": peak}
