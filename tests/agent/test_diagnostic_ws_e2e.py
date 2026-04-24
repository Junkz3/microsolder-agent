# SPDX-License-Identifier: Apache-2.0
"""End-to-end test for /ws/diagnostic/{slug} over a real TestClient WebSocket.

Unlike the existing tests in test_ws_flow.py — which drive the runtime loop
with a `FakeWS` double and skip routing entirely — this file opens the WS
through FastAPI's TestClient (`client.websocket_connect(...)`). That means
every layer between the browser and the runtime is exercised: URL routing,
query-param parsing (`tier`, `repair`, `conv`), the `DIAGNOSTIC_MODE`
dispatch in api.main, the WS accept handshake, and the exact JSON frame
protocol the frontend sees on the wire.

Anthropic is mocked at the `AsyncAnthropic` import boundary of
`api.agent.runtime_direct`, so no network is touched and `ANTHROPIC_API_KEY`
doesn't need to be set.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from api.board.model import Board, Layer, Part, Pin, Point
from api.main import app
from api.session.state import SessionState

# ----------------------------------------------------------------------------
# Fake stream helpers — mirror the shape of client.messages.stream(...) just
# enough for the direct runtime. Kept local to avoid cross-importing private
# helpers from test_ws_flow.py.
# ----------------------------------------------------------------------------


class _FakeStream:
    """Async context manager + iterator that yields scripted stream events."""

    def __init__(self, events: list[tuple], final_message: MagicMock) -> None:
        self._events = list(events)
        self._final = final_message
        self._snapshot_content: list = []

    async def __aenter__(self) -> _FakeStream:
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False

    def __aiter__(self) -> _FakeStream:
        return self

    async def __anext__(self):
        if not self._events:
            raise StopAsyncIteration
        event, new_snapshot_content = self._events.pop(0)
        self._snapshot_content = new_snapshot_content
        return event

    @property
    def current_message_snapshot(self) -> SimpleNamespace:
        return SimpleNamespace(content=list(self._snapshot_content))

    async def get_final_message(self) -> MagicMock:
        return self._final


_FAKE_USAGE = SimpleNamespace(
    input_tokens=10,
    output_tokens=5,
    cache_read_input_tokens=0,
    cache_creation_input_tokens=0,
)


def _stream_text(text: str) -> tuple[list[tuple], MagicMock]:
    block = MagicMock(type="text", text=text)
    stop_ev = MagicMock()
    stop_ev.type = "content_block_stop"
    stop_ev.index = 0
    events = [(stop_ev, [block])]
    # usage has to be real ints — cost_from_response runs getattr on this and
    # the result ends up inside a JSON-encoded turn_cost frame.
    final = MagicMock(content=[block], stop_reason="end_turn", usage=_FAKE_USAGE)
    return events, final


def _stream_tool_use(
    name: str, tool_input: dict, tool_id: str = "toolu_1"
) -> tuple[list[tuple], MagicMock]:
    block = MagicMock(type="tool_use", input=tool_input, id=tool_id)
    block.name = name
    final = MagicMock(content=[block], stop_reason="tool_use", usage=_FAKE_USAGE)
    return [], final


def _mock_anthropic(scripted: list[tuple[list[tuple], MagicMock]]) -> MagicMock:
    iterator = iter(scripted)
    client = MagicMock()

    def _stream_factory(**_kwargs):
        events, final = next(iterator)
        return _FakeStream(events, final)

    client.messages.stream = _stream_factory
    return client


def _patch_runtime(
    monkeypatch: pytest.MonkeyPatch,
    *,
    api_key: str = "sk-fake",
    memory_root: str = "/tmp/ws-e2e",
    board: Board | None = None,
    scripted: list | None = None,
) -> MagicMock:
    """Patch api.agent.runtime_direct so the real WS endpoint hits a mocked
    Anthropic client, a stub SessionState, and a fake settings object.

    Returns the mocked AsyncAnthropic client for further assertions.
    """
    import api.agent.runtime_direct as rt
    monkeypatch.setenv("DIAGNOSTIC_MODE", "direct")

    def _from_device(_slug: str) -> SessionState:
        s = SessionState()
        if board is not None:
            s.set_board(board)
        return s

    monkeypatch.setattr(
        "api.agent.runtime_direct.SessionState.from_device",
        staticmethod(_from_device),
    )
    monkeypatch.setattr(rt, "get_settings", lambda: MagicMock(
        anthropic_api_key=api_key,
        memory_root=memory_root,
        anthropic_model_main="claude-opus-4-7",
        anthropic_max_retries=5,
    ))
    fake_client = _mock_anthropic(scripted or [_stream_text("hello")])
    monkeypatch.setattr(rt, "AsyncAnthropic", lambda **_kw: fake_client)
    return fake_client


# ----------------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------------


def test_ws_diagnostic_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Open /ws/diagnostic/demo-pi?tier=fast, send one message, read back the
    session_ready ack, an assistant text frame and the turn_cost marker."""
    _patch_runtime(monkeypatch, scripted=[_stream_text("Hello tech.")])

    with TestClient(app) as client, client.websocket_connect(
        "/ws/diagnostic/demo-pi?tier=fast"
    ) as ws:
        ready = ws.receive_json()
        assert ready["type"] == "session_ready"
        assert ready["mode"] == "direct"
        assert ready["device_slug"] == "demo-pi"
        assert ready["tier"] == "fast"
        assert ready["board_loaded"] is False
        assert ready["repair_id"] is None

        ws.send_json({"type": "message", "text": "what's up"})

        frames: list[dict] = []
        for _ in range(10):
            frame = ws.receive_json()
            frames.append(frame)
            if frame.get("type") == "turn_cost":
                break

        types = [f.get("type") for f in frames]
        assert "message" in types, frames
        assert "turn_cost" in types, frames
        assistant = next(
            f for f in frames
            if f.get("type") == "message" and f.get("role") == "assistant"
        )
        assert assistant["text"] == "Hello tech."


def test_ws_diagnostic_rejects_missing_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without an API key, the server emits an error frame and closes — the
    runtime refuses to spin up an Anthropic client."""
    _patch_runtime(monkeypatch, api_key="", scripted=[_stream_text("unused")])

    with TestClient(app) as client, client.websocket_connect(
        "/ws/diagnostic/demo-pi?tier=fast"
    ) as ws:
        frame = ws.receive_json()
        assert frame["type"] == "error"
        assert "ANTHROPIC_API_KEY" in frame["text"]


def test_ws_diagnostic_invalid_tier_falls_back_to_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """A garbage `tier` query param is silently downgraded to `fast` so the
    session always opens rather than 400ing the browser."""
    _patch_runtime(monkeypatch, scripted=[_stream_text("ok")])

    with TestClient(app) as client, client.websocket_connect(
        "/ws/diagnostic/demo-pi?tier=bogus"
    ) as ws:
        ready = ws.receive_json()
        assert ready["type"] == "session_ready"
        assert ready["tier"] == "fast"


def test_ws_diagnostic_sanitizes_unknown_refdes_over_the_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sanitizer must wrap an unknown refdes in the outbound `message`
    frame — anti-hallucination guarantee, measured through the real WS."""
    board = Board(
        board_id="t", file_hash="sha256:x", source_format="t",
        outline=[],
        parts=[Part(
            refdes="U7", layer=Layer.TOP, is_smd=True,
            bbox=(Point(x=0, y=0), Point(x=10, y=10)), pin_refs=[0, 1],
        )],
        pins=[
            Pin(part_refdes="U7", index=1, pos=Point(x=2, y=2), layer=Layer.TOP),
            Pin(part_refdes="U7", index=2, pos=Point(x=8, y=8), layer=Layer.TOP),
        ],
        nets=[], nails=[],
    )
    _patch_runtime(
        monkeypatch, board=board,
        scripted=[_stream_text("U999 is suspect, U7 is fine.")],
    )

    with TestClient(app) as client, client.websocket_connect(
        "/ws/diagnostic/demo-pi?tier=fast"
    ) as ws:
        ws.receive_json()  # session_ready
        ws.send_json({"type": "message", "text": "diagnose"})

        # First frame that is an assistant message.
        assistant_text = None
        for _ in range(10):
            frame = ws.receive_json()
            if frame.get("type") == "message" and frame.get("role") == "assistant":
                assistant_text = frame["text"]
                break
        assert assistant_text is not None
        assert "⟨?U999⟩" in assistant_text
        assert "U7 is fine" in assistant_text


def test_ws_diagnostic_bv_tool_dispatch_emits_board_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bv_* tool use must surface as both a `tool_use` frame and the
    corresponding `boardview.*` event in the wire order the frontend
    expects (tool_use precedes the board mutation)."""
    board = Board(
        board_id="t", file_hash="sha256:x", source_format="t",
        outline=[],
        parts=[Part(
            refdes="U7", layer=Layer.TOP, is_smd=True,
            bbox=(Point(x=0, y=0), Point(x=10, y=10)), pin_refs=[0, 1],
        )],
        pins=[
            Pin(part_refdes="U7", index=1, pos=Point(x=2, y=2), layer=Layer.TOP),
            Pin(part_refdes="U7", index=2, pos=Point(x=8, y=8), layer=Layer.TOP),
        ],
        nets=[], nails=[],
    )
    _patch_runtime(
        monkeypatch, board=board,
        scripted=[
            _stream_tool_use("bv_highlight", {"refdes": "U7"}),
            _stream_text("Mis en évidence."),
        ],
    )

    with TestClient(app) as client, client.websocket_connect(
        "/ws/diagnostic/demo-pi?tier=fast"
    ) as ws:
        ready = ws.receive_json()
        assert ready["board_loaded"] is True

        ws.send_json({"type": "message", "text": "show U7"})

        # Scripted flow emits exactly 5 frames:
        # turn_cost (stream 1) / tool_use / boardview.highlight /
        # message (stream 2) / turn_cost (stream 2). Pull them explicitly so
        # a missing boardview event fails the test fast instead of blocking
        # forever on receive_json.
        frames = [ws.receive_json() for _ in range(5)]

        types = [f.get("type", "") for f in frames]
        tu_idx = types.index("tool_use")
        bv_idx = next(i for i, t in enumerate(types) if t.startswith("boardview."))
        assert tu_idx < bv_idx, (
            "tool_use must come before its boardview side-effect event"
        )
        tool_use = frames[tu_idx]
        assert tool_use["name"] == "bv_highlight"
        assert tool_use["input"] == {"refdes": "U7"}
