# SPDX-License-Identifier: Apache-2.0
"""Tests for V6 — refdes-in-source-URL-content check.

Two surfaces are tested in isolation:
- the HTTP / HTML helper in `source_fetch.py` (mocked transport, no real
  network),
- the synchronous validator pass that consumes the pre-fetched text dict.

The end-to-end orchestrator wiring is exercised indirectly via the
existing bench_generator tests once the offline `skip_url_check=True`
mode is asserted to behave like before.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from api.pipeline.bench_generator import source_fetch
from api.pipeline.bench_generator.schemas import (
    Cause,
    EvidenceSpan,
    ProposedScenarioDraft,
)
from api.pipeline.bench_generator.validator import (
    check_refdes_in_url_content,
    run_all,
)


def _draft(refdes: str = "U14", url: str = "https://example.com/a") -> ProposedScenarioDraft:
    return ProposedScenarioDraft(
        local_id=f"{refdes.lower()}-test",
        cause=Cause(refdes=refdes, mode="dead"),
        expected_dead_rails=[],
        expected_dead_components=[],
        source_url=url,
        source_quote=(
            "long enough source quote " * 4
        ),  # ≥ 50 chars; content irrelevant for V6
        confidence=0.9,
        evidence=[
            EvidenceSpan(
                field="cause.refdes",
                source_quote_substring="long enough source quote",
                reasoning="stub",
            ),
            EvidenceSpan(
                field="cause.mode",
                source_quote_substring="long enough source quote",
                reasoning="stub",
            ),
        ],
        reasoning_summary="stub",
    )


# --- check_refdes_in_url_content (sync) -----------------------------------


def test_v6_accepts_when_refdes_in_content() -> None:
    """Plain refdes mention in fetched body — accepted."""
    draft = _draft(refdes="U14")
    text = "On the MNT Reform R-2 the U14 always-on regulator powers LPC_VCC."
    assert check_refdes_in_url_content(draft, text) is None


def test_v6_accepts_with_word_boundary_case_insensitive() -> None:
    """Lowercase refdes matches uppercase via word-boundary regex."""
    draft = _draft(refdes="U14")
    text = "the always-on regulator (u14) powers the LPC."
    assert check_refdes_in_url_content(draft, text) is None


def test_v6_rejects_when_refdes_absent() -> None:
    """Cross-source contamination — refdes literal in dump but not in URL body."""
    draft = _draft(refdes="J24", url="https://forum.example/eDP-cable-thread")
    text = (
        "The internal display flickered after I reassembled the chassis. "
        "Turned out the USB cable was running across the eDP connector."
    )
    rej = check_refdes_in_url_content(draft, text)
    assert rej is not None
    assert rej.motive == "refdes_not_in_url_content"


def test_v6_word_boundary_excludes_substring_match() -> None:
    """U7 must not match AU7T or U70 inside an unrelated identifier."""
    draft = _draft(refdes="U7")
    text = "AU7T-class FPGA at the corner; the rail U70 carries 1.8V."
    rej = check_refdes_in_url_content(draft, text)
    assert rej is not None
    assert rej.motive == "refdes_not_in_url_content"


def test_v6_unreachable_url_yields_soft_reject() -> None:
    """`fetched_text=None` (network failure) is its own rejection motive."""
    draft = _draft(refdes="U14", url="https://invalid.example/")
    rej = check_refdes_in_url_content(draft, None)
    assert rej is not None
    assert rej.motive == "source_url_unreachable"


# --- run_all V6 wiring ----------------------------------------------------


def test_run_all_runs_v6_when_url_texts_provided(toy_graph) -> None:
    """run_all accepts a URL→text dict and applies V6 to surviving drafts."""
    # A draft that passes V1-V5 but fails V6 (refdes not in source URL body).
    d = ProposedScenarioDraft(
        local_id="u7-fab",
        cause=Cause(refdes="U7", mode="dead"),
        expected_dead_rails=[],
        expected_dead_components=[],
        source_url="https://forum.example/firmware-mismatch",
        source_quote=(
            "U7 is the main 5V buck regulator on this board and it died."
        ),
        confidence=0.85,
        evidence=[
            EvidenceSpan(
                field="cause.refdes",
                source_quote_substring="U7",
                reasoning="literal",
            ),
            EvidenceSpan(
                field="cause.mode",
                source_quote_substring="died",
                reasoning="dead",
            ),
        ],
        reasoning_summary="stub",
    )
    url_texts = {
        # The actual page only discusses firmware versions, not U7 silicon.
        "https://forum.example/firmware-mismatch": (
            "Update LPC firmware from version 1.0 to 1.2 to fix the boot loop."
        ),
    }
    accepted, rejected = run_all([d], toy_graph, url_texts=url_texts)
    assert accepted == []
    assert len(rejected) == 1
    assert rejected[0].motive == "refdes_not_in_url_content"


def test_run_all_skips_v6_when_url_texts_none(toy_graph) -> None:
    """No url_texts → V6 skipped, drafts that previously passed continue to."""
    d = ProposedScenarioDraft(
        local_id="u7-ok",
        cause=Cause(refdes="U7", mode="dead"),
        expected_dead_rails=[],
        expected_dead_components=[],
        source_url="https://forum.example/anywhere",
        source_quote="The U7 buck regulator stops switching after 30 minutes of load.",
        confidence=0.85,
        evidence=[
            EvidenceSpan(
                field="cause.refdes",
                source_quote_substring="U7",
                reasoning="literal",
            ),
            EvidenceSpan(
                field="cause.mode",
                source_quote_substring="stops switching",
                reasoning="dead",
            ),
        ],
        reasoning_summary="stub",
    )
    accepted, rejected = run_all([d], toy_graph, url_texts=None)
    assert len(accepted) == 1
    assert rejected == []


# --- source_fetch helpers -------------------------------------------------


def test_html_to_text_drops_script_and_tags() -> None:
    html = (
        "<html><head><style>.U10 { color: red; }</style></head>"
        "<body><script>const U99 = 'hidden';</script>"
        "<p>The <b>U14</b> regulator powers LPC_VCC at 3.3 V.</p>"
        "</body></html>"
    )
    text = source_fetch.html_to_text(html)
    # Script + style bodies dropped — would have leaked U10 / U99.
    assert "U99" not in text
    assert "U10" not in text
    # Visible body content survives, entities decoded, whitespace collapsed.
    assert "U14 regulator powers LPC_VCC at 3.3 V" in text


def test_fetch_text_uses_mock_transport_and_caches(monkeypatch) -> None:
    """fetch_text returns the body text and caches per-URL within a run."""
    source_fetch.clear_cache()
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(
            200,
            html="<html><body>The U14 regulator is fine.</body></html>",
            text="<html><body>The U14 regulator is fine.</body></html>",
        )

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)

    async def _go() -> None:
        a = await source_fetch.fetch_text("https://example.com/page", client=client)
        b = await source_fetch.fetch_text("https://example.com/page", client=client)
        assert a is not None and "U14" in a
        assert a == b
        assert call_count["n"] == 1  # second call hit the cache
        await client.aclose()

    asyncio.run(_go())


def test_fetch_text_returns_none_on_http_error(monkeypatch) -> None:
    """A 404 / 5xx response yields None (and is cached as None)."""
    source_fetch.clear_cache()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)

    async def _go() -> None:
        result = await source_fetch.fetch_text(
            "https://example.com/missing", client=client, retries=0
        )
        assert result is None
        await client.aclose()

    asyncio.run(_go())
