# SPDX-License-Identifier: Apache-2.0
"""Phase 2 — Registry Builder. Forced-tool output, Pydantic-validated.

Converts the Scout's raw Markdown dump into a canonical `Registry` JSON.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from anthropic import AsyncAnthropic

from api.pipeline.prompts import REGISTRY_SYSTEM, REGISTRY_USER_TEMPLATE
from api.pipeline.schemas import Registry
from api.pipeline.tool_call import call_with_forced_tool

if TYPE_CHECKING:
    from api.pipeline.schematic.schemas import ElectricalGraph
    from api.pipeline.telemetry.token_stats import PhaseTokenStats

logger = logging.getLogger("microsolder.pipeline.registry")


SUBMIT_REGISTRY_TOOL_NAME = "submit_registry"


def _submit_registry_tool() -> dict:
    """Build the forced-tool definition whose `input_schema` matches `Registry`."""
    schema = Registry.model_json_schema()
    return {
        "name": SUBMIT_REGISTRY_TOOL_NAME,
        "description": (
            "Submit the canonical glossary of components and signals for the device. "
            "This is your only valid form of output."
        ),
        "input_schema": schema,
    }


def _build_graph_targeting_block(graph: ElectricalGraph) -> str:
    """Render the technician-supplied ElectricalGraph for the registry prompt.

    Compact projection — refdes, MPN, kind/role per component, plus rails —
    so the Registry Builder can emit `refdes_candidates` justified by MPN
    matches with the dump. Pin-level detail and net topology are omitted;
    they don't help disambiguate canonical→refdes."""
    lines: list[str] = ["# Provided ElectricalGraph (for refdes_candidates targeting)"]

    lines.append("")
    lines.append("## Components (refdes → MPN, kind, role)")
    for refdes in sorted(graph.components):
        comp = graph.components[refdes]
        mpn = (comp.value.mpn if comp.value is not None else None) or "—"
        kind = comp.kind or "—"
        role = comp.role or "—"
        lines.append(f"- {refdes}: mpn={mpn} kind={kind} role={role}")

    lines.append("")
    lines.append("## Power rails")
    for rail_key in sorted(graph.power_rails):
        rail = graph.power_rails[rail_key]
        v = (
            f"{rail.voltage_nominal:.2f}V"
            if rail.voltage_nominal is not None
            else "?"
        )
        src = rail.source_refdes or "—"
        lines.append(f"- {rail.label}: voltage={v} source={src}")

    return "\n".join(lines)


def _build_user_prompt(
    *,
    device_label: str,
    raw_dump: str,
    graph: ElectricalGraph | None,
) -> str:
    """Assemble the Registry Builder user message.

    Without a graph → exactly `REGISTRY_USER_TEMPLATE.format(...)` (legacy,
    byte-for-byte identical to today). With a graph → appends the
    targeting block after the raw dump so the model sees the canonicals
    first and the refdes evidence second."""
    base = REGISTRY_USER_TEMPLATE.format(
        device_label=device_label,
        raw_dump=raw_dump,
    )
    if graph is None:
        return base
    return base + "\n\n" + _build_graph_targeting_block(graph)


async def run_registry_builder(
    *,
    client: AsyncAnthropic,
    model: str,
    device_label: str,
    raw_dump: str,
    graph: ElectricalGraph | None = None,
    stats: PhaseTokenStats | None = None,
) -> Registry:
    """Execute Phase 2 — return a validated `Registry` Pydantic model.

    `graph`, when supplied, lets the Registry Builder emit per-component
    `refdes_candidates` justified by MPN matches against the dump. Without
    it, the user prompt is byte-for-byte identical to today and
    `refdes_candidates` stays null on every component (legacy path).
    """
    logger.info(
        "[Registry] Building canonical glossary for device=%r · graph=%s",
        device_label,
        "yes" if graph is not None else "no",
    )

    user_prompt = _build_user_prompt(
        device_label=device_label,
        raw_dump=raw_dump,
        graph=graph,
    )

    registry = await call_with_forced_tool(
        client=client,
        model=model,
        system=REGISTRY_SYSTEM,
        messages=[{"role": "user", "content": user_prompt}],
        tools=[_submit_registry_tool()],
        forced_tool_name=SUBMIT_REGISTRY_TOOL_NAME,
        output_schema=Registry,
        max_attempts=2,
        log_label="Registry",
        stats=stats,
    )

    n_with_candidates = sum(
        1 for c in registry.components if c.refdes_candidates
    )
    logger.info(
        "[Registry] Built · components=%d (with refdes_candidates: %d) signals=%d",
        len(registry.components),
        n_with_candidates,
        len(registry.signals),
    )
    return registry
