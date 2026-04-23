"""End-to-end tests for the direct diagnostic runtime over a fake WebSocket."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.board.model import Board, Layer, Part, Pin, Point
from api.session.state import SessionState


class FakeWS:
    """Minimal WebSocket double that captures send_json calls."""

    def __init__(self, user_messages: list[str]) -> None:
        self.sent: list[dict] = []
        self._inbox: asyncio.Queue[str] = asyncio.Queue()
        for m in user_messages:
            self._inbox.put_nowait(json.dumps({"type": "message", "text": m}))
        self._closed = False

    async def accept(self) -> None:
        return

    async def close(self) -> None:
        self._closed = True

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)

    async def receive_text(self) -> str:
        if self._closed or self._inbox.empty():
            from fastapi import WebSocketDisconnect
            raise WebSocketDisconnect
        return await self._inbox.get()


def _stub_session(monkeypatch: pytest.MonkeyPatch, board: Board | None) -> None:
    """Force SessionState.from_device to return a pre-built session."""
    def _from_device(_slug: str) -> SessionState:
        s = SessionState()
        if board is not None:
            s.set_board(board)
        return s
    monkeypatch.setattr(
        "api.agent.runtime_direct.SessionState.from_device",
        staticmethod(_from_device),
    )


def _board_with_u7() -> Board:
    return Board(
        board_id="t", file_hash="sha256:x", source_format="t",
        outline=[],
        parts=[Part(refdes="U7", layer=Layer.TOP, is_smd=True,
                    bbox=(Point(x=0, y=0), Point(x=10, y=10)), pin_refs=[0, 1])],
        pins=[
            Pin(part_refdes="U7", index=1, pos=Point(x=2, y=2), layer=Layer.TOP),
            Pin(part_refdes="U7", index=2, pos=Point(x=8, y=8), layer=Layer.TOP),
        ],
        nets=[], nails=[],
    )


def _mock_anthropic(responses: list[MagicMock]) -> MagicMock:
    """Build an AsyncAnthropic whose messages.create cycles through `responses`."""
    client = MagicMock()
    client.messages.create = AsyncMock(side_effect=responses)
    return client


def _text_response(text: str) -> MagicMock:
    block = MagicMock(type="text", text=text)
    return MagicMock(content=[block], stop_reason="end_turn")


def _tool_use_response(name: str, tool_input: dict, tool_id: str = "toolu_1") -> MagicMock:
    # NOTE: `name` is a reserved kwarg in MagicMock (sets the mock's display
    # name, not a data attribute). Set it explicitly after construction.
    block = MagicMock(type="tool_use", input=tool_input, id=tool_id)
    block.name = name
    return MagicMock(content=[block], stop_reason="tool_use")


def _patch_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    import api.agent.runtime_direct as rt
    monkeypatch.setattr(rt, "get_settings", lambda: MagicMock(
        anthropic_api_key="sk-fake",
        memory_root=Path("/tmp/nope"),
        anthropic_model_main="claude-opus-4-7",
    ))


@pytest.mark.asyncio
async def test_bv_highlight_emits_tool_use_then_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """Agent calls bv_highlight(U7) → WS sees tool_use, then boardview.highlight, then final message."""
    _stub_session(monkeypatch, _board_with_u7())
    import api.agent.runtime_direct as rt
    fake_client = _mock_anthropic([
        _tool_use_response("bv_highlight", {"refdes": "U7"}),
        _text_response("Done."),
    ])
    monkeypatch.setattr(rt, "AsyncAnthropic", lambda api_key: fake_client)
    _patch_settings(monkeypatch)

    ws = FakeWS(["show U7"])
    await rt.run_diagnostic_session_direct(ws, "demo-pi", tier="fast")

    types = [m.get("type") for m in ws.sent]
    assert "session_ready" in types
    tu_idx = types.index("tool_use")
    bv_idx = next(i for i, t in enumerate(types) if t == "boardview.highlight")
    assert tu_idx < bv_idx
    assert any(m.get("type") == "message" and m.get("role") == "assistant" for m in ws.sent)


@pytest.mark.asyncio
async def test_bv_highlight_unknown_emits_no_boardview_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """bv_highlight(U999) → tool_use, NO boardview.* event, final message present."""
    _stub_session(monkeypatch, _board_with_u7())
    import api.agent.runtime_direct as rt
    fake_client = _mock_anthropic([
        _tool_use_response("bv_highlight", {"refdes": "U999"}),
        _text_response("Couldn't find that one."),
    ])
    monkeypatch.setattr(rt, "AsyncAnthropic", lambda api_key: fake_client)
    _patch_settings(monkeypatch)

    ws = FakeWS(["show U999"])
    await rt.run_diagnostic_session_direct(ws, "demo-pi", tier="fast")

    types = [m.get("type", "") for m in ws.sent]
    assert "tool_use" in types
    assert not any(t.startswith("boardview.") for t in types)


@pytest.mark.asyncio
async def test_tool_result_never_contains_event_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Core design invariant: the tool_result sent back to the agent has no 'event' key."""
    _stub_session(monkeypatch, _board_with_u7())
    import api.agent.runtime_direct as rt

    captured_messages: list[list[dict]] = []

    async def recording_create(**kwargs):
        captured_messages.append(list(kwargs["messages"]))
        if len(captured_messages) == 1:
            return _tool_use_response("bv_highlight", {"refdes": "U7"})
        return _text_response("ok")

    fake_client = MagicMock()
    fake_client.messages.create = AsyncMock(side_effect=recording_create)
    monkeypatch.setattr(rt, "AsyncAnthropic", lambda api_key: fake_client)
    _patch_settings(monkeypatch)

    ws = FakeWS(["show U7"])
    await rt.run_diagnostic_session_direct(ws, "demo-pi", tier="fast")

    second_call_messages = captured_messages[1]
    tool_result_blocks = [
        b for m in second_call_messages
        for b in (m.get("content") if isinstance(m.get("content"), list) else [])
        if isinstance(b, dict) and b.get("type") == "tool_result"
    ]
    assert tool_result_blocks, "expected at least one tool_result block"
    decoded = json.loads(tool_result_blocks[0]["content"])
    assert "event" not in decoded
    assert decoded.get("ok") is True


@pytest.mark.asyncio
async def test_sanitizer_wraps_unknown_refdes_in_final_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """Agent text 'U999 is suspect' gets wrapped to '⟨?U999⟩ is suspect' before WS send."""
    _stub_session(monkeypatch, _board_with_u7())
    import api.agent.runtime_direct as rt
    fake_client = _mock_anthropic([
        _text_response("U999 is suspect"),
    ])
    monkeypatch.setattr(rt, "AsyncAnthropic", lambda api_key: fake_client)
    _patch_settings(monkeypatch)

    ws = FakeWS(["what's wrong?"])
    await rt.run_diagnostic_session_direct(ws, "demo-pi", tier="fast")

    agent_msgs = [m for m in ws.sent if m.get("type") == "message" and m.get("role") == "assistant"]
    assert agent_msgs
    assert "⟨?U999⟩" in agent_msgs[0]["text"]
    assert "U999 is suspect" not in agent_msgs[0]["text"]
