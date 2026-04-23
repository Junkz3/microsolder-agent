# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the per-repair measurement journal."""

from __future__ import annotations

import pytest

from api.agent.measurement_memory import (
    MeasurementEvent,
    auto_classify,
    parse_target,
)


def test_measurement_event_shape():
    ev = MeasurementEvent(
        timestamp="2026-04-23T18:45:12Z",
        target="rail:+3V3",
        value=2.87,
        unit="V",
        nominal=3.3,
        source="ui",
    )
    assert ev.target == "rail:+3V3"
    assert ev.auto_classified_mode is None  # defaults to None


def test_parse_target_rail():
    assert parse_target("rail:+3V3") == ("rail", "+3V3")
    assert parse_target("rail:LPC_VCC") == ("rail", "LPC_VCC")


def test_parse_target_comp():
    assert parse_target("comp:U7") == ("comp", "U7")


def test_parse_target_pin():
    assert parse_target("pin:U7:3") == ("pin", "U7:3")
    assert parse_target("pin:U18:A7") == ("pin", "U18:A7")


def test_parse_target_invalid_kind():
    with pytest.raises(ValueError, match="unknown target kind"):
        parse_target("foo:bar")


def test_parse_target_missing_colon():
    with pytest.raises(ValueError, match="expected '<kind>:<name>'"):
        parse_target("U7")


def test_auto_classify_rail_alive():
    assert auto_classify(target="rail:+3V3", value=3.29, unit="V", nominal=3.3) == "alive"
    assert auto_classify(target="rail:+3V3", value=3.0, unit="V", nominal=3.3) == "alive"  # 90.9%


def test_auto_classify_rail_anomalous_sag():
    assert auto_classify(target="rail:+3V3", value=2.8, unit="V", nominal=3.3) == "anomalous"
    assert auto_classify(target="rail:+3V3", value=1.65, unit="V", nominal=3.3) == "anomalous"  # 50%


def test_auto_classify_rail_dead():
    assert auto_classify(target="rail:+3V3", value=0.02, unit="V", nominal=3.3) == "dead"


def test_auto_classify_rail_overvoltage_as_shorted():
    assert auto_classify(target="rail:+3V3", value=4.0, unit="V", nominal=3.3) == "shorted"


def test_auto_classify_rail_explicit_short_note():
    # near-zero voltage + explicit note='short' promotes dead → shorted.
    assert auto_classify(
        target="rail:+3V3", value=0.0, unit="V", nominal=3.3, note="short"
    ) == "shorted"


def test_auto_classify_ic_hot():
    assert auto_classify(target="comp:Q17", value=72.3, unit="°C") == "hot"
    assert auto_classify(target="comp:Q17", value=55.0, unit="°C") == "alive"


def test_auto_classify_rail_missing_nominal_returns_none():
    # Can't classify without knowing the expected value.
    assert auto_classify(target="rail:+3V3", value=2.8, unit="V", nominal=None) is None


def test_auto_classify_unknown_target_kind_returns_none():
    # Pin-level measurements don't auto-classify to component modes.
    assert auto_classify(target="pin:U7:3", value=0.8, unit="V", nominal=3.3) is None
