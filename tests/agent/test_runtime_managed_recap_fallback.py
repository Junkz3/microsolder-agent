# SPDX-License-Identifier: Apache-2.0
"""Recap-on-resume uses JSONL when the MA event stream can't rebuild history."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.agent import runtime_managed as rm
from api.agent.chat_history import append_event, create_conversation


def _haiku_response(text: str = "résumé ok") -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=42, output_tokens=17),
    )


def _seed_jsonl(memory_root, slug, repair_id, conv_id) -> None:
    append_event(
        device_slug=slug, repair_id=repair_id, conv_id=conv_id,
        event={"role": "user", "content": "Le téléphone ne démarre plus"},
        memory_root=memory_root,
    )
    append_event(
        device_slug=slug, repair_id=repair_id, conv_id=conv_id,
        event={
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Je vérifie la ligne PP_VDD_MAIN"},
                {"type": "tool_use", "id": "tu1", "name": "mb_get_component",
                 "input": {"refdes": "U1500"}},
            ],
        },
        memory_root=memory_root,
    )
    append_event(
        device_slug=slug, repair_id=repair_id, conv_id=conv_id,
        event={
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "tu1",
                 "content": "{\"found\": true, \"voltage\": \"3.7V\"}"},
            ],
        },
        memory_root=memory_root,
    )


@pytest.fixture
def jsonl_fixture(tmp_path, monkeypatch):
    class FakeSettings:
        anthropic_api_key = "sk-test"
        memory_root = str(tmp_path)
        chat_history_backend = "jsonl"
    monkeypatch.setattr("api.agent.chat_history.get_settings", lambda: FakeSettings())

    slug, repair_id = "iphone-x", "repair-abc"
    conv_id = create_conversation(
        device_slug=slug, repair_id=repair_id, tier="fast", memory_root=tmp_path,
    )
    _seed_jsonl(tmp_path, slug, repair_id, conv_id)
    return SimpleNamespace(
        root=tmp_path, slug=slug, repair_id=repair_id, conv_id=conv_id,
    )


@pytest.mark.asyncio
async def test_falls_back_to_jsonl_when_ma_events_list_empty(jsonl_fixture):
    """Dead MA session (events.list empty) → recap is built from the local JSONL."""
    client = MagicMock()

    async def empty_iter():
        return
        yield  # pragma: no cover
    client.beta.sessions.events.list = MagicMock(return_value=empty_iter())
    client.messages.create = AsyncMock(return_value=_haiku_response())

    result = await rm._summarize_prior_history_for_resume(
        client=client,
        old_session_id="sesn_dead",
        device_slug=jsonl_fixture.slug,
        repair_id=jsonl_fixture.repair_id,
        conv_id=jsonl_fixture.conv_id,
        memory_root=jsonl_fixture.root,
    )

    assert result is not None
    assert result["summary"] == "résumé ok"
    client.messages.create.assert_awaited_once()
    transcript = client.messages.create.await_args.kwargs["messages"][0]["content"]
    assert "[user] Le téléphone ne démarre plus" in transcript
    assert "[agent] Je vérifie la ligne PP_VDD_MAIN" in transcript
    assert "[tool] mb_get_component(" in transcript
    assert "[tool_result] →" in transcript


@pytest.mark.asyncio
async def test_falls_back_to_jsonl_when_events_list_raises(jsonl_fixture):
    """API error on events.list is treated like empty — JSONL still saves the recap."""
    client = MagicMock()
    client.beta.sessions.events.list = MagicMock(side_effect=RuntimeError("boom"))
    client.messages.create = AsyncMock(return_value=_haiku_response("ok"))

    result = await rm._summarize_prior_history_for_resume(
        client=client,
        old_session_id="sesn_dead",
        device_slug=jsonl_fixture.slug,
        repair_id=jsonl_fixture.repair_id,
        conv_id=jsonl_fixture.conv_id,
        memory_root=jsonl_fixture.root,
    )

    assert result is not None
    assert result["summary"] == "ok"


@pytest.mark.asyncio
async def test_ma_events_still_win_when_non_empty(jsonl_fixture, monkeypatch):
    """If MA returns events, we never touch the JSONL even when it has content."""
    client = MagicMock()

    async def one_event_iter():
        yield SimpleNamespace(
            type="user.message",
            content=[SimpleNamespace(type="text", text="Question depuis MA")],
        )
    client.beta.sessions.events.list = MagicMock(return_value=one_event_iter())
    client.messages.create = AsyncMock(return_value=_haiku_response())

    # Make the invariant explicit: when MA wins we never hit disk.
    load_events_spy = MagicMock(wraps=rm.load_events)
    monkeypatch.setattr(rm, "load_events", load_events_spy)

    result = await rm._summarize_prior_history_for_resume(
        client=client,
        old_session_id="sesn_live",
        device_slug=jsonl_fixture.slug,
        repair_id=jsonl_fixture.repair_id,
        conv_id=jsonl_fixture.conv_id,
        memory_root=jsonl_fixture.root,
    )

    assert result is not None
    transcript = client.messages.create.await_args.kwargs["messages"][0]["content"]
    assert "Question depuis MA" in transcript
    # The JSONL-only content must not leak in when MA had events.
    assert "Le téléphone ne démarre plus" not in transcript
    load_events_spy.assert_not_called()


@pytest.mark.asyncio
async def test_returns_none_when_no_source_has_content(tmp_path, monkeypatch):
    """No MA events + no JSONL on disk → nothing to summarize, caller sees None."""
    class FakeSettings:
        anthropic_api_key = "sk-test"
        memory_root = str(tmp_path)
        chat_history_backend = "jsonl"
    monkeypatch.setattr("api.agent.chat_history.get_settings", lambda: FakeSettings())

    client = MagicMock()

    async def empty_iter():
        return
        yield  # pragma: no cover
    client.beta.sessions.events.list = MagicMock(return_value=empty_iter())
    client.messages.create = AsyncMock()

    result = await rm._summarize_prior_history_for_resume(
        client=client,
        old_session_id="sesn_dead",
        device_slug="iphone-x",
        repair_id="repair-without-jsonl",
        conv_id="deadbeef",
        memory_root=tmp_path,
    )

    assert result is None
    client.messages.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_intro_wrapper_is_stripped_in_jsonl_path(tmp_path, monkeypatch):
    """Hidden bootstrap prefix (device + technician block) shouldn't leak into recap."""
    class FakeSettings:
        anthropic_api_key = "sk-test"
        memory_root = str(tmp_path)
        chat_history_backend = "jsonl"
    monkeypatch.setattr("api.agent.chat_history.get_settings", lambda: FakeSettings())

    slug, repair_id = "iphone-x", "r1"
    conv_id = create_conversation(
        device_slug=slug, repair_id=repair_id, tier="fast", memory_root=tmp_path,
    )
    intro_then_real = (
        "[Nouvelle session de diagnostic]\n"
        "Device: iPhone X (slug: iphone-x)\n\n---\n\n"
        "Salut, j'ai un souci de charge"
    )
    append_event(
        device_slug=slug, repair_id=repair_id, conv_id=conv_id,
        event={"role": "user", "content": intro_then_real},
        memory_root=tmp_path,
    )

    client = MagicMock()

    async def empty_iter():
        return
        yield  # pragma: no cover
    client.beta.sessions.events.list = MagicMock(return_value=empty_iter())
    client.messages.create = AsyncMock(return_value=_haiku_response())

    await rm._summarize_prior_history_for_resume(
        client=client,
        old_session_id="sesn_dead",
        device_slug=slug,
        repair_id=repair_id,
        conv_id=conv_id,
        memory_root=tmp_path,
    )

    transcript = client.messages.create.await_args.kwargs["messages"][0]["content"]
    assert "[user] Salut, j'ai un souci de charge" in transcript
    assert "Nouvelle session de diagnostic" not in transcript
