"""Tests for the `expand_pack` targeted mini-pipeline.

The goal is to verify the orchestration + merge logic, not re-test the
LLM-facing calls. We mock `_run_targeted_scout`, `run_registry_builder`,
and the Clinicien helper so the test runs offline.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from api import config as config_mod
from api.pipeline import expansion
from api.pipeline.schemas import (
    Cause,
    DeviceTaxonomy,
    Registry,
    RegistryComponent,
    RegistrySignal,
    Rule,
    RulesSet,
)


@pytest.fixture(autouse=True)
def reset_settings(monkeypatch):
    monkeypatch.setattr(config_mod, "_settings", None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    yield
    monkeypatch.setattr(config_mod, "_settings", None)


def _seed_pack(tmp_path: Path, slug: str) -> Path:
    pack = tmp_path / slug
    pack.mkdir()
    (pack / "raw_research_dump.md").write_text(
        "# Research Dump — test device\n\n## Device overview\ntest\n",
        encoding="utf-8",
    )
    seed_registry = Registry(
        device_label="Test Device",
        taxonomy=DeviceTaxonomy(brand="TestCo", model="Thing"),
        components=[RegistryComponent(canonical_name="U1", kind="ic")],
        signals=[RegistrySignal(canonical_name="VCC", kind="power_rail")],
    )
    (pack / "registry.json").write_text(seed_registry.model_dump_json(indent=2), encoding="utf-8")
    seed_rules = RulesSet(rules=[
        Rule(
            id="rule-existing-001",
            symptoms=["prior symptom"],
            likely_causes=[Cause(refdes="U1", probability=0.5, mechanism="short")],
            confidence=0.6,
        ),
    ])
    (pack / "rules.json").write_text(seed_rules.model_dump_json(indent=2), encoding="utf-8")
    return pack


async def test_expand_pack_merges_new_components_and_rules(tmp_path, monkeypatch):
    pack = _seed_pack(tmp_path, "test-device")

    async def fake_scout(**_kwargs):
        return "\n\n- **Symptom:** no sound\n  - **Components mentioned:** U3101\n"

    expanded_registry = Registry(
        device_label="Test Device",
        taxonomy=DeviceTaxonomy(brand="TestCo", model="Thing"),
        components=[
            RegistryComponent(canonical_name="U1", kind="ic"),
            RegistryComponent(canonical_name="U3101", kind="ic"),
        ],
        signals=[
            RegistrySignal(canonical_name="VCC", kind="power_rail"),
            RegistrySignal(canonical_name="VCC_AUDIO", kind="power_rail"),
        ],
    )

    expanded_rules = RulesSet(rules=[
        Rule(
            id="rule-existing-001",
            symptoms=["prior symptom"],
            likely_causes=[Cause(refdes="U1", probability=0.5, mechanism="short")],
            confidence=0.6,
        ),
        Rule(
            id="rule-audio-002",
            symptoms=["no sound"],
            likely_causes=[
                Cause(refdes="U3101", probability=0.7, mechanism="cold joint")
            ],
            confidence=0.8,
        ),
    ])

    monkeypatch.setattr(expansion, "_run_targeted_scout", fake_scout)
    with patch(
        "api.pipeline.expansion.run_registry_builder",
        new=AsyncMock(return_value=expanded_registry),
    ), patch(
        "api.pipeline.expansion._run_clinicien_on_full_dump",
        new=AsyncMock(return_value=expanded_rules),
    ):
        summary = await expansion.expand_pack(
            device_slug="test-device",
            focus_symptoms=["no sound"],
            focus_refdes=["U3101"],
            client=object(),  # unused — both helpers are mocked
            memory_root=tmp_path,
        )

    assert summary["expanded"] is True
    assert summary["new_rules_count"] == 1       # rule-audio-002
    assert summary["new_components_count"] == 1  # U3101
    assert summary["new_signals_count"] == 1     # VCC_AUDIO
    assert summary["total_rules_after"] == 2
    assert summary["dump_bytes_added"] > 0

    # Disk reflects the expansion.
    updated_registry = Registry.model_validate_json((pack / "registry.json").read_text())
    assert {c.canonical_name for c in updated_registry.components} == {"U1", "U3101"}
    # Taxonomy was preserved.
    assert updated_registry.taxonomy.brand == "TestCo"

    updated_rules = RulesSet.model_validate_json((pack / "rules.json").read_text())
    assert {r.id for r in updated_rules.rules} == {"rule-existing-001", "rule-audio-002"}

    # raw_research_dump.md carries the cumulative footprint.
    dump = (pack / "raw_research_dump.md").read_text()
    assert "no sound" in dump
    assert "Expansion" in dump  # separator header added


async def test_expand_pack_rejects_missing_pack(tmp_path):
    with pytest.raises(RuntimeError, match="no pack"):
        await expansion.expand_pack(
            device_slug="does-not-exist",
            focus_symptoms=["anything"],
            memory_root=tmp_path,
        )


async def test_expand_pack_rejects_empty_focus(tmp_path):
    _seed_pack(tmp_path, "test-device")
    with pytest.raises(RuntimeError, match="at least one focus symptom"):
        await expansion.expand_pack(
            device_slug="test-device",
            focus_symptoms=[],
            memory_root=tmp_path,
        )


async def test_expand_pack_preserves_taxonomy_when_regenerate_returns_blank(
    tmp_path, monkeypatch
):
    """If the re-run Registry produces an empty taxonomy (single-symptom focus
    can starve the brand signal), keep the pre-existing one instead of
    clobbering it.
    """
    pack = _seed_pack(tmp_path, "test-device")

    async def fake_scout(**_kwargs):
        return "\n\n- narrow scope chunk\n"

    blank_tax_registry = Registry(
        device_label="Test Device",
        taxonomy=DeviceTaxonomy(),  # all null
        components=[RegistryComponent(canonical_name="U1", kind="ic")],
        signals=[RegistrySignal(canonical_name="VCC", kind="power_rail")],
    )
    same_rules = RulesSet(rules=[
        Rule(
            id="rule-existing-001",
            symptoms=["prior symptom"],
            likely_causes=[Cause(refdes="U1", probability=0.5, mechanism="short")],
            confidence=0.6,
        ),
    ])

    monkeypatch.setattr(expansion, "_run_targeted_scout", fake_scout)
    with patch(
        "api.pipeline.expansion.run_registry_builder",
        new=AsyncMock(return_value=blank_tax_registry),
    ), patch(
        "api.pipeline.expansion._run_clinicien_on_full_dump",
        new=AsyncMock(return_value=same_rules),
    ):
        await expansion.expand_pack(
            device_slug="test-device",
            focus_symptoms=["narrow thing"],
            client=object(),
            memory_root=tmp_path,
        )

    saved = json.loads((pack / "registry.json").read_text())
    assert saved["taxonomy"]["brand"] == "TestCo"
    assert saved["taxonomy"]["model"] == "Thing"
