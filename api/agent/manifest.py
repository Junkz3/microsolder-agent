# SPDX-License-Identifier: Apache-2.0
"""Tool manifest + system prompt builders for the diagnostic agent.

- MB_TOOLS: the always-on memory-bank family (4 tools).
- BV_TOOLS: the boardview control family (12 tools), exposed only when
  a board is loaded in the session.
- build_tools_manifest(session): produces the per-session manifest
  passed to Anthropic's messages.create or the Managed Agent definition.
- render_system_prompt(session, device_slug): DIRECT-runtime only; the
  Managed-runtime prompt is carried by the agent server-side.
"""

from __future__ import annotations

from api.session.state import SessionState

MB_TOOLS: list[dict] = [
    {
        "type": "custom",
        "name": "mb_get_component",
        "description": (
            "Look up a component by refdes on the current device. Returns "
            "aggregated info: {found, canonical_name, memory_bank: {...}|null, "
            "board: {...}|null} when found. For unknown refdes returns "
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
            "symptom overlap + rule confidence."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symptoms": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "max_results": {"type": "integer", "default": 5},
            },
            "required": ["symptoms"],
        },
    },
    {
        "type": "custom",
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
        "type": "custom",
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


BV_TOOLS: list[dict] = [
    {
        "type": "custom",
        "name": "bv_highlight",
        "description": "Highlight one or more components on the PCB canvas.",
        "input_schema": {
            "type": "object",
            "properties": {
                "refdes": {
                    "oneOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"}},
                    ],
                },
                "color": {"type": "string", "enum": ["accent", "warn", "mute"], "default": "accent"},
                "additive": {"type": "boolean", "default": False},
            },
            "required": ["refdes"],
        },
    },
    {
        "type": "custom",
        "name": "bv_focus",
        "description": "Pan/zoom the PCB canvas to a specific component. Auto-flips the board if the component is on the hidden side.",
        "input_schema": {
            "type": "object",
            "properties": {
                "refdes": {"type": "string"},
                "zoom": {"type": "number", "default": 2.5},
            },
            "required": ["refdes"],
        },
    },
    {
        "type": "custom",
        "name": "bv_reset_view",
        "description": "Reset the PCB canvas: clear all highlights, annotations, arrows, dim, filter. The technician's manual selection is preserved.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "type": "custom",
        "name": "bv_flip",
        "description": "Flip the visible PCB side (top ↔ bottom).",
        "input_schema": {
            "type": "object",
            "properties": {"preserve_cursor": {"type": "boolean", "default": False}},
        },
    },
    {
        "type": "custom",
        "name": "bv_annotate",
        "description": "Attach a text label to a component on the canvas.",
        "input_schema": {
            "type": "object",
            "properties": {
                "refdes": {"type": "string"},
                "label": {"type": "string"},
            },
            "required": ["refdes", "label"],
        },
    },
    {
        "type": "custom",
        "name": "bv_dim_unrelated",
        "description": "Visually dim all components not currently highlighted — focuses the technician's attention.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "type": "custom",
        "name": "bv_highlight_net",
        "description": "Highlight every pin on a given net (rail/signal tracing).",
        "input_schema": {
            "type": "object",
            "properties": {"net": {"type": "string"}},
            "required": ["net"],
        },
    },
    {
        "type": "custom",
        "name": "bv_show_pin",
        "description": "Point to a specific pin of a component (e.g. for a probe instruction).",
        "input_schema": {
            "type": "object",
            "properties": {
                "refdes": {"type": "string"},
                "pin": {"type": "integer", "minimum": 1},
            },
            "required": ["refdes", "pin"],
        },
    },
    {
        "type": "custom",
        "name": "bv_draw_arrow",
        "description": "Draw an arrow between two components (e.g. to show a signal path).",
        "input_schema": {
            "type": "object",
            "properties": {
                "from_refdes": {"type": "string"},
                "to_refdes": {"type": "string"},
            },
            "required": ["from_refdes", "to_refdes"],
        },
    },
    {
        "type": "custom",
        "name": "bv_measure",
        "description": "Return the physical distance (mm) between two components' centers.",
        "input_schema": {
            "type": "object",
            "properties": {
                "refdes_a": {"type": "string"},
                "refdes_b": {"type": "string"},
            },
            "required": ["refdes_a", "refdes_b"],
        },
    },
    {
        "type": "custom",
        "name": "bv_filter_by_type",
        "description": "Show only components whose refdes starts with a given prefix. The prefix must be the letter(s) used in the refdes convention (e.g. 'C' for capacitors, 'U' for ICs, 'R' for resistors), not a category name like 'capacitor'.",
        "input_schema": {
            "type": "object",
            "properties": {"prefix": {"type": "string"}},
            "required": ["prefix"],
        },
    },
    {
        "type": "custom",
        "name": "bv_layer_visibility",
        "description": "Toggle visibility of a PCB layer (top or bottom).",
        "input_schema": {
            "type": "object",
            "properties": {
                "layer": {"type": "string", "enum": ["top", "bottom"]},
                "visible": {"type": "boolean"},
            },
            "required": ["layer", "visible"],
        },
    },
]


def build_tools_manifest(session: SessionState) -> list[dict]:
    """Return the tools list for `session`, exposing `bv_*` only when board is loaded."""
    manifest: list[dict] = list(MB_TOOLS)
    if session.board is not None:
        manifest.extend(BV_TOOLS)
    # Future: if session.schematic is not None: manifest.extend(SCH_TOOLS)
    return manifest


def render_system_prompt(session: SessionState, *, device_slug: str) -> str:
    """Build the system prompt for the DIRECT runtime only.

    The Managed runtime carries its prompt server-side via managed_ids.json
    and doesn't call this function.
    """
    boardview_status = "✅" if session.board is not None else "❌ (no board file loaded)"
    return f"""\
You are a calm, methodical board-level diagnostics assistant for a
microsoldering technician. Tu tutoies, en français, direct et pédagogique.

Device courant : {device_slug}.

Capabilities for this session:
  - memory bank ✅ (mb_get_component, mb_get_rules_for_symptoms,
    mb_list_findings, mb_record_finding)
  - boardview {boardview_status}
  - schematic ❌ (not yet parsed)

RÈGLE ANTI-HALLUCINATION : tu NE mentionnes JAMAIS un refdes (U7, C29,
J3100…) sans l'avoir validé via mb_get_component. Si le tool retourne
{{found: false, closest_matches: [...]}}, tu proposes une des
closest_matches ou tu demandes clarification — JAMAIS d'invention. Les
refdes non validés seront automatiquement wrapped ⟨?U999⟩ dans la
réponse finale (sanitizer post-hoc) — signal de debug, pas d'excuse.

Quand l'utilisateur décrit des symptômes, consulte d'abord mb_list_findings
(historique cross-session de ce device), puis mb_get_rules_for_symptoms.
Quand il demande un composant, appelle mb_get_component — il agrège
memory bank + board (topologie, nets connectés) en un seul appel. Si la
boardview est disponible, enchaîne bv_focus + bv_highlight pour MONTRER
le suspect au tech. Quand l'utilisateur confirme la cause, appelle
mb_record_finding pour l'archiver. Ne réponds JAMAIS depuis ta mémoire de formation pour des refdes ou des symptômes — utilise toujours les tools ci-dessus.
"""
