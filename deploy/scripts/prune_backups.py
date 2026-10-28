#!/usr/bin/env python3
"""Apply deterministic retention to timestamped Hansard PostgreSQL backups."""

from __future__ import annotations

import argparse
import re
import stat
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

_DUMP_NAME = re.compile(r"hansard-(\d{8}T\d{6}Z)\.dump")
_THREE_DAYS = timedelta(days=3)
_FOURTEEN_DAYS = timedelta(days=14)
_FIFTY_SIX_DAYS = timedelta(days=56)


def _backup_timestamp(name: str) -> datetime | None:
    match = _DUMP_NAME.fullmatch(name)
    if match is None:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def prune_backups(directory: Path, *, now: datetime, dry_run: bool = False) -> tuple[int, int]:
    """Prune eligible dump files and return (retained, deleted) counts."""
    if directory.is_symlink():
        raise ValueError("backup directory must not be a symlink")
    backup_dir = directory.resolve(strict=True)
    if not backup_dir.is_dir():
        raise NotADirectoryError(backup_dir)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    now_utc = now.astimezone(UTC)

    candidates: list[tuple[Path, datetime]] = []
    for entry in sorted(backup_dir.iterdir(), key=lambda item: item.name):
        timestamp = _backup_timestamp(entry.name)
        if timestamp is None:
            continue
        try:
            mode = entry.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISREG(mode):
            candidates.append((entry, timestamp))

    retain: set[Path] = set()
    daily: dict[tuple[int, int, int], tuple[Path, datetime]] = {}
    weekly: dict[tuple[int, int], tuple[Path, datetime]] = {}
    for path, timestamp in candidates:
        age = now_utc - timestamp
        if age <= _THREE_DAYS:
            retain.add(path)
        elif age <= _FOURTEEN_DAYS:
            daily_key = (timestamp.year, timestamp.month, timestamp.day)
            if daily_key not in daily or timestamp > daily[daily_key][1]:
                daily[daily_key] = (path, timestamp)
        elif age <= _FIFTY_SIX_DAYS:
            iso = timestamp.isocalendar()
            weekly_key = (iso.year, iso.week)
            if weekly_key not in weekly or timestamp > weekly[weekly_key][1]:
                weekly[weekly_key] = (path, timestamp)
        else:
            continue

    retain.update(path for path, _ in daily.values())
    retain.update(path for path, _ in weekly.values())

    deleted = 0
    for path, _ in candidates:
        if path in retain:
            print(f"Retaining {path.name}")
            continue
        print(f"{'Would delete' if dry_run else 'Deleting'} {path.name}")
        if dry_run:
            continue
        path.unlink()
        checksum = path.with_name(f"{path.name}.sha256")
        try:
            checksum_mode = checksum.lstat().st_mode
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISREG(checksum_mode) or stat.S_ISLNK(checksum_mode):
                checksum.unlink()
        deleted += 1

    print(f"Backup retention summary: retained={len(retain)}, deleted={deleted}")
    return len(retain), deleted


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backup_directory", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        prune_backups(args.backup_directory, now=datetime.now(UTC), dry_run=args.dry_run)
    except (OSError, ValueError) as exc:
        print(f"Backup retention failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
