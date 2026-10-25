"""Build a compact, deterministic policy frame from the canonical AI CSV export."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.parquet as pq
from psycopg.rows import dict_row

from hansard_annotator.db.config import DatabaseSettings

SCRIPT_VERSION = "policy-sampling-frame-v1"
EXPECTED_SOURCE_COLUMNS = {
    "corpus_order",
    "turn_key",
    "outcome_status",
    "is_non_policy",
    "primary_australian_domain",
    "campaign_id",
    "provider",
    "configured_model",
    "prompt_version",
    "prediction_run_id",
}
SOURCE_COLUMNS = [
    "corpus_order",
    "turn_key",
    "outcome_status",
    "is_non_policy",
    "primary_australian_domain",
    "campaign_id",
    "provider",
    "configured_model",
    "prompt_version",
    "prediction_run_id",
]
DOMAINS = tuple(f"AU{number:02d}" for number in range(1, 15))
OUTCOMES = {"PREDICTION", "PROVIDER_REFUSAL", "NO_USABLE_TEXT"}
FRAME_SCHEMA = pa.schema(
    [
        pa.field("corpus_order", pa.int64(), nullable=False),
        pa.field("turn_key", pa.string(), nullable=False),
        pa.field("policy_status", pa.string(), nullable=False),
        pa.field("primary_domain", pa.string()),
        pa.field("prediction_outcome", pa.string(), nullable=False),
        pa.field("source_campaign_id", pa.string()),
        pa.field("source_provider", pa.string()),
        pa.field("source_model", pa.string()),
        pa.field("source_prompt_version", pa.string()),
        pa.field("source_prediction_run_id", pa.string()),
    ]
)


@dataclass
class FrameStatistics:
    source_rows: int = 0
    output_rows: int = 0
    missing_turn_keys: int = 0
    duplicate_turn_keys: int = 0
    usable_classified_turns: int = 0
    policy_turns: int = 0
    non_policy_turns: int = 0
    unavailable_turns: int = 0
    policy_without_valid_domain: int = 0
    non_policy_with_domain: int = 0


def source_columns(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        columns = next(csv.reader(source), None)
    if not columns:
        raise ValueError("canonical CSV has no header")
    missing = sorted(EXPECTED_SOURCE_COLUMNS - set(columns))
    if missing:
        raise ValueError(f"canonical CSV is missing required columns: {missing}")
    return columns


def discover_source(processed_root: Path) -> Path:
    candidates: list[Path] = []
    for path in processed_root.rglob("*.csv"):
        try:
            columns = source_columns(path)
        except (OSError, UnicodeError, ValueError):
            continue
        if set(columns) >= EXPECTED_SOURCE_COLUMNS:
            candidates.append(path)
    if len(candidates) != 1:
        names = [path.as_posix() for path in candidates]
        raise ValueError(f"expected exactly one canonical policy CSV, found {names}")
    return candidates[0]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_policy(
    outcome: str, is_non_policy: str | None, domain: str | None
) -> tuple[str, str | None]:
    outcome = (outcome or "").strip()
    domain = (domain or "").strip() or None
    raw_non_policy = (is_non_policy or "").strip().lower()
    if outcome not in OUTCOMES:
        raise ValueError(f"unexpected prediction outcome: {outcome!r}")
    if outcome != "PREDICTION":
        if domain is not None:
            raise ValueError(f"{outcome} row unexpectedly carries domain {domain}")
        return "UNAVAILABLE", None
    if raw_non_policy == "true":
        if domain is not None:
            raise ValueError(f"non-policy row unexpectedly carries domain {domain}")
        return "NON_POLICY", None
    if raw_non_policy == "false":
        if domain not in DOMAINS:
            raise ValueError(f"policy row lacks valid AU01-AU14 domain: {domain!r}")
        return "POLICY", domain
    raise ValueError(f"prediction row has invalid is_non_policy value: {is_non_policy!r}")


def _optional(value: object) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def build_frame(source: Path, output: Path) -> tuple[FrameStatistics, Counter[str], Counter[str]]:
    source_columns(source)
    reader = pacsv.open_csv(  # type: ignore[attr-defined]
        source,
        read_options=pacsv.ReadOptions(  # type: ignore[attr-defined]
            block_size=16 * 1024 * 1024
        ),
        parse_options=pacsv.ParseOptions(  # type: ignore[attr-defined]
            newlines_in_values=True
        ),
        convert_options=pacsv.ConvertOptions(  # type: ignore[attr-defined]
            include_columns=SOURCE_COLUMNS,
            column_types={name: pa.string() for name in SOURCE_COLUMNS},
            strings_can_be_null=True,
        ),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    statistics = FrameStatistics()
    outcome_counts: Counter[str] = Counter()
    domain_counts: Counter[str] = Counter()
    seen: set[str] = set()
    partial_output = output.with_suffix(output.suffix + ".part")
    writer = pq.ParquetWriter(  # type: ignore[no-untyped-call]
        partial_output,
        FRAME_SCHEMA,
        compression="zstd",
        use_dictionary=True,
        write_statistics=True,
    )
    try:
        for batch in reader:
            rows: dict[str, list[object]] = {field.name: [] for field in FRAME_SCHEMA}
            columns = batch.to_pydict()
            for index in range(batch.num_rows):
                statistics.source_rows += 1
                turn_key = _optional(columns["turn_key"][index])
                if turn_key is None:
                    statistics.missing_turn_keys += 1
                    continue
                if turn_key in seen:
                    statistics.duplicate_turn_keys += 1
                seen.add(turn_key)
                outcome = str(columns["outcome_status"][index] or "").strip()
                outcome_counts[outcome] += 1
                status, domain = normalize_policy(
                    outcome,
                    _optional(columns["is_non_policy"][index]),
                    _optional(columns["primary_australian_domain"][index]),
                )
                if status == "POLICY":
                    statistics.policy_turns += 1
                    statistics.usable_classified_turns += 1
                    assert domain is not None
                    domain_counts[domain] += 1
                elif status == "NON_POLICY":
                    statistics.non_policy_turns += 1
                    statistics.usable_classified_turns += 1
                else:
                    statistics.unavailable_turns += 1
                rows["corpus_order"].append(int(str(columns["corpus_order"][index])))
                rows["turn_key"].append(turn_key)
                rows["policy_status"].append(status)
                rows["primary_domain"].append(domain)
                rows["prediction_outcome"].append(outcome)
                rows["source_campaign_id"].append(_optional(columns["campaign_id"][index]))
                rows["source_provider"].append(_optional(columns["provider"][index]))
                rows["source_model"].append(_optional(columns["configured_model"][index]))
                rows["source_prompt_version"].append(_optional(columns["prompt_version"][index]))
                rows["source_prediction_run_id"].append(
                    _optional(columns["prediction_run_id"][index])
                )
                statistics.output_rows += 1
            if rows["turn_key"]:
                writer.write_table(  # type: ignore[no-untyped-call]
                    pa.Table.from_pydict(rows, schema=FRAME_SCHEMA)
                )
    except Exception:
        writer.close()  # type: ignore[no-untyped-call]
        partial_output.unlink(missing_ok=True)
        raise
    else:
        writer.close()  # type: ignore[no-untyped-call]
    if statistics.missing_turn_keys or statistics.duplicate_turn_keys:
        partial_output.unlink(missing_ok=True)
        raise ValueError(
            "turn-key invariant failed: "
            f"missing={statistics.missing_turn_keys}, "
            f"duplicates={statistics.duplicate_turn_keys}"
        )
    partial_output.replace(output)
    return statistics, outcome_counts, domain_counts


def population_rows(domain_counts: Counter[str]) -> list[dict[str, object]]:
    total = sum(domain_counts[domain] for domain in DOMAINS)
    rows = [
        {
            "primary_domain": domain,
            "eligible_policy_speeches": domain_counts[domain],
            "share_of_policy_population": (domain_counts[domain] / total if total else 0.0),
        }
        for domain in DOMAINS
    ]
    rows.append(
        {
            "primary_domain": "TOTAL",
            "eligible_policy_speeches": total,
            "share_of_policy_population": 1.0 if total else 0.0,
        }
    )
    return rows


def write_population(path: Path, rows: Iterable[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as destination:
        writer = csv.DictWriter(
            destination,
            fieldnames=[
                "primary_domain",
                "eligible_policy_speeches",
                "share_of_policy_population",
            ],
            lineterminator="\n",
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    **row,
                    "share_of_policy_population": (
                        f"{float(str(row['share_of_policy_population'])):.8f}"
                    ),
                }
            )


def require_local_database(database_name: str) -> None:
    if database_name != "hansard_dev":
        raise RuntimeError(f"refusing database access: expected hansard_dev, got {database_name}")


def join_counts(frame_keys: set[str], database_keys: set[str]) -> dict[str, int]:
    matched = len(frame_keys & database_keys)
    return {
        "compact_frame_rows": len(frame_keys),
        "matched_speaker_turns": matched,
        "unmatched_frame_rows": len(frame_keys - database_keys),
        "speaker_turns_without_frame": len(database_keys - frame_keys),
        "duplicate_joins": 0,
    }


def validate_database(frame_path: Path) -> dict[str, int]:
    settings = DatabaseSettings.from_environment()
    frame_keys = set(
        pq.read_table(  # type: ignore[no-untyped-call]
            frame_path, columns=["turn_key"]
        )["turn_key"].to_pylist()
    )
    with psycopg.connect(settings.psycopg_url, row_factory=dict_row) as connection:
        identity = connection.execute(
            "SELECT current_database() AS name,host(inet_server_addr()) AS host,"
            "inet_server_port() AS port"
        ).fetchone()
        assert identity is not None
        require_local_database(str(identity["name"]))
        print(
            f"Database validation target: name={identity['name']} "
            f"host={identity['host']} port={identity['port']} rows={len(frame_keys)}"
        )
        database_keys = {
            str(row["turn_key"]) for row in connection.execute("SELECT turn_key FROM speaker_turns")
        }
    return join_counts(frame_keys, database_keys)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--processed-root", type=Path, default=Path("data/processed"))
    parser.add_argument("--source", type=Path)
    parser.add_argument("--validate-db", action="store_true")
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    root = arguments.processed_root.resolve()
    source = arguments.source.resolve() if arguments.source else discover_source(root)
    frame_path = root / "policy_sampling_frame.parquet"
    population_path = root / "policy_domain_population.csv"
    manifest_path = root / "policy_sampling_frame.manifest.json"
    columns = source_columns(source)
    source_sha256 = sha256_file(source)
    statistics, outcome_counts, domain_counts = build_frame(source, frame_path)
    populations = population_rows(domain_counts)
    write_population(population_path, populations)
    join_results = validate_database(frame_path) if arguments.validate_db else None
    manifest: dict[str, Any] = {
        "format": SCRIPT_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "source": {
            "filename": source.name,
            "bytes": source.stat().st_size,
            "sha256": source_sha256,
            "columns": columns,
            "row_count": statistics.output_rows,
        },
        "output": {
            "filename": frame_path.name,
            "bytes": frame_path.stat().st_size,
            "sha256": sha256_file(frame_path),
            "row_count": statistics.source_rows,
            "columns": FRAME_SCHEMA.names,
        },
        "statistics": asdict(statistics),
        "prediction_outcomes": dict(sorted(outcome_counts.items())),
        "domain_population": populations,
        "database_join": join_results,
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    print("\nPolicy-domain population")
    for row in populations:
        print(
            f"{row['primary_domain']:>5}  {row['eligible_policy_speeches']:>8}  "
            f"{float(str(row['share_of_policy_population'])):.4%}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
