#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Perf benchmark for hypothesize() on the fixture corpus.

Usage:
    .venv/bin/python scripts/bench_hypothesize.py
    .venv/bin/python scripts/bench_hypothesize.py --slug mnt-reform-motherboard --iterations 50

Emits a JSON summary with mean/p50/p95/p99 timings in ms plus pruning stats.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path

from api.pipeline.schematic.hypothesize import Observations, hypothesize
from api.pipeline.schematic.schemas import AnalyzedBootSequence, ElectricalGraph


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--slug", default="mnt-reform-motherboard")
    parser.add_argument("--iterations", type=int, default=50,
                        help="Each scenario is run this many times for timing stability.")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    fixture = root / "tests/pipeline/schematic/fixtures/hypothesize_scenarios.json"
    scenarios = [
        sc for sc in json.loads(fixture.read_text())
        if sc["slug"] == args.slug
    ]
    pack = root / "memory" / args.slug
    eg = ElectricalGraph.model_validate_json(
        (pack / "electrical_graph.json").read_text()
    )
    ab_path = pack / "boot_sequence_analyzed.json"
    ab = (
        AnalyzedBootSequence.model_validate_json(ab_path.read_text())
        if ab_path.exists()
        else None
    )

    samples_ms: list[float] = []
    single_tested: list[int] = []
    pair_tested: list[int] = []
    for _ in range(args.iterations):
        for sc in scenarios:
            obs = Observations(
                dead_comps=frozenset(sc["observations"]["dead_comps"]),
                alive_comps=frozenset(sc["observations"]["alive_comps"]),
                dead_rails=frozenset(sc["observations"]["dead_rails"]),
                alive_rails=frozenset(sc["observations"]["alive_rails"]),
            )
            t0 = time.perf_counter_ns()
            res = hypothesize(eg, analyzed_boot=ab, observations=obs)
            samples_ms.append((time.perf_counter_ns() - t0) / 1e6)
            single_tested.append(res.pruning.single_candidates_tested)
            pair_tested.append(res.pruning.two_fault_pairs_tested)

    samples_ms.sort()

    def pct(p: float) -> float:
        return samples_ms[max(0, int(len(samples_ms) * p) - 1)]

    print(
        json.dumps(
            {
                "slug": args.slug,
                "scenarios": len(scenarios),
                "iterations_each": args.iterations,
                "ms": {
                    "mean": round(statistics.fmean(samples_ms), 3),
                    "p50": round(pct(0.50), 3),
                    "p95": round(pct(0.95), 3),
                    "p99": round(pct(0.99), 3),
                },
                "single_candidates_tested": {
                    "mean": round(statistics.fmean(single_tested), 1),
                    "max": max(single_tested),
                },
                "two_fault_pairs_tested": {
                    "mean": round(statistics.fmean(pair_tested), 1),
                    "max": max(pair_tested),
                },
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
