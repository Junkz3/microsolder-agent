"""Atomic IO for memory/_stock/inventory.json.

Concurrent writes guarded by fcntl.flock (POSIX). Atomic publish via
write-temp-then-rename. See spec §12.
"""

from __future__ import annotations

import fcntl
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from api.config import get_settings
from api.stock.schemas import (
    ConsumedEvent,
    DonorEntry,
    StockInventory,
)


def _memory_root() -> Path:
    return Path(get_settings().memory_root)


def _stock_root() -> Path:
    return _memory_root() / "_stock"


def _inventory_path() -> Path:
    return _stock_root() / "inventory.json"


def load_inventory() -> StockInventory:
    p = _inventory_path()
    if not p.exists():
        return StockInventory(schema_version="1.0", donors={})
    with p.open("r", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_SH)
        try:
            return StockInventory.model_validate_json(f.read())
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def save_inventory(inv: StockInventory) -> None:
    """Atomic write: write temp file in same dir, fsync, rename."""
    root = _stock_root()
    root.mkdir(parents=True, exist_ok=True)
    payload = inv.model_dump_json(indent=2)
    fd, tmp_path = tempfile.mkstemp(prefix=".inventory.", suffix=".json", dir=str(root))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        os.replace(tmp_path, _inventory_path())
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def next_donor_id(slug: str) -> str:
    """Find next NNN counter for slug → '{slug}-donor-{YYYY}-{NNN}'."""
    inv = load_inventory()
    year = datetime.now(UTC).year
    pattern = re.compile(rf"^{re.escape(slug)}-donor-{year}-(\d{{3}})$")
    max_n = 0
    for did in inv.donors:
        m = pattern.match(did)
        if m:
            n = int(m.group(1))
            if n > max_n:
                max_n = n
    return f"{slug}-donor-{year}-{max_n + 1:03d}"


def mark_donor(
    device_slug: str,
    label: str,
    condition: str = "donor_only",
) -> str:
    """Add a donor entry. Returns the generated donor_id.

    Raises FileNotFoundError if the device_slug doesn't exist in memory/.
    """
    if not (_memory_root() / device_slug).exists():
        raise FileNotFoundError(f"device_slug not found in memory/: {device_slug}")

    donor_id = next_donor_id(device_slug)
    inv = load_inventory()
    inv.donors[donor_id] = DonorEntry(
        donor_id=donor_id,
        device_slug=device_slug,
        label=label,
        added_at=datetime.now(UTC),
        condition=condition,  # type: ignore[arg-type]
        consumed={},
    )
    save_inventory(inv)
    return donor_id


def unmark_donor(donor_id: str) -> bool:
    inv = load_inventory()
    if donor_id not in inv.donors:
        return False
    del inv.donors[donor_id]
    save_inventory(inv)
    return True


def consume_part(
    donor_id: str,
    refdes: str,
    repair_id: str | None = None,
    notes: str | None = None,
) -> bool:
    """Mark a refdes consumed on a donor. Idempotent — re-call updates notes."""
    inv = load_inventory()
    if donor_id not in inv.donors:
        return False
    inv.donors[donor_id].consumed[refdes] = ConsumedEvent(
        refdes=refdes,
        consumed_at=datetime.now(UTC),
        repair_id=repair_id,
        notes=notes,
    )
    save_inventory(inv)
    return True


def unconsume_part(donor_id: str, refdes: str) -> bool:
    inv = load_inventory()
    if donor_id not in inv.donors:
        return False
    if refdes not in inv.donors[donor_id].consumed:
        return False
    del inv.donors[donor_id].consumed[refdes]
    save_inventory(inv)
    return True
