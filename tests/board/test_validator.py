from pathlib import Path

from api.board.parser.brd import BRDParser
from api.board.validator import (
    is_valid_refdes,
    resolve_net,
    resolve_part,
    resolve_pin,
    suggest_similar,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _board():
    return BRDParser().parse_file(FIXTURE_DIR / "minimal.brd")


def test_is_valid_refdes_true():
    board = _board()
    assert is_valid_refdes(board, "R1") is True
    assert is_valid_refdes(board, "C1") is True


def test_is_valid_refdes_false_is_case_sensitive():
    board = _board()
    assert is_valid_refdes(board, "r1") is False
    assert is_valid_refdes(board, "U999") is False


def test_resolve_part():
    board = _board()
    r1 = resolve_part(board, "R1")
    assert r1 is not None
    assert r1.refdes == "R1"
    assert resolve_part(board, "U999") is None


def test_resolve_net():
    board = _board()
    vcc = resolve_net(board, "+3V3")
    assert vcc is not None
    assert vcc.name == "+3V3"
    assert resolve_net(board, "MISSING") is None


def test_resolve_pin():
    board = _board()
    pin = resolve_pin(board, "R1", 1)
    assert pin is not None
    assert pin.part_refdes == "R1"
    assert pin.index == 1
    assert resolve_pin(board, "R1", 99) is None
    assert resolve_pin(board, "U999", 1) is None


def test_suggest_similar_returns_close_matches():
    board = _board()
    suggestions = suggest_similar(board, "R2", k=3)
    # fixture only has R1 and C1 — R1 is closest to R2 (distance 1)
    assert "R1" in suggestions
    # empty string → empty list
    assert suggest_similar(board, "", k=3) == []


def test_suggest_similar_caps_at_k():
    board = _board()
    # fixture has 2 parts ; k=1 should return only the closest one
    one = suggest_similar(board, "R9", k=1)
    assert len(one) == 1
    assert one == ["R1"]


def test_suggest_similar_deterministic_order():
    """When multiple candidates have the same distance, ordering must be stable."""
    board = _board()
    # Both R1 and C1 are distance 2 from an unknown like "X2" :
    # edit(R1, X2) = substitute R→X + substitute 1→2 = 2
    # edit(C1, X2) = substitute C→X + substitute 1→2 = 2
    # Whatever order the function returns, it must be deterministic across runs.
    first = suggest_similar(board, "X2", k=2)
    second = suggest_similar(board, "X2", k=2)
    assert first == second
