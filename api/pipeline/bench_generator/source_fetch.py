# SPDX-License-Identifier: Apache-2.0
"""HTTP fetch + HTML-to-text helper for the V6 source-URL grounding check.

Phase 6 (V6) of the bench-generator validator confirms that each accepted
scenario's `cause.refdes` actually appears in the literal content of its
`source_url`. This catches "cross-source contamination" — Scout writing a
quote attributed to URL A but with refdes that only appear in URL B.

Design notes:
- httpx async client, 10 s connect/read timeout, 1 retry on transient
  network errors, follow redirects.
- HTML → text via regex strip (no bs4 dependency). The check only needs
  case-insensitive substring lookup on a refdes (e.g. "U14"), so heavy
  DOM parsing isn't required.
- Module-level cache keyed by URL — within a single bench-gen run, each
  URL is fetched at most once across all scenarios that cite it.
- All exceptions are caught and surfaced as None — the caller decides
  how to react (V6 treats unreachable URLs as a soft reject motive).
"""

from __future__ import annotations

import asyncio
import logging
import re
from html import unescape

import httpx

logger = logging.getLogger("microsolder.bench_generator.source_fetch")


# Module-level cache. Cleared by callers between bench-gen runs that want
# fresh fetches. Maps URL → fetched text body, or None when the fetch
# failed (so we don't re-attempt within the same run).
_CACHE: dict[str, str | None] = {}


# Stripping <script> / <style> bodies along with their tags is required —
# leaving the inline CSS / JS in the text would let unrelated refdes-shaped
# tokens slip through (e.g. CSS classes named "U10").
_SCRIPT_OR_STYLE_RE = re.compile(
    r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL
)
_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def html_to_text(html: str) -> str:
    """Crude but adequate HTML-to-text conversion.

    Drops `<script>` and `<style>` blocks entirely (including bodies),
    strips remaining tags, decodes entities, collapses whitespace. Used
    only for substring containment checks — no semantic parsing needed.
    """
    cleaned = _SCRIPT_OR_STYLE_RE.sub(" ", html)
    cleaned = _TAG_RE.sub(" ", cleaned)
    cleaned = unescape(cleaned)
    cleaned = _WHITESPACE_RE.sub(" ", cleaned)
    return cleaned.strip()


async def fetch_text(
    url: str,
    *,
    timeout_s: float = 10.0,
    retries: int = 1,
    client: httpx.AsyncClient | None = None,
) -> str | None:
    """Return the URL's body as plain text, None on failure.

    Cached at module level keyed by URL; second call for the same URL
    returns the cached value (or cached None when the prior attempt
    failed). Tests can clear the cache via `clear_cache()`.

    The optional `client` parameter accepts a pre-configured
    `httpx.AsyncClient` (used by tests with `httpx.MockTransport`). When
    omitted, a fresh client is created per call with redirect-following
    enabled and a short timeout.
    """
    if url in _CACHE:
        return _CACHE[url]

    text: str | None = None
    last_exc: Exception | None = None

    for attempt in range(retries + 1):
        try:
            if client is not None:
                response = await client.get(
                    url, timeout=timeout_s, follow_redirects=True
                )
            else:
                async with httpx.AsyncClient(
                    timeout=timeout_s, follow_redirects=True
                ) as fresh:
                    response = await fresh.get(url)
            response.raise_for_status()
            text = html_to_text(response.text)
            break
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            last_exc = exc
            if attempt < retries:
                await asyncio.sleep(0.5)
                continue
            logger.warning(
                "[source_fetch] giving up on url=%s after %d attempts: %s",
                url,
                attempt + 1,
                exc,
            )

    if text is None and last_exc is not None:
        logger.info("[source_fetch] last error for %s: %r", url, last_exc)

    _CACHE[url] = text
    return text


async def fetch_many(
    urls: set[str],
    *,
    timeout_s: float = 10.0,
    retries: int = 1,
    client: httpx.AsyncClient | None = None,
) -> dict[str, str | None]:
    """Fetch a batch of URLs concurrently. Returns url → text-or-None.

    Order is irrelevant; failures and successes coexist in the returned
    dict. Useful for the bench-gen orchestrator: pre-fetch every URL
    cited by any accepted scenario in one round-trip burst before V6
    runs synchronously over the result.
    """
    urls_list = sorted(urls)
    results = await asyncio.gather(
        *[
            fetch_text(u, timeout_s=timeout_s, retries=retries, client=client)
            for u in urls_list
        ],
        return_exceptions=False,
    )
    return dict(zip(urls_list, results, strict=True))


def clear_cache() -> None:
    """Clear the module-level cache. Tests rely on this to isolate cases."""
    _CACHE.clear()
