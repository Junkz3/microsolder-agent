"""Unit tests for compute_drift — the Python set-diff that replaced the
LLM Auditor's vocabulary check.
"""

from __future__ import annotations

from api.pipeline.drift import compute_drift
from api.pipeline.schemas import (
    Cause,
    ComponentSheet,
    Dictionary,
    KnowledgeEdge,
    KnowledgeGraph,
    KnowledgeNode,
    Registry,
    RegistryComponent,
    RegistrySignal,
    Rule,
    RulesSet,
)


def _base_registry() -> Registry:
    return Registry(
        device_label="Demo",
        components=[
            RegistryComponent(canonical_name="U7", kind="pmic"),
            RegistryComponent(canonical_name="C29", kind="capacitor"),
        ],
        signals=[RegistrySignal(canonical_name="3V3_RAIL", kind="power_rail")],
    )


def test_drift_empty_when_everything_matches():
    registry = _base_registry()
    kg = KnowledgeGraph(
        nodes=[
            KnowledgeNode(id="comp:U7", kind="component", label="PMIC"),
            KnowledgeNode(id="net:3V3_RAIL", kind="net", label="3V3 rail"),
            KnowledgeNode(id="sym:3v3-dead", kind="symptom", label="3V3 dead"),
        ],
        edges=[KnowledgeEdge(source_id="comp:U7", target_id="net:3V3_RAIL", relation="powers")],
    )
    rules = RulesSet(
        rules=[
            Rule(
                id="r1",
                symptoms=["3V3 dead"],
                likely_causes=[Cause(refdes="U7", probability=0.8, mechanism="short")],
                confidence=0.8,
            )
        ]
    )
    dictionary = Dictionary(entries=[ComponentSheet(canonical_name="U7")])

    assert compute_drift(
        registry=registry, knowledge_graph=kg, rules=rules, dictionary=dictionary
    ) == []


def test_drift_detects_unknown_component_in_graph():
    registry = _base_registry()
    kg = KnowledgeGraph(
        nodes=[KnowledgeNode(id="comp:U99", kind="component", label="Mystery")],
        edges=[],
    )
    rules = RulesSet(rules=[])
    dictionary = Dictionary(entries=[])

    drift = compute_drift(
        registry=registry, knowledge_graph=kg, rules=rules, dictionary=dictionary
    )
    assert len(drift) == 1
    assert drift[0].file == "knowledge_graph"
    assert drift[0].mentions == ["comp:U99"]


def test_drift_detects_unknown_net_in_graph():
    registry = _base_registry()
    kg = KnowledgeGraph(
        nodes=[KnowledgeNode(id="net:1V8_UNREGISTERED", kind="net", label="1.8V")],
        edges=[],
    )
    drift = compute_drift(
        registry=registry,
        knowledge_graph=kg,
        rules=RulesSet(rules=[]),
        dictionary=Dictionary(entries=[]),
    )
    assert len(drift) == 1
    assert drift[0].file == "knowledge_graph"
    assert drift[0].mentions == ["net:1V8_UNREGISTERED"]


def test_drift_detects_unknown_cause_refdes():
    registry = _base_registry()
    rules = RulesSet(
        rules=[
            Rule(
                id="r1",
                symptoms=["boot loop"],
                likely_causes=[
                    Cause(refdes="U7", probability=0.5, mechanism="brownout"),
                    Cause(refdes="Q42", probability=0.3, mechanism="short"),
                ],
                confidence=0.6,
            )
        ]
    )
    drift = compute_drift(
        registry=registry,
        knowledge_graph=KnowledgeGraph(nodes=[], edges=[]),
        rules=rules,
        dictionary=Dictionary(entries=[]),
    )
    assert len(drift) == 1
    assert drift[0].file == "rules"
    assert drift[0].mentions == ["Q42"]


def test_drift_detects_unknown_dictionary_entry():
    registry = _base_registry()
    dictionary = Dictionary(entries=[ComponentSheet(canonical_name="U7"), ComponentSheet(canonical_name="Z1")])
    drift = compute_drift(
        registry=registry,
        knowledge_graph=KnowledgeGraph(nodes=[], edges=[]),
        rules=RulesSet(rules=[]),
        dictionary=dictionary,
    )
    assert len(drift) == 1
    assert drift[0].file == "dictionary"
    assert drift[0].mentions == ["Z1"]


def test_drift_dedups_repeated_mentions():
    registry = _base_registry()
    kg = KnowledgeGraph(
        nodes=[
            KnowledgeNode(id="comp:U99", kind="component", label="a"),
            KnowledgeNode(id="comp:U99", kind="component", label="b"),
        ],
        edges=[],
    )
    drift = compute_drift(
        registry=registry,
        knowledge_graph=kg,
        rules=RulesSet(rules=[]),
        dictionary=Dictionary(entries=[]),
    )
    assert drift[0].mentions == ["comp:U99"]


def test_drift_ignores_symptom_nodes():
    registry = _base_registry()
    kg = KnowledgeGraph(
        nodes=[KnowledgeNode(id="sym:anything-goes", kind="symptom", label="x")],
        edges=[],
    )
    drift = compute_drift(
        registry=registry,
        knowledge_graph=kg,
        rules=RulesSet(rules=[]),
        dictionary=Dictionary(entries=[]),
    )
    assert drift == []
