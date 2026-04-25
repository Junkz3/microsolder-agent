# SPDX-License-Identifier: Apache-2.0
"""Generic BoardViewer 2.1.0.8 .cad parser — written from scratch.

The `.cad` extension is an umbrella used by the generic BoardViewer
2.1.0.8 distribution. The reliable path is the BRD2 sniff: when the
upload starts with `BRDOUT:` we delegate to `BRD2Parser` (verified
on real open-hardware BRD2 files). The Test_Link-shape ASCII fallback
is best-effort and may not match every wild `.cad` file —
production `.cad` is more likely a binary container. Anything
clearly binary trips a clear `ObfuscatedFileError`.

Source-format tag is always `"cad"` in the emitted Board so the
frontend and downstream pipeline know which upload produced the
artefact. No code copied from any external codebase.
"""

from __future__ import annotations

from api.board.model import Board
from api.board.parser._ascii_boardview import (
    DialectMarkers,
    looks_like_binary,
    parse_test_link_shape,
)
from api.board.parser.base import BoardParser, ObfuscatedFileError, register
from api.board.parser.brd2 import BRD2Parser

_CAD_MARKERS = DialectMarkers(
    header_count_marker="var_data:",
    outline_markers=("Format:", "FORMAT:"),
    parts_markers=("Parts:", "PARTS:", "Pins1:"),
    pins_markers=("Pins:", "PINS:", "Pins2:"),
    nails_markers=("Nails:", "NAILS:"),
)


@register
class CADParser(BoardParser):
    extensions = (".cad",)

    def parse(self, raw: bytes, *, file_hash: str, board_id: str) -> Board:
        text = raw.decode("utf-8", errors="replace")
        if "BRDOUT:" in text[:1024]:
            board = BRD2Parser().parse(raw, file_hash=file_hash, board_id=board_id)
            return board.model_copy(update={"source_format": "cad"})
        if looks_like_binary(raw):
            raise ObfuscatedFileError(
                "cad: this file looks like a binary BoardViewer container "
                "(non-printable byte ratio > 30%). Current parser supports "
                "BRD2 (sniffed via BRDOUT:) and Test_Link-shape ASCII only."
            )
        return parse_test_link_shape(
            text,
            markers=_CAD_MARKERS,
            source_format="cad",
            board_id=board_id,
            file_hash=file_hash,
        )
