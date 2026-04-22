"""Per-session state for the boardview panel."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from api.board.model import Board

Side = Literal["top", "bottom"]


@dataclass
class SessionState:
    board: Board | None = None
    layer: Side = "top"
    highlights: set[str] = field(default_factory=set)
    net_highlight: str | None = None
    annotations: dict[str, dict[str, Any]] = field(default_factory=dict)
    arrows: dict[str, dict[str, Any]] = field(default_factory=dict)
    dim_unrelated: bool = False
    filter_prefix: str | None = None
    layer_visibility: dict[Side, bool] = field(
        default_factory=lambda: {"top": True, "bottom": True}
    )

    def set_board(self, board: Board) -> None:
        """Load a new board and reset all view state."""
        self.board = board
        self.layer = "top"
        self.highlights = set()
        self.net_highlight = None
        self.annotations = {}
        self.arrows = {}
        self.dim_unrelated = False
        self.filter_prefix = None
        self.layer_visibility = {"top": True, "bottom": True}
