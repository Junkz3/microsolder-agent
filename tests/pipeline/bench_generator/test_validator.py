# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from api.pipeline.bench_generator.schemas import (
    Cause,
    EvidenceSpan,
    ProposedScenarioDraft,
)
from api.pipeline.bench_generator.validator import (
    check_sanity,
    check_duplicates,
    check_grounding,
)


def _with_overrides(base: ProposedScenarioDraft, **changes) -> ProposedScenarioDraft:
    data = base.model_dump()
    data.update(changes)
    return ProposedScenarioDraft.model_validate(data)


def test_v1_accepts_clean_draft(sample_draft):
    rej = check_sanity(sample_draft)
    assert rej is None


def test_v1_rejects_too_short_quote(sample_draft):
    # this would already fail at Pydantic load time — V1 is defence in depth
    # when we later relax Pydantic. For now, construct through .model_construct
    # to bypass validation.
    d = ProposedScenarioDraft.model_construct(**{
        **sample_draft.model_dump(),
        "source_quote": "too short",
    })
    rej = check_sanity(d)
    assert rej is not None
    assert rej.motive == "source_quote_too_short"


def test_v1_rejects_malformed_url(sample_draft):
    d = ProposedScenarioDraft.model_construct(**{
        **sample_draft.model_dump(),
        "source_url": "not-a-url",
    })
    rej = check_sanity(d)
    assert rej is not None
    assert rej.motive == "source_url_malformed"


def test_v5_dedup_keeps_first(sample_draft):
    d1 = sample_draft
    d2 = _with_overrides(sample_draft, local_id="c19-short-dup")
    accepted, rejected = check_duplicates([d1, d2])
    assert [d.local_id for d in accepted] == ["c19-short"]
    assert [r.local_id for r in rejected] == ["c19-short-dup"]
    assert rejected[0].motive == "duplicate_in_run"


def test_v5_no_dup_no_rejection(sample_draft):
    d1 = sample_draft
    d2 = _with_overrides(
        sample_draft,
        local_id="c19-open",
        cause=Cause(refdes="C19", mode="open").model_dump(),
    )
    accepted, rejected = check_duplicates([d1, d2])
    assert len(accepted) == 2
    assert rejected == []


def test_v2_accepts_clean_grounding(sample_draft):
    rej = check_grounding(sample_draft)
    assert rej is None


def test_v2_rejects_nonliteral_span(sample_draft):
    bad = sample_draft.model_copy(deep=True)
    bad.evidence[0] = EvidenceSpan(
        field="cause.refdes",
        source_quote_substring="C 19",  # extra space — not literally in quote
        reasoning="wrong",
    )
    rej = check_grounding(bad)
    assert rej is not None
    assert rej.motive == "evidence_span_not_literal"


def test_v2_rejects_missing_evidence_for_nonempty_rails(sample_draft):
    """If expected_dead_rails is non-empty, at least one evidence must target it."""
    bad = sample_draft.model_copy(deep=True)
    bad.evidence = [
        e for e in bad.evidence if e.field != "expected_dead_rails"
    ]
    rej = check_grounding(bad)
    assert rej is not None
    assert rej.motive == "evidence_missing"
    assert "expected_dead_rails" in rej.detail


def test_v2_rejects_evidence_on_empty_field(sample_draft):
    """If expected_dead_components is empty, no evidence may point at it."""
    bad = sample_draft.model_copy(deep=True)
    bad.evidence.append(
        EvidenceSpan(
            field="expected_dead_components",
            source_quote_substring="C19",
            reasoning="stale",
        )
    )
    rej = check_grounding(bad)
    assert rej is not None
    assert rej.motive == "evidence_field_empty"
