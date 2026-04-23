# api/tools/hypothesize.py
# SPDX-License-Identifier: Apache-2.0
"""mb_hypothesize — reverse diagnostic tool for the agent.

Reads memory/{slug}/electrical_graph.json (+ optional boot_sequence_analyzed.json),
validates every refdes / rail label against the graph, and dispatches to the
pure-Python hypothesize engine. Structured `{found: false, ...}` on any miss —
same anti-hallucination contract as mb_schematic_graph and mb_get_component.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from api.pipeline.schematic.hypothesize import (
    Observations,
    hypothesize,
)
from api.pipeline.schematic.schemas import AnalyzedBootSequence, ElectricalGraph


def _closest_matches(candidates: list[str], needle: str, k: int = 5) -> list[str]:
    needle_u = needle.upper()
    prefix = needle_u[:1] if needle_u else ""
    substr = sorted(c for c in candidates if needle_u and needle_u in c.upper())
    pfx = sorted(c for c in candidates if prefix and c.upper().startswith(prefix))
    merged = list(dict.fromkeys(substr + pfx))
    return merged[:k]


def mb_hypothesize(
    *,
    device_slug: str,
    memory_root: Path,
    dead_comps: list[str] | None = None,
    alive_comps: list[str] | None = None,
    dead_rails: list[str] | None = None,
    alive_rails: list[str] | None = None,
    max_results: int = 5,
) -> dict[str, Any]:
    """Rank candidate refdes-kills that explain the observations.

    Returns the HypothesizeResult JSON dict on success, or
    {found: false, reason, ...} on any input validation failure.
    """
    pack = memory_root / device_slug
    graph_path = pack / "electrical_graph.json"
    if not graph_path.exists():
        return {"found": False, "reason": "no_schematic_graph", "device_slug": device_slug}
    try:
        eg = ElectricalGraph.model_validate_json(graph_path.read_text())
    except (OSError, ValueError):
        return {"found": False, "reason": "malformed_graph", "device_slug": device_slug}

    known_comps = set(eg.components.keys())
    known_rails = set(eg.power_rails.keys())

    invalid_refdes = sorted(
        r for r in (dead_comps or []) + (alive_comps or [])
        if r not in known_comps
    )
    if invalid_refdes:
        return {
            "found": False,
            "reason": "unknown_refdes",
            "invalid_refdes": invalid_refdes,
            "closest_matches": {
                r: _closest_matches(list(known_comps), r) for r in invalid_refdes
            },
        }

    invalid_rails = sorted(
        r for r in (dead_rails or []) + (alive_rails or [])
        if r not in known_rails
    )
    if invalid_rails:
        return {
            "found": False,
            "reason": "unknown_rail",
            "invalid_rails": invalid_rails,
            "closest_matches": {
                r: _closest_matches(list(known_rails), r) for r in invalid_rails
            },
        }

    ab: AnalyzedBootSequence | None = None
    ab_path = pack / "boot_sequence_analyzed.json"
    if ab_path.exists():
        try:
            ab = AnalyzedBootSequence.model_validate_json(ab_path.read_text())
        except ValueError:
            ab = None

    observations = Observations(
        dead_comps=frozenset(dead_comps or []),
        alive_comps=frozenset(alive_comps or []),
        dead_rails=frozenset(dead_rails or []),
        alive_rails=frozenset(alive_rails or []),
    )
    result = hypothesize(
        eg, analyzed_boot=ab, observations=observations, max_results=max_results,
    )
    payload = result.model_dump()
    payload["found"] = True
    return payload
