"""Tests for api.pipeline.events — tiny per-slug async pubsub."""

from __future__ import annotations

import asyncio

import pytest

from api.pipeline import events


@pytest.fixture(autouse=True)
def _isolate_bus():
    """Reset the global bus between tests so they don't leak subscribers."""
    events.reset()
    yield
    events.reset()


async def test_subscribe_receives_published_events():
    q = events.subscribe("demo-pi")
    await events.publish("demo-pi", {"type": "phase_started", "phase": "scout"})
    ev = await asyncio.wait_for(q.get(), timeout=0.5)
    assert ev == {"type": "phase_started", "phase": "scout"}


async def test_subscribe_fans_out_to_multiple_listeners():
    q1 = events.subscribe("demo-pi")
    q2 = events.subscribe("demo-pi")
    await events.publish("demo-pi", {"type": "pipeline_started"})
    ev1 = await asyncio.wait_for(q1.get(), timeout=0.5)
    ev2 = await asyncio.wait_for(q2.get(), timeout=0.5)
    assert ev1 == ev2 == {"type": "pipeline_started"}


async def test_publish_to_unrelated_slug_is_not_delivered():
    q = events.subscribe("demo-pi")
    await events.publish("other-device", {"type": "phase_started"})
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(q.get(), timeout=0.2)


async def test_unsubscribe_removes_listener():
    q = events.subscribe("demo-pi")
    events.unsubscribe("demo-pi", q)
    await events.publish("demo-pi", {"type": "pipeline_started"})
    # A fresh queue should be empty.
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(q.get(), timeout=0.2)


async def test_subscribers_count_tracks_lifecycle():
    assert events.subscribers_count("demo-pi") == 0
    q1 = events.subscribe("demo-pi")
    q2 = events.subscribe("demo-pi")
    assert events.subscribers_count("demo-pi") == 2
    events.unsubscribe("demo-pi", q1)
    assert events.subscribers_count("demo-pi") == 1
    events.unsubscribe("demo-pi", q2)
    assert events.subscribers_count("demo-pi") == 0


async def test_publish_with_no_subscribers_is_a_noop():
    # Must not raise, must not block.
    await events.publish("nobody-home", {"type": "pipeline_started"})
