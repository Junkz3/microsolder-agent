# SPDX-License-Identifier: Apache-2.0
"""One-shot migration: move per-repair protocol + board_state artefacts into
the active conv directory.

Background: the protocol pointer (`protocol.json`), the protocol library
(`protocols/`), and the board-overlay snapshot (`board_state.json`) used
to live at `memory/{slug}/repairs/{rid}/`. They've been moved under the
per-conv directory `…/conversations/{conv_id}/` so that each chat thread
holds its own plan + canvas. Without this migration, the artefacts that
were already on disk become invisible to the new conv-scoped loaders —
the tech reopens the conv they had a protocol on and sees nothing.

For each repair this script:
1. Reads `conversations/index.json` and picks the most recently touched
   conv (`max(last_turn_at, started_at)`) — that's the conv the artefacts
   logically belong to (it's the one the tech was working in when the
   files were last written).
2. Moves `protocol.json` → `conversations/{conv}/protocol.json`
3. Moves `protocols/` → `conversations/{conv}/protocols/`
4. Moves `board_state.json` → `conversations/{conv}/board_state.json`

Skips:
- Repairs with no `conversations/` index → nothing to anchor the move on
- Files already present at the destination → don't overwrite (idempotent)
- Files missing at the source → no-op

Usage:
    .venv/bin/python scripts/migrate_repair_artefacts_to_conv.py            # dry-run
    .venv/bin/python scripts/migrate_repair_artefacts_to_conv.py --apply    # actually move
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from api.config import get_settings


def _pick_active_conv(index: list[dict]) -> str | None:
    """Most recently touched conv id, falling back to most recently started."""
    if not index:
        return None
    def _key(entry: dict) -> str:
        return entry.get("last_turn_at") or entry.get("started_at") or ""
    best = max(index, key=_key)
    return best.get("id")


def _migrate_one(
    repair_dir: Path, *, apply: bool, label: str,
) -> tuple[int, list[str]]:
    """Returns (moves_count, log_lines)."""
    index_path = repair_dir / "conversations" / "index.json"
    if not index_path.exists():
        return (0, [])
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return (0, [])
    if not isinstance(index, list) or not index:
        return (0, [])

    active = _pick_active_conv(index)
    if not active:
        return (0, [])

    conv_dir = repair_dir / "conversations" / active
    moves: list[tuple[Path, Path]] = []

    src_pointer = repair_dir / "protocol.json"
    if src_pointer.exists():
        moves.append((src_pointer, conv_dir / "protocol.json"))

    src_protocols = repair_dir / "protocols"
    if src_protocols.is_dir():
        moves.append((src_protocols, conv_dir / "protocols"))

    src_board = repair_dir / "board_state.json"
    if src_board.exists():
        moves.append((src_board, conv_dir / "board_state.json"))

    log: list[str] = []
    actually_moved = 0
    for src, dst in moves:
        if dst.exists():
            log.append(
                f"  [skip] {label} → conv {active}: destination exists ({dst.name})"
            )
            continue
        if apply:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            log.append(f"  [moved] {label}/{src.name} → conv {active}/{src.name}")
        else:
            log.append(
                f"  [dry-run] would move {label}/{src.name} → conv {active}/{src.name}"
            )
        actually_moved += 1

    return (actually_moved, log)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--memory-root", type=Path, default=None)
    parser.add_argument("--apply", action="store_true",
                        help="Actually move (default is dry-run)")
    args = parser.parse_args()

    memory_root: Path = args.memory_root or Path(get_settings().memory_root)
    if not memory_root.exists():
        print(f"memory root not found: {memory_root}", file=sys.stderr)
        return 1

    print(f"[{'apply' if args.apply else 'dry-run'}] scanning {memory_root}")
    total_repairs = 0
    total_moves = 0
    for slug_dir in sorted(memory_root.iterdir()):
        if not slug_dir.is_dir():
            continue
        repairs_dir = slug_dir / "repairs"
        if not repairs_dir.is_dir():
            continue
        for repair_dir in sorted(repairs_dir.iterdir()):
            if not repair_dir.is_dir():
                continue
            total_repairs += 1
            label = f"{slug_dir.name}/{repair_dir.name}"
            count, log = _migrate_one(repair_dir, apply=args.apply, label=label)
            for line in log:
                print(line)
            total_moves += count

    verb = "moved" if args.apply else "would move"
    print(
        f"\n{total_repairs} repair{'' if total_repairs == 1 else 's'} scanned · "
        f"{verb} {total_moves} artefact{'' if total_moves == 1 else 's'}"
    )
    if not args.apply and total_moves > 0:
        print("\nRe-run with --apply to perform the moves.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
