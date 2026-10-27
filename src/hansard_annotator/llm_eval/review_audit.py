"""Read-only post-review audit for Phase 4C model predictions."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, TextIO

import psycopg
from psycopg.rows import dict_row

from hansard_annotator.db.config import DatabaseSettings
from hansard_annotator.llm_eval.prediction_tasks import changed_fields, human_suggestion

TERMINAL_STATES = {"accepted", "edited", "skipped", "human_annotated_after_ai_failure"}
DECISION_FIELDS = (
    "is_non_policy",
    "primary_australian_domain",
    "secondary_australian_domains",
)


class ReviewAuditError(ValueError):
    """Review data is incomplete, ambiguous, or internally inconsistent."""


def choose_run(runs: list[dict[str, Any]], *, run_id: str | None, latest: bool) -> dict[str, Any]:
    if bool(run_id) == latest:
        raise ReviewAuditError("select exactly one of --run-id or --latest")
    if not runs:
        raise ReviewAuditError("no model prediction runs exist")
    if latest:
        return max(runs, key=lambda row: (row["imported_at"], int(row["id"])))
    matches = [row for row in runs if str(row["id"]) == run_id or str(row["public_id"]) == run_id]
    if len(matches) != 1:
        raise ReviewAuditError(f"run {run_id!r} did not resolve to exactly one run")
    return matches[0]


def _normalise_domains(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) for item in value):
        raise ReviewAuditError("domain values must be a list of strings")
    return list(value)


def _label(non_policy: bool, primary: object) -> str:
    return "Non-Policy" if non_policy else str(primary)


def _changed_fields(row: dict[str, Any], human: dict[str, Any]) -> list[str]:
    if row.get("prediction_task") == "hostility":
        suggested = human_suggestion("hostility", row["ai_prediction_values"])
        return changed_fields("hostility", suggested, human)
    changed: list[str] = []
    if bool(row["ai_is_non_policy"]) != bool(human.get("is_non_policy")):
        changed.append("is_non_policy")
    if row["ai_primary_domain"] != human.get("primary_australian_domain"):
        changed.append("primary_australian_domain")
    if _normalise_domains(row["ai_secondary_domains"]) != _normalise_domains(
        human.get("secondary_australian_domains")
    ):
        changed.append("secondary_australian_domains")
    return changed


def _edit_category(row: dict[str, Any], human: dict[str, Any], changed: list[str]) -> str:
    if row.get("prediction_task") == "hostility":
        return "HOSTILITY_" + ("_AND_".join(key.upper() for key in changed) or "UNCHANGED")
    ai_non_policy = bool(row["ai_is_non_policy"])
    human_non_policy = bool(human.get("is_non_policy"))
    if not ai_non_policy and human_non_policy:
        return "POLICY_TO_NON_POLICY"
    if ai_non_policy and not human_non_policy:
        return "NON_POLICY_TO_POLICY"
    fields = set(changed)
    domain_fields = {"primary_australian_domain", "secondary_australian_domains"}
    if "is_non_policy" in fields and fields & domain_fields:
        return "POLICY_AND_DOMAIN_CHANGED"
    if fields == domain_fields:
        return "PRIMARY_AND_SECONDARY_CHANGED"
    if fields == {"primary_australian_domain"}:
        return "PRIMARY_DOMAIN_CHANGED"
    if fields == {"secondary_australian_domains"}:
        return "SECONDARY_DOMAINS_CHANGED"
    return "OTHER_MULTIPLE"


def _flag_notes(row: dict[str, Any]) -> list[str]:
    notes = row.get("codebook_flag_notes") or []
    if not isinstance(notes, list):
        raise ReviewAuditError("codebook flag notes must be a list")
    output: list[str] = []
    for item in notes:
        note = item.get("note") if isinstance(item, dict) else None
        if note is not None:
            output.append(str(note))
    return output


def analyse_review_rows(
    run: dict[str, Any], rows: list[dict[str, Any]]
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    prediction_ids = [str(row["prediction_id"]) for row in rows]
    if len(prediction_ids) != len(set(prediction_ids)):
        raise ReviewAuditError("selected run produced duplicate prediction rows")
    if any(int(row["run_id"]) != int(run["id"]) for row in rows):
        raise ReviewAuditError("selected rows do not all belong to the selected run")

    outcome_counts: Counter[str] = Counter()
    overlap_counts: Counter[str] = Counter()
    edit_categories: Counter[str] = Counter()
    transitions: Counter[str] = Counter()
    flags_by_ai: Counter[str] = Counter()
    flags_by_human: Counter[str] = Counter()
    flag_transitions: Counter[str] = Counter()
    unique_notes: Counter[str] = Counter()
    edited_cases: list[dict[str, Any]] = []
    flagged_cases: list[dict[str, Any]] = []
    policy_gate_changes = primary_changes = secondary_only_changes = multiple_changes = 0
    flagged_policy_gate_changes = 0

    for row in rows:
        state = row.get("review_status")
        if state is not None and state not in TERMINAL_STATES:
            raise ReviewAuditError(f"unexpected terminal review state: {state}")
        flagged = bool(row.get("codebook_flag_count", 0))
        notes = _flag_notes(row)
        unique_notes.update(notes)
        if state is None:
            outcome_counts["unreviewed"] += 1
            if row.get("annotation_version_id") is not None:
                raise ReviewAuditError("unreviewed prediction unexpectedly links an annotation")
        else:
            outcome_counts[str(state)] += 1
            outcome_counts["reviewed"] += 1
            overlap_counts[f"{state}_{'flagged' if flagged else 'not_flagged'}"] += 1
        human_raw = row.get("human_values")
        if state in {"accepted", "edited", "human_annotated_after_ai_failure"}:
            if not isinstance(human_raw, dict) or row.get("annotation_version_id") is None:
                raise ReviewAuditError(
                    f"{state} prediction {row['prediction_id']} has no resulting human annotation"
                )
            if int(row.get("human_speaker_turn_id") or -1) != int(row["speaker_turn_id"]):
                raise ReviewAuditError(
                    f"prediction {row['prediction_id']} links an annotation for another turn"
                )
        human = human_raw if isinstance(human_raw, dict) else {}
        changed = (
            _changed_fields(row, human)
            if state in {"accepted", "edited"} and row.get("ai_valid_prediction") is not False
            else []
        )
        if state == "edited" and not changed:
            raise ReviewAuditError(
                f"edited prediction {row['prediction_id']} does not differ from its human annotation"
            )
        if state == "accepted" and changed:
            raise ReviewAuditError(
                f"accepted prediction {row['prediction_id']} differs from its human annotation"
            )

        if row.get("ai_valid_prediction") is False:
            ai_label = "INVALID_OUTPUT"
            human_label = (
                f"{human.get('hostility_present')}:{human.get('target') or 'N/A'}"
                if human else ""
            )
        elif row.get("prediction_task") == "hostility":
            ai_values = row["ai_prediction_values"]
            ai_label = f"{ai_values.get('hostility_present')}:{ai_values.get('target')}"
            human_label = (
                f"{human.get('hostility_present')}:{human.get('target') or 'N/A'}"
                if human
                else ""
            )
        else:
            ai_label = _label(bool(row["ai_is_non_policy"]), row["ai_primary_domain"])
            human_label = (
            _label(bool(human.get("is_non_policy")), human.get("primary_australian_domain"))
            if human
            else ""
            )
        base = {
            "prediction_id": str(row["prediction_id"]),
            "run_id": str(row["run_public_id"]),
            "record_id": str(row["record_id"]),
            "turn_key": str(row["turn_key"]),
            "speaker": row.get("speaker"),
            "sitting_date": row.get("sitting_date"),
            "chamber": row.get("chamber"),
            "major_heading": row.get("major_heading"),
            "minor_heading": row.get("minor_heading"),
            "business_type": row.get("business_type"),
            "speech_text": row.get("speech_text"),
            "ai_is_non_policy": bool(row["ai_is_non_policy"])
            if row.get("ai_valid_prediction") is not False else None,
            "ai_primary_domain": row["ai_primary_domain"],
            "ai_secondary_domains": _normalise_domains(row["ai_secondary_domains"]),
            "ai_reasoning": row.get("ai_reasoning"),
            "prediction_task": row.get("prediction_task"),
            "ai_prediction_values": row.get("ai_prediction_values"),
            "ai_outcome_status": row.get("ai_outcome_status", "PREDICTION"),
            "ai_failure_reason": row.get("ai_failure_reason"),
            "ai_valid_prediction": row.get("ai_valid_prediction", True),
            "human_values": human or None,
            "human_is_non_policy": human.get("is_non_policy") if human else None,
            "human_primary_domain": human.get("primary_australian_domain") if human else None,
            "human_secondary_domains": _normalise_domains(human.get("secondary_australian_domains"))
            if human
            else [],
            "review_status": state,
            "review_note": row.get("review_note"),
            "codebook_flagged": flagged,
            "codebook_flag_note": notes[-1] if notes else None,
            "all_codebook_flag_notes": notes,
            "ai_label": ai_label,
            "human_label": human_label,
        }
        if state == "edited":
            category = _edit_category(row, human, changed)
            edit_categories[category] += 1
            transitions[f"{ai_label} → {human_label}"] += 1
            if "is_non_policy" in changed:
                policy_gate_changes += 1
            if "primary_australian_domain" in changed:
                primary_changes += 1
            if changed == ["secondary_australian_domains"]:
                secondary_only_changes += 1
            if len(changed) > 1:
                multiple_changes += 1
            edited_cases.append({**base, "changed_fields": changed, "edit_category": category})
        if flagged:
            if "is_non_policy" in changed:
                flagged_policy_gate_changes += 1
            flags_by_ai[ai_label] += 1
            flags_by_human[human_label or "No terminal annotation"] += 1
            flag_transitions[f"{ai_label} → {human_label or 'No terminal annotation'}"] += 1
            flagged_cases.append({**base, "ai_annotation_changed": bool(changed)})

    total = len(rows)
    reviewed = outcome_counts["reviewed"]
    unreviewed = outcome_counts["unreviewed"]
    if reviewed + unreviewed != total:
        raise ReviewAuditError("reviewed + unreviewed does not equal total predictions")
    terminal_total = sum(outcome_counts[state] for state in TERMINAL_STATES)
    if terminal_total != reviewed:
        raise ReviewAuditError("accepted + edited + skipped does not equal reviewed")
    expected = int(run["valid_count"])
    expected_total = expected + int(run.get("invalid_count") or 0)
    if total != expected_total:
        raise ReviewAuditError(
            f"selected run contains {total} logical records but run metadata declares {expected_total} accounted records"
        )

    summary = {
        "run": {
            key: run.get(key)
            for key in (
                "id",
                "public_id",
                "provider",
                "configured_model",
                "returned_model",
                "prompt_version",
                "prompt_sha256",
                "canonical_schema_sha256",
                "prediction_contract_sha256",
                "provider_schema_sha256",
                "generation_config",
                "execution_mode",
                "provider_job_id",
                "artifact_sha256",
                "record_set_sha256",
                "imported_at",
                "valid_count",
            )
        },
        "counts": {
            "total_predictions": total,
            "reviewed_predictions": reviewed,
            "unreviewed_predictions": unreviewed,
            "accepted": outcome_counts["accepted"],
            "edited": outcome_counts["edited"],
            "skipped": outcome_counts["skipped"],
            "codebook_flagged": len(flagged_cases),
            "accepted_and_flagged": overlap_counts["accepted_flagged"],
            "edited_and_flagged": overlap_counts["edited_flagged"],
            "skipped_and_flagged": overlap_counts["skipped_flagged"],
            "accepted_not_flagged": overlap_counts["accepted_not_flagged"],
            "edited_not_flagged": overlap_counts["edited_not_flagged"],
        },
        "edit_analysis": {
            "edit_category_counts": dict(sorted(edit_categories.items())),
            "policy_gate_changes": policy_gate_changes,
            "primary_domain_changes": primary_changes,
            "secondary_only_changes": secondary_only_changes,
            "multiple_changes": multiple_changes,
            "ai_to_human_transitions": dict(sorted(transitions.items())),
        },
        "flag_analysis": {
            "by_ai_primary_label": dict(sorted(flags_by_ai.items())),
            "by_final_human_primary_label": dict(sorted(flags_by_human.items())),
            "ai_to_human_primary_transitions": dict(sorted(flag_transitions.items())),
            "policy_gate_changes": flagged_policy_gate_changes,
            "unique_flag_notes": dict(sorted(unique_notes.items())),
        },
        "integrity": {
            "reviewed_plus_unreviewed_equals_total": True,
            "terminal_states_equal_reviewed": True,
            "all_edited_resolve_human_annotation": True,
            "single_selected_run": True,
        },
    }
    if total > expected:
        counts = summary.get("counts")
        if not isinstance(counts, dict):
            raise ReviewAuditError("review counts were not generated")
        counts.update(
            {
                "valid_ai_predictions": expected,
                "terminal_ai_failures": total - expected,
                "coverage": expected / total if total else 0.0,
            }
        )
    return summary, edited_cases, flagged_cases


def full_review_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten every prediction while preserving prediction/review/human distinctions."""
    output: list[dict[str, Any]] = []
    for row in rows:
        human_raw = row.get("human_values")
        human = human_raw if isinstance(human_raw, dict) else {}
        notes = _flag_notes(row)
        output.append(
            {
                "run_id": str(row["run_public_id"]),
                "prediction_id": str(row["prediction_id"]),
                "speaker_turn_id": int(row["speaker_turn_id"]),
                "record_id": str(row["record_id"]),
                "turn_key": str(row["turn_key"]),
                "speaker": row.get("speaker"),
                "sitting_date": row.get("sitting_date"),
                "chamber": row.get("chamber"),
                "major_heading": row.get("major_heading"),
                "minor_heading": row.get("minor_heading"),
                "business_type": row.get("business_type"),
                "speech_text": row.get("speech_text"),
                "prediction_task": row.get("prediction_task"),
                "ai_prediction_values": row.get("ai_prediction_values"),
                "ai_outcome_status": row.get("ai_outcome_status", "PREDICTION"),
                "ai_failure_reason": row.get("ai_failure_reason"),
                "ai_failure_diagnostic": (
                    row["outcome_provenance"].get("final_validation_error")
                    if isinstance(row.get("outcome_provenance"), dict) else None
                ),
                "ai_valid_prediction": row.get("ai_valid_prediction", True),
                "recovery_attempt_count": (
                    len(row.get("outcome_provenance", {}).get("recovery_attempt_history", []))
                    if isinstance(row.get("outcome_provenance"), dict) else None
                ),
                "ai_hostility_present": (
                    row["ai_prediction_values"].get("hostility_present")
                    if isinstance(row.get("ai_prediction_values"), dict) else None
                ),
                "ai_target": (
                    row["ai_prediction_values"].get("target")
                    if isinstance(row.get("ai_prediction_values"), dict) else None
                ),
                "ai_evidence": (
                    row["ai_prediction_values"].get("evidence_spans")
                    if isinstance(row.get("ai_prediction_values"), dict) else None
                ),
                "reviewed_hostility": human.get("hostility_present"),
                "reviewed_target": human.get("target"),
                "reviewed_evidence": human.get("evidence_span"),
                "ai_is_non_policy": bool(row["ai_is_non_policy"])
                if row.get("ai_valid_prediction") is not False else None,
                "ai_primary_domain": row.get("ai_primary_domain"),
                "ai_secondary_domains": _normalise_domains(row.get("ai_secondary_domains")),
                "ai_reasoning": row.get("ai_reasoning"),
                "terminal_review_action": row.get("review_status"),
                "review_note": row.get("review_note"),
                "reviewer_user_id": row.get("terminal_reviewer_user_id"),
                "reviewer": row.get("terminal_reviewer"),
                "reviewed_at": row.get("terminal_reviewed_at"),
                "annotation_version_id": row.get("annotation_version_id"),
                "human_is_non_policy": human.get("is_non_policy") if human else None,
                "human_primary_domain": human.get("primary_australian_domain")
                if human
                else None,
                "human_secondary_domains": _normalise_domains(
                    human.get("secondary_australian_domains")
                )
                if human
                else [],
                "human_values": human or None,
                "codebook_flagged": bool(row.get("codebook_flag_count", 0)),
                "codebook_flag_note": notes[-1] if notes else None,
                "all_codebook_flag_notes": notes,
            }
        )
    return output


def _json_default(value: object) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _csv_value(value: object) -> object:
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _write_csv(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        for row in sorted(rows, key=lambda value: str(value["record_id"])):
            writer.writerow({column: _csv_value(row.get(column)) for column in columns})


def _markdown_table(values: dict[str, Any], total: int) -> str:
    lines = ["| Measure | Count | Percent |", "|---|---:|---:|"]
    for key, value in values.items():
        count = int(value)
        percent = count / total * 100 if total else 0.0
        lines.append(f"| {key.replace('_', ' ').title()} | {count} | {percent:.1f}% |")
    return "\n".join(lines)


def render_markdown(summary: dict[str, Any]) -> str:
    run = summary["run"]
    counts = summary["counts"]
    edit = summary["edit_analysis"]
    flags = summary["flag_analysis"]
    outcome = {
        key: counts[key]
        for key in (
            "total_predictions",
            "reviewed_predictions",
            "unreviewed_predictions",
            "accepted",
            "edited",
            "skipped",
            "codebook_flagged",
        )
    }
    overlap = {
        key: counts[key]
        for key in (
            "accepted_and_flagged",
            "edited_and_flagged",
            "skipped_and_flagged",
            "accepted_not_flagged",
            "edited_not_flagged",
        )
    }

    def bullets(values: dict[str, Any]) -> str:
        return "\n".join(f"- {key}: {value}" for key, value in values.items()) or "- None"

    def largest(values: dict[str, Any]) -> tuple[str, int] | None:
        ranked = sorted(
            ((str(key), int(value)) for key, value in values.items()),
            key=lambda item: (-item[1], item[0]),
        )
        return ranked[0] if ranked else None

    largest_edit = largest(edit["edit_category_counts"])
    largest_transition = largest(edit["ai_to_human_transitions"])
    largest_flag_label = largest(flags["by_ai_primary_label"])
    patterns = [
        (
            f"{largest_edit[1]} of {counts['edited']} edited cases were "
            f"{largest_edit[0]}."
            if largest_edit
            else "No edited cases were recorded."
        ),
        (
            f"{largest_transition[1]} of {counts['edited']} edited cases followed the "
            f"{largest_transition[0]} transition."
            if largest_transition
            else "No edited AI-to-human transitions were recorded."
        ),
        (
            f"{largest_flag_label[1]} of {counts['codebook_flagged']} flagged cases had AI "
            f"primary label {largest_flag_label[0]}."
            if largest_flag_label
            else "No codebook-flagged cases were recorded."
        ),
        (
            f"{flags['policy_gate_changes']} of {counts['codebook_flagged']} flagged cases "
            "also changed the policy gate."
        ),
    ]
    pattern_bullets = "\n".join(f"- {pattern}" for pattern in patterns)

    return f"""# Phase 4C Review Audit

## Run provenance

- Provider/model: {run["provider"]} / {run["configured_model"]}
- Returned model: {run["returned_model"]}
- Prompt: {run["prompt_version"]}
- Prompt hash: `{run["prompt_sha256"]}`
- Canonical schema hash: `{run["canonical_schema_sha256"]}`
- Thinking configuration: `{json.dumps(run["generation_config"], sort_keys=True)}`
- Run ID: `{run["public_id"]}`
- Artifact hash: `{run["artifact_sha256"]}`
- Record-set hash: `{run["record_set_sha256"]}`
- Execution mode/job: {run["execution_mode"]} / {run["provider_job_id"]}
- Record count: {counts["total_predictions"]}

## Review outcome

{_markdown_table(outcome, counts["total_predictions"])}

Accepted rate describes human confirmation in this assisted-review workflow; it is not automatically equivalent to independent benchmark accuracy.

## Overlap of review actions and codebook flags

{_markdown_table(overlap, counts["total_predictions"])}

Codebook flags are non-exclusive research metadata and may overlap accepted or edited decisions.

## Edited cases

- Edited cases: {counts["edited"]}
- Policy-gate changes: {edit["policy_gate_changes"]}
- Primary-domain changes: {edit["primary_domain_changes"]}
- Secondary-only changes: {edit["secondary_only_changes"]}
- Multiple-field changes: {edit["multiple_changes"]}

### Edit-type distribution

{bullets(edit["edit_category_counts"])}

### AI → human transitions

{bullets(edit["ai_to_human_transitions"])}

## Codebook flags

- Flagged predictions: {counts["codebook_flagged"]}
- Accepted and flagged: {counts["accepted_and_flagged"]}
- Edited and flagged: {counts["edited_and_flagged"]}
- Flagged cases with a policy-gate change: {flags["policy_gate_changes"]}

### Flags by AI primary label

{bullets(flags["by_ai_primary_label"])}

### Flags by final human primary label

{bullets(flags["by_final_human_primary_label"])}

### Flagged AI → human transitions

{bullets(flags["ai_to_human_primary_transitions"])}

### Flag notes exactly as stored

{bullets(flags["unique_flag_notes"])}

## Potential systematic patterns

{pattern_bullets}

These mechanically selected concentrations are deterministic descriptions of the stored review data. They do not establish that a codebook rule is incorrect and do not propose a prompt change.

## Decision inputs

- Compare the edit-category distribution to determine whether edits are heterogeneous or concentrated.
- Inspect AI → human transition counts for recurring domain boundaries.
- Use policy-gate and secondary-only counts to identify which kind of disagreement dominates.
- Review flag-note counts alongside domain concentrations; flags are not automatically model errors.

This is post-review diagnostic analysis. It does not modify V2, create V3, change human annotations, or select a production decision.
"""


def _load_runs(connection: psycopg.Connection[Any]) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in connection.execute(
            """
            SELECT id,public_id,prediction_task,annotation_schema_slug,
                   annotation_schema_version,provider,configured_model,returned_model,prompt_version,
                   prompt_sha256,canonical_schema_sha256,prediction_contract_sha256,
                   provider_schema_sha256,generation_config,execution_mode,
                   provider_job_id,artifact_sha256,record_set_sha256,imported_at,valid_count,invalid_count
            FROM model_prediction_runs ORDER BY imported_at,id
            """
        ).fetchall()
    ]


def _load_rows(connection: psycopg.Connection[Any], run_id: int) -> list[dict[str, Any]]:
    predictions = [
        dict(row)
        for row in connection.execute(
            """
            SELECT mp.id,mp.public_id AS prediction_id,mp.run_id,run.public_id AS run_public_id,
                   mp.record_id,mp.speaker_turn_id,mp.prediction_task,
                   mp.prediction_values AS ai_prediction_values,
                   'PREDICTION'::text AS ai_outcome_status,NULL::text AS ai_failure_reason,
                   true AS ai_valid_prediction,
                   mp.is_non_policy AS ai_is_non_policy,
                   mp.primary_australian_domain AS ai_primary_domain,
                   mp.secondary_australian_domains AS ai_secondary_domains,
                   mp.reasoning AS ai_reasoning,
                   st.turn_key,st.speaker_name_raw AS speaker,st.speech_date AS sitting_date,
                   st.chamber,st.major_heading_original AS major_heading,
                   st.minor_heading_original AS minor_heading,st.business_type,
                   st.text_clean AS speech_text,
                   terminal.review_status,terminal.note AS review_note,
                   terminal.reviewer_user_id AS terminal_reviewer_user_id,
                   reviewer.display_name AS terminal_reviewer,
                   terminal.reviewed_at AS terminal_reviewed_at,
                   terminal.annotation_version_id,av.values AS human_values,
                   human_task.speaker_turn_id AS human_speaker_turn_id,
                   COALESCE(flags.flag_count,0) AS codebook_flag_count,
                   COALESCE(flags.notes,'[]'::jsonb) AS codebook_flag_notes
            FROM model_predictions mp
            JOIN model_prediction_runs run ON run.id=mp.run_id
            JOIN speaker_turns st ON st.id=mp.speaker_turn_id
            LEFT JOIN LATERAL (
              SELECT review_status,note,annotation_version_id,reviewer_user_id,reviewed_at,id
              FROM model_prediction_reviews review
              WHERE review.model_prediction_id=mp.id
                AND review.review_status IN ('accepted','edited','skipped')
              ORDER BY reviewed_at DESC,id DESC LIMIT 1
            ) terminal ON true
            LEFT JOIN users reviewer ON reviewer.id=terminal.reviewer_user_id
            LEFT JOIN annotation_versions av ON av.id=terminal.annotation_version_id
            LEFT JOIN annotations human_annotation ON human_annotation.id=av.annotation_id
            LEFT JOIN assignments human_assignment
              ON human_assignment.id=human_annotation.assignment_id
            LEFT JOIN tasks human_task ON human_task.id=human_assignment.task_id
            LEFT JOIN LATERAL (
              SELECT count(*) AS flag_count,
                     jsonb_agg(jsonb_build_object(
                       'note',note,'reviewed_at',reviewed_at,'reviewer_user_id',reviewer_user_id
                     ) ORDER BY reviewed_at,id) AS notes
              FROM model_prediction_reviews flag
              WHERE flag.model_prediction_id=mp.id
                AND flag.review_status='flagged_codebook'
            ) flags ON true
            WHERE mp.run_id=%s
            ORDER BY mp.record_id,mp.id
            """,
            (run_id,),
        ).fetchall()
    ]
    failures = [
        dict(row)
        for row in connection.execute(
            """
            SELECT outcome.id,outcome.public_id AS prediction_id,outcome.run_id,
                   run.public_id AS run_public_id,outcome.record_id,outcome.speaker_turn_id,
                   'hostility'::text AS prediction_task,NULL::jsonb AS ai_prediction_values,
                   'INVALID_OUTPUT'::text AS ai_outcome_status,
                   outcome.resolution_reason AS ai_failure_reason,
                   outcome.outcome_provenance,false AS ai_valid_prediction,
                   NULL::boolean AS ai_is_non_policy,NULL::text AS ai_primary_domain,
                   NULL::text[] AS ai_secondary_domains,NULL::text AS ai_reasoning,
                   st.turn_key,st.speaker_name_raw AS speaker,st.speech_date AS sitting_date,
                   st.chamber,st.major_heading_original AS major_heading,
                   st.minor_heading_original AS minor_heading,st.business_type,
                   st.text_clean AS speech_text,terminal.review_status,terminal.note AS review_note,
                   terminal.reviewer_user_id AS terminal_reviewer_user_id,
                   reviewer.display_name AS terminal_reviewer,
                   terminal.reviewed_at AS terminal_reviewed_at,
                   terminal.annotation_version_id,av.values AS human_values,
                   human_task.speaker_turn_id AS human_speaker_turn_id,
                   0::bigint AS codebook_flag_count,'[]'::jsonb AS codebook_flag_notes
            FROM model_prediction_abstentions outcome
            JOIN model_prediction_runs run ON run.id=outcome.run_id
            JOIN speaker_turns st ON st.id=outcome.speaker_turn_id
            LEFT JOIN LATERAL (
              SELECT review_status,note,annotation_version_id,reviewer_user_id,reviewed_at,id
              FROM model_prediction_outcome_reviews review
              WHERE review.abstention_id=outcome.id
              ORDER BY reviewed_at DESC,id DESC LIMIT 1
            ) terminal ON true
            LEFT JOIN users reviewer ON reviewer.id=terminal.reviewer_user_id
            LEFT JOIN annotation_versions av ON av.id=terminal.annotation_version_id
            LEFT JOIN annotations human_annotation ON human_annotation.id=av.annotation_id
            LEFT JOIN assignments human_assignment ON human_assignment.id=human_annotation.assignment_id
            LEFT JOIN tasks human_task ON human_task.id=human_assignment.task_id
            WHERE outcome.run_id=%s AND outcome.resolution_type='INVALID_OUTPUT'
            ORDER BY outcome.record_id,outcome.id
            """,
            (run_id,),
        ).fetchall()
    ]
    return predictions + failures


def create_review_audit(
    settings: DatabaseSettings,
    output_dir: Path,
    *,
    run_id: str | None,
    latest: bool,
) -> dict[str, Any]:
    with (
        psycopg.connect(settings.psycopg_url, row_factory=dict_row) as connection,
        connection.transaction(),
    ):
        connection.execute("SET TRANSACTION READ ONLY")
        run = choose_run(_load_runs(connection), run_id=run_id, latest=latest)
        rows = _load_rows(connection, int(run["id"]))
        summary, edited, flagged = analyse_review_rows(run, rows)
        all_results = full_review_rows(rows)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "review_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True, default=_json_default)
        + "\n",
        encoding="utf-8",
    )
    (output_dir / "review_audit.md").write_text(render_markdown(summary), encoding="utf-8")
    base_columns = [
        "prediction_id",
        "run_id",
        "record_id",
        "turn_key",
        "speaker",
        "sitting_date",
        "chamber",
        "major_heading",
        "minor_heading",
        "business_type",
        "speech_text",
        "prediction_task",
        "ai_prediction_values",
        "ai_outcome_status",
        "ai_failure_reason",
        "ai_valid_prediction",
        "recovery_attempt_count",
        "ai_hostility_present",
        "ai_target",
        "ai_evidence",
        "reviewed_hostility",
        "reviewed_target",
        "reviewed_evidence",
        "ai_is_non_policy",
        "ai_primary_domain",
        "ai_secondary_domains",
        "ai_reasoning",
        "human_is_non_policy",
        "human_primary_domain",
        "human_secondary_domains",
        "human_values",
        "review_status",
        "review_note",
        "codebook_flagged",
        "codebook_flag_note",
        "all_codebook_flag_notes",
    ]
    _write_csv(
        output_dir / "edited_cases.csv",
        edited,
        [*base_columns, "changed_fields", "edit_category"],
    )
    _write_csv(
        output_dir / "codebook_flagged_cases.csv",
        flagged,
        [*base_columns, "ai_label", "human_label", "ai_annotation_changed"],
    )
    _write_csv(
        output_dir / "full_review_results.csv",
        all_results,
        [
            "run_id",
            "prediction_id",
            "speaker_turn_id",
            "record_id",
            "turn_key",
            "speaker",
            "sitting_date",
            "chamber",
            "major_heading",
            "minor_heading",
            "business_type",
            "speech_text",
            "prediction_task",
            "ai_prediction_values",
            "ai_outcome_status",
            "ai_failure_reason",
            "ai_failure_diagnostic",
            "ai_valid_prediction",
            "recovery_attempt_count",
            "ai_hostility_present",
            "ai_target",
            "ai_evidence",
            "reviewed_hostility",
            "reviewed_target",
            "reviewed_evidence",
            "ai_is_non_policy",
            "ai_primary_domain",
            "ai_secondary_domains",
            "ai_reasoning",
            "terminal_review_action",
            "review_note",
            "reviewer_user_id",
            "reviewer",
            "reviewed_at",
            "annotation_version_id",
            "human_is_non_policy",
            "human_primary_domain",
            "human_secondary_domains",
            "human_values",
            "codebook_flagged",
            "codebook_flag_note",
            "all_codebook_flag_notes",
        ],
    )
    return summary


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only Phase 4C review audit")
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--run-id")
    selection.add_argument("--latest", action="store_true")
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None, *, stdout: TextIO = sys.stdout) -> int:
    arguments = _parser().parse_args(argv)
    try:
        summary = create_review_audit(
            DatabaseSettings.from_environment(),
            arguments.output_dir,
            run_id=arguments.run_id,
            latest=arguments.latest,
        )
    except Exception as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print(json.dumps(summary["counts"], sort_keys=True), file=stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
