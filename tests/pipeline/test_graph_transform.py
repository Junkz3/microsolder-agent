"""Tests for api.pipeline.graph_transform."""

from __future__ import annotations

import json
from pathlib import Path

from api.pipeline.graph_transform import pack_to_graph_payload

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "demo-pack"


def _load(name: str) -> dict:
    return json.loads((FIXTURE_ROOT / name).read_text())


def test_pack_to_graph_returns_expected_shape():
    payload = pack_to_graph_payload(
        registry=_load("registry.json"),
        knowledge_graph=_load("knowledge_graph.json"),
        rules=_load("rules.json"),
        dictionary=_load("dictionary.json"),
    )

    assert set(payload.keys()) == {"nodes", "edges"}

    # Every knowledge_graph node carried over, enriched from dictionary + registry.
    node_ids = {n["id"] for n in payload["nodes"]}
    assert {"cmp_U7", "cmp_C29", "net_3V3"} <= node_ids

    # Symptom nodes are synthesized from rules.symptoms.
    symptom_nodes = [n for n in payload["nodes"] if n["type"] == "symptom"]
    assert len(symptom_nodes) == 2  # "3V3 rail dead" + "device doesn't boot"
    assert all(n["confidence"] >= 0.0 and n["confidence"] <= 1.0 for n in symptom_nodes)

    # Causes edges are synthesized: likely_causes[i].refdes → symptom.
    causes_edges = [e for e in payload["edges"] if e["relation"] == "causes"]
    assert len(causes_edges) >= 2  # C29 + U7 causing each of the 2 symptoms

    # Component nodes carry dictionary metadata under "meta".
    u7 = next(n for n in payload["nodes"] if n["id"] == "cmp_U7")
    assert u7["type"] == "component"
    assert u7["meta"]["package"] == "QFN-24"
    assert u7["label"] == "U7"


def test_empty_pack_returns_empty_graph():
    payload = pack_to_graph_payload(
        registry={"schema_version": "1.0", "device_label": "empty",
                  "components": [], "signals": []},
        knowledge_graph={"schema_version": "1.0", "nodes": [], "edges": []},
        rules={"schema_version": "1.0", "rules": []},
        dictionary={"schema_version": "1.0", "entries": []},
    )
    assert payload == {"nodes": [], "edges": []}
