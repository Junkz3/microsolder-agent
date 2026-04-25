#!/usr/bin/env python
# SPDX-License-Identifier: Apache-2.0
"""POC — Workflow A phases 3-4 through Managed Agents + shared memory store.

Does the Anthropic Managed Agents path save enough tokens vs. the legacy
`messages.create` + `cache_control: ephemeral` pipeline to justify migrating
the knowledge factory? This script answers the question on one device:

- Reads a pre-built pack (legacy run wrote `raw_research_dump.md` +
  `registry.json` to `memory/{slug}/`).
- Seeds a fresh read-only memory store with those two files under `/inputs/`.
- Runs three MA writer sessions in parallel (Cartographe + Clinicien Opus,
  Lexicographe Sonnet) — each attaches the store, reads the inputs via the
  mount, and emits its schema via a single forced custom tool.
- Runs the deterministic `compute_drift` locally.
- Seeds writer outputs + drift report back into the store, opens the Auditor
  session (Opus), collects the verdict.
- Writes `knowledge_graph.json`, `rules.json`, `dictionary.json`,
  `audit_verdict.json`, and a `run.json` (tokens, durations, USD estimate)
  to `benchmark/poc_multi_agent/{slug}_{ts}/`.

Usage:
    .venv/bin/python scripts/poc_pipeline_managed_agents.py --slug mnt-reform-motherboard

Cleanup: the memory store + 4 created agents are archived at the end so
re-runs do not leave stale resources. One environment is created per run
(they're cheap) and left in place — archive it manually if it bothers you.

Scope: this script does NOT replace the legacy pipeline. It runs alongside
it for a specific device so the token delta can be read off `run.json` and
the legacy `memory/{slug}/token_stats.json` side by side.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from anthropic import AsyncAnthropic
from pydantic import BaseModel

from api.config import get_settings
from api.pipeline.auditor import SUBMIT_AUDIT_TOOL_NAME
from api.pipeline.drift import compute_drift
from api.pipeline.prompts import (
    AUDITOR_SYSTEM,
    CARTOGRAPHE_TASK,
    CLINICIEN_TASK,
    LEXICOGRAPHE_TASK,
    WRITER_SYSTEM,
)
from api.pipeline.schemas import (
    AuditVerdict,
    Dictionary,
    DriftItem,
    KnowledgeGraph,
    Registry,
    RulesSet,
)
from api.pipeline.writers import (
    SUBMIT_DICT_TOOL_NAME,
    SUBMIT_KG_TOOL_NAME,
    SUBMIT_RULES_TOOL_NAME,
)

logger = logging.getLogger("microsolder.poc.pipeline_ma")


# ----------------------------------------------------------------------
# Model selection mirrors the legacy pipeline's Sonnet/Opus split.
# ----------------------------------------------------------------------

MODEL_OPUS = "claude-opus-4-7"
MODEL_SONNET = "claude-sonnet-4-6"

# USD per 1M tokens — for the run-report cost estimate only. Rates are the
# public Anthropic pricing at the time of this file (check the docs before
# reading too much into absolute numbers; ratios are stable).
PRICING: dict[str, dict[str, float]] = {
    MODEL_OPUS: {
        "input": 15.0,
        "output": 75.0,
        "cache_read": 1.5,       # 0.10×
        "cache_write": 18.75,    # 1.25×
    },
    MODEL_SONNET: {
        "input": 3.0,
        "output": 15.0,
        "cache_read": 0.3,
        "cache_write": 3.75,
    },
}


# The writers' existing `WRITER_SYSTEM` already restricts output to a forced
# tool call, but it assumes a `messages.create` caller that supplies inputs
# inline. Under MA the inputs live on the memory mount, so the agent needs a
# short guidance block about the mount + the "one call, then end turn" rule.
_MA_FORCING_SUFFIX = """

---
## Running inside a Managed Agents session

Inputs are mounted under `/mnt/memory/<store_dir>/`. Locate the mount with
`glob` if needed, then read the files via the `read` tool. Rules:

  1. Your ONLY valid output is ONE call to the submit_* tool defined below.
     No text, no clarifying questions, no comments.
  2. Read the entire input you need BEFORE calling submit_* — the payload
     is validated against a Pydantic schema and you get exactly one shot.
  3. After submit_* returns "OK", end the turn. Do NOT call submit_* twice.
"""


# ----------------------------------------------------------------------
# Cost accounting
# ----------------------------------------------------------------------


@dataclass
class PhaseCost:
    phase: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    duration_seconds: float = 0.0

    def estimate_usd(self) -> float:
        p = PRICING.get(self.model)
        if not p:
            return 0.0
        return (
            self.input_tokens * p["input"]
            + self.output_tokens * p["output"]
            + self.cache_read_tokens * p["cache_read"]
            + self.cache_creation_tokens * p["cache_write"]
        ) / 1_000_000.0


@dataclass
class RunCosts:
    device_slug: str
    started_at: str
    ended_at: str = ""
    phases: dict[str, PhaseCost] = field(default_factory=dict)

    @property
    def total_input(self) -> int:
        return sum(p.input_tokens for p in self.phases.values())

    @property
    def total_output(self) -> int:
        return sum(p.output_tokens for p in self.phases.values())

    @property
    def total_cache_read(self) -> int:
        return sum(p.cache_read_tokens for p in self.phases.values())

    @property
    def total_cache_write(self) -> int:
        return sum(p.cache_creation_tokens for p in self.phases.values())

    @property
    def total_usd(self) -> float:
        return sum(p.estimate_usd() for p in self.phases.values())


# ----------------------------------------------------------------------
# Resource helpers — environment, memory store, agents
# ----------------------------------------------------------------------


async def ensure_environment(client: AsyncAnthropic, run_id: str) -> Any:
    """Fresh environment per run — cheap, avoids stale-config surprises."""
    env = await client.beta.environments.create(
        name=f"poc-pipeline-{run_id}",
        config={"type": "cloud", "networking": {"type": "unrestricted"}},
    )
    logger.info("environment created: %s", env.id)
    return env


async def create_memory_store(
    client: AsyncAnthropic, slug: str, run_id: str
) -> Any:
    store = await client.beta.memory_stores.create(
        name=f"poc-pipeline-{slug}-{run_id}",
        description=(
            "POC per-run store. /inputs/ holds raw dump + registry (writer "
            "sources). /writers/ holds writer outputs for the auditor. "
            "/auditor/drift.json is the deterministic drift report."
        ),
    )
    logger.info("memory_store created: %s", store.id)
    return store


async def seed_inputs(
    client: AsyncAnthropic,
    store_id: str,
    *,
    raw_dump: str,
    registry: Registry,
) -> None:
    await client.beta.memory_stores.memories.create(
        store_id, path="/inputs/raw_research_dump.md", content=raw_dump,
    )
    await client.beta.memory_stores.memories.create(
        store_id,
        path="/inputs/registry.json",
        content=registry.model_dump_json(indent=2),
    )
    logger.info("seeded /inputs/ in %s", store_id)


async def seed_writer_outputs(
    client: AsyncAnthropic,
    store_id: str,
    *,
    kg: KnowledgeGraph,
    rules: RulesSet,
    dictionary: Dictionary,
) -> None:
    await client.beta.memory_stores.memories.create(
        store_id,
        path="/writers/knowledge_graph.json",
        content=kg.model_dump_json(indent=2),
    )
    await client.beta.memory_stores.memories.create(
        store_id,
        path="/writers/rules.json",
        content=rules.model_dump_json(indent=2),
    )
    await client.beta.memory_stores.memories.create(
        store_id,
        path="/writers/dictionary.json",
        content=dictionary.model_dump_json(indent=2),
    )
    logger.info("seeded /writers/ in %s", store_id)


async def seed_drift(
    client: AsyncAnthropic, store_id: str, drifts: list[DriftItem],
) -> None:
    payload = json.dumps([d.model_dump() for d in drifts], indent=2)
    await client.beta.memory_stores.memories.create(
        store_id, path="/auditor/drift.json", content=payload,
    )
    logger.info("seeded /auditor/drift.json (%d items)", len(drifts))


def _read_only_toolset() -> dict:
    """Enable read/grep/glob only — no bash, write, or web access needed."""
    return {
        "type": "agent_toolset_20260401",
        "default_config": {"enabled": False},
        "configs": [
            {"name": "read", "enabled": True},
            {"name": "grep", "enabled": True},
            {"name": "glob", "enabled": True},
        ],
    }


# MA's `tools[*].input_schema` validator accepts only a narrow top-level
# schema shape: object type with `properties` + `required`. Any extra key
# at the root (`$defs`, `additionalProperties`, `description`, `title`, …)
# trips "Extra inputs are not permitted". Inside nested schemas the rules
# are looser — keep `description` on properties (it helps Claude pick the
# right shape) but keep stripping `additionalProperties` everywhere.
_MA_ROOT_ALLOWED = frozenset({"type", "properties", "required"})
_MA_NESTED_STRIP = frozenset({"$defs", "additionalProperties"})


def _inline_defs(schema: dict) -> dict:
    """Normalize a Pydantic JSON schema for MA's `tools[*].input_schema`.

    Transforms:
      1. Pops top-level `$defs` and resolves every `$ref: "#/$defs/Foo"`
         into an inline copy of `$defs["Foo"]`, recursing into the inlined
         copy (cycle-safe via a seen set).
      2. At the ROOT, keeps only `type`, `properties`, `required` — strips
         `description`, `title`, `additionalProperties`, and any other
         metadata MA rejects at the top level.
      3. In nested schemas, strips only `additionalProperties` and `$defs`,
         keeping `description` / `enum` / etc. since they help the model
         pick the right shape.

    Pydantic emits `additionalProperties: false` to reflect `extra="forbid"`
    model config; MA enforces that strictness implicitly so dropping the
    key is semantically safe.
    """
    defs = dict(schema.pop("$defs", {}))

    def _walk(node: object, in_flight: frozenset[str]) -> object:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                name = ref.split("/", 3)[-1]
                if name in in_flight:
                    return {}
                target = defs.get(name)
                if target is None:
                    return {}
                return _walk(dict(target), in_flight | {name})
            return {
                k: _walk(v, in_flight)
                for k, v in node.items()
                if k not in _MA_NESTED_STRIP
            }
        if isinstance(node, list):
            return [_walk(x, in_flight) for x in node]
        return node

    inlined = _walk(schema, frozenset())
    # Root pruning: MA only accepts `type` / `properties` / `required` at
    # the top of `input_schema`.
    if isinstance(inlined, dict):
        return {k: v for k, v in inlined.items() if k in _MA_ROOT_ALLOWED}
    return inlined  # type: ignore[return-value]


def _submit_tool_def(
    name: str, description: str, schema_model: type[BaseModel]
) -> dict:
    return {
        "type": "custom",
        "name": name,
        "description": description,
        "input_schema": _inline_defs(schema_model.model_json_schema()),
    }


async def create_writer_agent(
    client: AsyncAnthropic,
    *,
    name: str,
    model: str,
    task_suffix: str,
    tool_name: str,
    tool_description: str,
    schema_model: type[BaseModel],
) -> Any:
    system = (
        f"{WRITER_SYSTEM}\n\n## Your specific task\n\n{task_suffix}"
        f"{_MA_FORCING_SUFFIX}"
    )
    agent = await client.beta.agents.create(
        name=name,
        model=model,
        system=system,
        tools=[
            _read_only_toolset(),
            _submit_tool_def(tool_name, tool_description, schema_model),
        ],
    )
    logger.info(
        "agent %s created (id=%s v=%d model=%s)",
        name, agent.id, agent.version, model,
    )
    return agent


async def create_auditor_agent(client: AsyncAnthropic) -> Any:
    system = AUDITOR_SYSTEM + _MA_FORCING_SUFFIX
    agent = await client.beta.agents.create(
        name="POC-Auditor",
        model=MODEL_OPUS,
        system=system,
        tools=[
            _read_only_toolset(),
            _submit_tool_def(
                SUBMIT_AUDIT_TOOL_NAME,
                "Submit the structured audit verdict. Your only valid output.",
                AuditVerdict,
            ),
        ],
    )
    logger.info("auditor agent created (id=%s v=%d)", agent.id, agent.version)
    return agent


# ----------------------------------------------------------------------
# Single-session runner — the two-step custom-tool dance with dedup
# ----------------------------------------------------------------------


class _ToolDanceFailed(RuntimeError):
    pass


async def _run_single_tool_session(
    client: AsyncAnthropic,
    *,
    agent: Any,
    environment_id: str,
    memory_store_id: str,
    kickoff_text: str,
    target_tool_name: str,
    output_schema: type[BaseModel],
    cost: PhaseCost,
    title_hint: str,
) -> BaseModel:
    """Drive one agent through one turn that ends with a single submit_* call.

    Follows the MA doc gotchas: stream-before-send, cache custom_tool_use by
    id, dedup `requires_action`, defensive accessor for event_ids shape.
    """
    session = await client.beta.sessions.create(
        agent={"type": "agent", "id": agent.id, "version": agent.version},
        environment_id=environment_id,
        title=title_hint,
        resources=[
            {
                "type": "memory_store",
                "memory_store_id": memory_store_id,
                "access": "read_only",
                "instructions": (
                    "Knowledge pipeline context. /inputs/ holds raw dump + "
                    "registry, /writers/ holds writer outputs, "
                    "/auditor/drift.json is the drift report. Read only "
                    "what your task needs."
                ),
            }
        ],
    )
    logger.info("session %s opened for %s", session.id, agent.name)

    events_by_id: dict[str, Any] = {}
    responded: set[str] = set()
    collected: BaseModel | None = None
    t_start = time.monotonic()

    stream_ctx = await client.beta.sessions.events.stream(session.id)
    async with stream_ctx as stream:
        await client.beta.sessions.events.send(
            session.id,
            events=[{
                "type": "user.message",
                "content": [{"type": "text", "text": kickoff_text}],
            }],
        )

        async for event in stream:
            etype = getattr(event, "type", None)

            if etype == "span.model_request_end":
                usage = getattr(event, "model_usage", None)
                if usage is not None:
                    cost.input_tokens += getattr(usage, "input_tokens", 0) or 0
                    cost.output_tokens += getattr(usage, "output_tokens", 0) or 0
                    cost.cache_read_tokens += (
                        getattr(usage, "cache_read_input_tokens", 0) or 0
                    )
                    cost.cache_creation_tokens += (
                        getattr(usage, "cache_creation_input_tokens", 0) or 0
                    )

            elif etype == "agent.custom_tool_use":
                events_by_id[event.id] = event

            elif etype == "session.status_idle":
                stop = getattr(event, "stop_reason", None)
                stop_type = getattr(stop, "type", None) if stop else None

                if stop_type == "requires_action":
                    # event_ids shape varies across SDK versions.
                    event_ids = (
                        getattr(stop, "event_ids", None)
                        or getattr(
                            getattr(stop, "requires_action", None),
                            "event_ids", None,
                        )
                        or []
                    )
                    for eid in event_ids:
                        if eid in responded:
                            # MA can re-emit requires_action with ids we've
                            # already answered after a brief hiccup — a second
                            # response tears the stream down with HTTP 400.
                            continue
                        tool_ev = events_by_id.get(eid)
                        if tool_ev is None:
                            logger.warning(
                                "requires_action for uncached eid=%s — skipping",
                                eid,
                            )
                            continue
                        tool_name = getattr(tool_ev, "name", "")
                        payload = getattr(tool_ev, "input", {}) or {}
                        ack_text = "OK"
                        if tool_name == target_tool_name:
                            try:
                                collected = output_schema.model_validate(payload)
                            except Exception as exc:  # noqa: BLE001
                                raise _ToolDanceFailed(
                                    f"{agent.name}: {target_tool_name} payload "
                                    f"failed schema validation: {exc}"
                                ) from exc
                            ack_text = "OK — end turn."
                        else:
                            logger.warning(
                                "unexpected custom tool %r from %s — "
                                "acking and ignoring",
                                tool_name, agent.name,
                            )
                        await client.beta.sessions.events.send(
                            session.id,
                            events=[{
                                "type": "user.custom_tool_result",
                                "custom_tool_use_id": eid,
                                "content": [{"type": "text", "text": ack_text}],
                            }],
                        )
                        responded.add(eid)
                elif stop_type == "end_turn":
                    break

            elif etype == "session.status_terminated":
                break
            elif etype == "session.error":
                err = getattr(event, "error", None)
                raise _ToolDanceFailed(
                    f"{agent.name}: session.error "
                    f"{getattr(err, 'message', 'unknown')}"
                )

    cost.duration_seconds = time.monotonic() - t_start

    if collected is None:
        raise _ToolDanceFailed(
            f"{agent.name}: session ended without a {target_tool_name} call"
        )
    return collected


# ----------------------------------------------------------------------
# Phase orchestration
# ----------------------------------------------------------------------


_WRITER_KICKOFF = (
    "Read `/mnt/memory/*/inputs/raw_research_dump.md` and "
    "`/mnt/memory/*/inputs/registry.json` (use `glob` first if you need to "
    "resolve the exact mount directory name). Then emit your schema via the "
    "single submit_* custom tool declared on you. One tool call, no text."
)

_AUDITOR_KICKOFF = (
    "Read the following files from the memory mount under /mnt/memory/: "
    "`inputs/raw_research_dump.md`, `inputs/registry.json`, "
    "`writers/knowledge_graph.json`, `writers/rules.json`, "
    "`writers/dictionary.json`, `auditor/drift.json`. "
    "Then emit submit_audit_verdict — include the drift items VERBATIM under "
    "`drift_detected`, judge coherence + plausibility, set overall_status. "
    "One tool call, no text."
)


async def run_writers_parallel(
    client: AsyncAnthropic,
    *,
    environment_id: str,
    memory_store_id: str,
    cart_agent: Any,
    clin_agent: Any,
    lex_agent: Any,
    costs: RunCosts,
) -> tuple[KnowledgeGraph, RulesSet, Dictionary]:
    cart_cost = PhaseCost(phase="writer_cartographe", model=MODEL_OPUS)
    clin_cost = PhaseCost(phase="writer_clinicien", model=MODEL_OPUS)
    lex_cost = PhaseCost(phase="writer_lexicographe", model=MODEL_SONNET)
    costs.phases["writer_cartographe"] = cart_cost
    costs.phases["writer_clinicien"] = clin_cost
    costs.phases["writer_lexicographe"] = lex_cost

    kg, rules, dictionary = await asyncio.gather(
        _run_single_tool_session(
            client, agent=cart_agent, environment_id=environment_id,
            memory_store_id=memory_store_id, kickoff_text=_WRITER_KICKOFF,
            target_tool_name=SUBMIT_KG_TOOL_NAME, output_schema=KnowledgeGraph,
            cost=cart_cost, title_hint="poc-cartographe",
        ),
        _run_single_tool_session(
            client, agent=clin_agent, environment_id=environment_id,
            memory_store_id=memory_store_id, kickoff_text=_WRITER_KICKOFF,
            target_tool_name=SUBMIT_RULES_TOOL_NAME, output_schema=RulesSet,
            cost=clin_cost, title_hint="poc-clinicien",
        ),
        _run_single_tool_session(
            client, agent=lex_agent, environment_id=environment_id,
            memory_store_id=memory_store_id, kickoff_text=_WRITER_KICKOFF,
            target_tool_name=SUBMIT_DICT_TOOL_NAME, output_schema=Dictionary,
            cost=lex_cost, title_hint="poc-lexicographe",
        ),
    )
    return kg, rules, dictionary


async def run_auditor(
    client: AsyncAnthropic,
    *,
    environment_id: str,
    memory_store_id: str,
    audit_agent: Any,
    costs: RunCosts,
) -> AuditVerdict:
    cost = PhaseCost(phase="auditor", model=MODEL_OPUS)
    costs.phases["auditor"] = cost
    verdict = await _run_single_tool_session(
        client, agent=audit_agent, environment_id=environment_id,
        memory_store_id=memory_store_id, kickoff_text=_AUDITOR_KICKOFF,
        target_tool_name=SUBMIT_AUDIT_TOOL_NAME, output_schema=AuditVerdict,
        cost=cost, title_hint="poc-auditor",
    )
    return verdict


# ----------------------------------------------------------------------
# Report + cleanup
# ----------------------------------------------------------------------


def print_report(costs: RunCosts, out_dir: Path, slug: str, legacy_dir: Path) -> None:
    lines = [
        "",
        "=" * 88,
        f"POC Managed-Agents pipeline — {slug}",
        "=" * 88,
        f"Artefacts: {out_dir}",
        "",
        f"{'Phase':<22} {'Model':<20} {'in':>8} {'out':>8} "
        f"{'cacheR':>8} {'cacheW':>8} {'dur':>7}  USD",
        "-" * 88,
    ]
    for name, p in costs.phases.items():
        lines.append(
            f"{name:<22} {p.model:<20} {p.input_tokens:>8} "
            f"{p.output_tokens:>8} {p.cache_read_tokens:>8} "
            f"{p.cache_creation_tokens:>8} {p.duration_seconds:>6.1f}s  "
            f"${p.estimate_usd():.4f}"
        )
    lines.append("-" * 88)
    lines.append(
        f"{'TOTAL':<22} {'':<20} {costs.total_input:>8} "
        f"{costs.total_output:>8} {costs.total_cache_read:>8} "
        f"{costs.total_cache_write:>8} {'':>7}  ${costs.total_usd:.4f}"
    )
    legacy_stats = legacy_dir / "token_stats.json"
    if legacy_stats.exists():
        try:
            data = json.loads(legacy_stats.read_text())
            lines.append("")
            lines.append(
                f"Legacy run (memory/{slug}/token_stats.json) for comparison:"
            )
            lines.append(json.dumps(data, indent=2)[:800])
        except Exception:  # noqa: BLE001
            pass
    lines.append("")
    print("\n".join(lines))


async def _cleanup(
    client: AsyncAnthropic,
    *,
    agent_ids: list[str],
    store_id: str | None,
) -> None:
    for aid in agent_ids:
        try:
            await client.beta.agents.archive(aid)
        except Exception as exc:  # noqa: BLE001
            logger.warning("archive agent %s failed: %s", aid, exc)
    if store_id:
        try:
            await client.beta.memory_stores.archive(store_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("archive store %s failed: %s", store_id, exc)


# ----------------------------------------------------------------------
# Main run
# ----------------------------------------------------------------------


async def run(slug: str, memory_root: Path) -> None:
    settings = get_settings()
    client = AsyncAnthropic(
        api_key=settings.anthropic_api_key,
        max_retries=settings.anthropic_max_retries,
    )

    pack_dir = memory_root / slug
    raw_dump_path = pack_dir / "raw_research_dump.md"
    registry_path = pack_dir / "registry.json"
    if not raw_dump_path.exists() or not registry_path.exists():
        raise FileNotFoundError(
            f"{slug} needs a pre-built legacy pack (raw_research_dump.md + "
            f"registry.json). Run the legacy pipeline first."
        )
    raw_dump = raw_dump_path.read_text()
    registry = Registry.model_validate_json(registry_path.read_text())

    run_id = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    out_dir = Path("benchmark/poc_multi_agent") / f"{slug}_{run_id}"
    out_dir.mkdir(parents=True, exist_ok=True)
    costs = RunCosts(device_slug=slug, started_at=datetime.now(UTC).isoformat())

    env = await ensure_environment(client, run_id)
    store = await create_memory_store(client, slug, run_id)
    agent_ids: list[str] = []

    try:
        await seed_inputs(client, store.id, raw_dump=raw_dump, registry=registry)

        cart, clin, lex, auditor = await asyncio.gather(
            create_writer_agent(
                client, name="POC-Cartographe", model=MODEL_OPUS,
                task_suffix=CARTOGRAPHE_TASK,
                tool_name=SUBMIT_KG_TOOL_NAME,
                tool_description="Cartographe output — typed knowledge graph.",
                schema_model=KnowledgeGraph,
            ),
            create_writer_agent(
                client, name="POC-Clinicien", model=MODEL_OPUS,
                task_suffix=CLINICIEN_TASK,
                tool_name=SUBMIT_RULES_TOOL_NAME,
                tool_description="Clinicien output — diagnostic rules.",
                schema_model=RulesSet,
            ),
            create_writer_agent(
                client, name="POC-Lexicographe", model=MODEL_SONNET,
                task_suffix=LEXICOGRAPHE_TASK,
                tool_name=SUBMIT_DICT_TOOL_NAME,
                tool_description="Lexicographe output — component sheets.",
                schema_model=Dictionary,
            ),
            create_auditor_agent(client),
        )
        agent_ids.extend([cart.id, clin.id, lex.id, auditor.id])

        logger.info("=== Phase 3 — writers in parallel ===")
        kg, rules, dictionary = await run_writers_parallel(
            client, environment_id=env.id, memory_store_id=store.id,
            cart_agent=cart, clin_agent=clin, lex_agent=lex, costs=costs,
        )

        drifts = compute_drift(
            registry=registry, knowledge_graph=kg,
            rules=rules, dictionary=dictionary,
        )
        logger.info("drift items: %d", len(drifts))

        await seed_writer_outputs(
            client, store.id, kg=kg, rules=rules, dictionary=dictionary,
        )
        await seed_drift(client, store.id, drifts)

        logger.info("=== Phase 4 — auditor ===")
        verdict = await run_auditor(
            client, environment_id=env.id, memory_store_id=store.id,
            audit_agent=auditor, costs=costs,
        )

        # Persist artefacts.
        (out_dir / "knowledge_graph.json").write_text(kg.model_dump_json(indent=2))
        (out_dir / "rules.json").write_text(rules.model_dump_json(indent=2))
        (out_dir / "dictionary.json").write_text(dictionary.model_dump_json(indent=2))
        (out_dir / "audit_verdict.json").write_text(verdict.model_dump_json(indent=2))

        costs.ended_at = datetime.now(UTC).isoformat()
        (out_dir / "run.json").write_text(json.dumps(
            {
                "device_slug": slug,
                "run_id": run_id,
                "started_at": costs.started_at,
                "ended_at": costs.ended_at,
                "verdict_status": verdict.overall_status,
                "verdict_consistency": verdict.consistency_score,
                "drift_items": len(drifts),
                "totals": {
                    "input_tokens": costs.total_input,
                    "output_tokens": costs.total_output,
                    "cache_read_tokens": costs.total_cache_read,
                    "cache_creation_tokens": costs.total_cache_write,
                    "usd_estimate": round(costs.total_usd, 4),
                },
                "phases": {k: asdict(v) for k, v in costs.phases.items()},
                "resources": {
                    "environment_id": env.id,
                    "memory_store_id": store.id,
                    "agent_ids": agent_ids,
                },
            },
            indent=2,
        ))
        print_report(costs, out_dir, slug, pack_dir)
    finally:
        await _cleanup(client, agent_ids=agent_ids, store_id=store.id)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__.splitlines()[0] if __doc__ else None
    )
    p.add_argument(
        "--slug", required=True,
        help="Device slug under memory/ with a pre-built legacy pack "
             "(raw_research_dump.md + registry.json).",
    )
    p.add_argument(
        "--memory-root", default=None,
        help="Override memory root (defaults to settings.memory_root).",
    )
    p.add_argument(
        "--verbose", "-v", action="store_true",
        help="DEBUG logging.",
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    memory_root = (
        Path(args.memory_root) if args.memory_root
        else Path(get_settings().memory_root)
    )
    asyncio.run(run(args.slug, memory_root))


if __name__ == "__main__":
    main()
