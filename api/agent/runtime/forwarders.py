"""WS event loops in / out.

* ``_forward_ws_to_session`` — read tech-side frames off the WS, dispatch
  client-side handlers, forward user.message to the MA session.
* ``_forward_session_to_ws`` — stream MA events back, sanitise + relay
  agent text and tool_use events, dispatch custom tool calls.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from anthropic import AsyncAnthropic
from fastapi import WebSocket, WebSocketDisconnect

from api.agent import runtime_managed as _rm
from api.agent._session_mirrors import SessionMirrors as _SessionMirrors
from api.agent.chat_history import (
    append_event,
    touch_conversation,
)
from api.agent.pricing import compute_turn_cost
from api.agent.runtime._aux import (
    TierLiteral,
    _mirror_jsonl,
    _PendingConv,
    _safe_tool_result_text,
    logger,
)
from api.agent.runtime.camera import _dispatch_cam_capture
from api.agent.runtime.handlers import (
    _handle_client_capabilities,
    _handle_client_capture_response,
    _handle_client_protocol_confirmation,
    _handle_client_upload_macro,
)
from api.agent.runtime.protocol import _dispatch_protocol_with_confirmation
from api.agent.runtime.subagents import (
    _run_knowledge_curator,
    _run_subagent_consultation,
)
from api.agent.sanitize import sanitize_agent_text
from api.session.state import SessionState


async def _forward_ws_to_session(
    ws: WebSocket,
    client: AsyncAnthropic,
    session_id: str,
    *,
    pending_intro: str | None = None,
    ctx_tag: str | None = None,
    repair_id: str | None = None,
    device_slug: str | None = None,
    conv_id: str | None = None,
    memory_root: Path | None = None,
    pending_conv: _PendingConv | None = None,
    session_state: SessionState | None = None,
) -> None:
    """Read user text from the WS, post it as `user.message` to the session.

    When `pending_intro` is set, it is PREFIXED to the tech's very first
    message so the agent sees (device context + reported symptom) and the
    tech's actual question in a single turn — avoids the empty-ack turn
    that happens when context is sent in isolation.

    When `ctx_tag` is set, it is prepended to EVERY user message as a
    stable, cacheable single-line prefix that restates the device +
    symptom — keeps Haiku from losing context on later turns.
    """
    intro_pending = pending_intro
    first_user_seen = False
    while True:
        raw = await ws.receive_text()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"text": raw}

        ptype = payload.get("type")

        # Files+Vision frames — handled before MA forwarding.
        if ptype == "client.capabilities":
            if session_state is not None:
                _handle_client_capabilities(session_state, payload)
            continue

        if ptype == "client.upload_macro":
            if session_state is None or not repair_id or not device_slug or not memory_root:
                logger.warning("[Diag-MA] upload_macro received but session context incomplete")
                continue
            try:
                await _handle_client_upload_macro(
                    client=client,
                    session=session_state,
                    memory_root=memory_root,
                    slug=device_slug,
                    repair_id=repair_id,
                    ma_session_id=session_id,
                    frame=payload,
                )
            except ValueError as exc:
                logger.warning("[Diag-MA] upload_macro rejected: %s", exc)
                await ws.send_json({
                    "type": "server.upload_macro_error",
                    "reason": str(exc),
                })
            continue

        if ptype == "client.capture_response":
            if session_state is not None:
                await _handle_client_capture_response(session=session_state, frame=payload)
            continue

        # Pattern 4 (tool_confirmation round-trip) for `bv_propose_protocol`.
        # The runtime parked the tool call on a Future in
        # `session_state.pending_protocol_confirmations[tool_use_id]`; the UI
        # modal resolves it by sending us this frame.
        if ptype == "client.protocol_confirmation":
            if session_state is not None:
                await _handle_client_protocol_confirmation(
                    session=session_state, frame=payload,
                )
            continue

        # Tech pressed Stop — forward as a user.interrupt MA event so the
        # agent halts any in-flight turn. Session stays alive; the tech can
        # keep typing afterwards.
        if payload.get("type") == "interrupt":
            try:
                await client.beta.sessions.events.send(
                    session_id,
                    events=[{"type": "user.interrupt"}],
                )
                logger.info("[Diag-MA] Forwarded user.interrupt for session=%s", session_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[Diag-MA] interrupt failed: %s", exc)
            continue

        # Client submits a step result from the protocol UI panel.
        # Record it, emit a protocol_updated WS event, then forward a
        # synthetic user.message to the agent summarising the outcome so
        # it can react (adjust next steps, give a reading, etc.).
        if payload.get("type") == "protocol_step_result":
            from api.tools.protocol import (
                load_active_protocol,
            )
            from api.tools.protocol import (
                record_step_result as _record,
            )
            res = _record(
                memory_root=memory_root,
                device_slug=device_slug,
                repair_id=repair_id or "",
                step_id=payload.get("step_id", ""),
                value=payload.get("value"),
                unit=payload.get("unit"),
                observation=payload.get("observation"),
                skip_reason=payload.get("skip_reason"),
                submitted_by="tech",
                conv_id=conv_id,
            )
            if res.get("ok"):
                proto = load_active_protocol(memory_root, device_slug, repair_id or "", conv_id=conv_id)
                history_tail = proto.history[-3:] if proto is not None else []
                await ws.send_json({
                    "type": "protocol_updated",
                    "protocol_id": res.get("protocol_id"),
                    "action": "step_completed",
                    "current_step_id": res.get("current_step_id"),
                    "steps": [s.model_dump(mode="json") for s in (proto.steps if proto else [])],
                    "history_tail": [h.model_dump(mode="json") for h in history_tail],
                })
                step_id = payload.get("step_id", "")
                target = ""
                value = payload.get("value")
                unit = payload.get("unit") or ""
                outcome = res.get("outcome", "neutral")
                current = res.get("current_step_id") or "completed"
                step_count = len(proto.steps) if proto else 0
                if proto is not None:
                    src_step = next((s for s in proto.steps if s.id == step_id), None)
                    if src_step is not None:
                        target = src_step.target or src_step.test_point or ""
                synthetic = (
                    f"[step_result] step={step_id} target={target} "
                    f"value={value}{unit} outcome={outcome} · "
                    f"plan: {step_count} steps, current={current}"
                )
                await client.beta.sessions.events.send(
                    session_id,
                    events=[{"type": "user.message",
                             "content": [{"type": "text", "text": synthetic}]}],
                )
            else:
                await ws.send_json({"type": "error", "code": "protocol_result_rejected",
                                     "text": res.get("reason", "unknown")})
            continue

        # Tech pressed Abandon on the running quest panel — mark the protocol
        # as abandoned in the on-disk store, broadcast a protocol_updated WS
        # event so the UI cleans its state, and forward a synthetic
        # user.message so the agent stops acting on the dead protocol. The
        # session.events.send call is wrapped in try/except: if the MA
        # state machine rejects the synthetic (rare, was previously masked
        # by the now-fixed oversized seed bug), the protocol is still
        # abandoned cleanly on disk and the UI panel cleans up — only the
        # agent stays oblivious until its next protocol-aware tool call
        # gets a "no_active_protocol" return.
        if payload.get("type") == "protocol_abandon":
            from api.tools.protocol import (
                load_active_protocol,
            )
            from api.tools.protocol import (
                update_protocol as _update_protocol,
            )
            reason = (payload.get("reason") or "tech_dismiss").strip() or "tech_dismiss"
            res = _update_protocol(
                memory_root=memory_root,
                device_slug=device_slug,
                repair_id=repair_id or "",
                action="abandon_protocol",
                reason=reason,
                conv_id=conv_id,
            )
            if res.get("ok"):
                proto = load_active_protocol(memory_root, device_slug, repair_id or "", conv_id=conv_id)
                history_tail = proto.history[-3:] if proto is not None else []
                await ws.send_json({
                    "type": "protocol_updated",
                    "protocol_id": res.get("protocol_id"),
                    "action": "abandoned",
                    "current_step_id": None,
                    "steps": [s.model_dump(mode="json") for s in (proto.steps if proto else [])],
                    "history_tail": [h.model_dump(mode="json") for h in history_tail],
                    "status": "abandoned",
                    "reason": reason,
                })
                synthetic = (
                    f"[protocol_abandoned] The technician abandoned the "
                    f"running protocol. Reason: {reason}. Stop acting on "
                    f"this protocol; do not re-emit it; if relevant, "
                    f"propose a fresh approach or ask a clarifying question."
                )
                try:
                    await client.beta.sessions.events.send(
                        session_id,
                        events=[{"type": "user.message",
                                 "content": [{"type": "text", "text": synthetic}]}],
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "[Diag-MA] protocol_abandoned synthetic forward failed "
                        "session=%s exc=%s — UI cleaned up, agent will learn on "
                        "next tool call (no_active_protocol)",
                        session_id, type(exc).__name__,
                    )
            else:
                await ws.send_json({
                    "type": "error",
                    "code": "protocol_abandon_rejected",
                    "text": res.get("reason", "unknown"),
                })
            continue

        # Intercept validation trigger events before they reach the agent as
        # ordinary messages. Synthesise a user-role prompt that asks the agent
        # to summarise fixes and call mb_validate_finding.
        if payload.get("type") == "validation.start":
            text = (
                "I just finished this repair. Can you summarise in one "
                "sentence which component(s) I fixed or replaced based on "
                "the history of our chat and the measurements taken, then "
                "record the result with the `mb_validate_finding` tool? "
                "If you have any doubt about a refdes or a mode, ask me "
                "before calling the tool."
            )
            if repair_id and conv_id and device_slug and memory_root:
                if pending_conv is not None:
                    pending_conv.materialize_now()
                append_event(
                    device_slug=device_slug,
                    repair_id=repair_id,
                    conv_id=conv_id,
                    memory_root=memory_root,
                    event={
                        "role": "user",
                        "content": text,
                        "source": "trigger",
                        "trigger_kind": "validation.start",
                    },
                )
        else:
            text = (payload.get("text") or "").strip()

        if not text:
            continue

        # Stamp the conv title from the first real user message (before the
        # intro prefix is glued on so the popover shows what the tech typed,
        # not the device-context boilerplate). Materialize the conv on disk
        # at the same moment if it was opened lazily — this is the point at
        # which the slot stops being a no-op WS open and starts holding
        # actual content worth indexing.
        if not first_user_seen and repair_id and conv_id and device_slug:
            if pending_conv is not None:
                pending_conv.materialize_now()
            touch_conversation(
                device_slug=device_slug,
                repair_id=repair_id,
                conv_id=conv_id,
                first_message=text,
                memory_root=memory_root,
            )
            first_user_seen = True

        if intro_pending:
            text = intro_pending + "\n\n---\n\n" + text
            intro_pending = None
            if repair_id and device_slug:
                from api.agent.chat_history import touch_status

                touch_status(device_slug=device_slug, repair_id=repair_id, status="in_progress")
        if ctx_tag:
            text = ctx_tag + "\n\n" + text
        await client.beta.sessions.events.send(
            session_id,
            events=[
                {
                    "type": "user.message",
                    "content": [{"type": "text", "text": text}],
                }
            ],
        )
        # Mirror the user turn to local JSONL so we still have the transcript
        # if MA later archives the session. Symmetric with what MA stores —
        # ctx_tag + intro prefix included; the replay path strips them.
        _mirror_jsonl(
            device_slug=device_slug,
            repair_id=repair_id,
            conv_id=conv_id,
            memory_root=memory_root,
            event={
                "role": "user",
                "content": [{"type": "text", "text": text}],
            },
        )


async def _forward_session_to_ws(
    ws: WebSocket,
    client: AsyncAnthropic,
    session_id: str,
    device_slug: str,
    memory_root: Path,
    events_by_id: dict[str, Any],
    session_state: SessionState,
    agent_model: str,
    *,
    tier: TierLiteral,
    environment_id: str,
    repair_id: str | None = None,
    conv_id: str | None = None,
    session_mirrors: _SessionMirrors | None = None,
    pending_conv: _PendingConv | None = None,
) -> None:
    """Stream session events to the WS and dispatch custom tool calls.

    `agent_model` is the tier's configured model (claude-haiku-4-5 etc.),
    used as a fallback when MA's span.model_request_end doesn't carry a
    model name on its model_usage payload.
    """
    # AsyncAnthropic: `.stream(...)` returns a coroutine resolving to an
    # `AsyncStream[...]`. We must await first, then use it as an async
    # context manager — otherwise we get `TypeError: 'coroutine' object
    # does not support the asynchronous context manager protocol`.
    stream_ctx = await client.beta.sessions.events.stream(session_id)
    # Deduplicate tool-use responses. MA can re-emit `session.status_idle`
    # with `stop_reason=requires_action` carrying the SAME event_ids after
    # we've already sent their `user.custom_tool_result` — a naive re-dispatch
    # then posts a duplicate response, which MA rejects with 400
    # ("Invalid user.custom_tool_result event [...] waiting on responses to
    # events [...]") and tears down the stream. Track ids we've answered.
    responded_tool_ids: set[str] = set()
    # Tool-result processing telemetry. Every event MA streams back carries
    # `processed_at` (ISO 8601 — null while queued, populated once the agent
    # picks it up). For our `user.custom_tool_result` events the round-trip
    # tells us how long the agent took to consume our response: a healthy
    # session shows sub-second deltas; multi-second values usually mean the
    # agent is rate-limited or blocked on an upstream call. We don't react
    # programmatically — just log so post-mortems on a slow turn can pinpoint
    # the stall without re-running the trace. Keys are the eid of the
    # original `agent.custom_tool_use`; value is the local `time.monotonic()`
    # at send time. Cleared on echo; entries that linger past the watchdog
    # are dropped silently with the rest of the loop state.
    pending_tool_results: dict[str, float] = {}
    # Stream watchdog: each .__anext__() is wrapped in asyncio.wait_for so an
    # SSE stall (Anthropic outage, dropped TCP without RST, slow keepalive)
    # surfaces as a clean close + WS notification instead of hanging the
    # session indefinitely. Window is per-event (settings.ma_stream_event_
    # _timeout_seconds, default 600 s) — generous enough that an Opus turn
    # with adaptive thinking can spend a minute before its first chunk.
    settings_for_watchdog = _rm.get_settings()
    stream_timeout = settings_for_watchdog.ma_stream_event_timeout_seconds
    async with stream_ctx as stream:
        stream_iter = stream.__aiter__()
        while True:
            try:
                event = await asyncio.wait_for(
                    stream_iter.__anext__(), timeout=stream_timeout,
                )
            except StopAsyncIteration:
                break
            except TimeoutError:
                logger.warning(
                    "[Diag-MA] stream inactive for %.0fs — closing session=%s",
                    stream_timeout,
                    session_id,
                )
                try:
                    await ws.send_json(
                        {
                            "type": "stream_timeout",
                            "session_id": session_id,
                            "timeout_seconds": stream_timeout,
                        }
                    )
                except Exception:  # noqa: BLE001
                    pass
                break
            except WebSocketDisconnect:
                # Client window closed mid-stream — bubble up so the caller's
                # asyncio.wait observes the task completion and the symmetric
                # WS→session forwarder can shut down too. Not an MA-side error.
                raise
            except Exception as exc:  # noqa: BLE001 — SSE transport collapse
                # Anything else from the SSE iterator is a transport-level
                # failure (TLS reset, ConnectionError, anthropic.APIStatusError
                # mid-stream, etc.). Without an explicit catch the task ended
                # silently, the WS client kept its socket open expecting
                # `agent.message` chunks that never arrived, and the technician
                # saw a frozen UI with no signal. Surface it to the WS so the
                # frontend can render a "session lost — reconnect" hint, then
                # break cleanly so the orchestrator's finally block runs.
                logger.exception(
                    "[Diag-MA] stream iterator failed session=%s exc=%s",
                    session_id,
                    type(exc).__name__,
                )
                try:
                    await ws.send_json(
                        {
                            "type": "stream_error",
                            "session_id": session_id,
                            "error": type(exc).__name__,
                            "message": str(exc)[:500],
                        }
                    )
                except Exception:  # noqa: BLE001
                    pass
                break

            etype = getattr(event, "type", None)

            if etype == "agent.message":
                for block in getattr(event, "content", None) or []:
                    if getattr(block, "type", None) == "text":
                        clean, unknown = sanitize_agent_text(block.text, session_state.board)
                        if unknown:
                            logger.warning("sanitizer wrapped unknown refdes: %s", unknown)
                        await ws.send_json({"type": "message", "role": "assistant", "text": clean})
                        _mirror_jsonl(
                            device_slug=device_slug,
                            repair_id=repair_id,
                            conv_id=conv_id,
                            memory_root=memory_root,
                            event={
                                "role": "assistant",
                                "content": [{"type": "text", "text": clean}],
                            },
                        )

            elif etype == "agent.thinking":
                # MA surfaces summarized thinking text on this event when the
                # configured model supports adaptive thinking (Opus 4.6/4.7,
                # Sonnet 4.6 — all enabled by default server-side; the agent
                # config doesn't expose a `thinking` knob, see bootstrap docs).
                # Empty `text` means MA emitted the marker but the model chose
                # `display: omitted` for that block — skip.
                text = getattr(event, "text", "") or ""
                if text:
                    await ws.send_json({"type": "thinking", "text": text})

            elif etype == "span.model_request_end":
                # MA attaches token usage to the span terminator. The model
                # name may or may not be carried on model_usage across SDK
                # versions — fall back to the tier-configured agent model
                # (claude-haiku-4-5 / sonnet-4-6 / opus-4-7) so pricing still
                # resolves.
                usage = getattr(event, "model_usage", None)
                if usage is not None:
                    model_label = (
                        getattr(usage, "model", None)
                        or getattr(event, "model", None)
                        or agent_model
                    )
                    in_tok = getattr(usage, "input_tokens", 0) or 0
                    out_tok = getattr(usage, "output_tokens", 0) or 0
                    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
                    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
                    cost = compute_turn_cost(
                        model_label,
                        input_tokens=in_tok,
                        output_tokens=out_tok,
                        cache_read_input_tokens=cache_read,
                        cache_creation_input_tokens=cache_write,
                    )
                    # Per-turn cache hit rate (read / total prompt-tokens). Useful
                    # to confirm the warm-up + 4-store layered prompt actually
                    # pays off across resumed sessions.
                    total_prompt = in_tok + cache_read + cache_write
                    if total_prompt > 0:
                        hit_rate = (cache_read / total_prompt) * 100.0
                        logger.info(
                            "[CacheRate] session=%s tier=%s rate=%.1f%% (read=%d total=%d)",
                            session_id,
                            tier,
                            hit_rate,
                            cache_read,
                            total_prompt,
                        )
                    await ws.send_json({"type": "turn_cost", **cost})
                    if repair_id and conv_id:
                        # Defensive: in normal flow `_forward_ws_to_session`
                        # has already materialized on the user message that
                        # triggered this turn, but call it again so a cost
                        # event never lands against an unindexed conv slot.
                        if pending_conv is not None:
                            pending_conv.materialize_now()
                        touch_conversation(
                            device_slug=device_slug,
                            repair_id=repair_id,
                            conv_id=conv_id,
                            cost_usd=cost.get("cost_usd") if isinstance(cost, dict) else None,
                            model=model_label,
                            memory_root=memory_root,
                        )

            elif etype == "agent.custom_tool_use":
                events_by_id[event.id] = event
                tool_name = getattr(event, "name", None)
                tool_input = getattr(event, "input", {}) or {}
                await ws.send_json(
                    {
                        "type": "tool_use",
                        "name": tool_name,
                        "input": tool_input,
                    }
                )
                _mirror_jsonl(
                    device_slug=device_slug,
                    repair_id=repair_id,
                    conv_id=conv_id,
                    memory_root=memory_root,
                    event={
                        "role": "assistant",
                        "content": [{
                            "type": "tool_use",
                            "id": getattr(event, "id", None),
                            "name": tool_name,
                            "input": tool_input,
                        }],
                    },
                )

            elif etype == "agent.tool_use":
                # MA-native memory_* tools (memory_search / memory_list /
                # memory_read / memory_write) are dispatched server-side by
                # Anthropic, not by our runtime. Surface them on the WS so
                # benchmarks can attribute cost — inference tokens don't
                # include the per-op memory charges Anthropic bills on top.
                await ws.send_json(
                    {
                        "type": "memory_tool_use",
                        "name": getattr(event, "name", None),
                        "input": getattr(event, "input", {}) or {},
                    }
                )

            elif etype == "session.status_idle":
                stop = getattr(event, "stop_reason", None)
                stop_type = getattr(stop, "type", None) if stop is not None else None
                if stop_type != "requires_action":
                    # Agent finished its tech-turn and is waiting for the
                    # next user.message. Expose this as an explicit signal
                    # for WS clients that need to know when it's safe to
                    # send the next user input (bench scripts, automated
                    # tests). UI chat clients can ignore it.
                    await ws.send_json(
                        {
                            "type": "turn_complete",
                            "stop_reason": stop_type,
                        }
                    )
                    continue
                event_ids = getattr(stop, "event_ids", None) or []
                for eid in event_ids:
                    if eid in responded_tool_ids:
                        # MA re-emitted a requires_action whose event_ids
                        # include ones we already responded to. Skip —
                        # responding twice yields HTTP 400.
                        continue
                    tool_event = events_by_id.get(eid)
                    if tool_event is None:
                        logger.warning("[Diag-MA] requires_action for unknown event id %s", eid)
                        continue
                    name = getattr(tool_event, "name", "")
                    payload = getattr(tool_event, "input", {}) or {}

                    # mb_expand_knowledge: route through the MA
                    # KnowledgeCurator sub-agent instead of the inline
                    # Scout `messages.create`. The curator does the focused
                    # research; the existing Registry + Clinicien validate
                    # and merge the chunk into rules.json.
                    if name == "mb_expand_knowledge":
                        from api.pipeline.expansion import expand_pack

                        focus_symptoms = list(payload.get("focus_symptoms") or [])
                        focus_refdes = list(payload.get("focus_refdes") or [])

                        async def _curator_provider(
                            *,
                            device_label: str,
                            focus_symptoms: list[str],
                            focus_refdes: list[str],
                        ) -> str:
                            return await _run_knowledge_curator(
                                client=client,
                                device_label=device_label,
                                focus_symptoms=focus_symptoms,
                                focus_refdes=focus_refdes,
                                environment_id=environment_id,
                                parent_session_id=session_id,
                                ws=ws,
                            )

                        try:
                            expand_result = await expand_pack(
                                device_slug=device_slug,
                                focus_symptoms=focus_symptoms,
                                focus_refdes=focus_refdes,
                                client=client,
                                memory_root=memory_root,
                                chunk_provider=_curator_provider,
                            )
                            expand_result["ok"] = True
                            if session_state is not None:
                                session_state.invalidate_pack_cache(device_slug)
                            # Sync the MA memory store mount with the freshly
                            # expanded pack so the agent's mount-based reads
                            # (grep on /mnt/memory/wrench-board-{slug}/) see the
                            # new rules + registry mid-session, not just on
                            # the next session-create. Custom mb_* tools see
                            # the changes immediately via the cache invalidate
                            # above; this closes the gap on the mount path.
                            try:
                                from api.agent.memory_seed import (
                                    seed_memory_store_from_pack,
                                )
                                sync_status = await seed_memory_store_from_pack(
                                    client=client,
                                    device_slug=device_slug,
                                    pack_dir=memory_root / device_slug,
                                    only_files=["rules.json", "registry.json"],
                                )
                                seeded = [
                                    p for p, s in sync_status.items()
                                    if s == "seeded"
                                ]
                                logger.info(
                                    "[Curator] mount sync slug=%s seeded=%s",
                                    device_slug,
                                    seeded,
                                )
                            except Exception as sync_exc:  # noqa: BLE001
                                logger.warning(
                                    "[Curator] memory store sync failed "
                                    "(non-critical): %s",
                                    sync_exc,
                                )
                        except Exception as exc:  # noqa: BLE001
                            logger.exception(
                                "[Curator] expand_pack failed device=%s",
                                device_slug,
                            )
                            expand_result = {
                                "ok": False,
                                "expanded": False,
                                "reason": type(exc).__name__,
                                "error": str(exc)[:300],
                            }

                        await ws.send_json({
                            "type": "knowledge_expanded",
                            "ok": bool(expand_result.get("ok")),
                            "stats": {
                                k: v for k, v in expand_result.items()
                                if k in (
                                    "new_rules_count",
                                    "new_components_count",
                                    "new_signals_count",
                                    "total_rules_after",
                                    "dump_bytes_added",
                                )
                            },
                        })
                        await client.beta.sessions.events.send(
                            session_id,
                            events=[{
                                "type": "user.custom_tool_result",
                                "custom_tool_use_id": eid,
                                "content": [{
                                    "type": "text",
                                    "text": _safe_tool_result_text(expand_result),
                                }],
                            }],
                        )
                        responded_tool_ids.add(eid)
                        continue

                    # consult_specialist is async (spawns a fresh MA session
                    # on another tier and streams its events). Intercept
                    # before _dispatch_tool because the helper needs the
                    # parent session's environment + tier in closure.
                    if name == "consult_specialist":
                        requested_tier = str(payload.get("tier", "")).strip()
                        if not requested_tier:
                            sub_result = {
                                "ok": False,
                                "reason": "missing-tier",
                                "error": "tier is required",
                            }
                        elif requested_tier == tier:
                            sub_result = {
                                "ok": False,
                                "reason": "self-consultation",
                                "error": (
                                    f"refusing to consult tier={requested_tier} "
                                    "from itself — pick a different tier"
                                ),
                            }
                        else:
                            sub_result = await _run_subagent_consultation(
                                client=client,
                                tier=requested_tier,  # type: ignore[arg-type]
                                query=str(payload.get("query", "")),
                                context=payload.get("context"),
                                environment_id=environment_id,
                                parent_session_id=session_id,
                            )
                        await ws.send_json({
                            "type": "subagent_result",
                            "tier": requested_tier,
                            "ok": bool(sub_result.get("ok")),
                        })
                        await client.beta.sessions.events.send(
                            session_id,
                            events=[{
                                "type": "user.custom_tool_result",
                                "custom_tool_use_id": eid,
                                "content": [{
                                    "type": "text",
                                    "text": _safe_tool_result_text(sub_result),
                                }],
                            }],
                        )
                        responded_tool_ids.add(eid)
                        continue

                    # cam_capture is async (round-trips to the frontend) and
                    # produces its own user.custom_tool_result. Intercept
                    # before the generic _dispatch_tool which wouldn't know
                    # how to handle the WS round-trip.
                    #
                    # Track via session_mirrors (not bare create_task) so a
                    # WS close before the round-trip completes drains the
                    # task instead of orphaning it. The eid goes into the
                    # dedup set IMMEDIATELY to block MA from re-dispatching
                    # while the capture is in flight; on crash we DISCARD
                    # the eid in the done callback so MA's next
                    # `requires_action` re-emit gets a real retry instead of
                    # being silently swallowed. Without the rollback, a
                    # camera dispatch failure would permablock the tool_use:
                    # responded_tool_ids would say "answered" but no
                    # user.custom_tool_result ever reached MA, leaving the
                    # session waiting forever.
                    if name == "cam_capture":
                        cam_eid = eid

                        def _release_eid_on_failure(
                            task: asyncio.Task,
                            *,
                            eid: str = cam_eid,
                        ) -> None:
                            if task.cancelled():
                                responded_tool_ids.discard(eid)
                                logger.warning(
                                    "[Diag-MA] cam_capture cancelled for "
                                    "eid=%s — released for retry",
                                    eid,
                                )
                                return
                            exc = task.exception()
                            if exc is not None:
                                responded_tool_ids.discard(eid)
                                logger.warning(
                                    "[Diag-MA] cam_capture crashed for "
                                    "eid=%s — released for retry: %s",
                                    eid,
                                    exc,
                                )

                        responded_tool_ids.add(cam_eid)
                        cam_task = session_mirrors.spawn(_dispatch_cam_capture(
                            client=client,
                            session=session_state,
                            ws=ws,
                            memory_root=memory_root,
                            slug=device_slug,
                            repair_id=repair_id or "default",
                            ma_session_id=session_id,
                            tool_use_id=cam_eid,
                            tool_input=payload,
                        ))
                        cam_task.add_done_callback(_release_eid_on_failure)
                        continue

                    # bv_propose_protocol — Pattern 4 round-trip with tech.
                    # The runtime emits `protocol_pending_confirmation`, the
                    # UI modal accepts/rejects, and only an accept dispatches
                    # the actual tool. Same crash-rollback discipline as
                    # cam_capture: eid goes into the dedup set IMMEDIATELY,
                    # but the done callback releases it on cancellation /
                    # crash so MA's next requires_action re-emit gets a real
                    # retry instead of a permablock.
                    if name == "bv_propose_protocol":
                        proto_eid = eid

                        def _release_proto_on_failure(
                            task: asyncio.Task,
                            *,
                            eid: str = proto_eid,
                        ) -> None:
                            if task.cancelled():
                                responded_tool_ids.discard(eid)
                                logger.warning(
                                    "[Diag-MA] propose_protocol cancelled "
                                    "for eid=%s — released for retry",
                                    eid,
                                )
                                return
                            exc = task.exception()
                            if exc is not None:
                                responded_tool_ids.discard(eid)
                                logger.warning(
                                    "[Diag-MA] propose_protocol crashed for "
                                    "eid=%s — released for retry: %s",
                                    eid,
                                    exc,
                                )

                        responded_tool_ids.add(proto_eid)
                        proto_task = session_mirrors.spawn(
                            _dispatch_protocol_with_confirmation(
                                client=client,
                                session=session_state,
                                ws=ws,
                                memory_root=memory_root,
                                device_slug=device_slug,
                                repair_id=repair_id,
                                conv_id=conv_id,
                                ma_session_id=session_id,
                                tool_use_id=proto_eid,
                                tool_input=payload,
                                session_mirrors=session_mirrors,
                            )
                        )
                        proto_task.add_done_callback(_release_proto_on_failure)
                        continue

                    result = await _rm._dispatch_tool(
                        name,
                        payload,
                        device_slug,
                        memory_root,
                        client,
                        session_state,
                        session_id,
                        repair_id=repair_id,
                        session_mirrors=session_mirrors,
                        conv_id=conv_id,
                    )
                    # Emit the WS event(s) if the dispatch succeeded. Atomic
                    # tools return `event` (single), composites like bv_scene
                    # return `events` (list); fan both out as individual WS
                    # frames so the frontend stays oblivious.
                    single_event = result.get("event")
                    multi_events = (
                        result.get("events")
                        if isinstance(result.get("events"), list)
                        else None
                    )
                    emitted_any = False
                    if result.get("ok") and single_event is not None:
                        await ws.send_json(
                            single_event if isinstance(single_event, dict)
                            else single_event.model_dump(by_alias=True)
                        )
                        emitted_any = True
                    if multi_events:
                        for ev in multi_events:
                            await ws.send_json(
                                ev if isinstance(ev, dict)
                                else ev.model_dump(by_alias=True)
                            )
                            emitted_any = True
                    if emitted_any and name.startswith("bv_"):
                        # Snapshot board overlay after every successful bv_*
                        # mutation so a WS reconnect can replay highlights /
                        # annotations / focus instead of showing a bare board
                        # while the chat references "I highlighted U7 for you".
                        from api.agent.board_state import save_board_state
                        save_board_state(
                            memory_root=memory_root,
                            device_slug=device_slug,
                            repair_id=repair_id,
                            session=session_state,
                            conv_id=conv_id,
                        )
                    result_for_agent = {k: v for k, v in result.items() if k not in ("event", "events")}
                    pending_tool_results[eid] = time.monotonic()
                    await client.beta.sessions.events.send(
                        session_id,
                        events=[
                            {
                                "type": "user.custom_tool_result",
                                "custom_tool_use_id": eid,
                                "content": [
                                    {
                                        "type": "text",
                                        "text": _safe_tool_result_text(result_for_agent),
                                    }
                                ],
                            }
                        ],
                    )
                    responded_tool_ids.add(eid)

            elif etype == "user.custom_tool_result":
                # MA echoes user-sent events back on the stream — first with
                # `processed_at: null` (queued), then with a timestamp once
                # the agent picked up our response. Both arrive after our own
                # `events.send`, so the second copy gives us the agent's
                # consumption latency. Useful for diagnosing slow turns: a
                # healthy session shows sub-second deltas; multi-second
                # values usually mean the agent is rate-limited or blocked
                # on an upstream call. Strictly observational — no retry,
                # no failover, just a log line.
                processed_at = getattr(event, "processed_at", None)
                if processed_at is None:
                    continue
                eid = getattr(event, "custom_tool_use_id", None)
                sent_at = pending_tool_results.pop(eid, None) if eid else None
                if sent_at is None:
                    continue
                delay = time.monotonic() - sent_at
                if delay >= 5.0:
                    logger.warning(
                        "[Diag-MA] tool_result consumed slowly session=%s "
                        "eid=%s delay=%.2fs",
                        session_id,
                        eid,
                        delay,
                    )
                else:
                    logger.info(
                        "[Diag-MA] tool_result consumed session=%s eid=%s "
                        "delay=%.2fs",
                        session_id,
                        eid,
                        delay,
                    )

            elif etype == "session.status_terminated":
                await ws.send_json({"type": "session_terminated"})
                return

            elif etype == "session.error":
                err = getattr(event, "error", None)
                msg = getattr(err, "message", None) if err is not None else None
                # Dump the full event so the next "An internal service error
                # occurred." surfaces with enough context to act on (MA error
                # type, request_id if present, the raw event payload). Without
                # this, the frontend shows the user a wall and we have no log
                # to bisect transient-MA-hiccup vs. our-own-bug.
                err_type = getattr(err, "type", None) if err is not None else None
                request_id = getattr(event, "request_id", None) or (
                    getattr(err, "request_id", None) if err is not None else None
                )
                try:
                    raw = event.model_dump() if hasattr(event, "model_dump") else repr(event)
                except Exception:  # noqa: BLE001
                    raw = repr(event)
                logger.error(
                    "[Diag-MA] session.error session=%s err_type=%s msg=%s "
                    "request_id=%s raw=%s",
                    session_id,
                    err_type,
                    msg,
                    request_id,
                    raw,
                )
                await ws.send_json({"type": "error", "text": msg or "session error"})
