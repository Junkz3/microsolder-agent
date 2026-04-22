from pathlib import Path

import pytest

from api.board.parser.test_link import BRDParser
from api.session.state import SessionState
from api.tools.boardview import highlight_component

FIXTURE_DIR = Path(__file__).parent.parent / "board" / "fixtures"


@pytest.fixture
def session() -> SessionState:
    s = SessionState()
    s.set_board(BRDParser().parse_file(FIXTURE_DIR / "minimal.brd"))
    return s


def test_highlight_component_happy_path(session):
    result = highlight_component(session, refdes="R1")
    assert result["ok"] is True
    assert result["event"].type == "boardview.highlight"
    assert result["event"].refdes == ["R1"]
    assert "R1" in session.highlights


def test_highlight_component_accepts_list(session):
    result = highlight_component(session, refdes=["R1", "C1"])
    assert result["ok"] is True
    assert set(session.highlights) == {"R1", "C1"}


def test_highlight_component_invalid_refdes_returns_suggestions(session):
    result = highlight_component(session, refdes="R2")
    assert result["ok"] is False
    assert result["reason"] == "unknown-refdes"
    assert "R1" in result["suggestions"]
    assert "R1" not in session.highlights  # state untouched


def test_highlight_component_additive(session):
    highlight_component(session, refdes="R1")
    highlight_component(session, refdes="C1", additive=True)
    assert session.highlights == {"R1", "C1"}


def test_highlight_component_non_additive_replaces(session):
    highlight_component(session, refdes="R1")
    highlight_component(session, refdes="C1", additive=False)
    assert session.highlights == {"C1"}
