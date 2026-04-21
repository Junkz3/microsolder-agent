"""Abstract base and format dispatch for board file parsers."""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from pathlib import Path

from api.board.model import Board


class BoardParserError(Exception):
    """Base class for parser errors."""


class UnsupportedFormatError(BoardParserError):
    """Raised when no parser is registered for a file's extension."""


class InvalidBoardFile(BoardParserError):
    """Raised when a file is recognized but malformed or refused."""


class ObfuscatedFileError(InvalidBoardFile):
    """Raised on OBV-signature obfuscated files — we refuse to decode."""


class MalformedHeaderError(InvalidBoardFile):
    def __init__(self, field: str):
        super().__init__(f"malformed header block: {field}")
        self.field = field


class PinPartMismatchError(InvalidBoardFile):
    def __init__(self, pin_index: int):
        super().__init__(f"pin {pin_index} references an unknown part")
        self.pin_index = pin_index


class BoardParser(ABC):
    """Abstract parser. One subclass per file format."""

    extensions: tuple[str, ...] = ()

    def parse_file(self, path: Path) -> Board:
        raw = path.read_bytes()
        file_hash = "sha256:" + hashlib.sha256(raw).hexdigest()
        return self.parse(raw, file_hash=file_hash, board_id=path.stem)

    @abstractmethod
    def parse(self, raw: bytes, *, file_hash: str, board_id: str) -> Board: ...


_REGISTRY: dict[str, type[BoardParser]] = {}


def register(parser_cls: type[BoardParser]) -> type[BoardParser]:
    """Decorator : register a parser by its extensions."""
    for ext in parser_cls.extensions:
        _REGISTRY[ext.lower()] = parser_cls
    return parser_cls


def parser_for(path: Path) -> BoardParser:
    ext = path.suffix.lower()
    cls = _REGISTRY.get(ext)
    if cls is None:
        raise UnsupportedFormatError(f"no parser registered for extension {ext!r}")
    return cls()
