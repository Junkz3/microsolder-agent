"""One-shot CLI for validating a single page through the vision pass.

Usage:
    .venv/bin/python -m api.pipeline.schematic.cli <pdf_path> <page_number>

Renders the requested page to a temp PNG, calls Claude Opus vision with the
SchematicPageGraph forced tool, and pretty-prints the validated result. Token
usage is logged by `call_with_forced_tool` so cost can be read off stdout.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import tempfile
from pathlib import Path

from anthropic import AsyncAnthropic

from api.config import get_settings
from api.logging_setup import configure_logging
from api.pipeline.schematic.grounding import (
    extract_grounding,
    format_grounding_for_prompt,
)
from api.pipeline.schematic.page_vision import extract_page
from api.pipeline.schematic.renderer import render_pages

logger = logging.getLogger("microsolder.pipeline.schematic.cli")


async def _run(
    pdf: Path,
    page_number: int,
    *,
    model: str,
    output: Path,
    grounding_enabled: bool,
) -> None:
    settings = get_settings()
    if not settings.anthropic_api_key:
        raise SystemExit("ANTHROPIC_API_KEY missing from .env")

    pdf = pdf.resolve()
    if not pdf.is_file():
        raise SystemExit(f"PDF not found: {pdf}")

    with tempfile.TemporaryDirectory(prefix="schematic_cli_") as tmp:
        tmp_dir = Path(tmp)
        logger.info("rendering %s into %s", pdf, tmp_dir)
        all_pages = render_pages(pdf, tmp_dir, dpi=200)

        target = next(
            (p for p in all_pages if p.page_number == page_number), None
        )
        if target is None:
            raise SystemExit(
                f"page {page_number} not found in {pdf.name} "
                f"(PDF has {len(all_pages)} pages)"
            )

        grounding_text: str | None = None
        if grounding_enabled:
            g = extract_grounding(pdf, target.page_number)
            grounding_text = format_grounding_for_prompt(g)
            logger.info(
                "grounding extracted: refdes=%d nets=%d values=%d sheet=%s",
                len(g.refdes),
                len(g.net_labels),
                len(g.values),
                g.sheet_file,
            )

        logger.info(
            "vision call on page %d (orientation=%s, scanned=%s, model=%s, grounding=%s)",
            target.page_number,
            target.orientation,
            target.is_scanned,
            model,
            "on" if grounding_enabled else "off",
        )

        client = AsyncAnthropic(api_key=settings.anthropic_api_key)
        graph = await extract_page(
            client=client,
            model=model,
            rendered=target,
            total_pages=len(all_pages),
            device_label=pdf.stem,
            grounding=grounding_text,
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(graph.model_dump_json(indent=2))
    logger.info(
        "wrote %s (nodes=%d, nets=%d, edges=%d, notes=%d, ambiguities=%d, conf=%.2f)",
        output,
        len(graph.nodes),
        len(graph.nets),
        len(graph.typed_edges),
        len(graph.designer_notes),
        len(graph.ambiguities),
        graph.confidence,
    )


def main() -> None:
    configure_logging()
    settings = get_settings()
    parser = argparse.ArgumentParser(
        prog="schematic-cli",
        description="Run Claude vision on a single page of a schematic PDF.",
    )
    parser.add_argument("pdf", type=Path, help="Path to the schematic PDF.")
    parser.add_argument("page", type=int, help="1-based page number to analyse.")
    parser.add_argument(
        "--model",
        default=settings.anthropic_model_main,
        help=f"Anthropic model id (default: {settings.anthropic_model_main}).",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Path to write the SchematicPageGraph JSON (default: /tmp/schematic_page_<N>_<model>.json).",
    )
    parser.add_argument(
        "--no-grounding",
        action="store_true",
        help="Disable pdfplumber grounding dump (default: grounding on).",
    )
    args = parser.parse_args()
    output = args.output or Path(
        f"/tmp/schematic_page_{args.page}_{args.model.replace('-', '_')}.json"
    )
    asyncio.run(
        _run(
            args.pdf,
            args.page,
            model=args.model,
            output=output,
            grounding_enabled=not args.no_grounding,
        )
    )


if __name__ == "__main__":
    main()
