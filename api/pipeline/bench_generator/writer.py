# SPDX-License-Identifier: Apache-2.0
"""Atomic file writes for the bench generator.

Four per-run artefacts + the cross-run `_latest.json` aggregate + the
runtime-consumed `memory/{slug}/simulator_reliability.json` + source
archive snapshots. Every write uses tempfile + os.replace to avoid
half-written files on crash.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

from api.pipeline.bench_generator.schemas import (
    ProposedScenario,
    Rejection,
    RunManifest,
)
from api.pipeline.schematic.evaluator import Scorecard

logger = logging.getLogger("microsolder.bench_generator.writer")


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path_s = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent,
    )
    tmp_path = Path(tmp_path_s)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _jsonl_dump(items: list[dict]) -> str:
    return "\n".join(json.dumps(it, ensure_ascii=False) for it in items) + "\n"


def write_per_run_files(
    *,
    output_dir: Path,
    run_date: str,
    slug: str,
    accepted: list[ProposedScenario],
    rejected: list[Rejection],
    manifest: RunManifest,
    scorecard: Scorecard,
) -> None:
    """Write the four per-run files atomically."""
    base = output_dir / f"{slug}-{run_date}"
    _atomic_write_text(
        Path(str(base) + ".jsonl"),
        _jsonl_dump([s.model_dump(exclude_none=False) for s in accepted]),
    )
    _atomic_write_text(
        Path(str(base) + ".rejected.jsonl"),
        _jsonl_dump([r.model_dump(exclude_none=False) for r in rejected]),
    )
    _atomic_write_text(
        Path(str(base) + ".manifest.json"),
        json.dumps(manifest.model_dump(), indent=2),
    )
    _atomic_write_text(
        Path(str(base) + ".score.json"),
        json.dumps(scorecard.model_dump(), indent=2),
    )
    logger.info(
        "[bench_generator.writer] wrote 4 files for slug=%s run_date=%s "
        "(n_accepted=%d, n_rejected=%d)",
        slug, run_date, len(accepted), len(rejected),
    )
