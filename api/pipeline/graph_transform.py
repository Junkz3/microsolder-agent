"""Transform on-disk pack files (V2 schema) into the graph payload
expected by web/index.html (frontend design v3).

Synthesizes `symptom` nodes from rules.symptoms and `causes` edges from
rules.likely_causes — V2 pipeline only emits component/net nodes natively.
`action` nodes are left empty for now (out of scope for V2; will be added
when the diagnostic agent starts saving recommended actions).
"""

from __future__ import annotations

import re
from typing import Any


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "unknown"


def pack_to_graph_payload(
    *,
    registry: dict[str, Any],
    knowledge_graph: dict[str, Any],
    rules: dict[str, Any],
    dictionary: dict[str, Any],
) -> dict[str, Any]:
    """Merge the four pack files into a single {nodes, edges} payload.

    Returned shape matches what web/index.html's D3 layer expects:
      node: {id, type, label, description, confidence, meta}
      edge: {source, target, relation, label, weight}
    """
    kg_nodes = knowledge_graph.get("nodes", [])
    kg_edges = knowledge_graph.get("edges", [])
    dict_by_name = {e["canonical_name"]: e for e in dictionary.get("entries", [])}
    reg_components = {c["canonical_name"]: c for c in registry.get("components", [])}
    reg_signals = {s["canonical_name"]: s for s in registry.get("signals", [])}

    if not kg_nodes and not rules.get("rules"):
        return {"nodes": [], "edges": []}

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    # 1. Carry component + net nodes from knowledge_graph, enrich from dict/registry.
    for n in kg_nodes:
        kind = n.get("kind")
        if kind not in ("component", "net"):
            continue
        label = n.get("label", "")
        reg = reg_components.get(label) if kind == "component" else reg_signals.get(label)
        dct = dict_by_name.get(label) if kind == "component" else None
        meta: dict[str, Any] = {}
        if dct:
            if dct.get("package"):
                meta["package"] = dct["package"]
            if dct.get("role"):
                meta["role"] = dct["role"]
        if kind == "net" and reg and reg.get("nominal_voltage") is not None:
            meta["nominal"] = f"{reg['nominal_voltage']} V"
        nodes.append(
            {
                "id": n["id"],
                "type": kind,
                "label": label,
                "description": (reg or {}).get("description") or (dct or {}).get("notes") or "",
                "confidence": 0.80 if reg else 0.55,
                "meta": meta,
            }
        )

    # 2. Carry native edges (typed).
    for e in kg_edges:
        edges.append(
            {
                "source": e["source_id"],
                "target": e["target_id"],
                "relation": e["relation"],
                "label": e.get("relation", ""),
                "weight": 1.0,
            }
        )

    # 3. Synthesize symptom nodes + causes edges from rules.
    component_id_by_refdes = {n["label"]: n["id"] for n in nodes if n["type"] == "component"}
    seen_symptoms: dict[str, str] = {}
    for rule in rules.get("rules", []):
        for symptom_text in rule.get("symptoms", []):
            if symptom_text not in seen_symptoms:
                sid = f"sym_{_slug(symptom_text)}"
                seen_symptoms[symptom_text] = sid
                nodes.append(
                    {
                        "id": sid,
                        "type": "symptom",
                        "label": symptom_text,
                        "description": "",
                        "confidence": rule.get("confidence", 0.6),
                        "meta": {},
                    }
                )
            sid = seen_symptoms[symptom_text]
            for cause in rule.get("likely_causes", []):
                cid = component_id_by_refdes.get(cause["refdes"])
                if cid is None:
                    continue  # refdes not in registry → skip (anti-hallucination)
                edges.append(
                    {
                        "source": cid,
                        "target": sid,
                        "relation": "causes",
                        "label": cause.get("mechanism", "causes"),
                        "weight": float(cause.get("probability", 0.5)),
                    }
                )

    return {"nodes": nodes, "edges": edges}
