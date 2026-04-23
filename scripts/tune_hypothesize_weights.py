#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Sweep (fp_weight, fn_weight) pairs and pick the best weighted top-3."""

from __future__ import annotations

import json
from pathlib import Path

import api.pipeline.schematic.hypothesize as hypothesize_mod
from api.pipeline.schematic.hypothesize import Observations

FIXTURE = Path(__file__).resolve().parents[1] / "tests/pipeline/schematic/fixtures/hypothesize_scenarios.json"
MEMORY_ROOT = Path(__file__).resolve().parents[1] / "memory"

MODE_WEIGHT = {"dead": 0.4, "anomalous": 0.3, "shorted": 0.2, "hot": 0.1}


def evaluate(fp_w: int, fn_w: int) -> tuple[float, dict[str, float]]:
    from api.pipeline.schematic.schemas import AnalyzedBootSequence, ElectricalGraph

    hypothesize_mod.PENALTY_WEIGHTS = (fp_w, fn_w)
    scenarios = json.loads(FIXTURE.read_text())
    by_slug: dict[str, list[dict]] = {}
    for sc in scenarios:
        by_slug.setdefault(sc["slug"], []).append(sc)
    per_mode_hits: dict[str, tuple[int, int]] = {m: (0, 0) for m in MODE_WEIGHT}
    for slug, group in by_slug.items():
        pack = MEMORY_ROOT / slug
        if not (pack / "electrical_graph.json").exists():
            continue
        eg = ElectricalGraph.model_validate_json((pack / "electrical_graph.json").read_text())
        ab_path = pack / "boot_sequence_analyzed.json"
        ab = AnalyzedBootSequence.model_validate_json(ab_path.read_text()) if ab_path.exists() else None
        for sc in group:
            obs = Observations(
                state_comps=sc["observations"]["state_comps"],
                state_rails=sc["observations"]["state_rails"],
            )
            result = hypothesize_mod.hypothesize(eg, analyzed_boot=ab, observations=obs)
            gt_refdes = tuple(sorted(sc["ground_truth_kill"]))
            gt_modes = tuple(sc["ground_truth_modes"])
            top3 = [(tuple(sorted(h.kill_refdes)), tuple(h.kill_modes)) for h in result.hypotheses[:3]]
            m = sc["ground_truth_modes"][0]
            hit, total = per_mode_hits[m]
            per_mode_hits[m] = (hit + (1 if (gt_refdes, gt_modes) in top3 else 0), total + 1)
    per_mode_acc = {m: (h / t if t else 0.0) for m, (h, t) in per_mode_hits.items()}
    weighted = sum(acc * MODE_WEIGHT[m] for m, acc in per_mode_acc.items())
    return weighted, per_mode_acc


def main() -> None:
    best = (0, 0, 0.0)
    for fp_w in (5, 10, 15, 20, 30):
        for fn_w in (1, 2, 3, 5):
            weighted, per_mode = evaluate(fp_w, fn_w)
            print(f"(fp={fp_w:>2}, fn={fn_w}) → weighted={weighted:.3%}   " + "  ".join(
                f"{m}={acc:.2%}" for m, acc in per_mode.items()
            ))
            if weighted > best[2]:
                best = (fp_w, fn_w, weighted)
    print(f"\nBEST: fp={best[0]}, fn={best[1]} → {best[2]:.3%}")


if __name__ == "__main__":
    main()
