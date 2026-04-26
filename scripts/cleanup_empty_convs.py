# SPDX-License-Identifier: Apache-2.0
"""One-shot cleanup of 0-turn conversations from `memory/{slug}/repairs/{repair_id}/`.

Pre-lazy-materialization, every WS open / tier switch / "+ Nouvelle conversation"
click eagerly created an index entry + conv directory, even when the technician
never sent a message. This script walks every repair index and removes the
entries (and their dirs) that satisfy ALL of the following:

  - `turns == 0` in `index.json`
  - either `messages.jsonl` is missing OR it has no records that contain a
    real user message (i.e. only intro lines that start with the
    "[Nouvelle session de diagnostic]" wrapper count as auto-injected
    context, not actual content)
  - any `ma_session_{tier}.json` files in the conv dir are removed too

`--dry-run` (default) prints what would be deleted without touching disk.
Pass `--apply` to actually perform the cleanup.

Usage:
    .venv/bin/python scripts/cleanup_empty_convs.py
    .venv/bin/python scripts/cleanup_empty_convs.py --apply
    .venv/bin/python scripts/cleanup_empty_convs.py --memory-root /custom/path --apply
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from api.config import get_settings


INTRO_PREFIX = "[Nouvelle session de diagnostic]"


def _has_real_user_content(messages_jsonl: Path) -> bool:
    """True if the JSONL contains any record that isn't just the auto-injected
    device-context intro. Pending convs that materialized only because the
    intro was flushed should still be considered empty for cleanup purposes
    if no user typed anything.
    """
    if not messages_jsonl.exists():
        return False
    try:
        text = messages_jsonl.read_text(encoding="utf-8")
    except OSError:
        return False
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        event = rec.get("event") or {}
        role = event.get("role")
        content = event.get("content")
        if role == "assistant":
            return True  # any assistant turn means real activity happened
        if role == "user":
            if isinstance(content, str):
                if not content.startswith(INTRO_PREFIX):
                    return True
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_result":
                        return True
                    if block.get("type") == "text":
                        text_block = block.get("text") or ""
                        if not text_block.startswith(INTRO_PREFIX):
                            return True
    return False


def _is_empty(entry: dict, conv_dir: Path) -> bool:
    """True iff this conv looks 0-turn AND has no real on-disk content."""
    if (entry.get("turns") or 0) != 0:
        return False
    if (entry.get("cost_usd") or 0.0) > 0:
        return False
    if entry.get("title"):
        # If the title was stamped, the user typed at least once even if the
        # turn counter is stuck — preserve out of an abundance of caution.
        return False
    return not _has_real_user_content(conv_dir / "messages.jsonl")


def cleanup_repair(
    repair_dir: Path,
    *,
    apply: bool,
) -> tuple[int, int]:
    """Walk one repair's `conversations/index.json`, drop empty entries.

    Returns `(scanned, removed)` counts for reporting.
    """
    index_path = repair_dir / "conversations" / "index.json"
    if not index_path.exists():
        return (0, 0)
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return (0, 0)
    if not isinstance(index, list):
        return (0, 0)
    scanned = len(index)
    kept: list[dict] = []
    removed = 0
    for entry in index:
        conv_id = entry.get("id")
        if not isinstance(conv_id, str):
            kept.append(entry)
            continue
        conv_dir = repair_dir / "conversations" / conv_id
        if _is_empty(entry, conv_dir):
            removed += 1
            label = (
                f"{repair_dir.parent.parent.name}/{repair_dir.name}/{conv_id}"
                f" ({entry.get('tier', '?')})"
            )
            if apply:
                if conv_dir.exists():
                    shutil.rmtree(conv_dir)
                print(f"  [removed] {label}")
            else:
                print(f"  [dry-run] would remove {label}")
        else:
            kept.append(entry)
    if apply and removed > 0:
        index_path.write_text(json.dumps(kept, indent=2), encoding="utf-8")
    return (scanned, removed)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--memory-root",
        type=Path,
        default=None,
        help="Override settings.memory_root (defaults to .env value)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete (default is dry-run — list what would be removed)",
    )
    args = parser.parse_args()

    memory_root: Path = args.memory_root or Path(get_settings().memory_root)
    if not memory_root.exists():
        print(f"memory root not found: {memory_root}", file=sys.stderr)
        return 1

    if not args.apply:
        print(f"[dry-run] scanning {memory_root}")
    else:
        print(f"[apply] cleaning {memory_root}")

    total_scanned = 0
    total_removed = 0
    for slug_dir in sorted(memory_root.iterdir()):
        if not slug_dir.is_dir():
            continue
        repairs_dir = slug_dir / "repairs"
        if not repairs_dir.is_dir():
            continue
        for repair_dir in sorted(repairs_dir.iterdir()):
            if not repair_dir.is_dir():
                continue
            scanned, removed = cleanup_repair(repair_dir, apply=args.apply)
            total_scanned += scanned
            total_removed += removed

    summary_label = "removed" if args.apply else "would remove"
    print(
        f"\n{total_scanned} conversation entr{'y' if total_scanned == 1 else 'ies'} "
        f"scanned · {summary_label} {total_removed}"
    )
    if not args.apply and total_removed > 0:
        print("\nRe-run with --apply to delete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
