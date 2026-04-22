# SPDX-License-Identifier: Apache-2.0
"""Fallback diagnostic runtime using `messages.create` (no Managed Agents).

Keeps the WebSocket protocol identical to `runtime_managed`, so the frontend
doesn't care which mode is active. Activated with env var
`DIAGNOSTIC_MODE=direct`; used when the Managed Agents beta is unavailable
or when we want a lighter-weight path for local demos.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from anthropic import AsyncAnthropic
from fastapi import WebSocket, WebSocketDisconnect

from api.agent.tools import (
    mb_get_component,
    mb_get_rules_for_symptoms,
    mb_list_findings,
    mb_record_finding,
)
from api.config import get_settings

logger = logging.getLogger("microsolder.agent.direct")

SYSTEM_PROMPT_DIRECT = """\
You are a calm, methodical board-level diagnostics assistant for a
microsoldering technician. Tu tutoies, en français, direct et pédagogique.

RÈGLE ANTI-HALLUCINATION STRICTE : tu NE mentionnes JAMAIS un refdes
(U7, C29, J3100…) sans l'avoir validé via mb_get_component. Si le tool
retourne {{found: false, closest_matches: [...]}}, tu proposes une des
closest_matches ou tu demandes clarification — JAMAIS d'invention.

Device courant : {device_slug}.

Quand l'utilisateur décrit des symptômes, consulte d'abord
mb_list_findings (historique cross-session de ce device — technicien A a
peut-être déjà confirmé la cause), puis enchaîne mb_get_rules_for_symptoms
si besoin. Quand il demande un composant, valide avec mb_get_component.
Quand l'utilisateur confirme explicitement la cause finale d'une
réparation, appelle mb_record_finding — ça sera lu par les sessions
futures. Privilégie les causes à haute probabilité et les étapes de
diagnostic concrètes (mesurer tel voltage sur tel test point).
"""

TOOLS = [
    {
        "name": "mb_get_component",
        "description": (
            "Look up a component by refdes on the current device. Returns "
            "role/package/typical_failure_modes if found, otherwise "
            "{found: false, closest_matches: [...]}."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "refdes": {"type": "string", "description": "e.g. U7, C29, J3100"},
            },
            "required": ["refdes"],
        },
    },
    {
        "name": "mb_get_rules_for_symptoms",
        "description": (
            "Find diagnostic rules matching a list of symptoms, ranked by "
            "symptom overlap + rule confidence."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symptoms": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                },
                "max_results": {"type": "integer", "default": 5},
            },
            "required": ["symptoms"],
        },
    },
    {
        "name": "mb_list_findings",
        "description": (
            "Return prior confirmed findings (field reports) for the current "
            "device, newest first. Cross-session memory — check on open."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 100},
                "filter_refdes": {"type": "string"},
            },
        },
    },
    {
        "name": "mb_record_finding",
        "description": (
            "Persist a confirmed repair finding so future sessions see it. "
            "Only when the technician explicitly confirms the cause."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "refdes": {"type": "string"},
                "symptom": {"type": "string"},
                "confirmed_cause": {"type": "string"},
                "mechanism": {"type": "string"},
                "notes": {"type": "string"},
            },
            "required": ["refdes", "symptom", "confirmed_cause"],
        },
    },
]


async def _dispatch_tool(
    name: str,
    payload: dict,
    device_slug: str,
    memory_root: Path,
    client: AsyncAnthropic,
    session_id: str | None = None,
) -> dict:
    if name == "mb_get_component":
        return mb_get_component(
            device_slug=device_slug,
            refdes=payload.get("refdes", ""),
            memory_root=memory_root,
        )
    if name == "mb_get_rules_for_symptoms":
        return mb_get_rules_for_symptoms(
            device_slug=device_slug,
            symptoms=payload.get("symptoms", []),
            memory_root=memory_root,
            max_results=payload.get("max_results", 5),
        )
    if name == "mb_list_findings":
        return mb_list_findings(
            device_slug=device_slug,
            memory_root=memory_root,
            limit=payload.get("limit", 20),
            filter_refdes=payload.get("filter_refdes"),
        )
    if name == "mb_record_finding":
        return await mb_record_finding(
            client=client,
            device_slug=device_slug,
            refdes=payload.get("refdes", ""),
            symptom=payload.get("symptom", ""),
            confirmed_cause=payload.get("confirmed_cause", ""),
            memory_root=memory_root,
            mechanism=payload.get("mechanism"),
            notes=payload.get("notes"),
            session_id=session_id,
        )
    return {"error": f"unknown tool: {name}"}


async def run_diagnostic_session_direct(
    ws: WebSocket, device_slug: str, tier: str = "fast"
) -> None:
    """Run a direct-mode diagnostic session over `ws` for `device_slug`.

    Protocol on the wire (same as `runtime_managed`):
      - Client sends `{"type": "message", "text": "..."}`
      - Server emits `{"type": "message", "role": "assistant", "text": "..."}`
        and `{"type": "tool_use", "name": ..., "input": ...}` blocks.
    """
    settings = get_settings()
    if not settings.anthropic_api_key:
        await ws.accept()
        await ws.send_json({"type": "error", "text": "ANTHROPIC_API_KEY not set"})
        await ws.close()
        return

    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    memory_root = Path(settings.memory_root)
    tier_to_model = {
        "fast": "claude-haiku-4-5",
        "normal": "claude-sonnet-4-6",
        "deep": "claude-opus-4-7",
    }
    model = tier_to_model.get(tier, settings.anthropic_model_main)
    await ws.accept()
    await ws.send_json(
        {"type": "session_ready", "mode": "direct", "device_slug": device_slug,
         "tier": tier, "model": model}
    )

    messages: list[dict] = []
    try:
        while True:
            raw = await ws.receive_text()
            try:
                user_text = (json.loads(raw).get("text") or "").strip()
            except json.JSONDecodeError:
                user_text = raw.strip()
            if not user_text:
                continue

            messages.append({"role": "user", "content": user_text})
            while True:
                response = await client.messages.create(
                    model=model,
                    max_tokens=8000,
                    system=SYSTEM_PROMPT_DIRECT.format(device_slug=device_slug),
                    messages=messages,
                    tools=TOOLS,
                )

                for block in response.content:
                    if block.type == "text":
                        await ws.send_json(
                            {"type": "message", "role": "assistant", "text": block.text}
                        )

                if response.stop_reason != "tool_use":
                    messages.append({"role": "assistant", "content": response.content})
                    break

                messages.append({"role": "assistant", "content": response.content})
                tool_results: list[dict] = []
                for block in response.content:
                    if block.type != "tool_use":
                        continue
                    await ws.send_json(
                        {"type": "tool_use", "name": block.name, "input": block.input}
                    )
                    result = await _dispatch_tool(
                        block.name, block.input, device_slug, memory_root, client
                    )
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(result),
                        }
                    )
                messages.append({"role": "user", "content": tool_results})
    except WebSocketDisconnect:
        logger.info("[Diag-Direct] WS closed for device=%s", device_slug)
