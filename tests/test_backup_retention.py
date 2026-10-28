from __future__ import annotations

import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from deploy.scripts.prune_backups import prune_backups

ROOT = Path(__file__).parents[1]
NOW = datetime(2026, 10, 28, 12, tzinfo=UTC)


def _dump(directory: Path, timestamp: datetime, suffix: str = "") -> Path:
    name = timestamp.strftime("hansard-%Y%m%dT%H%M%SZ.dump")
    path = directory / f"{name}{suffix}"
    path.write_text("dump", encoding="utf-8")
    return path


def test_recent_backups_are_all_retained(tmp_path: Path) -> None:
    paths = [_dump(tmp_path, NOW - timedelta(hours=hours)) for hours in (1, 24, 72)]

    assert prune_backups(tmp_path, now=NOW) == (3, 0)
    assert all(path.exists() for path in paths)


def test_older_daily_window_keeps_newest_backup_per_utc_day(tmp_path: Path) -> None:
    older = _dump(tmp_path, NOW - timedelta(days=6, hours=4))
    newest = _dump(tmp_path, NOW - timedelta(days=6, hours=1))
    other_day = _dump(tmp_path, NOW - timedelta(days=8))

    assert prune_backups(tmp_path, now=NOW) == (2, 1)
    assert not older.exists()
    assert newest.exists()
    assert other_day.exists()


def test_weekly_window_keeps_newest_backup_per_iso_week(tmp_path: Path) -> None:
    older = _dump(tmp_path, NOW - timedelta(days=20))
    newest = _dump(tmp_path, NOW - timedelta(days=18))
    other_week = _dump(tmp_path, NOW - timedelta(days=26))

    assert prune_backups(tmp_path, now=NOW) == (2, 1)
    assert not older.exists()
    assert newest.exists()
    assert other_week.exists()


def test_backups_older_than_56_days_and_matching_checksum_are_deleted(
    tmp_path: Path,
) -> None:
    old = _dump(tmp_path, NOW - timedelta(days=57))
    checksum = tmp_path / f"{old.name}.sha256"
    checksum.write_text("digest", encoding="utf-8")
    unrelated_checksum = tmp_path / "notes.sha256"
    unrelated_checksum.write_text("keep", encoding="utf-8")
    unrelated_file = tmp_path / "hansard-production-seed-20260814.dump"
    unrelated_file.write_text("keep", encoding="utf-8")

    assert prune_backups(tmp_path, now=NOW) == (0, 1)
    assert not old.exists()
    assert not checksum.exists()
    assert unrelated_checksum.exists()
    assert unrelated_file.exists()


def test_dry_run_does_not_delete_dump_or_checksum(tmp_path: Path) -> None:
    old = _dump(tmp_path, NOW - timedelta(days=60))
    checksum = tmp_path / f"{old.name}.sha256"
    checksum.write_text("digest", encoding="utf-8")

    assert prune_backups(tmp_path, now=NOW, dry_run=True) == (0, 0)
    assert old.exists()
    assert checksum.exists()


def test_non_regular_and_malformed_timestamp_candidates_are_untouched(
    tmp_path: Path,
) -> None:
    malformed = tmp_path / "hansard-20260230T120000Z.dump"
    malformed.write_text("keep", encoding="utf-8")
    directory_named_dump = tmp_path / "hansard-20200101T000000Z.dump"
    directory_named_dump.mkdir()

    assert prune_backups(tmp_path, now=NOW) == (0, 0)
    assert malformed.exists()
    assert directory_named_dump.is_dir()


def test_backup_script_only_invokes_pruning_after_successful_dump_and_checksum(
    tmp_path: Path,
) -> None:
    shell = shutil.which("sh")
    sha256sum = shutil.which("sha256sum")
    if shell is None or sha256sum is None or os.name == "nt":
        pytest.skip("POSIX shell and sha256sum are required for backup workflow test")

    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    seed = backup_dir / "hansard-production-seed-20260814.dump"
    seed.write_text("preserve", encoding="utf-8")
    env_file = tmp_path / "backup.env"
    env_file.write_text(
        f"POSTGRES_USER=test_user\nPOSTGRES_DB=test_db\nHANSARD_BACKUP_DIR={backup_dir}\n",
        encoding="utf-8",
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    prune_log = tmp_path / "prune.log"
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        'case "$*" in *pg_dump*) ;; *) exit 2 ;; esac\n'
        'if [ "${FAIL_DUMP:-0}" = 1 ]; then exit 42; fi\n'
        "printf 'dump contents\\n'\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    python_wrapper = bin_dir / "python3"
    python_wrapper.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" > "{prune_log}"\n'
        f'test -s "{backup_dir}"/hansard-*.dump\n'
        f'test -s "{backup_dir}"/hansard-*.dump.sha256\n'
        f"exec {sys.executable} \"$@\"\n",
        encoding="utf-8",
    )
    python_wrapper.chmod(0o755)
    environment = os.environ | {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HANSARD_ENV_FILE": str(env_file),
    }

    failed = subprocess.run(
        [shell, str(ROOT / "deploy/scripts/backup.sh")],
        env=environment | {"FAIL_DUMP": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert failed.returncode != 0
    assert not prune_log.exists()
    assert seed.read_text(encoding="utf-8") == "preserve"

    succeeded = subprocess.run(
        [shell, str(ROOT / "deploy/scripts/backup.sh")],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert succeeded.returncode == 0, succeeded.stderr
    assert prune_log.exists()
    assert len(list(backup_dir.glob("hansard-????????T??????Z.dump"))) == 1
    assert len(list(backup_dir.glob("hansard-????????T??????Z.dump.sha256"))) == 1
    assert seed.exists()
