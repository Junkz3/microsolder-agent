"""Repair-session CRUD + the `POST /repairs` orchestration entry point.

Hosts the background helpers (`_run_pipeline_with_events`,
`_run_expand_with_events`, `_maybe_check_coverage`) and the
`_persist_repair` dedup writer that backs the home library.

Also re-exports `_run_pipeline_with_events` via the package
`__init__.py` — `tests/pipeline/test_pipeline_events_narration.py`
imports it directly under that name.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from anthropic import AsyncAnthropic
from fastapi import APIRouter, HTTPException

import api.pipeline as _pkg  # noqa: PLC0415 — module-attribute lookups for patchability
from api.agent.memory_stores import delete_repair_store
from api.pipeline import events
from api.pipeline.models import (
    RepairRequest,
    RepairResponse,
    RepairSummary,
)
from api.pipeline.orchestrator import _slugify
from api.pipeline.routes._helpers import _validate_repair_id
from api.pipeline.routes.packs import _pack_is_complete

logger = logging.getLogger("wrench_board.pipeline.api")

router = APIRouter()


def _persist_repair(
    memory_root: Path,
    slug: str,
    device_label: str,
    symptom: str,
) -> tuple[str, bool]:
    """Write the repair metadata to memory/{slug}/repairs/{repair_id}.json.

    Returns `(repair_id, is_new)`. When an `open` or `in_progress` repair
    already exists on this `(slug, normalised_symptom)` we **reuse its id**
    and return `is_new=False` — the caller short-circuits coverage + expand
    so the technician doesn't burn $0.40 of LLM tokens every time they
    resubmit the same form. Closed repairs do NOT block dedup: starting a
    new ticket on a previously-resolved symptom is intentional.

    `status` starts at 'open' on a fresh repair and is updated as the
    session evolves.
    """
    repairs_dir = memory_root / slug / "repairs"
    repairs_dir.mkdir(parents=True, exist_ok=True)

    norm_symptom = symptom.strip().lower()
    if norm_symptom:
        for existing_path in repairs_dir.glob("*.json"):
            try:
                payload = json.loads(existing_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            if payload.get("status") not in ("open", "in_progress"):
                continue
            if (payload.get("symptom") or "").strip().lower() != norm_symptom:
                continue
            existing_id = payload.get("repair_id")
            if isinstance(existing_id, str) and existing_id:
                return existing_id, False

    repair_id = uuid.uuid4().hex[:12]
    payload = {
        "repair_id": repair_id,
        "device_slug": slug,
        "device_label": device_label,
        "symptom": symptom,
        "status": "open",
        "created_at": datetime.now(UTC).isoformat(),
    }
    (repairs_dir / f"{repair_id}.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return repair_id, True


@router.get("/repairs", response_model=list[RepairSummary])
async def list_repairs() -> list[RepairSummary]:
    """Return every repair ever created, across every device, newest first.

    Powers the home library: each row is one client intervention the
    technician can open, reopen, or finish. Status drives the visual
    badge ('open' · 'in_progress' · 'closed').
    """
    settings = _pkg.get_settings()
    root = Path(settings.memory_root)
    results: list[RepairSummary] = []
    if not root.exists():
        return results

    for pack_dir in root.iterdir():
        if not pack_dir.is_dir():
            continue
        repairs_dir = pack_dir / "repairs"
        if not repairs_dir.exists():
            continue
        for path in repairs_dir.glob("*.json"):
            try:
                payload = json.loads(path.read_text())
            except json.JSONDecodeError:
                logger.warning("Skipping malformed repair file: %s", path)
                continue
            results.append(
                RepairSummary(
                    repair_id=payload.get("repair_id", path.stem),
                    device_slug=payload.get("device_slug", pack_dir.name),
                    device_label=payload.get("device_label", pack_dir.name),
                    symptom=payload.get("symptom", ""),
                    status=payload.get("status", "open"),
                    created_at=payload.get("created_at", ""),
                )
            )
    results.sort(key=lambda r: r.created_at, reverse=True)
    return results


@router.get("/repairs/{repair_id}", response_model=RepairSummary)
async def get_repair(repair_id: str) -> RepairSummary:
    """Return one repair's metadata — used to resume a session from its id."""
    settings = _pkg.get_settings()
    root = Path(settings.memory_root)
    if not root.exists():
        raise HTTPException(status_code=404, detail=f"No repair {repair_id!r}")
    for pack_dir in root.iterdir():
        if not pack_dir.is_dir():
            continue
        candidate = pack_dir / "repairs" / f"{repair_id}.json"
        if candidate.exists():
            payload = json.loads(candidate.read_text())
            return RepairSummary(
                repair_id=payload.get("repair_id", repair_id),
                device_slug=payload.get("device_slug", pack_dir.name),
                device_label=payload.get("device_label", pack_dir.name),
                symptom=payload.get("symptom", ""),
                status=payload.get("status", "open"),
                created_at=payload.get("created_at", ""),
            )
    raise HTTPException(status_code=404, detail=f"No repair {repair_id!r}")


@router.delete("/repairs/{repair_id}")
async def delete_repair(repair_id: str) -> dict:
    """Delete a repair: its disk artefacts AND any per-repair MA memory store.

    Scope:
      - removes `memory/{slug}/repairs/{repair_id}.json` (the metadata)
      - removes `memory/{slug}/repairs/{repair_id}/` (subdir with chat
        history, findings, managed.json marker, conversations…)
      - calls the Managed Agents API to delete the per-repair memory store
        named `wrench-board-repair-{slug}-{repair_id}` (if a marker is on
        disk). Best-effort: an MA failure logs but doesn't block disk
        cleanup, since a stranded store is recoverable manually whereas a
        half-deleted disk state is not.

    Does NOT touch the device-level pack (`memory/{slug}/*.json`) or the
    shared `device-{slug}` / `global-*` memory stores — that knowledge is
    reused by the next repair on the same device.
    """
    settings = _pkg.get_settings()
    root = Path(settings.memory_root)
    if not root.exists():
        raise HTTPException(status_code=404, detail=f"No repair {repair_id!r}")

    metadata_file: Path | None = None
    device_slug: str | None = None
    for pack_dir in root.iterdir():
        if not pack_dir.is_dir():
            continue
        candidate = pack_dir / "repairs" / f"{repair_id}.json"
        if candidate.exists():
            metadata_file = candidate
            device_slug = pack_dir.name
            break

    if metadata_file is None or device_slug is None:
        raise HTTPException(status_code=404, detail=f"No repair {repair_id!r}")

    # 1. MA cleanup (best-effort). Drives off the on-disk marker so that a
    # repair which never opened a session simply no-ops here.
    store_deleted = False
    try:
        client = AsyncAnthropic(api_key=settings.anthropic_api_key or "missing", max_retries=settings.anthropic_max_retries)  # noqa: E501
        store_deleted = await delete_repair_store(
            client, device_slug=device_slug, repair_id=repair_id
        )
    except Exception as exc:  # noqa: BLE001 — MA cleanup is best-effort; never block disk wipe
        logger.warning(
            "delete_repair: MA cleanup raised for repair=%s: %s — proceeding with disk",
            repair_id,
            exc,
        )

    # 2. Disk cleanup. Remove subdir first (best-effort), then the metadata
    # JSON. Order matters — list_repairs scans the JSONs, so wiping the
    # metadata last is what makes the repair disappear from the library.
    subdir = metadata_file.parent / repair_id
    dir_deleted = False
    if subdir.exists() and subdir.is_dir():
        try:
            shutil.rmtree(subdir)
            dir_deleted = True
        except OSError as exc:
            logger.error(
                "delete_repair: rmtree failed for %s: %s", subdir, exc
            )
            raise HTTPException(
                status_code=500,
                detail=f"Failed to remove repair directory: {exc}",
            ) from exc

    try:
        metadata_file.unlink()
    except OSError as exc:
        logger.error(
            "delete_repair: unlink failed for %s: %s", metadata_file, exc
        )
        raise HTTPException(
            status_code=500,
            detail=f"Failed to remove repair metadata: {exc}",
        ) from exc

    return {
        "repair_id": repair_id,
        "device_slug": device_slug,
        "store_deleted": store_deleted,
        "dir_deleted": dir_deleted,
    }


@router.get("/repairs/{repair_id}/conversations")
def list_repair_conversations(repair_id: str) -> dict:
    """Return the conversation index for a repair.

    The repair's `device_slug` is inferred from the metadata file one level
    up in `memory/{slug}/repairs/{repair_id}.json` — clients don't pass it.
    """
    from api.agent.chat_history import list_conversations

    settings = _pkg.get_settings()
    memory = Path(settings.memory_root)
    found_slug: str | None = None
    if memory.exists():
        for metadata_file in memory.glob(f"*/repairs/{repair_id}.json"):
            found_slug = metadata_file.parent.parent.name
            break
    if not found_slug:
        raise HTTPException(status_code=404, detail=f"unknown repair_id {repair_id}")
    convs = list_conversations(device_slug=found_slug, repair_id=repair_id)
    return {
        "device_slug": found_slug,
        "repair_id": repair_id,
        "conversations": convs,
    }


@router.delete("/repairs/{repair_id}/conversations/{conv_id}")
def delete_repair_conversation(repair_id: str, conv_id: str) -> dict:
    """Delete a single conversation from a repair.

    Wipes `memory/{slug}/repairs/{repair_id}/conversations/{conv_id}/` and
    drops the matching entry from `conversations/index.json`. The repair
    itself, its metadata file, sibling conversations, and the shared
    repair-level MA memory store are untouched. The per-tier MA sessions
    stored under the conv dir are dropped with it; their upstream Anthropic
    counterparts are left to expire naturally.
    """
    safe_repair_id = _validate_repair_id(repair_id)
    safe_conv_id = _validate_repair_id(conv_id)  # same shape constraints
    from api.agent.chat_history import delete_conversation

    settings = _pkg.get_settings()
    memory = Path(settings.memory_root)
    found_slug: str | None = None
    if memory.exists():
        for metadata_file in memory.glob(f"*/repairs/{safe_repair_id}.json"):
            found_slug = metadata_file.parent.parent.name
            break
    if not found_slug:
        raise HTTPException(status_code=404, detail=f"unknown repair_id {safe_repair_id}")

    try:
        removed = delete_conversation(
            device_slug=found_slug,
            repair_id=safe_repair_id,
            conv_id=safe_conv_id,
        )
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to remove conversation: {exc}",
        ) from exc

    if not removed:
        raise HTTPException(
            status_code=404,
            detail=f"unknown conversation {safe_conv_id} for repair {safe_repair_id}",
        )

    return {
        "repair_id": safe_repair_id,
        "conv_id": safe_conv_id,
        "device_slug": found_slug,
        "removed": True,
    }


@router.get("/repairs/{repair_id}/protocol")
async def get_repair_protocol(
    repair_id: str, device_slug: str, conv: str | None = None,
) -> dict:
    """Return the active protocol artifact for this repair (or {active: false}).

    `conv` is the conversation id to scope the lookup. When omitted, falls
    back to the legacy repair-root protocol pointer (kept for backward
    compatibility with pre-per-conv artefacts).
    """
    from api.tools.protocol import load_active_protocol
    settings = _pkg.get_settings()
    proto = load_active_protocol(
        Path(settings.memory_root), device_slug, repair_id, conv_id=conv,
    )
    if proto is None:
        return {"active": False}
    return {
        "active": True,
        "protocol_id": proto.protocol_id,
        "title": proto.title,
        "rationale": proto.rationale,
        "current_step_id": proto.current_step_id,
        "status": proto.status,
        "steps": [s.model_dump(mode="json") for s in proto.steps],
        "history": [h.model_dump(mode="json") for h in proto.history],
    }


async def _run_pipeline_with_events(
    device_label: str,
    slug: str,
    focus_symptom: str | None = None,
) -> None:
    """Background task: run the pipeline, relaying its events on the bus.

    On every `phase_finished` event we also spawn a fire-and-forget narration
    task: a small Haiku call reads the just-written artifact and publishes a
    `phase_narration` event so the landing UI can render a human-readable
    sentence next to the progress dot. Narration failures are silent; the
    pipeline never blocks waiting for them.

    `focus_symptom`, when supplied, is threaded to Scout so the technician's
    reason-for-opening-the-repair is prioritised in the web_search rounds.
    """
    t0 = time.monotonic()

    # Lazily-built Haiku client for narration (kept in closure so we don't
    # spawn a new TCP pool per phase).
    settings = _pkg.get_settings()
    narrator_client: AsyncAnthropic | None = None
    if settings.anthropic_api_key:
        narrator_client = AsyncAnthropic(api_key=settings.anthropic_api_key, max_retries=settings.anthropic_max_retries)

    async def _narrate_and_publish(phase: str) -> None:
        if narrator_client is None:
            return
        try:
            text = await _pkg.narrate_phase(phase, slug, client=narrator_client)
        except Exception as exc:  # noqa: BLE001 — narrate_phase already swallows; defence-in-depth
            logger.warning(
                "[API] narrator unexpected raise (phase=%s slug=%s): %s",
                phase, slug, exc,
            )
            return
        if not text:
            return
        await events.publish(
            slug,
            {"type": "phase_narration", "phase": phase, "text": text},
        )

    async def _on_event(ev: dict) -> None:
        await events.publish(slug, ev)
        if ev.get("type") == "phase_finished":
            phase = ev.get("phase")
            if isinstance(phase, str) and phase:
                # Fire-and-forget: do not await — the next phase must start now.
                asyncio.create_task(_narrate_and_publish(phase))

    try:
        await _pkg.generate_knowledge_pack(
            device_label,
            on_event=_on_event,
            focus_symptom=focus_symptom,
        )
    except Exception as exc:  # noqa: BLE001 — fire-and-forget bg task; report failure on event bus
        logger.exception("[API] background pipeline failed for slug=%r", slug)
        await events.publish(
            slug,
            {
                "type": "pipeline_failed",
                "status": "ERROR",
                "error": str(exc),
                "elapsed_s": time.monotonic() - t0,
            },
        )


async def _run_expand_with_events(slug: str, symptom: str) -> None:
    """Background task: run expand_pack with event-bus relaying.

    Kicked off by `create_repair` when the pack already exists and the
    coverage classifier decided the new symptom is NOT covered by an
    existing rule.

    We emit events in two shapes simultaneously on the WS bus:

    1. **Generic `pipeline_*` events** — same types the landing UI
       listens for on a full pipeline run (`pipeline_started`,
       `phase_started/finished` on a synthetic "expand" phase,
       `pipeline_finished/failed`). Each carries `kind: "expand"` so
       consumers that care can branch; consumers that don't can treat
       the flow identically to a full run. This keeps the landing
       timeline + auto-redirect working without frontend changes.
    2. **Specific `expand_*` events** — in addition, so a future UI
       that wants to render expand differently has a distinct stream.
    """
    t0 = time.monotonic()
    # Compat: landing.js / pipeline_progress.js listen for pipeline_started.
    await events.publish(
        slug,
        {"type": "pipeline_started", "kind": "expand", "device_slug": slug, "symptom": symptom},
    )
    await events.publish(slug, {"type": "phase_started", "phase": "expand"})
    # Specific flavour (optional consumers).
    await events.publish(slug, {"type": "expand_started", "symptom": symptom})
    try:
        summary = await _pkg.expand_pack(
            device_slug=slug,
            focus_symptoms=[symptom],
        )
        elapsed = time.monotonic() - t0
        counts = {
            "new_rules_count": summary.get("new_rules_count", 0),
            "new_components_count": summary.get("new_components_count", 0),
            "total_rules_after": summary.get("total_rules_after", 0),
        }
        await events.publish(
            slug,
            {"type": "phase_finished", "phase": "expand", "elapsed_s": elapsed, "counts": counts},
        )
        await events.publish(slug, {"type": "expand_finished", "elapsed_s": elapsed, **counts})
        # Compat: triggers landing goToWorkspace redirect.
        await events.publish(
            slug,
            {
                "type": "pipeline_finished",
                "kind": "expand",
                "device_slug": slug,
                "status": "APPROVED",
                "elapsed_s": elapsed,
                **counts,
            },
        )
    except Exception as exc:  # noqa: BLE001 — bus delivery must not crash
        logger.exception("[API] expand_pack failed for slug=%r", slug)
        elapsed = time.monotonic() - t0
        await events.publish(slug, {"type": "expand_failed", "error": str(exc), "elapsed_s": elapsed})
        # Compat: landing_failed branch.
        await events.publish(
            slug,
            {
                "type": "pipeline_failed",
                "kind": "expand",
                "status": "ERROR",
                "error": str(exc),
                "elapsed_s": elapsed,
            },
        )


async def _maybe_check_coverage(
    slug: str,
    symptom: str,
    memory_root: Path,
) -> CoverageCheck:  # noqa: F821 — forward-only type ref
    """Call the Haiku coverage classifier; on any failure, fall back to
    an uncovered verdict so the expand-pack path still fires.

    Lazy-imports `check_symptom_coverage` to avoid a module-load cycle:
    coverage → schemas → api.pipeline (this module)."""
    from api.pipeline.coverage import check_symptom_coverage
    from api.pipeline.schemas import CoverageCheck

    settings = _pkg.get_settings()
    if not settings.anthropic_api_key:
        return CoverageCheck(
            covered=False,
            matched_rule_id=None,
            confidence=0.0,
            reason="no Anthropic API key configured — treating as uncovered",
        )
    client = AsyncAnthropic(api_key=settings.anthropic_api_key, max_retries=settings.anthropic_max_retries)
    try:
        return await check_symptom_coverage(
            client=client,
            model=settings.anthropic_model_fast,
            device_slug=slug,
            symptom=symptom,
            memory_root=memory_root,
        )
    except Exception as exc:  # noqa: BLE001 — failure falls through to expand
        logger.warning(
            "[API] coverage check failed for slug=%r (%s); treating as uncovered",
            slug,
            exc,
        )
        return CoverageCheck(
            covered=False,
            matched_rule_id=None,
            confidence=0.0,
            reason=f"coverage classifier error: {exc}",
        )


@router.post("/repairs", response_model=RepairResponse)
async def create_repair(request: RepairRequest) -> RepairResponse:
    """Register a repair and kick off the pipeline in the background.

    The response returns immediately with the generated repair_id and device_slug.
    Real-time pipeline progress is streamed via WS /pipeline/progress/{slug}.
    If the pack is already complete on disk we skip the pipeline to save tokens —
    the client can proceed straight to the Memory Bank.
    """
    settings = _pkg.get_settings()
    memory_root = Path(settings.memory_root)
    # Prefer the explicit slug when the client picked an existing pack — this
    # protects us from Registry-rewrite drift (the LLM can amend device_label
    # after the pack's directory was named from the original call slug).
    slug = request.device_slug or _slugify(request.device_label)
    pack_dir = memory_root / slug

    # Every "new repair" IS a repair session — persist the record
    # whether the pack is fresh or already on disk. Two repairs on the same
    # iPhone X are two separate sessions with two separate contexts; both
    # must be reopenable later from the library.
    repair_id, is_new = _persist_repair(
        memory_root, slug, request.device_label, request.symptom
    )

    # Branch 0 — the tech resubmitted the form for an already-open ticket
    # on the same (device, symptom). Reuse the existing repair without
    # burning a coverage check or an expand-pack round-trip. Without this
    # short-circuit a stuck-on-low-confidence coverage classifier loops
    # and chews $0.40 of LLM tokens every retry.
    if not is_new:
        logger.info(
            "[API] /pipeline/repairs · reusing open repair=%s for slug=%r — no LLM run",
            repair_id,
            slug,
        )
        return RepairResponse(
            repair_id=repair_id,
            device_slug=slug,
            device_label=request.device_label,
            pipeline_started=False,
            pipeline_kind="none",
            coverage_reason="reusing existing open ticket on the same symptom",
        )

    pack_complete = _pack_is_complete(pack_dir)

    # Branch 1 — pack missing (or force_rebuild): fire the full pipeline
    # with the symptom threaded to Scout as a priority target.
    if not pack_complete or request.force_rebuild:
        if request.force_rebuild and pack_complete:
            logger.info(
                "[API] /pipeline/repairs · force_rebuild=True · repair=%s regenerating pack for slug=%r",
                repair_id,
                slug,
            )
        logger.info(
            "[API] /pipeline/repairs · firing full pipeline for slug=%r · focus_symptom=yes",
            slug,
        )
        asyncio.create_task(
            _run_pipeline_with_events(
                request.device_label, slug, focus_symptom=request.symptom
            )
        )
        return RepairResponse(
            repair_id=repair_id,
            device_slug=slug,
            device_label=request.device_label,
            pipeline_started=True,
            pipeline_kind="full",
        )

    # Pack complete — compare the symptom against existing rules before
    # spending tokens on an expand round-trip.
    coverage = await _pkg._maybe_check_coverage(slug, request.symptom, memory_root)

    # Branch 2 — symptom already covered by an existing rule: skip LLM
    # work entirely and return the matched_rule_id so the UI can surface
    # the known diagnostic flow immediately.
    if (
        coverage.covered
        and coverage.confidence >= 0.7
        and coverage.matched_rule_id is not None
    ):
        logger.info(
            "[API] /pipeline/repairs · symptom covered by %s (confidence=%.2f) — no LLM run",
            coverage.matched_rule_id,
            coverage.confidence,
        )
        return RepairResponse(
            repair_id=repair_id,
            device_slug=slug,
            device_label=request.device_label,
            pipeline_started=False,
            pipeline_kind="none",
            matched_rule_id=coverage.matched_rule_id,
            coverage_reason=coverage.reason,
        )

    # Branch 3 — pack complete but symptom uncovered: fire a targeted
    # expand_pack that grows the existing pack with Scout + Clinicien on
    # the symptom alone (much cheaper than the full pipeline).
    logger.info(
        "[API] /pipeline/repairs · pack complete for slug=%r; symptom uncovered — firing expand",
        slug,
    )
    asyncio.create_task(_run_expand_with_events(slug, request.symptom))
    return RepairResponse(
        repair_id=repair_id,
        device_slug=slug,
        device_label=request.device_label,
        pipeline_started=True,
        pipeline_kind="expand",
        coverage_reason=coverage.reason,
    )
