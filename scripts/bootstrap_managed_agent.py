# SPDX-License-Identifier: Apache-2.0
"""Bootstrap the Managed Agents resources for the diagnostic conversation.

Creates **three tier-scoped agents** that differ only by `model`:

    fast    — claude-haiku-4-5  (default, cheapest)
    normal  — claude-sonnet-4-6 (balanced)
    deep    — claude-opus-4-7   (deep reasoning)

All three share the **same** system prompt and the **same** 16 tools
(4 `mb_*` + 12 `bv_*` sourced from `api/agent/manifest`). No escalation /
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

import argparse
import json
import os
import sys
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

from api.agent.manifest import BV_TOOLS, MB_TOOLS

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
  - mb_list_findings(limit?, filter_refdes?) — liste les field reports
    de réparations confirmées sur ce device (technicien A a déjà confirmé
    que U7 était le coupable de tel symptôme). CONSULTE TOUJOURS en début
    de session — le travail des techs précédents doit informer ta
    diagnose avant d'enchaîner les règles génériques.
  - mb_record_finding(refdes, symptom, confirmed_cause, mechanism?, notes?)
    — persiste un finding confirmé par le technicien en fin de session.
    Appelle ce tool UNIQUEMENT quand le technicien confirme explicitement
    la cause ("c'était bien U7, je l'ai remplacé, ça fonctionne"). Ce
    record sera lu par les sessions futures sur le même device.
  - mb_expand_knowledge(focus_symptoms, focus_refdes?) — étend la memory
    bank quand mb_get_rules_for_symptoms retourne 0 résultats sur un
    symptôme sérieux. Déclenche un Scout ciblé + Clinicien qui ajoutent
    composants et règles au pack existant (~30-60s, ~$0.40). Après ça,
    re-appelle mb_get_rules_for_symptoms pour voir les nouvelles règles.
    Explique au technicien ce que tu fais ("je cherche sur les sources
    microsoudure…") pour qu'il attende.

Le device en cours est fourni dans le premier message user (slug +
display name). Quand l'utilisateur décrit des symptômes, consulte
d'abord mb_list_findings puis enchaîne mb_get_rules_for_symptoms.
Si 0 résultat sur le symptôme → mb_expand_knowledge pour le combler,
puis re-query. Quand il demande un composant par refdes, valide-le.
Privilégie les causes à haute probabilité et les étapes de diagnostic
concrètes (mesurer tel voltage sur tel test point).
"""

TOOLS = MB_TOOLS + BV_TOOLS

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


def _ensure_agent(
    client: Anthropic, tier: str, spec: dict, data: dict, *, refresh_tools: bool = False
) -> None:
    existing = data["agents"].get(tier)
    if existing and not existing.get("legacy") and not refresh_tools:
        print(
            f"✅ Existing agent [{tier}]: {existing['id']} "
            f"(v{existing['version']}, {existing['model']})"
        )
        return
    if existing and (existing.get("legacy") or refresh_tools):
        reason = "legacy agent" if existing.get("legacy") else "refresh requested"
        print(
            f"♻️  Replacing agent at tier [{tier}] ({existing['id']}) — {reason}. "
            "Archiving and re-creating with current TOOLS."
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
    parser = argparse.ArgumentParser(
        description="Bootstrap or refresh MA agents for microsolder-agent."
    )
    parser.add_argument(
        "--refresh-tools",
        action="store_true",
        help=(
            "Archive existing non-legacy agents and recreate them with the current TOOLS set. "
            "Use after updating the tool manifest."
        ),
    )
    args = parser.parse_args()

    load_dotenv(REPO_ROOT / ".env")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit(
            "ERROR: ANTHROPIC_API_KEY not set. Copy .env.example to .env and fill it in."
        )

    client = Anthropic()
    data = _load_or_init()

    _ensure_environment(client, data)
    for tier, spec in TIERS.items():
        _ensure_agent(client, tier, spec, data, refresh_tools=args.refresh_tools)

    print(f"\n✅ managed_ids.json up-to-date at {IDS_FILE.name}")
    print(f"   environment: {data['environment_id']}")
    for tier, info in data["agents"].items():
        print(f"   agent [{tier}]: {info['id']} v{info['version']} · {info['model']}")


if __name__ == "__main__":
    main()
