# SPDX-License-Identifier: Apache-2.0
"""Post-hoc refdes sanitizer.

Second layer of defense against hallucinated component IDs. The first
layer is tool discipline (mb_get_component returns {found: false} for
unknown refdes); this layer scans outbound agent text and wraps
refdes-shaped tokens that don't resolve on the current board.
"""

from __future__ import annotations

import re

from api.board.model import Board
from api.board.validator import is_valid_refdes

REFDES_RE = re.compile(r"\b[A-Z]{1,3}\d{1,4}\b")


def sanitize_agent_text(text: str, board: Board | None) -> tuple[str, list[str]]:
    """Return (clean_text, unknown_refdes_list).

    If board is None, no ground truth exists — returns text unchanged.
    """
    if board is None:
        return text, []

    unknown: list[str] = []

    def _wrap(match: re.Match[str]) -> str:
        token = match.group(0)
        if is_valid_refdes(board, token):
            return token
        unknown.append(token)
        return f"⟨?{token}⟩"

    return REFDES_RE.sub(_wrap, text), unknown
