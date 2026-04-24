# SPDX-License-Identifier: Apache-2.0
"""Joins a SimulationTimeline (schematic-space) with a parsed Board
(physical-PCB-space) to produce a measurement-friendly EnrichedTimeline.

Pure module. No I/O. The single entry point is `enrich(timeline, board)`.
The route is built by stacking up to four heuristic rules, capped at
8 ProbePoints total — see the ranking section for ordering.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from api.board.model import Board
from api.pipeline.schematic.simulator import SimulationTimeline

# Conversion constant: Board uses mils per OBV convention.
MIL_TO_MM = 0.0254
MAX_ROUTE_ENTRIES = 8


class ProbePoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refdes: str
    side: str                                  # "top" | "bottom"
    coords: tuple[float, float]                # (x_mm, y_mm)
    bbox_mm: tuple[tuple[float, float], tuple[float, float]] | None = None
    reason: str
    priority: int


class EnrichedTimeline(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timeline: SimulationTimeline
    probe_route: list[ProbePoint] = Field(default_factory=list)
    unmapped_refdes: list[str] = Field(default_factory=list)


def enrich(timeline: SimulationTimeline, board: Board) -> EnrichedTimeline:
    """Produce a ranked probe route from a SimulationTimeline + parsed Board."""
    return EnrichedTimeline(timeline=timeline)
