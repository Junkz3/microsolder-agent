# SPDX-License-Identifier: Apache-2.0
"""mb_validate_finding — persist a repair outcome + emit WS event.

Called by the agent at the end of a diagnostic session once the tech
has clicked « Marquer fix » and Claude has confirmed the fixes via
chat. Writes outcome.json and fans out simulation.repair_validated to
the UI so the dashboard can flip to a « validated » state.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from api.agent.validation import RepairOutcome, ValidatedFix, write_outcome

# Pluggable WS emitter — set by the runtime at session open.
_ws_emitter: Callable[[dict[str, Any]], None] | None = None


def set_ws_emitter(emitter: Callable[[dict[str, Any]], None] | None) -> None:
    global _ws_emitter
    _ws_emitter = emitter


def _emit(event: dict[str, Any]) -> None:
    if _ws_emitter is not None:
        try:
            _ws_emitter(event)
        except Exception:   # noqa: BLE001 — best-effort broadcast
            pass


def _known_refdes(memory_root: Path, device_slug: str) -> set[str] | None:
    """Return the refdes set from the device's electrical_graph, or None if absent."""
    graph_path = memory_root / device_slug / "electrical_graph.json"
    if not graph_path.exists():
        return None
    try:
        from api.pipeline.schematic.schemas import ElectricalGraph
        eg = ElectricalGraph.model_validate_json(graph_path.read_text())
        return set(eg.components.keys())
    except (OSError, ValueError):
        return None


def mb_validate_finding(
    *,
    device_slug: str,
    repair_id: str,
    memory_root: Path,
    fixes: list[dict],
    tech_note: str | None = None,
    agent_confidence: str = "high",
) -> dict[str, Any]:
    """Persist a RepairOutcome for this repair. Emits WS event on success.

    Each fix is a dict {refdes, mode, rationale}. Rejects empty fixes,
    invalid modes, or unknown refdes (when a graph is available).
    """
    if not fixes:
        return {"validated": False, "reason": "empty_fixes"}

    parsed_fixes: list[ValidatedFix] = []
    for raw in fixes:
        try:
            parsed_fixes.append(ValidatedFix.model_validate(raw))
        except ValueError as exc:
            return {"validated": False, "reason": "invalid_fix", "detail": str(exc)}

    known = _known_refdes(memory_root, device_slug)
    if known is not None:
        invalid = sorted(f.refdes for f in parsed_fixes if f.refdes not in known)
        if invalid:
            return {
                "validated": False,
                "reason": "unknown_refdes",
                "invalid_refdes": invalid,
            }

    try:
        outcome = RepairOutcome(
            validated_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            repair_id=repair_id,
            device_slug=device_slug,
            fixes=parsed_fixes,
            tech_note=tech_note,
            agent_confidence=agent_confidence,  # type: ignore[arg-type]
        )
    except ValueError as exc:
        return {"validated": False, "reason": "invalid_outcome", "detail": str(exc)}

    if not write_outcome(memory_root=memory_root, outcome=outcome):
        return {"validated": False, "reason": "io_error"}

    _emit({
        "type": "simulation.repair_validated",
        "repair_id": repair_id,
        "fixes_count": len(parsed_fixes),
    })
    return {
        "validated": True,
        "repair_id": repair_id,
        "fixes_count": len(parsed_fixes),
        "validated_at": outcome.validated_at,
    }
