# SPDX-License-Identifier: Apache-2.0
"""Per-device memory store cache for Managed Agents sessions.

Anthropic **memory stores** are in Research Preview at the time of writing.
When the beta is available, the first session for a given device slug
creates a store via the API and persists its id in
`memory/{slug}/managed.json`. Subsequent sessions reuse that store so the
agent retains learnings across repairs.

Graceful degradation: if the memory-store beta is not enabled on the
current Anthropic account (403 / unsupported SDK method), this module
returns `None` and the caller proceeds without memory. The diagnostic
session still works — it just starts cold each time.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from anthropic import AsyncAnthropic

from api.config import get_settings

logger = logging.getLogger("microsolder.agent.memory_stores")


async def ensure_memory_store(client: AsyncAnthropic, device_slug: str) -> str | None:
    """Return the `memstore_...` id for this device, or `None` if unavailable.

    Creates the store lazily on first access and persists the id in
    `memory/{slug}/managed.json`. On any failure (missing beta access,
    network error, missing SDK surface) logs at WARNING and returns `None`
    so the session can run without memory.
    """
    settings = get_settings()
    pack_dir = Path(settings.memory_root) / device_slug
    pack_dir.mkdir(parents=True, exist_ok=True)
    meta_path = pack_dir / "managed.json"

    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
        except json.JSONDecodeError:
            meta = {}
        store_id = meta.get("memory_store_id")
        if store_id:
            return store_id

    try:
        memory_stores = client.beta.memory_stores  # type: ignore[attr-defined]
    except AttributeError:
        logger.warning(
            "[MemoryStore] anthropic SDK has no beta.memory_stores surface; "
            "running session without memory for device=%s",
            device_slug,
        )
        return None

    try:
        store = await memory_stores.create(
            name=f"microsolder-{device_slug}",
            description=(
                f"Repair history and learned facts for device {device_slug}. "
                "Contains previous diagnostic sessions, confirmed component "
                "failures, and patterns observed across multiple repairs."
            ),
        )
    except Exception as exc:  # noqa: BLE001 — beta may be denied, want a single catch
        logger.warning(
            "[MemoryStore] create failed for device=%s: %s — running without memory",
            device_slug,
            exc,
        )
        return None

    meta_path.write_text(
        json.dumps(
            {"memory_store_id": store.id, "device_slug": device_slug},
            indent=2,
        )
        + "\n"
    )
    logger.info("[MemoryStore] Created id=%s for device=%s", store.id, device_slug)
    return store.id
