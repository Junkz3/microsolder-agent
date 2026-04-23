"""Tests for sanitize_agent_text — post-hoc refdes guard."""

from api.agent.sanitize import sanitize_agent_text
from api.board.model import Board, Layer, Part, Point


def _board_with_parts(refdeses: list[str]) -> Board:
    parts = [
        Part(
            refdes=r,
            layer=Layer.TOP,
            is_smd=True,
            bbox=(Point(x=0, y=0), Point(x=10, y=10)),
            pin_refs=[],
        )
        for r in refdeses
    ]
    return Board(
        board_id="test", file_hash="sha256:x", source_format="test",
        outline=[], parts=parts, pins=[], nets=[], nails=[],
    )


def test_noop_when_board_is_none() -> None:
    text = "Check U7 and U999 please"
    clean, unknown = sanitize_agent_text(text, None)
    assert clean == text
    assert unknown == []


def test_wraps_unknown_refdes_and_keeps_known() -> None:
    board = _board_with_parts(["U7"])
    clean, unknown = sanitize_agent_text("Check U7 and U999 please", board)
    assert clean == "Check U7 and ⟨?U999⟩ please"
    assert unknown == ["U999"]


def test_multiple_unknown_refdes_all_wrapped() -> None:
    board = _board_with_parts(["C1"])
    clean, unknown = sanitize_agent_text("U1, U2, C1, R3 are suspect", board)
    assert "⟨?U1⟩" in clean
    assert "⟨?U2⟩" in clean
    assert "C1" in clean  # known, not wrapped
    assert "⟨?R3⟩" in clean
    assert set(unknown) == {"U1", "U2", "R3"}


def test_does_not_match_net_names_with_underscore() -> None:
    board = _board_with_parts([])
    clean, unknown = sanitize_agent_text("HDMI_D0 and VDD_3V3 are rails", board)
    assert clean == "HDMI_D0 and VDD_3V3 are rails"
    assert unknown == []


def test_does_not_match_lowercase() -> None:
    board = _board_with_parts([])
    clean, unknown = sanitize_agent_text("the u7 part is mentioned", board)
    assert clean == "the u7 part is mentioned"
    assert unknown == []


def test_flags_refdes_shaped_protocol_names() -> None:
    """Tokens like USB3 match the pattern; flagged when absent. Known limitation."""
    board = _board_with_parts([])
    clean, unknown = sanitize_agent_text("USB3 is fine", board)
    assert clean == "⟨?USB3⟩ is fine"
    assert unknown == ["USB3"]


def test_empty_text() -> None:
    board = _board_with_parts(["U1"])
    clean, unknown = sanitize_agent_text("", board)
    assert clean == ""
    assert unknown == []


def test_refdes_at_string_boundaries() -> None:
    board = _board_with_parts(["U1"])
    clean, unknown = sanitize_agent_text("U999", board)
    assert clean == "⟨?U999⟩"
    assert unknown == ["U999"]
    clean, unknown = sanitize_agent_text("U1", board)
    assert clean == "U1"
    assert unknown == []
