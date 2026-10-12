"""Safe, deterministic CSV export of annotation-ready speaker turns."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from hansard_annotator.db.config import DatabaseSettings

HintFilter = Literal["true", "false", "unknown"]

EXPORT_COLUMNS = (
    "turn_key",
    "speech_date",
    "speaker_id_raw",
    "speaker_name_raw",
    "major_heading_original",
    "minor_heading_original",
    "business_type",
    "calculated_word_count",
    "interrupted",
    "interruption_count",
    "is_orphan_continuation",
    "procedural_hint",
    "ceremonial_hint",
    "source_file",
    "source_url",
    "run_name",
    "pipeline_version",
    "corpus_slug",
    "text_clean",
)


@dataclass(frozen=True)
class ReviewExportOptions:
    output: Path
    limit: int = 30
    min_words: int = 50
    max_words: int | None = None
    seed: int | None = None
    excel_compatible: bool = False
    include_orphans: bool = False
    year: int | None = None
    date_from: date | None = None
    date_to: date | None = None
    question_time_hint: HintFilter | None = None
    procedural_hint: HintFilter | None = None
    ceremonial_hint: HintFilter | None = None
    min_interruptions: int = 0
    keywords: tuple[str, ...] = ()
    keyword_mode: Literal["any", "all"] = "any"
    annotation_project_slug: str | None = None
    primary_domain: str | None = None
    annotation_status: Literal["policy", "non-policy"] | None = None


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def validate_output_path(
    output: Path,
    repository_root: Path | None = None,
    processed_data_root: Path | None = None,
) -> Path:
    """Resolve an export target and reject corpus, processed, and backup trees."""
    root = (repository_root or Path.cwd()).resolve()
    target = output.resolve()
    if target.suffix.lower() != ".csv":
        raise ValueError("review export output must have a .csv extension")
    forbidden = (
        root / "hansard_xml_files",
        root / "data" / "processed",
        root / "backups",
        *((processed_data_root.resolve(),) if processed_data_root is not None else ()),
    )
    for directory in forbidden:
        if _is_within(target, directory.resolve()):
            raise ValueError(f"review export path is inside forbidden directory: {directory}")
    if target.exists():
        raise FileExistsError(f"review export already exists: {target}")
    return target


def _validate_options(options: ReviewExportOptions) -> None:
    if options.limit < 1 or options.limit > 100_000:
        raise ValueError("limit must be between 1 and 100000")
    if options.min_words < 0:
        raise ValueError("min_words must be non-negative")
    if options.max_words is not None and options.max_words < options.min_words:
        raise ValueError("max_words must be greater than or equal to min_words")
    if options.min_interruptions < 0:
        raise ValueError("min_interruptions must be non-negative")
    if options.date_from and options.date_to and options.date_from > options.date_to:
        raise ValueError("date_from must be on or before date_to")
    if options.year is not None and not 1901 <= options.year <= 9999:
        raise ValueError("year must be between 1901 and 9999")
    if options.keyword_mode not in {"any", "all"}:
        raise ValueError("keyword_mode must be any or all")
    if any(not keyword.strip() for keyword in options.keywords):
        raise ValueError("keywords must not be empty")
    if (
        options.primary_domain or options.annotation_status
    ) and not options.annotation_project_slug:
        raise ValueError("human-label filters require annotation_project_slug")
    if options.primary_domain and not (
        options.primary_domain == "AU_OTHER_REVIEW"
        or (
            len(options.primary_domain) == 4
            and options.primary_domain.startswith("AU")
            and options.primary_domain[2:].isdigit()
        )
    ):
        raise ValueError("primary_domain must be an AU policy-domain code")


def _hint_condition(column: str, value: HintFilter) -> sql.Composed:
    identifier = sql.Identifier(column)
    if value == "unknown":
        return sql.SQL("{} IS NULL").format(identifier)
    return sql.SQL("{} = {}").format(identifier, sql.Literal(value == "true"))


def _format_nullable_bool(value: object) -> str:
    if value is None:
        return "unknown"
    return "true" if bool(value) else "false"


def write_review_csv(
    target: Path,
    rows: list[dict[str, Any]],
    *,
    excel_compatible: bool,
) -> None:
    """Write already selected rows with strict column alignment and Unicode encoding."""
    encoding = "utf-8-sig" if excel_compatible else "utf-8"
    created = False
    try:
        with target.open("x", encoding=encoding, newline="") as destination:
            created = True
            writer = csv.DictWriter(
                destination,
                fieldnames=EXPORT_COLUMNS,
                extrasaction="raise",
                lineterminator="\r\n",
            )
            writer.writeheader()
            for database_row in rows:
                row = dict(database_row)
                row["interrupted"] = _format_nullable_bool(row["interrupted"])
                row["is_orphan_continuation"] = _format_nullable_bool(row["is_orphan_continuation"])
                row["procedural_hint"] = _format_nullable_bool(row["procedural_hint"])
                row["ceremonial_hint"] = _format_nullable_bool(row["ceremonial_hint"])
                writer.writerow(row)
    except Exception:
        if created and target.exists():
            target.unlink()
        raise


def export_review_sample(
    settings: DatabaseSettings,
    options: ReviewExportOptions,
    *,
    repository_root: Path | None = None,
) -> dict[str, object]:
    """Export current annotation-ready turns without mutating database state."""
    _validate_options(options)
    target = validate_output_path(
        options.output,
        repository_root,
        processed_data_root=settings.processed_data_root,
    )
    conditions: list[sql.Composable] = [
        sql.SQL("calculated_word_count >= %s"),
        sql.SQL("interruption_count >= %s"),
    ]
    parameters: list[Any] = [options.min_words, options.min_interruptions]
    if not options.include_orphans:
        conditions.append(sql.SQL("is_orphan_continuation IS NOT TRUE"))
    if options.max_words is not None:
        conditions.append(sql.SQL("calculated_word_count <= %s"))
        parameters.append(options.max_words)
    if options.year is not None:
        conditions.append(sql.SQL("EXTRACT(YEAR FROM speech_date) = %s"))
        parameters.append(options.year)
    if options.date_from is not None:
        conditions.append(sql.SQL("speech_date >= %s"))
        parameters.append(options.date_from)
    if options.date_to is not None:
        conditions.append(sql.SQL("speech_date <= %s"))
        parameters.append(options.date_to)
    for column, value in (
        ("is_question_time", options.question_time_hint),
        ("is_procedural", options.procedural_hint),
        ("is_ceremonial", options.ceremonial_hint),
    ):
        if value is not None:
            conditions.append(_hint_condition(column, value))
    if options.keywords:
        keyword_conditions: list[sql.Composable] = []
        for keyword in options.keywords:
            keyword_conditions.append(sql.SQL("text_clean ILIKE %s"))
            parameters.append(f"%{keyword.strip()}%")
        joiner = sql.SQL(" OR ") if options.keyword_mode == "any" else sql.SQL(" AND ")
        conditions.append(sql.SQL("({})").format(joiner.join(keyword_conditions)))
    if options.annotation_project_slug:
        annotation_conditions: list[sql.Composable] = [
            sql.SQL("project.slug=%s"),
            sql.SQL("turn.turn_key=current_annotation_ready_turns.turn_key"),
            sql.SQL("annotation.status IN ('submitted','revised')"),
        ]
        parameters.append(options.annotation_project_slug)
        if options.primary_domain:
            annotation_conditions.append(sql.SQL("version.values->>'primary_australian_domain'=%s"))
            parameters.append(options.primary_domain)
        if options.annotation_status:
            annotation_conditions.append(
                sql.SQL("COALESCE((version.values->>'is_non_policy')::boolean,false)=%s")
            )
            parameters.append(options.annotation_status == "non-policy")
        conditions.append(
            sql.SQL(
                """EXISTS (
                  SELECT 1 FROM projects project
                  JOIN tasks task ON task.project_id=project.id
                  JOIN speaker_turns turn ON turn.id=task.speaker_turn_id
                  JOIN assignments assignment ON assignment.task_id=task.id
                  JOIN annotations annotation ON annotation.assignment_id=assignment.id
                  JOIN annotation_versions version ON version.id=annotation.current_version_id
                  WHERE {}
                )"""
            ).format(sql.SQL(" AND ").join(annotation_conditions))
        )

    selected = sql.SQL(
        """
        SELECT turn_key, speech_date, speaker_id_raw, speaker_name_raw,
               major_heading_original, minor_heading_original, business_type,
               calculated_word_count, (interruption_count > 0) AS interrupted,
               interruption_count, is_orphan_continuation,
               is_procedural AS procedural_hint,
               is_ceremonial AS ceremonial_hint,
               source_file, source_url, run_name, pipeline_version, corpus_slug,
               text_clean
        FROM current_annotation_ready_turns
        WHERE {}
        """
    ).format(sql.SQL(" AND ").join(conditions))
    if options.seed is None:
        ordering = sql.SQL(" ORDER BY turn_key")
    else:
        ordering = sql.SQL(" ORDER BY md5(turn_key || %s), turn_key")
        parameters.append(str(options.seed))
    query = selected + ordering + sql.SQL(" LIMIT %s")
    parameters.append(options.limit)

    with psycopg.connect(settings.psycopg_url, row_factory=dict_row) as connection:
        rows = list(connection.execute(query, parameters))

    target.parent.mkdir(parents=True, exist_ok=True)
    write_review_csv(target, rows, excel_compatible=options.excel_compatible)
    return {
        "ok": True,
        "output": str(target),
        "rows": len(rows),
        "columns": len(EXPORT_COLUMNS),
        "excel_compatible": options.excel_compatible,
        "seed": options.seed,
        "database_writes": 0,
    }
