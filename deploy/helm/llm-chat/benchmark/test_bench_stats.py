# Copyright © Advanced Micro Devices, Inc., or its affiliates.
#
# SPDX-License-Identifier: MIT
"""Checks bench_stats against numpy (the reference `vllm bench serve` uses).

    python3 test_bench_stats.py
"""

import random

import numpy as np

from bench_stats import PERCENTILES, describe, min_samples_for, percentile, time_weighted_concurrency


def test_percentiles_match_numpy():
    rng = random.Random(1)
    for n in (1, 2, 3, 7, 10, 99, 100, 101, 1000, 5003):
        xs = [rng.lognormvariate(0, 1.5) for _ in range(n)]
        s = sorted(xs)
        for q in (0, 1, 25, 50, 75, 90, 95, 99, 99.9, 100):
            assert abs(percentile(s, q) - float(np.percentile(xs, q))) < 1e-9, (n, q)


def test_describe_matches_numpy():
    rng = random.Random(2)
    xs = [rng.gauss(100, 15) for _ in range(500)]
    d = describe(xs, bootstrap=True, n_resamples=200)
    assert abs(d["mean"] - np.mean(xs)) < 1e-9
    assert abs(d["std"] - np.std(xs, ddof=1)) < 1e-9
    for q in PERCENTILES:
        assert abs(d[f"p{q}"] - np.percentile(xs, q)) < 1e-9
    lo, hi = d["p50_ci95"]
    assert lo <= d["p50"] <= hi


def test_reliability_flags():
    assert min_samples_for(50) == 2
    assert min_samples_for(90) == 10
    assert min_samples_for(95) == 20
    assert min_samples_for(99) == 100
    d = describe(list(range(20)))
    assert d["p95_reliable"] and not d["p99_reliable"]


def test_describe_ignores_missing():
    d = describe([1.0, None, float("nan"), 3.0])
    assert d["n"] == 2 and d["mean"] == 2.0


def test_concurrency():
    assert time_weighted_concurrency([(0, 10), (0, 10)]) == {"mean": 2.0, "max": 2}
    back_to_back = time_weighted_concurrency([(0, 5), (5, 10)])
    assert back_to_back == {"mean": 1.0, "max": 1}
    half = time_weighted_concurrency([(0, 10), (0, 5)])
    assert abs(half["mean"] - 1.5) < 1e-9 and half["max"] == 2


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"PASS {name}")
