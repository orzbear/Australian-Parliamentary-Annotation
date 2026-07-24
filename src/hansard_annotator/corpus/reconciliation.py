"""Per-file and whole-run reconciliation invariants."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from hansard_annotator.corpus.models import FileResult


@dataclass(frozen=True)
class ReconciliationFailure:
    source_file: str
    failure_code: str
    detail: str

    def to_row(self) -> dict[str, str]:
        return vars(self)


def reconcile_file(result: FileResult) -> list[ReconciliationFailure]:
    failures: list[ReconciliationFailure] = []
    source_file = result.source.relative_path

    observed_speeches = result.observed_tag_counts.get("speech", 0)
    if observed_speeches != len(result.fragments):
        failures.append(
            ReconciliationFailure(
                source_file,
                "SPEECH_FRAGMENT_COUNT_MISMATCH",
                f"observed={observed_speeches}; fragments={len(result.fragments)}",
            )
        )

    for tag, produced in (
        ("major-heading", result.major_heading_count),
        ("minor-heading", result.minor_heading_count),
        ("division", result.division_count),
        ("bills", result.bills_element_count),
    ):
        observed = result.observed_tag_counts.get(tag, 0)
        if observed != produced:
            failures.append(
                ReconciliationFailure(
                    source_file,
                    f"{tag.upper().replace('-', '_')}_COUNT_MISMATCH",
                    f"observed={observed}; produced={produced}",
                )
            )

    observed_bills = result.observed_tag_counts.get("bill", 0)
    if observed_bills != result.bill_record_count:
        failures.append(
            ReconciliationFailure(
                source_file,
                "BILL_RECORD_COUNT_MISMATCH",
                f"observed={observed_bills}; produced={result.bill_record_count}",
            )
        )

    fragment_keys = [fragment.fragment_key for fragment in result.fragments]
    if len(fragment_keys) != len(set(fragment_keys)):
        failures.append(
            ReconciliationFailure(
                source_file,
                "DUPLICATE_GENERATED_FRAGMENT_KEY",
                "generated fragment keys are not unique",
            )
        )

    lineage_counts = Counter(item.fragment_key for item in result.turn_fragments)
    interjection_counts = Counter(
        item.source_fragment_key for item in result.interjections
    )
    fragment_by_key = {fragment.fragment_key: fragment for fragment in result.fragments}

    for fragment in result.fragments:
        lineage = lineage_counts[fragment.fragment_key]
        interjections = interjection_counts[fragment.fragment_key]
        if fragment.is_interjection:
            if lineage != 0 or interjections != 1:
                failures.append(
                    ReconciliationFailure(
                        source_file,
                        "INTERJECTION_DISPOSITION_INVALID",
                        (
                            f"sequence={fragment.sequence_number}; "
                            f"lineage={lineage}; interjections={interjections}"
                        ),
                    )
                )
        elif lineage != 1 or interjections != 0:
            failures.append(
                ReconciliationFailure(
                    source_file,
                    "FRAGMENT_DISPOSITION_INVALID",
                    (
                        f"sequence={fragment.sequence_number}; "
                        f"lineage={lineage}; interjections={interjections}"
                    ),
                )
            )

    for key in lineage_counts:
        if key not in fragment_by_key:
            failures.append(
                ReconciliationFailure(
                    source_file,
                    "LINEAGE_REFERENCES_UNKNOWN_FRAGMENT",
                    key,
                )
            )
    turn_keys = {turn.turn_key for turn in result.turns}
    for item in result.turn_fragments:
        if item.turn_key not in turn_keys:
            failures.append(
                ReconciliationFailure(
                    source_file,
                    "LINEAGE_REFERENCES_UNKNOWN_TURN",
                    item.turn_key,
                )
            )

    continuation_count = sum(
        fragment.talktype_raw == "continuation" for fragment in result.fragments
    )
    merged_count = sum(
        fragment.is_continuation_merged for fragment in result.fragments
    )
    orphan_count = sum(
        fragment.is_orphan_continuation for fragment in result.fragments
    )
    if continuation_count != merged_count + orphan_count:
        failures.append(
            ReconciliationFailure(
                source_file,
                "CONTINUATION_DISPOSITION_MISMATCH",
                (
                    f"source={continuation_count}; merged={merged_count}; "
                    f"orphan={orphan_count}"
                ),
            )
        )
    if orphan_count != len(result.continuation_anomalies):
        failures.append(
            ReconciliationFailure(
                source_file,
                "ORPHAN_ANOMALY_COUNT_MISMATCH",
                (
                    f"orphans={orphan_count}; "
                    f"anomalies={len(result.continuation_anomalies)}"
                ),
            )
        )

    turn_fragment_totals = Counter(item.turn_key for item in result.turn_fragments)
    for turn in result.turns:
        if turn_fragment_totals[turn.turn_key] != turn.fragment_count:
            failures.append(
                ReconciliationFailure(
                    source_file,
                    "TURN_FRAGMENT_COUNT_MISMATCH",
                    (
                        f"turn={turn.turn_key}; declared={turn.fragment_count}; "
                        f"lineage={turn_fragment_totals[turn.turn_key]}"
                    ),
                )
            )
    return failures

