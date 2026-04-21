from pathlib import Path

import pytest

from api.board.parser.base import (
    BoardParser,
    UnsupportedFormatError,
    parser_for,
)


def test_parser_for_unknown_extension_raises(tmp_path: Path):
    p = tmp_path / "nope.xyz"
    p.write_bytes(b"irrelevant")
    with pytest.raises(UnsupportedFormatError):
        parser_for(p)


def test_parser_for_brd_returns_brd_parser(tmp_path: Path):
    try:
        from api.board.parser.brd import BRDParser  # noqa: F401
    except ImportError:
        pytest.skip("BRDParser not yet implemented (Task 5)")
    p = tmp_path / "mini.brd"
    p.write_text("str_length: 0\nvar_data: 0 0 0 0\n")
    parser = parser_for(p)
    assert isinstance(parser, BoardParser)
    assert ".brd" in parser.extensions
