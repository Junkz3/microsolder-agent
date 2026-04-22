# SPDX-License-Identifier: Apache-2.0
"""`mb_*` custom tools for the hackathon diagnostic agent.

Deliberately simple: prefix-letter closest-matches (no Levenshtein at this
layer — the boardview validator keeps the distance-based version for refdes
typos on a parsed board). Reads straight from disk on every call; caching is
a Phase-D concern.

mb_record_finding and mb_list_findings power cross-session memory: every
confirmed repair becomes a field report on disk, and the next session on the
same device can surface prior learnings without depending on the MA memory
store research preview.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from api.agent.field_reports import list_field_reports, record_field_report


def _load_pack(slug: str, memory_root: Path) -> dict[str, Any]:
    pack_dir = memory_root / slug
    return {
        "registry": json.loads((pack_dir / "registry.json").read_text()),
        "dictionary": json.loads((pack_dir / "dictionary.json").read_text()),
        "rules": json.loads((pack_dir / "rules.json").read_text()),
    }


def mb_get_component(
    *, device_slug: str, refdes: str, memory_root: Path
) -> dict[str, Any]:
    """Return component info, or `{found: False, closest_matches: [...]}`.

    Never fabricates data: if the refdes is unknown, the tool returns the
    structured not-found payload and lets the agent choose (ask the user,
    pick one of `closest_matches`, etc.).
    """
    pack = _load_pack(device_slug, memory_root)
    reg_comp = {c["canonical_name"]: c for c in pack["registry"].get("components", [])}
    dct_comp = {e["canonical_name"]: e for e in pack["dictionary"].get("entries", [])}

    if refdes in reg_comp:
        dct = dct_comp.get(refdes, {})
        reg = reg_comp[refdes]
        return {
            "found": True,
            "canonical_name": refdes,
            "aliases": reg.get("aliases", []),
            "kind": reg.get("kind", "unknown"),
            "role": dct.get("role"),
            "package": dct.get("package"),
            "typical_failure_modes": dct.get("typical_failure_modes", []),
            "description": reg.get("description", ""),
        }

    prefix = refdes[0].upper() if refdes else ""
    candidates = sorted(c for c in reg_comp if prefix and c.startswith(prefix))
    return {
        "found": False,
        "error": "not_found",
        "queried_refdes": refdes,
        "closest_matches": candidates[:5],
        "hint": f"No refdes {refdes!r} in the registry for {device_slug!r}.",
    }


def mb_get_rules_for_symptoms(
    *,
    device_slug: str,
    symptoms: list[str],
    memory_root: Path,
    max_results: int = 5,
) -> dict[str, Any]:
    """Return rules whose symptoms overlap the query, ranked by overlap + confidence."""
    pack = _load_pack(device_slug, memory_root)
    qset = {s.lower() for s in symptoms}
    matches: list[dict[str, Any]] = []
    for rule in pack["rules"].get("rules", []):
        rset = {s.lower() for s in rule.get("symptoms", [])}
        overlap = qset & rset
        if not overlap:
            continue
        matches.append(
            {
                "rule_id": rule["id"],
                "overlap_count": len(overlap),
                "symptoms_matched": sorted(overlap),
                "likely_causes": rule.get("likely_causes", []),
                "diagnostic_steps": rule.get("diagnostic_steps", []),
                "confidence": rule.get("confidence", 0.5),
                "sources": rule.get("sources", []),
            }
        )
    matches.sort(key=lambda m: (m["overlap_count"], m["confidence"]), reverse=True)
    return {
        "device_slug": device_slug,
        "query_symptoms": symptoms,
        "matches": matches[: max(max_results, 0)],
        "total_available_rules": len(pack["rules"].get("rules", [])),
    }


async def mb_record_finding(
    *,
    client,  # AsyncAnthropic | None — typed loose to keep this import-light
    device_slug: str,
    refdes: str,
    symptom: str,
    confirmed_cause: str,
    memory_root: Path,
    mechanism: str | None = None,
    notes: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Persist a confirmed repair finding for cross-session learning.

    JSON-first write to `memory/{slug}/field_reports/*.md`. When the MA
    memory_stores flag is on, the same content is mirrored to the device's
    memory store so native `memory_search` can surface it too.
    """
    return await record_field_report(
        client=client,
        device_slug=device_slug,
        refdes=refdes,
        symptom=symptom,
        confirmed_cause=confirmed_cause,
        mechanism=mechanism,
        notes=notes,
        session_id=session_id,
        memory_root=memory_root,
    )


def mb_list_findings(
    *,
    device_slug: str,
    memory_root: Path,
    limit: int = 20,
    filter_refdes: str | None = None,
) -> dict[str, Any]:
    """Return prior confirmed findings for this device, newest first."""
    reports = list_field_reports(
        device_slug=device_slug,
        memory_root=memory_root,
        limit=limit,
        filter_refdes=filter_refdes,
    )
    return {
        "device_slug": device_slug,
        "count": len(reports),
        "filter_refdes": filter_refdes,
        "reports": reports,
    }
