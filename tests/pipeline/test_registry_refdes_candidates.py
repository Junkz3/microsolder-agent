# SPDX-License-Identifier: Apache-2.0
"""Schema-level tests for the Registry refdes_candidates extension.

Behavioural tests for the actual run_registry_builder enrichment live
in test_registry_builder.py once that exists; here we only assert that
the Pydantic shape accepts both the legacy (no candidates) and enriched
(with candidates) forms without breaking either.
"""

from __future__ import annotations

import pytest

from api.pipeline.schemas import (
    RefdesCandidate,
    Registry,
    RegistryComponent,
)


def test_legacy_registry_component_has_null_candidates() -> None:
    """A component constructed without refdes_candidates is unaffected."""
    comp = RegistryComponent(canonical_name="U14", kind="ic")
    assert comp.refdes_candidates is None


def test_legacy_registry_json_roundtrip_with_no_candidates() -> None:
    """Existing on-disk registry shape (no `refdes_candidates` key) loads."""
    payload = {
        "schema_version": "1.0",
        "device_label": "demo",
        "components": [
            {
                "canonical_name": "LPC controller",
                "aliases": ["LPC"],
                "kind": "ic",
                "description": "MCU sequencer",
            }
        ],
        "signals": [],
    }
    reg = Registry.model_validate(payload)
    assert reg.components[0].refdes_candidates is None
    # And serializes back: refdes_candidates: null is acceptable in JSON shape.
    serialized = reg.model_dump()
    assert serialized["components"][0]["refdes_candidates"] is None


def test_enriched_registry_accepts_refdes_candidates() -> None:
    payload = {
        "schema_version": "1.0",
        "device_label": "demo",
        "components": [
            {
                "canonical_name": "LPC controller",
                "aliases": ["LPC"],
                "kind": "ic",
                "description": "MCU sequencer",
                "refdes_candidates": [
                    {
                        "refdes": "U14",
                        "confidence": 0.92,
                        "evidence": (
                            "Forum thread cites the LPC as U14 in the rev 2.0 "
                            "schematic; matches the Reform community wiki."
                        ),
                    },
                    {
                        "refdes": "U7",
                        "confidence": 0.4,
                        "evidence": "weaker — alternative LPC reference seen on rev 1.0",
                    },
                ],
            }
        ],
        "signals": [],
    }
    reg = Registry.model_validate(payload)
    cands = reg.components[0].refdes_candidates
    assert cands is not None and len(cands) == 2
    assert cands[0].refdes == "U14"
    assert 0.0 <= cands[0].confidence <= 1.0


def test_refdes_candidate_rejects_empty_evidence() -> None:
    with pytest.raises(ValueError):
        RefdesCandidate(refdes="U14", confidence=0.9, evidence="")


def test_refdes_candidate_rejects_out_of_range_confidence() -> None:
    with pytest.raises(ValueError):
        RefdesCandidate(refdes="U14", confidence=1.7, evidence="ok")


# --- run_registry_builder user-prompt assembly ---------------------------


def test_registry_user_prompt_without_graph_is_legacy_byte_for_byte() -> None:
    """No graph supplied → user prompt equals REGISTRY_USER_TEMPLATE exactly."""
    from api.pipeline.prompts import REGISTRY_USER_TEMPLATE
    from api.pipeline.registry import _build_user_prompt

    actual = _build_user_prompt(
        device_label="MNT Reform motherboard",
        raw_dump="dummy dump body",
        graph=None,
    )
    expected = REGISTRY_USER_TEMPLATE.format(
        device_label="MNT Reform motherboard",
        raw_dump="dummy dump body",
    )
    assert actual == expected


def test_registry_user_prompt_with_graph_appends_targeting_block() -> None:
    """Graph supplied → block with MPN map + rails appended after the dump."""
    from api.pipeline.registry import _build_user_prompt
    from api.pipeline.schematic.schemas import (
        ComponentNode,
        ComponentValue,
        ElectricalGraph,
        PowerRail,
        SchematicQualityReport,
    )

    graph = ElectricalGraph(
        device_slug="demo",
        components={
            "U7": ComponentNode(
                refdes="U7",
                type="ic",
                kind="ic",
                role="buck_regulator",
                value=ComponentValue(raw="LM2677SX-5", mpn="LM2677SX-5"),
            )
        },
        power_rails={
            "+5V": PowerRail(
                label="+5V",
                voltage_nominal=5.0,
                source_refdes="U7",
            )
        },
        boot_sequence=[],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1),
    )

    out = _build_user_prompt(
        device_label="demo",
        raw_dump="some dump",
        graph=graph,
    )
    assert "# Provided ElectricalGraph" in out
    assert "U7: mpn=LM2677SX-5 kind=ic role=buck_regulator" in out
    assert "+5V: voltage=5.00V source=U7" in out
    # Order: dump first (the canonical entities), graph second (targeting).
    assert out.index("some dump") < out.index("# Provided ElectricalGraph")
