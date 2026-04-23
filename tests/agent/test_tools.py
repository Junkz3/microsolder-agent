# SPDX-License-Identifier: Apache-2.0
"""Tests for api.agent.tools (the 2 mb_* tools exposed in v1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from api.agent.tools import mb_get_component, mb_get_rules_for_symptoms

FIXTURE_DIR = Path(__file__).parent.parent / "pipeline" / "fixtures" / "demo-pack"


@pytest.fixture
def seeded_memory_root(tmp_path):
    dest = tmp_path / "demo-pi"
    dest.mkdir()
    for name in ("registry.json", "dictionary.json", "knowledge_graph.json", "rules.json"):
        (dest / name).write_text((FIXTURE_DIR / name).read_text())
    return tmp_path


def test_mb_get_component_found(seeded_memory_root):
    result = mb_get_component(
        device_slug="demo-pi", refdes="U7", memory_root=seeded_memory_root,
    )
    assert result["found"] is True
    assert result["canonical_name"] == "U7"
    assert result["memory_bank"] is not None
    assert result["memory_bank"]["role"] == "PMIC"
    assert result["memory_bank"]["package"] == "QFN-24"
    assert result["memory_bank"]["kind"] == "pmic"
    assert result["board"] is None  # no session passed


def test_mb_get_component_not_found_suggests_closest(seeded_memory_root):
    result = mb_get_component(
        device_slug="demo-pi", refdes="U999", memory_root=seeded_memory_root,
    )
    assert result["found"] is False
    assert result["error"] == "not_found"
    assert "closest_matches" in result
    assert "U7" in result["closest_matches"]
    assert "memory_bank" not in result
    assert "board" not in result


def test_mb_get_component_empty_refdes_returns_not_found(seeded_memory_root):
    result = mb_get_component(
        device_slug="demo-pi", refdes="", memory_root=seeded_memory_root,
    )
    assert result["found"] is False
    assert result["error"] == "not_found"


def test_mb_get_rules_for_symptoms_returns_matches(seeded_memory_root):
    result = mb_get_rules_for_symptoms(
        device_slug="demo-pi",
        symptoms=["3V3 rail dead"],
        memory_root=seeded_memory_root,
    )
    assert isinstance(result["matches"], list)
    assert len(result["matches"]) >= 1
    assert result["matches"][0]["rule_id"] == "rule-demo-001"
    assert result["matches"][0]["overlap_count"] == 1
    assert result["matches"][0]["confidence"] == 0.82
    assert result["total_available_rules"] == 1


def test_mb_get_rules_for_symptoms_case_insensitive(seeded_memory_root):
    result = mb_get_rules_for_symptoms(
        device_slug="demo-pi",
        symptoms=["3V3 RAIL DEAD"],
        memory_root=seeded_memory_root,
    )
    assert len(result["matches"]) == 1


def test_mb_get_rules_for_symptoms_no_overlap_empty(seeded_memory_root):
    result = mb_get_rules_for_symptoms(
        device_slug="demo-pi",
        symptoms=["completely unrelated symptom"],
        memory_root=seeded_memory_root,
    )
    assert result["matches"] == []
    assert result["total_available_rules"] == 1


def test_mb_get_rules_for_symptoms_max_results(seeded_memory_root):
    result = mb_get_rules_for_symptoms(
        device_slug="demo-pi",
        symptoms=["3V3 rail dead", "device doesn't boot"],
        memory_root=seeded_memory_root,
        max_results=0,
    )
    assert result["matches"] == []
