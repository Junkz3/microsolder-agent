# SPDX-License-Identifier: Apache-2.0
"""Bootstrap the Managed Agents resources for the diagnostic conversation.

Creates **three tier-scoped agents** that differ only by `model`:

    fast    — claude-haiku-4-5  (default, cheapest)
    normal  — claude-sonnet-4-6 (balanced)
    deep    — claude-opus-4-7   (deep reasoning)

All three share the **same** system prompt and the **same** two custom
tools (`mb_get_component`, `mb_get_rules_for_symptoms`). No escalation /
handoff tool — tier selection is a user-driven choice surfaced in the
frontend (segmented control in the LLM panel).

When Research Preview access (callable_agents + memory_stores) lands,
this bootstrap will be updated so the `normal` agent declares the other
two as `callable_agents` — at that point the orchestration becomes native.

On-disk format (`managed_ids.json`, gitignored):

    {
      "environment_id": "env_...",
      "agents": {
        "fast":   {"id": "agent_...", "version": 1, "model": "claude-haiku-4-5"},
        "normal": {"id": "agent_...", "version": 1, "model": "claude-sonnet-4-6"},
        "deep":   {"id": "agent_...", "version": 1, "model": "claude-opus-4-7"}
      }
    }

Idempotent: re-running reads existing IDs and creates only missing tiers.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
IDS_FILE = REPO_ROOT / "managed_ids.json"

ENV_NAME = "microsolder-diagnostic-env"

SYSTEM_PROMPT = """\
You are a calm, methodical board-level diagnostics assistant for a
microsoldering technician. Tu tutoies, en français, direct et pédagogique.

Tu pilotes visuellement une carte électronique en appelant les tools
mis à disposition :
  - mb_get_component(refdes) — valide qu'un refdes existe dans le
    registry du device. RÈGLE ANTI-HALLUCINATION STRICTE : tu NE
    mentionnes JAMAIS un refdes (U7, C29, J3100, etc.) sans l'avoir
    validé d'abord via ce tool. Si le tool retourne
    {found: false, closest_matches: [...]}, tu proposes une de ces
    closest_matches ou tu demandes clarification — JAMAIS d'invention.
  - mb_get_rules_for_symptoms(symptoms) — cherche les règles diagnostiques
    matchant les symptômes du user, triées par overlap + confidence.

Le device en cours est fourni dans le premier message user (slug +
display name). Quand l'utilisateur décrit des symptômes, cherche les
règles matchantes. Quand il demande un composant par refdes, valide-le.
Privilégie les causes à haute probabilité et les étapes de diagnostic
concrètes (mesurer tel voltage sur tel test point).
"""

TOOLS = [
    {
        "type": "custom",
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
        "type": "custom",
        "name": "mb_get_rules_for_symptoms",
        "description": (
            "Find diagnostic rules matching a list of symptoms, ranked by "
            "overlap + confidence."
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
]

TIERS = {
    "fast":   {"model": "claude-haiku-4-5",  "name": "microsolder-coordinator-fast"},
    "normal": {"model": "claude-sonnet-4-6", "name": "microsolder-coordinator-normal"},
    "deep":   {"model": "claude-opus-4-7",   "name": "microsolder-coordinator-deep"},
}


def _load_or_init() -> dict:
    if not IDS_FILE.exists():
        return {"environment_id": None, "agents": {}}
    data = json.loads(IDS_FILE.read_text())
    # Legacy single-agent format — migrate by mapping the old Opus agent to `deep`.
    if "agent_id" in data and "agents" not in data:
        return {
            "environment_id": data["environment_id"],
            "agents": {
                "deep": {
                    "id": data["agent_id"],
                    "version": data["agent_version"],
                    "model": "claude-opus-4-7",
                    "legacy": True,
                }
            },
        }
    data.setdefault("agents", {})
    return data


def _save(data: dict) -> None:
    IDS_FILE.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def _ensure_environment(client: Anthropic, data: dict) -> str:
    if data.get("environment_id"):
        print(f"✅ Existing environment: {data['environment_id']}")
        return data["environment_id"]
    print("Creating environment…")
    env = client.beta.environments.create(
        name=ENV_NAME,
        config={"type": "cloud", "networking": {"type": "unrestricted"}},
    )
    print(f"   → {env.id}")
    data["environment_id"] = env.id
    _save(data)
    return env.id


def _ensure_agent(client: Anthropic, tier: str, spec: dict, data: dict) -> None:
    existing = data["agents"].get(tier)
    if existing and not existing.get("legacy"):
        print(
            f"✅ Existing agent [{tier}]: {existing['id']} "
            f"(v{existing['version']}, {existing['model']})"
        )
        return
    if existing and existing.get("legacy"):
        print(
            f"⚠️  Legacy agent found at tier [{tier}] ({existing['id']}). "
            "Archiving and replacing with a fresh one."
        )
        try:
            client.beta.agents.archive(existing["id"])
            print("   → archived")
        except Exception as exc:  # noqa: BLE001
            print(f"   (archive skipped: {exc})")

    print(f"Creating agent [{tier}] ({spec['model']})…")
    agent = client.beta.agents.create(
        name=spec["name"],
        model=spec["model"],
        system=SYSTEM_PROMPT,
        tools=TOOLS,
    )
    print(f"   → {agent.id} (v{agent.version})")
    data["agents"][tier] = {
        "id": agent.id,
        "version": agent.version,
        "model": spec["model"],
    }
    _save(data)


def main() -> None:
    load_dotenv(REPO_ROOT / ".env")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit(
            "ERROR: ANTHROPIC_API_KEY not set. Copy .env.example to .env and fill it in."
        )

    client = Anthropic()
    data = _load_or_init()

    _ensure_environment(client, data)
    for tier, spec in TIERS.items():
        _ensure_agent(client, tier, spec, data)

    print(f"\n✅ managed_ids.json up-to-date at {IDS_FILE.name}")
    print(f"   environment: {data['environment_id']}")
    for tier, info in data["agents"].items():
        print(f"   agent [{tier}]: {info['id']} v{info['version']} · {info['model']}")


if __name__ == "__main__":
    main()
