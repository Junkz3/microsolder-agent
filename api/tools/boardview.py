"""Tool handlers for the boardview panel — invoked by the agent via tool-use."""

from __future__ import annotations

from typing import Any

from api.board.validator import is_valid_refdes, suggest_similar
from api.session.state import SessionState
from api.tools.ws_events import Highlight


def _no_board(session: SessionState) -> dict[str, Any] | None:
    if session.board is None:
        return {"ok": False, "reason": "no-board-loaded", "suggestions": []}
    return None


def _unknown_refdes(session: SessionState, refdes: str) -> dict[str, Any]:
    return {
        "ok": False,
        "reason": "unknown-refdes",
        "suggestions": suggest_similar(session.board, refdes, k=3),
    }


def highlight_component(
    session: SessionState,
    *,
    refdes: str | list[str],
    color: str = "accent",
    additive: bool = False,
) -> dict[str, Any]:
    err = _no_board(session)
    if err:
        return err

    targets = [refdes] if isinstance(refdes, str) else list(refdes)
    for r in targets:
        if not is_valid_refdes(session.board, r):
            return _unknown_refdes(session, r)

    if not additive:
        session.highlights = set()
    session.highlights.update(targets)

    event = Highlight(refdes=targets, color=color, additive=additive)
    summary = f"Highlighted {', '.join(targets)}."
    return {"ok": True, "summary": summary, "event": event}
