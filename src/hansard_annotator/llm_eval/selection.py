"""Explicit, package-compatible record exclusion for held-out evaluation."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

RECORD_ID_PATTERN = re.compile(
    r"^annotation:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}:revision:[1-9][0-9]*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ExclusionSet:
    record_ids: frozenset[str]
    sha256: str
    source_package_snapshot_sha256: str | None

    @property
    def count(self) -> int:
        return len(self.record_ids)


def exclusion_hash(record_ids: set[str] | frozenset[str]) -> str:
    canonical = "".join(f"{record_id}\n" for record_id in sorted(record_ids))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_exclusion_set(
    path: Path,
    *,
    package_snapshot_sha256: str,
    package_record_ids: set[str],
) -> ExclusionSet:
    if not path.is_file():
        raise FileNotFoundError(f"record exclusion file not found: {path}")
    record_ids: list[str] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        record_id = line.strip()
        if not record_id:
            continue
        if not RECORD_ID_PATTERN.fullmatch(record_id):
            raise ValueError(f"malformed record ID on exclusion line {line_number}")
        record_ids.append(record_id)
    if not record_ids:
        raise ValueError("record exclusion file is empty")
    if len(record_ids) != len(set(record_ids)):
        raise ValueError("record exclusion file contains duplicate record IDs")
    unknown = set(record_ids) - package_record_ids
    if unknown:
        raise ValueError(
            "record exclusion file contains IDs absent from this package: "
            + ", ".join(sorted(unknown))
        )

    manifest_path = path.with_suffix(".manifest.json")
    manifest: dict[str, Any] | None = None
    if manifest_path.exists():
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("record exclusion manifest must be a JSON object")
        manifest = value
        expected_snapshot = manifest.get("source_package_snapshot_sha256")
        if expected_snapshot != package_snapshot_sha256:
            raise ValueError("record exclusion manifest is incompatible with this package snapshot")
        if manifest.get("development_record_count") != len(record_ids):
            raise ValueError("record exclusion manifest count does not match its ID file")
        if manifest.get("record_ids_sha256") != exclusion_hash(set(record_ids)):
            raise ValueError("record exclusion manifest hash does not match its ID file")
    return ExclusionSet(
        frozenset(record_ids),
        exclusion_hash(set(record_ids)),
        str(manifest["source_package_snapshot_sha256"]) if manifest is not None else None,
    )
