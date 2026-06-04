"""Selection explanation helpers for local operator planning artifacts."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

SCHEMA_VERSION = "selection-explanation.v1"


class SelectionExplanationError(ValueError):
    """Raised when a selection explanation object is malformed."""


def stable_explanation_id(
    *,
    selection_kind: str,
    subject_id: str | None = None,
    workspace_id: str | None = None,
    run_id: str | None = None,
    selected_candidate_id: str | None = None,
    policy_id: str | None = None,
) -> str:
    seed = "\x1f".join(
        [
            selection_kind,
            subject_id or "",
            workspace_id or "",
            run_id or "",
            selected_candidate_id or "",
            policy_id or "",
        ]
    )
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]
    return f"selection:{selection_kind}:{digest}"


def candidate_record(
    *,
    candidate_id: str,
    candidate_type: str,
    label: str | None = None,
    score: float | int | None = None,
    selected: bool = False,
    eligibility_status: str = "eligible",
    rationale: str | None = None,
    reason_codes: Sequence[str] | None = None,
    source: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not candidate_id:
        raise SelectionExplanationError("candidate_id is required")
    if not candidate_type:
        raise SelectionExplanationError("candidate_type is required")
    payload: dict[str, Any] = {
        "candidate_id": candidate_id,
        "candidate_type": candidate_type,
        "label": label,
        "score": None if score is None else round(float(score), 4),
        "selected": bool(selected),
        "eligibility_status": eligibility_status,
        "rationale": rationale,
        "reason_codes": list(reason_codes or []),
        "source": source,
        "metadata": dict(metadata or {}),
    }
    return payload


def excluded_candidate_record(
    *,
    candidate_id: str,
    candidate_type: str,
    reason: str,
    label: str | None = None,
    score: float | int | None = None,
    policy_id: str | None = None,
    retryable: bool = True,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not reason:
        raise SelectionExplanationError("excluded candidate reason is required")
    payload = candidate_record(
        candidate_id=candidate_id,
        candidate_type=candidate_type,
        label=label,
        score=score,
        selected=False,
        eligibility_status="excluded",
        rationale=reason,
        reason_codes=[reason],
        metadata=metadata,
    )
    payload["reason"] = reason
    payload["policy_id"] = policy_id
    payload["retryable"] = bool(retryable)
    return payload


def feedback_candidate_records(
    *,
    facet_scores: Sequence[Mapping[str, Any]],
    lead_scores: Sequence[Mapping[str, Any]],
    next_action: Mapping[str, Any],
) -> list[dict[str, Any]]:
    selected_object_ref = next_action.get("selected_object_ref")
    selected_facet = next_action.get("selected_facet")
    records: list[dict[str, Any]] = []
    for item in facet_scores:
        facet = str(item.get("facet") or "")
        records.append(
            candidate_record(
                candidate_id=str(item.get("candidate_id") or f"facet:{facet}"),
                candidate_type="facet",
                label=facet,
                score=item.get("score") if isinstance(item.get("score"), (int, float)) else None,
                selected=selected_object_ref is None and facet == selected_facet,
                eligibility_status="eligible",
                rationale=str(item.get("rationale") or ""),
                reason_codes=[
                    str(code)
                    for code in item.get("reason_codes", [])
                    if isinstance(code, str) and code
                ],
                source="candidate_feedback_plan.facet_scores",
                metadata={
                    "facet": facet,
                    "rank": item.get("rank"),
                    "prompt_bundle_id": item.get("prompt_bundle_id"),
                },
            )
        )
    for item in lead_scores:
        object_ref = str(item.get("object_ref") or item.get("candidate_id") or "")
        records.append(
            candidate_record(
                candidate_id=str(item.get("candidate_id") or object_ref),
                candidate_type=f"lead:{item.get('lead_kind') or 'unknown'}",
                label=str(item.get("label") or object_ref),
                score=item.get("score") if isinstance(item.get("score"), (int, float)) else None,
                selected=object_ref == selected_object_ref,
                eligibility_status="eligible",
                rationale=str(item.get("rationale") or ""),
                reason_codes=[
                    str(code)
                    for code in item.get("reason_codes", [])
                    if isinstance(code, str) and code
                ],
                source="candidate_feedback_plan.lead_scores",
                metadata={
                    "object_ref": object_ref,
                    "lead_kind": item.get("lead_kind"),
                    "facet": item.get("facet"),
                    "rank": item.get("rank"),
                    "review_state": item.get("review_state"),
                },
            )
        )
    return records


def feedback_excluded_records(
    *,
    deferred: Sequence[Mapping[str, Any]],
    policy_id: str,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for item in deferred:
        reason = str(item.get("reason") or "")
        records.append(
            excluded_candidate_record(
                candidate_id=str(item.get("candidate_id") or ""),
                candidate_type=str(item.get("candidate_kind") or "feedback_candidate"),
                label=str(item.get("candidate_id") or ""),
                score=item.get("score") if isinstance(item.get("score"), (int, float)) else None,
                reason=reason,
                policy_id=policy_id,
                retryable=True,
                metadata={"source": "candidate_feedback_plan.deferred"},
            )
        )
    return records


def selected_feedback_candidate(
    *,
    considered_candidates: Sequence[Mapping[str, Any]],
    next_action: Mapping[str, Any],
) -> dict[str, Any]:
    selected_object_ref = next_action.get("selected_object_ref")
    if selected_object_ref is not None:
        for item in considered_candidates:
            metadata = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
            if metadata.get("object_ref") == selected_object_ref:
                return dict(item)
    selected_facet = next_action.get("selected_facet")
    for item in considered_candidates:
        metadata = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
        if item.get("candidate_type") == "facet" and metadata.get("facet") == selected_facet:
            return dict(item)
    raise SelectionExplanationError("feedback next action did not match a considered candidate")


def build_feedback_selection_explanation(
    *,
    subject_id: str,
    generated_at: str,
    scoring_policy: Mapping[str, Any],
    facet_scores: Sequence[Mapping[str, Any]],
    lead_scores: Sequence[Mapping[str, Any]],
    next_action: Mapping[str, Any],
    deferred: Sequence[Mapping[str, Any]],
    workspace_id: str | None = None,
    run_id: str | None = None,
    cycle_event_id: str | None = None,
) -> dict[str, Any]:
    considered = feedback_candidate_records(
        facet_scores=facet_scores,
        lead_scores=lead_scores,
        next_action=next_action,
    )
    selected = selected_feedback_candidate(
        considered_candidates=considered,
        next_action=next_action,
    )
    excluded = feedback_excluded_records(
        deferred=deferred,
        policy_id=str(scoring_policy.get("policy_id") or ""),
    )
    return build_selection_explanation(
        selection_kind="feedback_next_action",
        subject_id=subject_id,
        workspace_id=workspace_id,
        run_id=run_id,
        cycle_event_id=cycle_event_id,
        stage_name="build_candidate_feedback_plan",
        created_at=generated_at,
        selected_candidate=selected,
        considered_candidates=considered,
        excluded_candidates=excluded,
        policy=scoring_policy,
        budget=scoring_policy.get("limits") if isinstance(scoring_policy.get("limits"), Mapping) else {},
        eligibility_constraints=[
            "enabled_facets",
            "lead_review_states",
            "canonical_subject_scope",
        ],
    )


def build_scheduler_selection_explanation(
    *,
    planner_run_id: str,
    planned_at: str,
    registry_path: str,
    selected_workspaces: Sequence[Mapping[str, Any]],
    skipped_workspaces: Sequence[Mapping[str, Any]],
    limit: int | None,
    include_manual: bool,
    include_saturated: bool,
    ignore_saturation: bool,
    saturation_policy: str | None,
) -> dict[str, Any]:
    considered: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for item in selected_workspaces:
        workspace_id = str(item.get("workspace_id") or "")
        considered.append(
            candidate_record(
                candidate_id=workspace_id,
                candidate_type="workspace",
                label=str(item.get("topic_label") or workspace_id),
                selected=True,
                eligibility_status="selected",
                rationale=str(item.get("cadence_reason") or "workspace selected by scheduler policy"),
                reason_codes=["selected"],
                source="select_scheduled_workspaces.selected_workspaces",
                metadata={
                    "workspace_root": item.get("workspace_root"),
                    "schedule_posture": item.get("schedule_posture"),
                    "lifecycle_state": item.get("lifecycle_state"),
                    "saturation": item.get("saturation"),
                },
            )
        )
    for item in skipped_workspaces:
        workspace_id = str(item.get("workspace_id") or "")
        reasons = [
            str(reason)
            for reason in item.get("reasons", [])
            if isinstance(reason, str) and reason
        ]
        reason = reasons[0] if reasons else "not_selected"
        candidate = candidate_record(
            candidate_id=workspace_id,
            candidate_type="workspace",
            label=str(item.get("topic_label") or workspace_id),
            selected=False,
            eligibility_status="excluded",
            rationale=reason,
            reason_codes=reasons or [reason],
            source="select_scheduled_workspaces.skipped_workspaces",
            metadata={
                "workspace_root": item.get("workspace_root"),
                "schedule_posture": item.get("schedule_posture"),
                "lifecycle_state": item.get("lifecycle_state"),
                "saturation": item.get("saturation"),
            },
        )
        considered.append(candidate)
        excluded.append(
            excluded_candidate_record(
                candidate_id=workspace_id,
                candidate_type="workspace",
                label=str(item.get("topic_label") or workspace_id),
                reason=reason,
                retryable=True,
                metadata={"reasons": reasons},
            )
        )
    if selected_workspaces:
        selected_candidate = next(item for item in considered if item["selected"])
    else:
        selected_candidate = candidate_record(
            candidate_id=f"{planner_run_id}:no-selection",
            candidate_type="workspace_selection",
            label="no workspace selected",
            selected=True,
            eligibility_status="no_selection",
            rationale="No workspace satisfied the scheduler policy.",
            reason_codes=["no_eligible_workspace"],
            source="select_scheduled_workspaces",
        )
        considered.append(selected_candidate)
    return build_selection_explanation(
        selection_kind="scheduled_workspace",
        run_id=planner_run_id,
        created_at=planned_at,
        selected_candidate=selected_candidate,
        considered_candidates=considered,
        excluded_candidates=excluded,
        policy={
            "policy_id": "scheduled-workspace-selector.default.v1",
            "registry_path": registry_path,
            "limit": limit,
            "include_manual": include_manual,
            "include_saturated": include_saturated,
            "ignore_saturation": ignore_saturation,
            "saturation_policy": saturation_policy,
        },
        budget={},
        eligibility_constraints=[
            "workspace lifecycle_state active",
            "workspace schedule_posture eligible",
            "default subject manifest resolved",
        ],
    )


def validate_selection_explanation(payload: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    if payload.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must equal {SCHEMA_VERSION}")
    for key in (
        "explanation_id",
        "selection_kind",
        "created_at",
        "policy",
        "selected_candidate",
        "considered_candidates",
        "excluded_candidates",
        "operator_overrides",
    ):
        if key not in payload:
            errors.append(f"missing required selection_explanation key: {key}")
    selected = payload.get("selected_candidate")
    if not isinstance(selected, Mapping):
        errors.append("selected_candidate must be an object")
        selected_id = None
    else:
        selected_id = selected.get("candidate_id")
        if not isinstance(selected_id, str) or not selected_id:
            errors.append("selected_candidate.candidate_id must be a non-blank string")
        if not isinstance(selected.get("rationale"), str) or not selected.get("rationale"):
            errors.append("selected_candidate.rationale must be a non-blank string")
    considered = payload.get("considered_candidates")
    if not isinstance(considered, list) or not considered:
        errors.append("considered_candidates must be a non-empty array")
        considered_ids: set[str] = set()
    else:
        considered_ids = {
            item.get("candidate_id")
            for item in considered
            if isinstance(item, Mapping) and isinstance(item.get("candidate_id"), str)
        }
        for index, item in enumerate(considered):
            if not isinstance(item, Mapping):
                errors.append(f"considered_candidates[{index}] must be an object")
                continue
            for key in ("candidate_id", "candidate_type", "selected", "eligibility_status"):
                if key not in item:
                    errors.append(f"missing required considered_candidates[{index}] key: {key}")
    overrides = payload.get("operator_overrides")
    has_override = isinstance(overrides, list) and bool(overrides)
    if selected_id and selected_id not in considered_ids and not has_override:
        errors.append(
            "selected candidate must appear in considered_candidates unless operator_overrides is non-empty"
        )
    excluded = payload.get("excluded_candidates")
    if not isinstance(excluded, list):
        errors.append("excluded_candidates must be an array")
    else:
        for index, item in enumerate(excluded):
            if not isinstance(item, Mapping):
                errors.append(f"excluded_candidates[{index}] must be an object")
                continue
            if not isinstance(item.get("reason"), str) or not item.get("reason"):
                errors.append(f"excluded_candidates[{index}].reason must be a non-blank string")
    policy = payload.get("policy")
    if not isinstance(policy, Mapping):
        errors.append("policy must be an object")
    elif not isinstance(policy.get("policy_id"), str) or not policy.get("policy_id"):
        errors.append("policy.policy_id must be a non-blank string")
    return errors


def build_selection_explanation(
    *,
    selection_kind: str,
    created_at: str,
    selected_candidate: Mapping[str, Any],
    considered_candidates: Sequence[Mapping[str, Any]],
    excluded_candidates: Sequence[Mapping[str, Any]],
    subject_id: str | None = None,
    workspace_id: str | None = None,
    run_id: str | None = None,
    cycle_event_id: str | None = None,
    stage_name: str | None = None,
    policy: Mapping[str, Any] | None = None,
    budget: Mapping[str, Any] | None = None,
    eligibility_constraints: Sequence[str] | None = None,
    operator_overrides: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    selected_id = str(selected_candidate.get("candidate_id") or "")
    policy_payload = dict(policy or {})
    policy_id = policy_payload.get("policy_id")
    explanation_id = stable_explanation_id(
        selection_kind=selection_kind,
        subject_id=subject_id,
        workspace_id=workspace_id,
        run_id=run_id,
        selected_candidate_id=selected_id,
        policy_id=str(policy_id) if policy_id is not None else None,
    )
    considered = [dict(item) for item in considered_candidates]
    excluded = [dict(item) for item in excluded_candidates]
    selected_seen = any(item.get("candidate_id") == selected_id for item in considered)
    overrides = [dict(item) for item in operator_overrides or []]
    if not selected_seen and not overrides:
        raise SelectionExplanationError(
            "selected candidate must appear in considered candidates unless an override is recorded"
        )
    for item in excluded:
        if not item.get("reason"):
            raise SelectionExplanationError("every excluded candidate must include a reason")
    return {
        "schema_version": SCHEMA_VERSION,
        "explanation_id": explanation_id,
        "selection_kind": selection_kind,
        "subject_id": subject_id,
        "workspace_id": workspace_id,
        "run_id": run_id,
        "cycle_event_id": cycle_event_id,
        "stage_name": stage_name,
        "created_at": created_at,
        "policy": policy_payload,
        "budget": dict(budget or {}),
        "eligibility_constraints": list(eligibility_constraints or []),
        "operator_overrides": overrides,
        "selected_candidate": dict(selected_candidate),
        "considered_candidates": considered,
        "excluded_candidates": excluded,
    }
