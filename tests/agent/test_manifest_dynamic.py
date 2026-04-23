"""Tests for build_tools_manifest and render_system_prompt."""

from api.agent.manifest import BV_TOOLS, MB_TOOLS, build_tools_manifest, render_system_prompt
from api.board.model import Board, Layer, Part, Point
from api.session.state import SessionState


def _session_with_board() -> SessionState:
    parts = [Part(refdes="U7", layer=Layer.TOP, is_smd=True,
                  bbox=(Point(x=0, y=0), Point(x=10, y=10)), pin_refs=[])]
    board = Board(board_id="b", file_hash="sha256:x", source_format="t",
                  outline=[], parts=parts, pins=[], nets=[], nails=[])
    s = SessionState()
    s.set_board(board)
    return s


def test_mb_tools_has_four_entries() -> None:
    assert len(MB_TOOLS) == 4
    names = {t["name"] for t in MB_TOOLS}
    assert names == {
        "mb_get_component", "mb_get_rules_for_symptoms",
        "mb_list_findings", "mb_record_finding",
    }


def test_bv_tools_has_twelve_entries() -> None:
    assert len(BV_TOOLS) == 12
    names = {t["name"] for t in BV_TOOLS}
    assert names == {
        "bv_highlight", "bv_focus", "bv_reset_view", "bv_flip",
        "bv_annotate", "bv_dim_unrelated", "bv_highlight_net",
        "bv_show_pin", "bv_draw_arrow", "bv_measure",
        "bv_filter_by_type", "bv_layer_visibility",
    }


def test_every_tool_has_name_description_input_schema() -> None:
    for tool in MB_TOOLS + BV_TOOLS:
        assert isinstance(tool["name"], str) and tool["name"]
        assert isinstance(tool["description"], str) and tool["description"]
        assert isinstance(tool["input_schema"], dict)
        assert tool["input_schema"].get("type") == "object"
        assert "properties" in tool["input_schema"]


def test_manifest_without_board_has_only_mb_tools() -> None:
    session = SessionState()  # board=None
    manifest = build_tools_manifest(session)
    names = {t["name"] for t in manifest}
    assert names == {t["name"] for t in MB_TOOLS}
    assert len(manifest) == 4


def test_manifest_with_board_adds_bv_tools() -> None:
    session = _session_with_board()
    manifest = build_tools_manifest(session)
    names = {t["name"] for t in manifest}
    assert names == {t["name"] for t in MB_TOOLS} | {t["name"] for t in BV_TOOLS}
    assert len(manifest) == 16


def test_manifest_has_no_sch_tools_regardless_of_session() -> None:
    session = _session_with_board()
    manifest = build_tools_manifest(session)
    assert not any(t["name"].startswith("sch_") for t in manifest)


def test_render_system_prompt_mentions_boardview_when_available() -> None:
    session = _session_with_board()
    prompt = render_system_prompt(session, device_slug="demo-pi")
    assert "boardview" in prompt.lower()
    assert "demo-pi" in prompt


def test_render_system_prompt_mentions_boardview_absent_when_no_board() -> None:
    session = SessionState()
    prompt = render_system_prompt(session, device_slug="demo-pi")
    assert "boardview" in prompt.lower()
    assert "memory bank" in prompt.lower()


def test_bv_highlight_refdes_accepts_string_or_array() -> None:
    """Req 6 — oneOf schema for refdes param."""
    schema = next(t for t in BV_TOOLS if t["name"] == "bv_highlight")["input_schema"]
    refdes_schema = schema["properties"]["refdes"]
    assert "oneOf" in refdes_schema
    types = {s["type"] for s in refdes_schema["oneOf"]}
    assert types == {"string", "array"}


def test_enum_constraints_present() -> None:
    """Req 7 — color and layer fields declare enum constraint."""
    bv_h = next(t for t in BV_TOOLS if t["name"] == "bv_highlight")["input_schema"]
    assert "enum" in bv_h["properties"]["color"]
    assert set(bv_h["properties"]["color"]["enum"]) == {"accent", "warn", "mute"}
    bv_lv = next(t for t in BV_TOOLS if t["name"] == "bv_layer_visibility")["input_schema"]
    assert "enum" in bv_lv["properties"]["layer"]
    assert set(bv_lv["properties"]["layer"]["enum"]) == {"top", "bottom"}


def test_bv_show_pin_minimum() -> None:
    """Req 8 — pin index must be >= 1."""
    schema = next(t for t in BV_TOOLS if t["name"] == "bv_show_pin")["input_schema"]
    assert schema["properties"]["pin"].get("minimum") == 1


def test_mb_list_findings_limit_constraints() -> None:
    """Req 9 — limit range 1..100."""
    schema = next(t for t in MB_TOOLS if t["name"] == "mb_list_findings")["input_schema"]
    limit = schema["properties"]["limit"]
    assert limit.get("minimum") == 1
    assert limit.get("maximum") == 100
