from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from hansard_annotator.llm_eval.campaign_artifact_backup import (
    ArtifactBackupError,
    backup_artifacts,
    restore_backup,
    verify_backup,
)


def fake_remote(tmp_path: Path):
    root = tmp_path / "remote"
    root.mkdir()

    def remote_path(value: str) -> Path:
        return root / value.split(":", 1)[1]

    def run(*args: str) -> str:
        action, *items = args
        if action == "mkdir":
            (root / items[0].split(":", 1)[1]).mkdir(parents=True, exist_ok=True)
            return ""
        if action == "lsf":
            directory = root / items[0].split(":", 1)[1]
            return "\n".join(sorted(path.name for path in directory.iterdir() if path.is_file()))
        if action == "copyto":
            source, destination = items
            source_remote = ":" in source
            destination_remote = ":" in destination
            if source_remote and destination_remote:
                source_path = remote_path(source)
                destination_path = remote_path(destination)
                if not source_path.exists():
                    raise ArtifactBackupError("rclone operation failed: missing remote object")
                destination_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source_path, destination_path)
            elif source_remote:
                source_path = remote_path(source)
                if not source_path.exists():
                    raise ArtifactBackupError("rclone operation failed: missing remote object")
                Path(destination).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source_path, destination)
            elif destination_remote:
                destination_path = remote_path(destination)
                destination_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination_path)
            else:
                Path(destination).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
            return ""
        if action == "moveto":
            source, destination = items
            remote_source = root / source.split(":", 1)[1]
            remote_destination = root / destination.split(":", 1)[1]
            remote_destination.parent.mkdir(parents=True, exist_ok=True)
            remote_source.replace(remote_destination)
            return ""
        raise AssertionError(action)

    return root, run


def test_backup_is_incremental_and_verifiable(tmp_path: Path) -> None:
    source = tmp_path / "artifacts"
    source.mkdir()
    (source / "plan.json").write_text('{"records":1}', encoding="utf-8")
    remote_root, runner = fake_remote(tmp_path)
    checksum = "a" * 64
    first = backup_artifacts(source, remote="offsite:campaigns", database_backup_sha256=checksum,
                             snapshot_id="snapshot-1", runner=runner)
    second = backup_artifacts(source, remote="offsite:campaigns", database_backup_sha256=checksum,
                              snapshot_id="snapshot-2", runner=runner)
    assert first["files"] == second["files"]
    assert (remote_root / "campaigns/objects" / first["files"][0]["sha256"]).is_file()
    assert verify_backup("offsite:campaigns", "snapshot-1", runner=runner)["complete"]
    with pytest.raises(ArtifactBackupError, match="snapshot ID already exists"):
        backup_artifacts(source, remote="offsite:campaigns", database_backup_sha256=checksum,
                         snapshot_id="snapshot-1", runner=runner)


def test_interrupted_upload_never_publishes_a_complete_snapshot(tmp_path: Path) -> None:
    source = tmp_path / "artifacts"
    source.mkdir()
    (source / "plan.json").write_text("{}", encoding="utf-8")
    remote_root, runner = fake_remote(tmp_path)

    def fail_before_object_publish(*args: str) -> str:
        if args[0] == "moveto" and "objects/" in args[2]:
            raise ArtifactBackupError("interrupted")
        return runner(*args)

    with pytest.raises(ArtifactBackupError, match="interrupted"):
        backup_artifacts(source, remote="offsite:campaigns", database_backup_sha256="a" * 64,
                         snapshot_id="incomplete", runner=fail_before_object_publish)
    assert not (remote_root / "campaigns/snapshots/incomplete.manifest.json").exists()
    with pytest.raises(ArtifactBackupError, match="manifest is missing"):
        verify_backup("offsite:campaigns", "incomplete", runner=runner)


def test_missing_external_remote_fails_clearly(tmp_path: Path) -> None:
    with pytest.raises(ArtifactBackupError, match="off-host destination is required"):
        backup_artifacts(tmp_path, remote=None, database_backup_sha256="a" * 64)
    with pytest.raises(ArtifactBackupError, match="local filesystem remotes"):
        backup_artifacts(tmp_path, remote="local:/tmp/backup", database_backup_sha256="a" * 64)


def test_corrupt_and_missing_objects_fail_verification(tmp_path: Path) -> None:
    source = tmp_path / "artifacts"
    source.mkdir()
    (source / "result.jsonl").write_text("{}\n", encoding="utf-8")
    remote_root, runner = fake_remote(tmp_path)
    manifest = backup_artifacts(source, remote="offsite:campaigns", database_backup_sha256="b" * 64,
                                snapshot_id="snap", runner=runner)
    object_path = remote_root / "campaigns" / manifest["files"][0]["object"]
    object_path.write_text("corrupt", encoding="utf-8")
    with pytest.raises(ArtifactBackupError, match="failed verification"):
        verify_backup("offsite:campaigns", "snap", runner=runner)
    object_path.unlink()
    with pytest.raises(ArtifactBackupError, match="rclone operation failed"):
        verify_backup("offsite:campaigns", "snap", runner=runner)


def test_incomplete_manifest_and_mismatched_database_snapshot_rejected(tmp_path: Path) -> None:
    source = tmp_path / "artifacts"
    source.mkdir()
    (source / "response.json").write_text("{}", encoding="utf-8")
    remote_root, runner = fake_remote(tmp_path)
    backup_artifacts(source, remote="offsite:campaigns", database_backup_sha256="c" * 64,
                     snapshot_id="done", runner=runner)
    (remote_root / "campaigns/snapshots/interrupted.manifest.partial").write_text("{}")
    with pytest.raises(ArtifactBackupError, match="manifest is missing"):
        verify_backup("offsite:campaigns", "interrupted", runner=runner)
    with pytest.raises(ArtifactBackupError, match="does not match"):
        restore_backup("offsite:campaigns", "done", tmp_path / "restore",
                       database_backup_sha256="d" * 64, runner=runner)


def test_restore_only_to_empty_isolated_directory(tmp_path: Path) -> None:
    source = tmp_path / "artifacts"
    source.mkdir()
    (source / "chunks/1/response.json").parent.mkdir(parents=True)
    (source / "chunks/1/response.json").write_text('{"ok":true}', encoding="utf-8")
    _, runner = fake_remote(tmp_path)
    backup_artifacts(source, remote="offsite:campaigns", database_backup_sha256="e" * 64,
                     snapshot_id="restore-test", runner=runner)
    destination = tmp_path / "isolated"
    result = restore_backup("offsite:campaigns", "restore-test", destination,
                            database_backup_sha256="e" * 64, runner=runner)
    assert result["restored_files"] == 1
    assert (destination / "chunks/1/response.json").read_text(encoding="utf-8") == '{"ok":true}'
    (destination / "do-not-overwrite").write_text("keep")
    with pytest.raises(ArtifactBackupError, match="must be absent or empty"):
        restore_backup("offsite:campaigns", "restore-test", destination,
                       database_backup_sha256="e" * 64, runner=runner)
