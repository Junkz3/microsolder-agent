"""Parser for OpenBoardView BRD2 format."""

from pathlib import Path

import pytest

from api.board.model import Layer
from api.board.parser.base import (
    InvalidBoardFile,
    MalformedHeaderError,
)
from api.board.parser.brd2 import BRD2Parser

FIXTURE_DIR = Path(__file__).parent / "fixtures"
REPO_ROOT = Path(__file__).resolve().parents[2]


def test_parses_mnt_reform_motherboard():
    """The committed MNT Reform BRD2 fixture must parse cleanly and match header counts."""
    path = REPO_ROOT / "board_assets" / "mnt-reform-motherboard.brd"
    board = BRD2Parser().parse_file(path)

    assert board.source_format == "brd2"
    assert board.board_id == "mnt-reform-motherboard"
    assert len(board.parts) == 493
    assert len(board.pins) == 2104
    assert len(board.nets) == 647
    assert len(board.nails) == 5
    assert len(board.outline) == 9

    # Spot-check a known component : C2 should exist on the top layer.
    c2 = board.part_by_refdes("C2")
    assert c2 is not None
    assert c2.layer == Layer.TOP

    # Known net should classify as ground.
    gnd = board.net_by_name("GND")
    assert gnd is not None
    assert gnd.is_ground is True

    # HDMI differential-pair nets exist under their real names.
    hdmi = board.net_by_name("HDMI_D2+")
    assert hdmi is not None


def test_rejects_plain_test_link_by_mistake(tmp_path: Path):
    """A Test_Link file handed to BRD2Parser must refuse, not silently produce garbage."""
    f = tmp_path / "wrong_format.brd"
    f.write_text("str_length: 0\nvar_data: 0 0 0 0\n")
    with pytest.raises(InvalidBoardFile):
        BRD2Parser().parse_file(f)


def test_malformed_brdout_header(tmp_path: Path):
    f = tmp_path / "bad.brd"
    f.write_text("0\nBRDOUT: not-a-number 0 0\n")
    with pytest.raises(MalformedHeaderError):
        BRD2Parser().parse_file(f)


def test_pin_without_valid_net_id(tmp_path: Path):
    """net_id referencing a NET that doesn't exist (past end of NETS block) must fail."""
    f = tmp_path / "bad_net.brd"
    f.write_text(
        "0\n"
        "BRDOUT: 4 100 100\n"
        "0 0\n100 0\n100 100\n0 100\n"
        "\n"
        "NETS: 1\n"
        "1 +3V3\n"
        "\n"
        "PARTS: 1\n"
        "R1 0 0 10 10 0 1\n"
        "\n"
        "PINS: 1\n"
        "5 5 99 1\n"  # net_id=99 references nothing
        "\n"
        "NAILS: 0\n"
    )
    with pytest.raises(MalformedHeaderError):
        BRD2Parser().parse_file(f)
