from __future__ import annotations

from tools.common.selection_explanation import (
    SCHEMA_VERSION,
    build_selection_explanation,
    candidate_record,
    excluded_candidate_record,
    validate_selection_explanation,
)


def test_selection_explanation_requires_selected_candidate_to_be_considered() -> None:
    selected = candidate_record(
        candidate_id="candidate:selected",
        candidate_type="facet",
        label="selected",
        selected=True,
        rationale="selected by fixture policy",
    )
    considered = [
        candidate_record(
            candidate_id="candidate:other",
            candidate_type="facet",
            label="other",
            rationale="eligible but lower priority",
        )
    ]

    try:
        build_selection_explanation(
            selection_kind="feedback_next_action",
            created_at="2026-06-03T12:34:56Z",
            selected_candidate=selected,
            considered_candidates=considered,
            excluded_candidates=[],
            policy={"policy_id": "fixture-policy.v1"},
        )
    except ValueError as exc:
        assert "selected candidate must appear" in str(exc)
    else:
        raise AssertionError("selected candidate outside considered set should fail")


def test_selection_explanation_allows_explicit_operator_override() -> None:
    selected = candidate_record(
        candidate_id="candidate:override",
        candidate_type="facet",
        label="override",
        selected=True,
        rationale="operator selected a manual facet",
    )
    explanation = build_selection_explanation(
        selection_kind="feedback_next_action",
        created_at="2026-06-03T12:34:56Z",
        selected_candidate=selected,
        considered_candidates=[
            candidate_record(
                candidate_id="candidate:other",
                candidate_type="facet",
                label="other",
                rationale="eligible but not manually selected",
            )
        ],
        excluded_candidates=[],
        policy={"policy_id": "fixture-policy.v1"},
        operator_overrides=[
            {
                "override_kind": "manual_facet",
                "override_value": "override",
                "reason": "fixture override",
            }
        ],
    )

    assert explanation["schema_version"] == SCHEMA_VERSION
    assert validate_selection_explanation(explanation) == []


def test_selection_explanation_requires_exclusion_reasons() -> None:
    selected = candidate_record(
        candidate_id="candidate:selected",
        candidate_type="facet",
        label="selected",
        selected=True,
        rationale="selected by fixture policy",
    )
    considered = [selected]
    excluded = [
        excluded_candidate_record(
            candidate_id="candidate:excluded",
            candidate_type="facet",
            label="excluded",
            reason="lower_score",
        )
    ]
    explanation = build_selection_explanation(
        selection_kind="feedback_next_action",
        created_at="2026-06-03T12:34:56Z",
        selected_candidate=selected,
        considered_candidates=considered,
        excluded_candidates=excluded,
        policy={"policy_id": "fixture-policy.v1"},
    )
    explanation["excluded_candidates"][0]["reason"] = ""

    assert "excluded_candidates[0].reason must be a non-blank string" in (
        validate_selection_explanation(explanation)
    )


def test_selection_explanation_rejects_out_of_range_scores() -> None:
    selected = candidate_record(
        candidate_id="candidate:selected",
        candidate_type="facet",
        label="selected",
        score=101.0,
        selected=True,
        rationale="selected by fixture policy",
    )
    explanation = build_selection_explanation(
        selection_kind="feedback_next_action",
        created_at="2026-06-03T12:34:56Z",
        selected_candidate=selected,
        considered_candidates=[selected],
        excluded_candidates=[],
        policy={"policy_id": "fixture-policy.v1"},
    )

    errors = validate_selection_explanation(explanation)

    assert any("selected_candidate.score must be a finite number between" in error for error in errors)
