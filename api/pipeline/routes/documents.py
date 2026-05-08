"""Technician-supplied document uploads + active-source pin management.

Endpoints:
  POST /packs/{slug}/documents          — upload a file under uploads/
  GET  /packs/{slug}/documents          — list uploaded files
  GET  /packs/{slug}/sources            — list versioned sources + active pin
  PUT  /packs/{slug}/sources/{kind}     — switch the active pin
  GET/HEAD /packs/{slug}/boardview      — serve the active boardview file
  GET/HEAD /packs/{slug}/schematic.pdf  — serve the active schematic PDF

The auto-pin logic and `_apply_schematic_pin` cache-vs-reingest helper
are local to this module — they're shared by the POST /documents
auto-pin path and the explicit PUT /sources/{kind} switch.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from anthropic import AsyncAnthropic
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

import api.pipeline as _pkg  # noqa: PLC0415 — module-attribute lookups for patchability
from api.pipeline import sources
from api.pipeline.models import (
    DocumentUploadResponse,
    SourceKindEntry,
    SourcesResponse,
    SourceVersion,
    SwitchSourceRequest,
    SwitchSourceResponse,
)
from api.pipeline.orchestrator import _slugify
from api.pipeline.routes._helpers import _validate_slug
from api.pipeline.routes.packs import _find_boardview

logger = logging.getLogger("wrench_board.pipeline.api")

router = APIRouter()


# ======================================================================
# Technician-supplied document uploads — feeds Scout / Registry enrichment
# ======================================================================


_UPLOAD_KINDS = {"schematic_pdf", "boardview", "datasheet", "notes", "other"}
# Defense in depth — clamp the upload size at a sane ceiling so a 1 GB
# blob doesn't fill /tmp during a multipart parse. 50 MB is enough for
# any schematic PDF or datasheet we've seen in the wild.
_MAX_UPLOAD_BYTES = 50 * 1024 * 1024


def _safe_filename(name: str) -> str:
    """Return a path-segment-safe version of `name`.

    Strips directory components, control characters, and leading dots so
    nothing the technician uploads can escape `memory/{slug}/uploads/`.
    """
    base = Path(name).name  # drop any directory traversal
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", base).strip("-_.")
    return cleaned or "upload"


@router.post(
    "/packs/{device_slug}/documents",
    response_model=DocumentUploadResponse,
    status_code=201,
)
async def post_pack_document(
    device_slug: str,
    kind: str = Form(...),
    description: str | None = Form(default=None),
    file: UploadFile = File(...),  # noqa: B008 — FastAPI dependency-injection idiom
) -> DocumentUploadResponse:
    """Persist a technician-supplied document under `memory/{slug}/uploads/`.

    Triggers no processing — the orchestrator picks the file up on the
    next `POST /pipeline/generate` (or `/pipeline/repairs`) call. The
    `kind` decides how the file is consumed downstream:
    `schematic_pdf` triggers an inline `ingest_schematic` if the device
    has no `electrical_graph.json` yet; `boardview` is parsed into a
    `Board`; `datasheet` is listed for Scout to cite via `local://`;
    `notes` and `other` are stored but not fed into prompts.
    """
    slug = _validate_slug(device_slug)
    if kind not in _UPLOAD_KINDS:
        raise HTTPException(
            status_code=422,
            detail=f"unknown kind={kind!r} — allowed: {sorted(_UPLOAD_KINDS)}",
        )

    settings = _pkg.get_settings()
    uploads_dir = Path(settings.memory_root) / slug / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    filename = _safe_filename(file.filename or "upload")
    target = uploads_dir / f"{timestamp}-{kind}-{filename}"

    # Stream the upload to disk in chunks so we never hold the entire
    # blob in memory and we can abort cleanly on the size cap.
    total = 0
    try:
        with target.open("wb") as fh:
            while True:
                chunk = await file.read(1 << 20)  # 1 MiB
                if not chunk:
                    break
                total += len(chunk)
                if total > _MAX_UPLOAD_BYTES:
                    fh.close()
                    target.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=413,
                        detail=f"upload exceeds {_MAX_UPLOAD_BYTES // (1024 * 1024)} MiB cap",
                    )
                fh.write(chunk)
    except HTTPException:
        raise
    except OSError as exc:
        target.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"could not persist upload: {exc}") from exc
    finally:
        await file.close()

    if description:
        # Best-effort breadcrumb — failures don't fail the upload.
        try:
            (uploads_dir / f"{target.name}.description.txt").write_text(
                description.strip(), encoding="utf-8"
            )
        except OSError:
            logger.warning(
                "could not persist description sidecar for %s",
                target,
                exc_info=True,
            )

    logger.info(
        "[API] /pipeline/packs/%s/documents · kind=%s file=%s bytes=%d",
        slug,
        kind,
        target.name,
        total,
    )

    # Versioning glue. Two responsibilities:
    #   1. Archive the legacy schematic.pdf (one that pre-dates this system)
    #      into uploads/ + cache, so the technician can switch back to it.
    #   2. Auto-pin: the FIRST upload of a kind becomes active. We then
    #      materialise the pin (cache hit or kick off ingestion). Without
    #      step 2, the pin would point to a file that doesn't match the
    #      derived artefacts on disk — incoherent.
    if kind in sources.KNOWN_KINDS:
        pack_dir = uploads_dir.parent
        if kind == sources.SCHEMATIC_KIND:
            # MUST run before reading existing uploads — once we've added
            # the new file, list_uploads_for_kind would no longer be empty.
            _archive_legacy_schematic_if_present(pack_dir)
        pins = sources.read_active(pack_dir)
        if not pins.get(kind):
            pins[kind] = target.name
            sources.write_active(pack_dir, pins)
            if kind == sources.SCHEMATIC_KIND:
                # Materialise the pin we just wrote. Cache hit (rare here
                # since this is a brand-new upload) → instant; cache miss
                # → background ingestion. Either way, schematic.pdf on
                # disk now matches the active pin.
                try:
                    _apply_schematic_pin(slug, pack_dir, target.name)
                except OSError:
                    logger.warning(
                        "could not materialise schematic pin for %s",
                        target.name,
                        exc_info=True,
                    )

    return DocumentUploadResponse(
        device_slug=slug,
        kind=kind,
        stored_path=str(target),
        filename=filename,
        size_bytes=total,
    )


@router.get("/packs/{device_slug}/documents")
async def list_pack_documents(device_slug: str) -> dict:
    """List every upload persisted for this pack, grouped by kind."""
    slug = _validate_slug(device_slug)
    settings = _pkg.get_settings()
    uploads_dir = Path(settings.memory_root) / slug / "uploads"
    if not uploads_dir.exists():
        return {"device_slug": slug, "uploads": []}

    items: list[dict] = []
    for path in sorted(uploads_dir.iterdir()):
        if not path.is_file() or path.name.endswith(".description.txt"):
            continue
        match = re.match(r"^(?P<ts>[^-]+(?:-[^-]+)*?)-(?P<kind>[a-z_]+)-(?P<filename>.+)$", path.name)
        if match is None:
            kind = "other"
            timestamp = ""
            original = path.name
        else:
            kind = match.group("kind")
            timestamp = match.group("ts")
            original = match.group("filename")
        sidecar = uploads_dir / f"{path.name}.description.txt"
        description = (
            sidecar.read_text(encoding="utf-8") if sidecar.exists() else None
        )
        items.append(
            {
                "name": path.name,
                "kind": kind,
                "timestamp": timestamp,
                "filename": original,
                "size_bytes": path.stat().st_size,
                "description": description,
            }
        )
    return {"device_slug": slug, "uploads": items}


# ─── Versioned sources — list + switch ─────────────────────────────────

# Vision pipeline ~ wall-clock per page on Opus 4.7 with grounding +
# fan-out parallelism: empirically 25-35s on iPhone X / MNT Reform sized
# packs. We bias slightly high (35s) so the countdown rarely undershoots
# the real completion. Updated value lives here so a single tweak
# propagates to both the response ETA and the doc string above.
_VISION_SECONDS_PER_PAGE = 35


def _count_pdf_pages(pdf_path: Path) -> int | None:
    """Return the page count without opening the file twice.

    pdfplumber is already in the dependency tree (used by the renderer +
    grounding extractor) so reusing it avoids pulling in a second PDF
    library. Returns None on any read failure — the caller treats that
    as "ETA unknown" rather than failing the switch.
    """
    try:
        import pdfplumber

        with pdfplumber.open(str(pdf_path)) as pdf:
            return len(pdf.pages)
    except Exception:  # noqa: BLE001 — pdfplumber may raise many parse-time errors; "ETA unknown" is fine
        logger.warning("could not count pages in %s", pdf_path, exc_info=True)
        return None


@router.get("/packs/{device_slug}/sources", response_model=SourcesResponse)
async def get_pack_sources(device_slug: str) -> SourcesResponse:
    """List every uploaded version per kind, with the active pin marked."""
    slug = _validate_slug(device_slug)
    settings = _pkg.get_settings()
    pack_dir = Path(settings.memory_root) / slug
    if not pack_dir.exists():
        raise HTTPException(status_code=404, detail=f"No pack for device_slug={slug!r}")

    pins = sources.read_active(pack_dir)
    out_kinds: dict[str, SourceKindEntry] = {}
    for kind in (sources.SCHEMATIC_KIND, sources.BOARDVIEW_KIND):
        versions = [
            SourceVersion(**v.to_dict())
            for v in sources.list_versions(pack_dir, kind)
        ]
        out_kinds[kind] = SourceKindEntry(
            kind=kind, active=pins.get(kind), versions=versions
        )
    return SourcesResponse(
        device_slug=slug,
        schematic_pdf=out_kinds[sources.SCHEMATIC_KIND],
        boardview=out_kinds[sources.BOARDVIEW_KIND],
    )


async def _reingest_and_cache(slug: str, pack_dir: Path, pdf_path: Path, pdf_hash: str) -> None:
    """Run the schematic vision pipeline then write-through to the hashed cache.

    Used as a background task on PUT sources when the target hash is not
    cached. Errors are logged; the pin stays set so the UI can retry.
    """
    settings = _pkg.get_settings()
    api_key = settings.anthropic_api_key
    if not api_key:
        logger.error("[sources] cannot reingest without ANTHROPIC_API_KEY for %s", slug)
        return
    try:
        client = AsyncAnthropic(api_key=api_key, max_retries=4)
        await _pkg.ingest_schematic(
            device_slug=slug,
            pdf_path=pdf_path,
            client=client,
            memory_root=Path(settings.memory_root),
        )
        # Persist this version's artefacts so a future switch back is instant.
        sources.write_through_cache(pack_dir, pdf_hash)
        logger.info("[sources] reingest complete for %s hash=%s", slug, pdf_hash)
    except Exception:  # noqa: BLE001 — fire-and-forget bg task; pin stays set so UI can retry
        logger.exception("[sources] reingest failed for %s hash=%s", slug, pdf_hash)


# Reserved filename used to surface a "legacy" schematic.pdf — one that
# pre-dates the versioning system and therefore lives at
# `memory/{slug}/schematic.pdf` without a matching entry in `uploads/`.
# The leading 0-timestamp sorts it to the bottom of the version list so
# newer real uploads stay on top, and the literal "baseline" suffix
# makes it scannable in the UI.
_LEGACY_SCHEMATIC_FILENAME = "00000000T000000Z-schematic_pdf-baseline.pdf"


def _archive_legacy_schematic_if_present(pack_dir: Path) -> str | None:
    """Snapshot an in-place schematic.pdf into `uploads/` + cache.

    Runs once per pack, on the first technician upload of a schematic_pdf.
    If `memory/{slug}/schematic.pdf` exists AND no upload of that kind has
    been recorded yet, this copies it into `uploads/` under a fixed
    "baseline" filename and (when a derived `electrical_graph.json` is
    present) writes through to the hashed cache so the user can switch
    back to it instantly later. Returns the archived filename, or None
    when nothing needed archiving.
    """
    legacy_pdf = pack_dir / "schematic.pdf"
    if not legacy_pdf.exists():
        return None
    # Already archived (or any other schematic_pdf upload exists)? Skip.
    existing = sources.list_uploads_for_kind(pack_dir, sources.SCHEMATIC_KIND)
    if existing:
        return None

    uploads_dir = pack_dir / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    target = uploads_dir / _LEGACY_SCHEMATIC_FILENAME
    try:
        shutil.copyfile(legacy_pdf, target)
    except OSError:
        logger.warning("could not archive legacy schematic for %s", pack_dir.name, exc_info=True)
        return None

    # Cache the legacy artefacts under their hash so a switch back is free.
    if (pack_dir / "electrical_graph.json").exists():
        try:
            legacy_hash = sources.hash_pdf(legacy_pdf)
            sources.write_through_cache(pack_dir, legacy_hash)
        except OSError:
            logger.warning(
                "could not write-through legacy cache for %s",
                pack_dir.name,
                exc_info=True,
            )
    logger.info("[sources] archived legacy schematic for %s as %s", pack_dir.name, target.name)
    return _LEGACY_SCHEMATIC_FILENAME


def _apply_schematic_pin(
    slug: str, pack_dir: Path, target_filename: str
) -> tuple[Literal["cached", "rebuilding"], int | None, int | None]:
    """Materialise a schematic_pdf pin in place. Returns (status, eta, pages).

    Centralises the cache-vs-reingest decision shared by POST /documents
    auto-pin and PUT /sources/{kind}. Caller must already have:
      - validated the target file exists in `uploads/`
      - written the new pin to `active_sources.json`

    On `cached`: copies the cached artefacts back into place; the new
    graph is live before the function returns.
    On `rebuilding`: copies the source PDF to `memory/{slug}/schematic.pdf`,
    drops stale derivatives, schedules a background ingestion task. ETA
    is heuristic (page count × seconds-per-page).
    """
    target = pack_dir / "uploads" / target_filename
    pdf_hash = sources.hash_pdf(target)

    if sources.is_cached(pack_dir, pdf_hash):
        sources.restore_from_cache(pack_dir, pdf_hash)
        return "cached", None, None

    # Cache miss — install the new PDF in place, drop stale derived files
    # (so detect helpers report has_electrical_graph=False during build),
    # then kick off the vision pipeline as a background task.
    shutil.copyfile(target, pack_dir / "schematic.pdf")
    sources.clear_in_place_schematic(pack_dir)
    shutil.copyfile(target, pack_dir / "schematic.pdf")
    asyncio.create_task(
        _reingest_and_cache(slug, pack_dir, pack_dir / "schematic.pdf", pdf_hash)
    )
    pages = _count_pdf_pages(pack_dir / "schematic.pdf")
    eta = pages * _VISION_SECONDS_PER_PAGE if pages else None
    return "rebuilding", eta, pages


@router.put(
    "/packs/{device_slug}/sources/{kind}",
    response_model=SwitchSourceResponse,
)
async def switch_pack_source(
    device_slug: str,
    kind: str,
    payload: SwitchSourceRequest,
) -> SwitchSourceResponse:
    """Pin a different uploaded version as the active source for this kind.

    For `boardview` the switch is just a pin update — the new file takes
    effect at the next WS open (`SessionState.from_device`).
    For `schematic_pdf` the response distinguishes three statuses:
      - `cached`     : we found the target PDF's hash in the cache, copied
        the cached artefacts back into place; the new graph is live now.
      - `rebuilding` : the hash is unknown, the source PDF was copied to
        `memory/{slug}/schematic.pdf`, the in-place derived files were
        cleared, and a background ingest_schematic was kicked off. The
        Schematic / Electrical graph cards stay in `building` until done.
      - `pinned`     : (rare) pin updated but the target file is missing
        from disk; nothing else changed.
    """
    slug = _validate_slug(device_slug)
    if kind not in sources.KNOWN_KINDS:
        raise HTTPException(
            status_code=422,
            detail=f"unknown kind={kind!r} — allowed: {list(sources.KNOWN_KINDS)}",
        )

    settings = _pkg.get_settings()
    pack_dir = Path(settings.memory_root) / slug
    if not pack_dir.exists():
        raise HTTPException(status_code=404, detail=f"No pack for device_slug={slug!r}")

    target = pack_dir / "uploads" / payload.filename
    if not target.exists() or not target.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"upload {payload.filename!r} not found in {slug!r}/uploads",
        )
    # Defense in depth: filename must contain the kind marker.
    if f"-{kind}-" not in payload.filename:
        raise HTTPException(
            status_code=422,
            detail=f"filename does not match kind={kind!r}",
        )

    pins = sources.read_active(pack_dir)
    pins[kind] = payload.filename
    sources.write_active(pack_dir, pins)

    if kind == sources.BOARDVIEW_KIND:
        return SwitchSourceResponse(
            device_slug=slug,
            kind=kind,
            active=payload.filename,
            status="pinned",
            detail="boardview pin updated; effective at next WS open.",
        )

    # schematic_pdf — delegate to the shared helper so the cache-or-reingest
    # logic stays consistent between explicit switch and auto-pin paths.
    try:
        status, eta, pages = _apply_schematic_pin(slug, pack_dir, payload.filename)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"could not apply pin: {exc}") from exc

    if status == "cached":
        return SwitchSourceResponse(
            device_slug=slug,
            kind=kind,
            active=payload.filename,
            status="cached",
            detail="cache hit; electrical graph restored from cache.",
        )
    return SwitchSourceResponse(
        device_slug=slug,
        kind=kind,
        active=payload.filename,
        status="rebuilding",
        detail="cache miss; vision pipeline launched in background.",
        eta_seconds=eta,
        page_count=pages,
    )


_BOARDVIEW_MEDIA_TYPES = {
    ".brd":       "application/octet-stream",
    ".brd2":      "application/octet-stream",
    ".kicad_pcb": "application/octet-stream",
    ".asc":       "text/plain",
    ".bdv":       "application/octet-stream",
    ".bv":        "application/octet-stream",
    ".cad":       "application/octet-stream",
    ".cst":       "application/octet-stream",
    ".f2b":       "application/octet-stream",
    ".fz":        "application/octet-stream",
    ".gr":        "application/octet-stream",
    ".tvw":       "application/octet-stream",
}


@router.api_route("/packs/{device_slug}/boardview", methods=["GET", "HEAD"])
async def get_pack_boardview(device_slug: str) -> FileResponse:
    """Serve the active boardview file for this device.

    Resolution chain (same as `_find_boardview`):
      1. `active_sources.json` pin -> `memory/{slug}/uploads/<pinned>`.
      2. `board_assets/{slug}.<ext>` (in-repo demo boards).
      3. `memory/{slug}/uploads/*-boardview-*` (alphabetical first).
    Returns 404 when none resolves. Served with the original filename in
    `Content-Disposition` so the frontend can preserve the extension when
    re-POSTing to `/api/board/parse` (extension drives parser dispatch).
    """
    settings = _pkg.get_settings()
    slug = _slugify(device_slug)
    pack_dir = Path(settings.memory_root) / slug
    path = _find_boardview(slug, pack_dir)
    if path is None:
        raise HTTPException(
            status_code=404,
            detail=f"No boardview on disk for device_slug={slug!r}",
        )
    media_type = _BOARDVIEW_MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(
        path,
        media_type=media_type,
        headers={"Content-Disposition": f'inline; filename="{path.name}"'},
    )


@router.api_route("/packs/{device_slug}/schematic.pdf", methods=["GET", "HEAD"])
async def get_pack_schematic_pdf(device_slug: str) -> FileResponse:
    """Serve the source schematic PDF for this device.

    Lookup order:
    1. `memory/{slug}/schematic.pdf` — persisted by `ingest_schematic`.
    2. `board_assets/{slug}.pdf` — fallback for devices whose schematic
       ships pre-rendered in the repo.
    Returns 404 when neither exists. Served as `application/pdf` with
    `Content-Disposition: inline` so the browser's native viewer handles
    pagination, zoom, and search.
    """
    settings = _pkg.get_settings()
    slug = _slugify(device_slug)
    candidates = [
        Path(settings.memory_root) / slug / "schematic.pdf",
        Path.cwd() / "board_assets" / f"{slug}.pdf",
    ]
    for path in candidates:
        if path.exists() and path.is_file():
            return FileResponse(
                path,
                media_type="application/pdf",
                headers={"Content-Disposition": f'inline; filename="{slug}.pdf"'},
            )
    raise HTTPException(
        status_code=404,
        detail=f"No schematic PDF on disk for device_slug={slug!r}",
    )
