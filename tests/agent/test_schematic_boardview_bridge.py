# SPDX-License-Identifier: Apache-2.0
"""Coverage for api.agent.schematic_boardview_bridge."""

from __future__ import annotations

import pytest

from api.agent.schematic_boardview_bridge import (
    EnrichedTimeline,
    ProbePoint,
    enrich,
)
from api.board.model import Board
from api.pipeline.schematic.simulator import BoardState, SimulationTimeline


def _empty_board() -> Board:
    return Board(
        board_id="test",
        file_hash="sha256:x",
        source_format="test_link",
        outline=[],
        parts=[],
        pins=[],
        nets=[],
        nails=[],
    )


@pytest.fixture
def empty_timeline() -> SimulationTimeline:
    return SimulationTimeline(
        device_slug="test",
        killed_refdes=[],
        states=[BoardState(phase_index=1, phase_name="Phase 1")],
        final_verdict="completed",
    )


@pytest.fixture
def empty_board() -> Board:
    return _empty_board()


def test_enrich_returns_enriched_timeline_with_empty_route(
    empty_timeline, empty_board
):
    out = enrich(empty_timeline, empty_board)
    assert isinstance(out, EnrichedTimeline)
    assert out.timeline == empty_timeline
    assert out.probe_route == []
    assert out.unmapped_refdes == []


def test_probe_point_shape():
    pp = ProbePoint(
        refdes="U7",
        side="top",
        coords=(45.2, 23.1),
        bbox_mm=((40.0, 20.0), (50.0, 26.0)),
        reason="rail source",
        priority=1,
    )
    assert pp.refdes == "U7"
    assert pp.priority == 1
