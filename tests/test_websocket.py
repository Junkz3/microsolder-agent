"""Placeholder WebSocket tests — real agent loop not implemented yet."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from api.main import app


def test_websocket_echoes_placeholder() -> None:
    """Until the agent loop is wired up, /ws just echoes a 'not implemented' reply."""
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_text(json.dumps({"text": "hello"}))
            raw = ws.receive_text()

    payload = json.loads(raw)
    assert payload["type"] == "message"
    assert payload["role"] == "assistant"
    assert "not implemented yet" in payload["text"]
    assert "hello" in payload["text"]


@pytest.mark.xfail(reason="Real agent streaming over /ws is not implemented yet.", strict=True)
def test_websocket_streams_agent_reply() -> None:
    """Once `api/agent/` is implemented, the agent should stream tokens back."""
    raise NotImplementedError("Agent streaming not wired up yet.")
