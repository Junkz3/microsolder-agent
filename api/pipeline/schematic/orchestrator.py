"""Orchestrator — full schematic ingestion for one device.

Renders the PDF page by page, extracts pdfplumber grounding, runs the Claude
vision pass in parallel with a cache-warmup sequence (page 1 first, then
`asyncio.gather` on the rest so the prompt-cache entry materialises before the
burst), merges the per-page graphs into a flat catalogue, compiles that into
an `ElectricalGraph`, and persists every artefact under `memory/{device_slug}/`.

Side-effect artefacts written:
- `schematic_pages/page_XXX.json`      — one per page, raw vision output
- `schematic_graph.json`               — merged flat catalogue
- `electrical_graph.json`              — final interrogeable graph

Returns the `ElectricalGraph` for callers that want to act on it directly
without re-reading from disk.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path

from anthropic import AsyncAnthropic

from api.config import get_settings
from api.pipeline.schematic.compiler import compile_electrical_graph
from api.pipeline.schematic.grounding import (
    extract_grounding,
    format_grounding_for_prompt,
)
from api.pipeline.schematic.merger import merge_pages
from api.pipeline.schematic.page_vision import extract_page
from api.pipeline.schematic.renderer import render_pages
from api.pipeline.schematic.schemas import ElectricalGraph, SchematicPageGraph

logger = logging.getLogger("microsolder.pipeline.schematic.orchestrator")


async def ingest_schematic(
    *,
    device_slug: str,
    pdf_path: Path,
    client: AsyncAnthropic,
    memory_root: Path | None = None,
    model: str | None = None,
    device_label: str | None = None,
    use_grounding: bool = True,
    cache_warmup_seconds: float | None = None,
    render_dpi: int = 200,
) -> ElectricalGraph:
    """Run the full ingestion pipeline for `pdf_path` and persist artefacts.

    Caller is responsible for providing a ready `AsyncAnthropic` client.
    `memory_root` defaults to the configured `memory` directory; callers may
    override for tests or alternate storage layouts.
    """
    settings = get_settings()
    model = model or settings.anthropic_model_main
    memory_root = memory_root or Path(settings.memory_root)
    warmup = (
        cache_warmup_seconds
        if cache_warmup_seconds is not None
        else settings.pipeline_cache_warmup_seconds
    )
    device_label = device_label or pdf_path.stem

    pdf_path = Path(pdf_path).resolve()
    output_dir = Path(memory_root) / device_slug
    pages_dir = output_dir / "schematic_pages"
    pages_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=f"schematic_{device_slug}_") as tmp:
        render_dir = Path(tmp)
        logger.info("rendering %s → %s (dpi=%d)", pdf_path, render_dir, render_dpi)
        rendered_pages = render_pages(pdf_path, render_dir, dpi=render_dpi)
        total = len(rendered_pages)
        logger.info("rendered %d pages", total)

        grounding_texts: list[str | None] = [None] * total
        if use_grounding:
            for i, page in enumerate(rendered_pages):
                g = extract_grounding(pdf_path, page.page_number)
                grounding_texts[i] = format_grounding_for_prompt(g)
                logger.info(
                    "grounding page %d: refdes=%d nets=%d values=%d sheet=%s",
                    page.page_number,
                    len(g.refdes),
                    len(g.net_labels),
                    len(g.values),
                    g.sheet_file,
                )

        async def _one_page(idx: int) -> SchematicPageGraph:
            rp = rendered_pages[idx]
            logger.info(
                "vision call page %d/%d (model=%s)", rp.page_number, total, model
            )
            graph = await extract_page(
                client=client,
                model=model,
                rendered=rp,
                total_pages=total,
                device_label=device_label,
                grounding=grounding_texts[idx],
            )
            (pages_dir / f"page_{rp.page_number:03d}.json").write_text(
                graph.model_dump_json(indent=2)
            )
            return graph

        # Fan every page out immediately. The earlier pattern serialised page
        # 1 so its `cache_write` would land before the rest arrived, but with
        # explicit `cache_control` breakpoints on the system prompt + tool
        # schema that dance buys nothing — Anthropic's cache key is the
        # prefix, not the order of arrival, and the ephemeral entry persists
        # for the ~minute-long burst. Parallel from t=0 cuts wall-time ~2×.
        # `cache_warmup_seconds` is retained on the signature for callers
        # that still want a warmup (default 0 = no wait).
        if warmup > 0 and total > 1:
            await asyncio.sleep(warmup)
        page_graphs = await asyncio.gather(
            *[_one_page(i) for i in range(total)]
        )

        schematic_graph = merge_pages(
            page_graphs,
            device_slug=device_slug,
            source_pdf=str(pdf_path),
        )
        (output_dir / "schematic_graph.json").write_text(
            schematic_graph.model_dump_json(indent=2)
        )
        logger.info(
            "merged: components=%d nets=%d edges=%d notes=%d ambiguities=%d",
            len(schematic_graph.components),
            len(schematic_graph.nets),
            len(schematic_graph.typed_edges),
            len(schematic_graph.designer_notes),
            len(schematic_graph.ambiguities),
        )

        page_confidences = {g.page: g.confidence for g in page_graphs}
        electrical = compile_electrical_graph(
            schematic_graph, page_confidences=page_confidences
        )
        (output_dir / "electrical_graph.json").write_text(
            electrical.model_dump_json(indent=2)
        )
        logger.info(
            "compiled: rails=%d boot_phases=%d degraded=%s global_conf=%.2f",
            len(electrical.power_rails),
            len(electrical.boot_sequence),
            electrical.quality.degraded_mode,
            electrical.quality.confidence_global,
        )

        return electrical
