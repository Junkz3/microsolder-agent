"""Pack-level read endpoints — listings, summaries, taxonomy, graph payload.

Also hosts the shared on-disk presence helpers (`_find_boardview`,
`_detect_boardview`, `_detect_schematic_pdf`, `_pack_is_complete`,
`_read_optional_json`) reused by the documents/repairs/schematic route
modules. Keeping them here avoids a circular dependency: documents.py
imports `_find_boardview`, repairs.py imports `_pack_is_complete`, and
neither of those modules has a more natural home for the helper than the
"pack composition" concern owned by this file.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException

import api.pipeline as _pkg  # noqa: PLC0415 — module-attribute lookups for patchability
from api.agent.field_reports import list_field_reports
from api.pipeline import sources
from api.pipeline.graph_transform import pack_to_graph_payload
from api.pipeline.models import (
    ExpandRequest,
    PackSummary,
    TaxonomyPackEntry,
    TaxonomyTree,
)
from api.pipeline.orchestrator import _slugify
from api.pipeline.routes._helpers import _validate_slug

logger = logging.getLogger("wrench_board.pipeline.api")

router = APIRouter()


# Boardview parsers — the dispatch registry is the source of truth, but we
# materialise the supported extension list once for filesystem scans (the
# registry is keyed on extension, not on file path glob).
_BOARDVIEW_EXTENSIONS = (
    ".kicad_pcb",
    ".brd",
    ".brd2",
    ".asc",
    ".bdv",
    ".bv",
    ".cad",
    ".cst",
    ".f2b",
    ".fz",
    ".gr",
    ".tvw",
)


def _find_boardview(slug: str, pack_dir: Path) -> Path | None:
    """Return the absolute path of the active boardview for this slug, or None.

    Lookup order — same priority chain as `_detect_boardview`:
        1. The active pin from `active_sources.json` (if present).
        2. `board_assets/{slug}.<ext>` — canonical, in-repo demo boards.
        3. `memory/{slug}/uploads/*-boardview-*` — technician-uploaded
           (alphabetical first match).
    Used by both `_detect_boardview` (for the on-disk presence bitmask in
    `PackSummary`) and by `GET /pipeline/packs/{slug}/boardview` (which
    needs the actual path to stream the file).
    """
    pinned = sources.resolve_path(pack_dir, sources.BOARDVIEW_KIND)
    if pinned is not None:
        return pinned

    assets_root = Path.cwd() / "board_assets"
    for ext in _BOARDVIEW_EXTENSIONS:
        candidate = assets_root / f"{slug}{ext}"
        if candidate.exists() and candidate.is_file():
            return candidate

    uploads_dir = pack_dir / "uploads"
    if uploads_dir.exists():
        for path in sorted(uploads_dir.iterdir()):
            if not path.is_file():
                continue
            if "-boardview-" not in path.name:
                continue
            return path
    return None


def _detect_boardview(slug: str, pack_dir: Path) -> tuple[bool, str | None]:
    """Return (present, extension) for a slug's boardview — bitmask helper.

    Returns the dotted extension (e.g. ".kicad_pcb") so the UI can label
    the format on the boardview card.
    """
    path = _find_boardview(slug, pack_dir)
    if path is None:
        return False, None
    return True, path.suffix.lower() or None


def _detect_schematic_pdf(slug: str, pack_dir: Path) -> bool:
    """True when a source schematic PDF exists for this slug.

    Order:
      1. Active pin from `active_sources.json`.
      2. `memory/{slug}/schematic.pdf` (canonical post-ingest copy).
      3. `board_assets/{slug}.pdf`.
      4. Any technician-uploaded `*-schematic_pdf-*`.
    """
    if sources.resolve_path(pack_dir, sources.SCHEMATIC_KIND) is not None:
        return True
    if (pack_dir / "schematic.pdf").exists():
        return True
    if (Path.cwd() / "board_assets" / f"{slug}.pdf").exists():
        return True
    uploads_dir = pack_dir / "uploads"
    if uploads_dir.exists():
        for path in uploads_dir.iterdir():
            if path.is_file() and "-schematic_pdf-" in path.name:
                return True
    return False


def _summarize_pack(pack_dir: Path) -> PackSummary:
    slug = pack_dir.name
    bv_present, bv_ext = _detect_boardview(slug, pack_dir)
    return PackSummary(
        device_slug=slug,
        disk_path=str(pack_dir),
        has_raw_dump=(pack_dir / "raw_research_dump.md").exists(),
        has_registry=(pack_dir / "registry.json").exists(),
        has_knowledge_graph=(pack_dir / "knowledge_graph.json").exists(),
        has_rules=(pack_dir / "rules.json").exists(),
        has_dictionary=(pack_dir / "dictionary.json").exists(),
        has_audit_verdict=(pack_dir / "audit_verdict.json").exists(),
        has_boardview=bv_present,
        boardview_format=bv_ext,
        has_schematic_pdf=_detect_schematic_pdf(slug, pack_dir),
        has_electrical_graph=(pack_dir / "electrical_graph.json").exists(),
        has_parts_index=(pack_dir / "parts_index.json").exists(),
    )


def _read_optional_json(path: Path) -> dict | None:
    """Return the parsed JSON at path, or None if the file is absent.

    Raises HTTPException(422) if the file exists but is not valid JSON.
    """
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid JSON in {path.name}: {exc}",
        ) from exc


def _pack_is_complete(pack_dir: Path) -> bool:
    """A pack is 'complete' when the 4 writer files are present — audit is optional."""
    return all(
        (pack_dir / name).exists()
        for name in ("registry.json", "knowledge_graph.json", "rules.json", "dictionary.json")
    )


@router.get("/packs", response_model=list[PackSummary])
async def list_packs() -> list[PackSummary]:
    settings = _pkg.get_settings()
    root = Path(settings.memory_root)
    if not root.exists():
        return []
    return sorted(
        (_summarize_pack(d) for d in root.iterdir() if d.is_dir()),
        key=lambda s: s.device_slug,
    )


@router.get("/taxonomy", response_model=TaxonomyTree)
async def get_taxonomy() -> TaxonomyTree:
    """Scan every pack's registry.json and group by taxonomy.

    A pack lands in `brands[brand][model]` when both `taxonomy.brand` and
    `taxonomy.model` are present; otherwise it falls to `uncategorized`. The UI
    uses this to populate the 'New repair' modal's accordion by manufacturer
    and the home section headers.
    """
    settings = _pkg.get_settings()
    root = Path(settings.memory_root)
    tree = TaxonomyTree()
    if not root.exists():
        return tree

    for pack_dir in sorted(root.iterdir(), key=lambda p: p.name):
        if not pack_dir.is_dir():
            continue
        registry = _read_optional_json(pack_dir / "registry.json")
        if registry is None:
            continue

        taxonomy = registry.get("taxonomy") or {}
        brand = taxonomy.get("brand")
        model = taxonomy.get("model")

        entry = TaxonomyPackEntry(
            device_slug=pack_dir.name,
            device_label=registry.get("device_label") or pack_dir.name,
            version=taxonomy.get("version"),
            form_factor=taxonomy.get("form_factor"),
            complete=_pack_is_complete(pack_dir),
        )

        if brand and model:
            tree.brands.setdefault(brand, {}).setdefault(model, []).append(entry)
        else:
            tree.uncategorized.append(entry)

    return tree


@router.get("/packs/{device_slug}", response_model=PackSummary)
async def get_pack(device_slug: str) -> PackSummary:
    settings = _pkg.get_settings()
    root = Path(settings.memory_root)
    # Normalize: accept either a raw slug or a device_label.
    slug = _slugify(device_slug)
    pack_dir = root / slug
    if not pack_dir.exists():
        raise HTTPException(status_code=404, detail=f"No pack for device_slug={slug!r}")
    return _summarize_pack(pack_dir)


@router.get("/packs/{device_slug}/full")
async def get_pack_full(device_slug: str) -> dict:
    """Return every JSON artefact of a pack in a single payload.

    Missing files become `null` — never fabricated (hard rule #4). Consumed by
    the Memory Bank UI so it can render all five sections in one fetch.
    """
    settings = _pkg.get_settings()
    slug = _slugify(device_slug)
    pack_dir = Path(settings.memory_root) / slug
    if not pack_dir.exists():
        raise HTTPException(status_code=404, detail=f"No pack for device_slug={slug!r}")

    registry = _read_optional_json(pack_dir / "registry.json")
    knowledge_graph = _read_optional_json(pack_dir / "knowledge_graph.json")
    rules = _read_optional_json(pack_dir / "rules.json")
    dictionary = _read_optional_json(pack_dir / "dictionary.json")
    audit_verdict = _read_optional_json(pack_dir / "audit_verdict.json")

    device_label = (registry or {}).get("device_label") or slug

    return {
        "device_slug": slug,
        "device_label": device_label,
        "registry": registry,
        "knowledge_graph": knowledge_graph,
        "rules": rules,
        "dictionary": dictionary,
        "audit_verdict": audit_verdict,
    }


@router.get("/packs/{device_slug}/findings")
async def list_device_findings(device_slug: str, limit: int = 50) -> list[dict]:
    """Return every field report recorded for this device, newest first.

    Same content the agent reads via grep on the FUSE mount, exposed to
    the web UI so the Journal dashboard can render cross-session memory
    without a WS round-trip. Strictly JSON-on-disk — no MA memory-store.
    """
    return list_field_reports(device_slug=_validate_slug(device_slug), limit=limit)


@router.post("/packs/{device_slug}/expand")
async def expand_device_pack(device_slug: str, request: ExpandRequest) -> dict:
    """Grow an existing pack's memory bank around a focus symptom area.

    Called by the diagnostic agent via the `mb_expand_knowledge` tool when
    the current ruleset comes up empty for a live symptom. Runs a targeted
    Scout + Registry + Clinicien mini-pipeline and merges the output into
    the existing pack. See api/pipeline/expansion.py for the mechanics.
    """
    slug = _slugify(device_slug)
    logger.info(
        "[API] /packs/%s/expand · focus=%s · refdes=%s",
        slug,
        request.focus_symptoms,
        request.focus_refdes,
    )
    try:
        return await _pkg.expand_pack(
            device_slug=slug,
            focus_symptoms=request.focus_symptoms,
            focus_refdes=request.focus_refdes,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/packs/{device_slug}/graph")
async def get_pack_graph(device_slug: str) -> dict:
    """Return the combined graph payload ({nodes, edges}) consumed by web/index.html."""
    settings = _pkg.get_settings()
    slug = _slugify(device_slug)
    pack_dir = Path(settings.memory_root) / slug
    if not pack_dir.exists():
        raise HTTPException(status_code=404, detail=f"No pack for device_slug={slug!r}")

    try:
        registry = json.loads((pack_dir / "registry.json").read_text())
        knowledge_graph = json.loads((pack_dir / "knowledge_graph.json").read_text())
        rules = json.loads((pack_dir / "rules.json").read_text())
        dictionary = json.loads((pack_dir / "dictionary.json").read_text())
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Pack for {slug!r} is incomplete: {exc.filename}",
        ) from exc

    return pack_to_graph_payload(
        registry=registry,
        knowledge_graph=knowledge_graph,
        rules=rules,
        dictionary=dictionary,
    )
