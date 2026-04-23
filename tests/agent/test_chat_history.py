"""Unit tests for per-repair JSONL chat history."""

from __future__ import annotations

import json

import pytest

from api import config as config_mod
from api.agent.chat_history import append_event, load_events, touch_status


@pytest.fixture(autouse=True)
def reset_settings(monkeypatch):
    monkeypatch.setattr(config_mod, "_settings", None)
    yield
    monkeypatch.setattr(config_mod, "_settings", None)


def test_append_then_load_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_HISTORY_BACKEND", "jsonl")
    monkeypatch.setenv("MEMORY_ROOT", str(tmp_path))

    append_event(
        device_slug="demo-pi",
        repair_id="r1",
        event={"role": "user", "content": "Pas de son"},
        memory_root=tmp_path,
    )
    append_event(
        device_slug="demo-pi",
        repair_id="r1",
        event={"role": "assistant", "content": [{"type": "text", "text": "OK"}]},
        memory_root=tmp_path,
    )

    events = load_events(
        device_slug="demo-pi", repair_id="r1", memory_root=tmp_path
    )
    assert len(events) == 2
    assert events[0]["role"] == "user"
    assert events[0]["content"] == "Pas de son"
    assert events[1]["role"] == "assistant"


def test_load_returns_empty_when_no_history(tmp_path):
    events = load_events(
        device_slug="nobody", repair_id="never-happened", memory_root=tmp_path
    )
    assert events == []


def test_append_is_noop_without_repair_id(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_HISTORY_BACKEND", "jsonl")
    monkeypatch.setenv("MEMORY_ROOT", str(tmp_path))
    append_event(
        device_slug="demo-pi",
        repair_id=None,
        event={"role": "user", "content": "Pas de son"},
        memory_root=tmp_path,
    )
    # Nothing should have been written under demo-pi/repairs/.
    assert not (tmp_path / "demo-pi" / "repairs").exists()


def test_append_is_noop_when_backend_not_jsonl(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_HISTORY_BACKEND", "managed_agents")
    monkeypatch.setenv("MEMORY_ROOT", str(tmp_path))
    append_event(
        device_slug="demo-pi",
        repair_id="r1",
        event={"role": "user", "content": "Pas de son"},
        memory_root=tmp_path,
    )
    assert not (tmp_path / "demo-pi" / "repairs" / "r1").exists()


def test_load_skips_malformed_lines(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_HISTORY_BACKEND", "jsonl")
    monkeypatch.setenv("MEMORY_ROOT", str(tmp_path))
    d = tmp_path / "demo-pi" / "repairs" / "r1"
    d.mkdir(parents=True)
    (d / "messages.jsonl").write_text(
        '{"ts":"t1","event":{"role":"user","content":"ok"}}\n'
        "not-json\n"
        '{"ts":"t2","event":{"role":"assistant","content":"reply"}}\n',
        encoding="utf-8",
    )
    events = load_events(
        device_slug="demo-pi", repair_id="r1", memory_root=tmp_path
    )
    assert len(events) == 2  # middle line dropped


def test_touch_status_updates_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_HISTORY_BACKEND", "jsonl")
    monkeypatch.setenv("MEMORY_ROOT", str(tmp_path))
    # Seed a metadata file like /pipeline/repairs creates.
    repairs_dir = tmp_path / "demo-pi" / "repairs"
    repairs_dir.mkdir(parents=True)
    meta_path = repairs_dir / "r1.json"
    meta_path.write_text(json.dumps({
        "repair_id": "r1",
        "device_slug": "demo-pi",
        "device_label": "Demo Pi",
        "symptom": "no boot",
        "status": "open",
        "created_at": "2026-04-22T12:00:00+00:00",
    }))

    touch_status(
        device_slug="demo-pi",
        repair_id="r1",
        status="in_progress",
        memory_root=tmp_path,
    )
    updated = json.loads(meta_path.read_text())
    assert updated["status"] == "in_progress"
    assert "status_updated_at" in updated


def test_touch_status_noop_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_ROOT", str(tmp_path))
    # Should not raise.
    touch_status(
        device_slug="nobody",
        repair_id="never",
        status="closed",
        memory_root=tmp_path,
    )


def test_save_and_load_ma_session_id_per_tier(tmp_path, monkeypatch):
    from api.agent.chat_history import load_ma_session_id, save_ma_session_id

    monkeypatch.setenv("MEMORY_ROOT", str(tmp_path))
    rdir = tmp_path / "demo-pi" / "repairs"
    rdir.mkdir(parents=True)
    meta_path = rdir / "r1.json"
    meta_path.write_text(json.dumps({
        "repair_id": "r1",
        "device_slug": "demo-pi",
        "device_label": "Demo Pi",
        "symptom": "no boot",
        "status": "open",
        "created_at": "2026-04-22T12:00:00+00:00",
        "ma_session_id": "legacy_session_pre_tier_storage",
    }))

    # Legacy top-level ma_session_id is IGNORED by the new loader.
    assert load_ma_session_id(
        device_slug="demo-pi", repair_id="r1", tier="fast", memory_root=tmp_path
    ) is None

    save_ma_session_id(
        device_slug="demo-pi", repair_id="r1",
        session_id="sesn_fast_A", tier="fast",
        memory_root=tmp_path,
    )
    save_ma_session_id(
        device_slug="demo-pi", repair_id="r1",
        session_id="sesn_normal_B", tier="normal",
        memory_root=tmp_path,
    )

    assert load_ma_session_id(
        device_slug="demo-pi", repair_id="r1", tier="fast", memory_root=tmp_path
    ) == "sesn_fast_A"
    assert load_ma_session_id(
        device_slug="demo-pi", repair_id="r1", tier="normal", memory_root=tmp_path
    ) == "sesn_normal_B"
    assert load_ma_session_id(
        device_slug="demo-pi", repair_id="r1", tier="deep", memory_root=tmp_path
    ) is None

    updated = json.loads(meta_path.read_text())
    assert updated["ma_sessions"] == {
        "fast": "sesn_fast_A",
        "normal": "sesn_normal_B",
    }
    # Legacy field is wiped on the first save.
    assert "ma_session_id" not in updated
