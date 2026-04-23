# SPDX-License-Identifier: Apache-2.0
"""Per-repair append-only journal of tech measurements.

Same JSONL pattern as `api/agent/chat_history.py` — one `{ts, event}`
record per line at `memory/{slug}/repairs/{repair_id}/measurements.jsonl`.

Public surface:
- MeasurementEvent (Pydantic shape)
- append_measurement / load_measurements / compare_measurements
- synthesise_observations (derive Observations from the latest-per-target
  state in the journal)
- auto_classify (pure function — map a value + nominal + unit to a
  ComponentMode / RailMode, or None if it can't decide)
- parse_target (parser for "kind:name" strings)
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

logger = logging.getLogger("microsolder.agent.measurement_memory")


Source = Literal["ui", "agent"]
Unit = Literal["V", "A", "W", "°C", "Ω", "mV"]


class MeasurementEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timestamp: str
    target: str
    value: float
    unit: Unit
    nominal: float | None = None
    note: str | None = None
    source: Source
    auto_classified_mode: str | None = None


# ---------------------------------------------------------------------------
# Target grammar
# ---------------------------------------------------------------------------

TargetKind = Literal["rail", "comp", "pin"]
_KNOWN_KINDS: frozenset[str] = frozenset({"rail", "comp", "pin"})


def parse_target(target: str) -> tuple[str, str]:
    """Split a target string into (kind, name).

    Examples:
      "rail:+3V3"  → ("rail", "+3V3")
      "comp:U7"    → ("comp", "U7")
      "pin:U7:3"   → ("pin", "U7:3")

    Raises ValueError for unknown kinds or malformed input.
    """
    if ":" not in target:
        raise ValueError(f"expected '<kind>:<name>', got {target!r}")
    kind, _, name = target.partition(":")
    if kind not in _KNOWN_KINDS:
        raise ValueError(f"unknown target kind {kind!r}; expected one of {sorted(_KNOWN_KINDS)}")
    if not name:
        raise ValueError(f"empty name in target {target!r}")
    return kind, name


# ---------------------------------------------------------------------------
# Auto-classify rules
# ---------------------------------------------------------------------------

# Central, tunable. Values are ratios of nominal unless otherwise stated.
CLASSIFY_RAIL_ALIVE_LOW = 0.90         # ≥ 90% of nominal
CLASSIFY_RAIL_ALIVE_HIGH = 1.10        # ≤ 110% of nominal
CLASSIFY_RAIL_DEAD_THRESHOLD_V = 0.05  # absolute volts, < this → dead
CLASSIFY_RAIL_ANOMALOUS_LOW = 0.50     # 50-90% of nominal → anomalous
CLASSIFY_IC_HOT_CELSIUS = 65.0         # IC temperature threshold


def auto_classify(
    *, target: str, value: float, unit: str,
    nominal: float | None = None, note: str | None = None,
) -> str | None:
    """Map a (target, value, unit, nominal?) to a mode string.

    Returns None when we can't decide (missing nominal, unsupported
    kind, etc.) — the caller keeps the measurement in storage but
    leaves the mode unset.
    """
    try:
        kind, name = parse_target(target)
    except ValueError:
        return None

    if kind == "rail" and unit in ("V", "mV"):
        if nominal is None:
            return None
        # Normalise mV to V.
        v = value / 1000.0 if unit == "mV" else value
        nom = nominal / 1000.0 if unit == "mV" else nominal
        # Explicit short note dominates.
        if note and "short" in note.lower() and abs(v) < CLASSIFY_RAIL_DEAD_THRESHOLD_V:
            return "shorted"
        if v < CLASSIFY_RAIL_DEAD_THRESHOLD_V:
            return "dead"
        ratio = v / nom if nom != 0 else 0.0
        if ratio > CLASSIFY_RAIL_ALIVE_HIGH:
            return "shorted"   # overvoltage folded into shorted for Phase 1
        if ratio >= CLASSIFY_RAIL_ALIVE_LOW:
            return "alive"
        if ratio >= CLASSIFY_RAIL_ANOMALOUS_LOW:
            return "anomalous"
        return "anomalous"   # any non-zero sag below 50% is still anomalous

    if kind == "comp" and unit == "°C":
        return "hot" if value >= CLASSIFY_IC_HOT_CELSIUS else "alive"

    # Unsupported combinations — we store the measurement but leave the
    # mode empty for the tech to decide manually.
    return None
