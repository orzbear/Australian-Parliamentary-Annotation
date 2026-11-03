"""Checksummed off-host backup, verification, and isolated restore for campaign artifacts."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, cast

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MANIFEST_VERSION = "campaign-artifact-backup-v1"
SECRET_NAME_MARKERS = (
    ".env", "secret", "credential", "api_key", "api-key", "apikey",
    "service-account", "client_secret", "client-secret",
)


class ArtifactBackupError(RuntimeError):
    """Backup or restore is incomplete or cannot be safely verified."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_relative(path: Path, root: Path) -> str:
    relative = path.relative_to(root).as_posix()
    if not relative or relative.startswith("../") or "\\" in relative:
        raise ArtifactBackupError("artifact path escapes the supplied root")
    if any(
        part.lower().endswith((".key", ".pem"))
        or any(marker in part.lower() for marker in SECRET_NAME_MARKERS)
        for part in PurePosixPath(relative).parts
    ):
        raise ArtifactBackupError(f"refusing to back up secret-like artifact path: {relative}")
    return relative


def _run_rclone(*args: str) -> str:
    try:
        result = subprocess.run(
            ["rclone", *args], check=True, capture_output=True, text=True, timeout=3600
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ArtifactBackupError(f"rclone operation failed: {exc}") from exc
    return result.stdout


def _remote_join(remote: str, *parts: str) -> str:
    if not remote.strip() or ":" not in remote or remote.startswith(":"):
        raise ArtifactBackupError("configure HANSARD_ARTIFACT_BACKUP_REMOTE as an off-host rclone destination")
    remote_name = remote.split(":", 1)[0].lower()
    if remote_name == "local":
        raise ArtifactBackupError("local filesystem remotes are not off-host backups")
    return remote.rstrip("/") + "/" + "/".join(parts)


def _files(source: Path) -> list[tuple[Path, str, int, str]]:
    root = source.resolve(strict=True)
    if not root.is_dir():
        raise ArtifactBackupError("campaign artifact source must be a directory")
    result: list[tuple[Path, str, int, str]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ArtifactBackupError(f"symbolic links are not allowed in artifact volume: {path}")
        if not path.is_file():
            continue
        relative = _safe_relative(path, root)
        size_before = path.stat().st_size
        digest_before = _sha256(path)
        size_after = path.stat().st_size
        digest_after = _sha256(path)
        if size_before != size_after or digest_before != digest_after:
            raise ArtifactBackupError(f"artifact changed while snapshotting: {relative}")
        result.append((path, relative, size_after, digest_after))
    if not result:
        raise ArtifactBackupError("campaign artifact root contains no regular files; refusing empty backup")
    return result


def backup_artifacts(
    source: Path,
    *,
    remote: str | None,
    database_backup_sha256: str,
    snapshot_id: str | None = None,
    runner: Any = _run_rclone,
) -> dict[str, Any]:
    destination = (remote or os.getenv("HANSARD_ARTIFACT_BACKUP_REMOTE") or "").strip()
    if not destination:
        raise ArtifactBackupError("off-host destination is required; set HANSARD_ARTIFACT_BACKUP_REMOTE")
    _remote_join(destination, "objects")
    if not SHA256_RE.fullmatch(database_backup_sha256):
        raise ArtifactBackupError("a verified PostgreSQL backup SHA-256 is required")
    snapshot = snapshot_id or uuid.uuid4().hex
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", snapshot):
        raise ArtifactBackupError("snapshot ID contains unsafe characters")
    lock_path = source.resolve().parent / ".campaign-artifact-backup.lock"
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ArtifactBackupError("another artifact backup is running (or stale lock needs operator review)") from exc
    try:
        os.write(lock_fd, f"{os.getpid()}\n".encode())
        os.close(lock_fd)
        file_list = _files(source)
        manifest_files: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory(prefix="hansard-artifact-backup-") as temporary:
            temp_root = Path(temporary)
            objects_remote = _remote_join(destination, "objects")
            snapshots_remote = _remote_join(destination, "snapshots")
            runner("mkdir", objects_remote)
            runner("mkdir", snapshots_remote)
            existing_objects = set(runner("lsf", objects_remote).splitlines())
            existing_snapshots = set(runner("lsf", snapshots_remote).splitlines())
            if (
                f"{snapshot}.manifest.json" in existing_snapshots
                or f"{snapshot}.manifest.partial" in existing_snapshots
            ):
                raise ArtifactBackupError(
                    "snapshot ID already exists remotely; choose a new ID to preserve prior backups"
                )
            for path, relative, size, digest in file_list:
                object_remote = _remote_join(destination, "objects", digest)
                if digest not in existing_objects:
                    # Publish only complete, verified objects; hashes make objects immutable and reusable.
                    temporary_remote = _remote_join(destination, "temporary", f"{snapshot}-{uuid.uuid4().hex}")
                    runner("copyto", str(path), temporary_remote)
                    staged_copy = temp_root / f"staged-{digest}"
                    runner("copyto", temporary_remote, str(staged_copy))
                    if staged_copy.stat().st_size != size or _sha256(staged_copy) != digest:
                        raise ArtifactBackupError(f"uploaded object failed verification: {relative}")
                    runner("moveto", temporary_remote, object_remote)
                manifest_files.append(
                    {"path": relative, "size": size, "sha256": digest, "object": f"objects/{digest}"}
                )
            for path, relative, size, digest in file_list:
                if path.stat().st_size != size or _sha256(path) != digest:
                    raise ArtifactBackupError(f"artifact changed during upload: {relative}")
            manifest: dict[str, Any] = {
                "format": MANIFEST_VERSION,
                "snapshot_id": snapshot,
                "database_backup_sha256": database_backup_sha256,
                "source": "HANSARD_CAMPAIGN_ARTIFACT_ROOT",
                "files": manifest_files,
            }
            content = json.dumps(manifest, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
            manifest["manifest_sha256"] = hashlib.sha256(content).hexdigest()
            local_manifest = temp_root / "manifest.json"
            local_manifest.write_text(json.dumps(manifest, sort_keys=True, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            partial_remote = _remote_join(destination, "snapshots", f"{snapshot}.manifest.partial")
            final_remote = _remote_join(destination, "snapshots", f"{snapshot}.manifest.json")
            runner("copyto", str(local_manifest), partial_remote)
            runner("moveto", partial_remote, final_remote)
        return manifest
    finally:
        with contextlib.suppress(OSError):
            os.close(lock_fd)
        lock_path.unlink(missing_ok=True)


def _load_remote_manifest(remote: str, snapshot: str, workdir: Path, runner: Any) -> dict[str, Any]:
    manifest_path = workdir / "manifest.json"
    try:
        runner("copyto", _remote_join(remote, "snapshots", f"{snapshot}.manifest.json"), str(manifest_path))
    except ArtifactBackupError as exc:
        raise ArtifactBackupError("backup manifest is missing or unavailable; snapshot is incomplete") from exc
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArtifactBackupError("backup manifest is missing or invalid") from exc
    supplied_hash = manifest.pop("manifest_sha256", None)
    expected = hashlib.sha256(json.dumps(manifest, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    if supplied_hash != expected or manifest.get("format") != MANIFEST_VERSION:
        raise ArtifactBackupError("backup manifest checksum or version is invalid")
    if not isinstance(manifest, dict):
        raise ArtifactBackupError("backup manifest root must be a JSON object")
    return cast(dict[str, Any], manifest)


def verify_backup(remote: str, snapshot: str, *, runner: Any = _run_rclone) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="hansard-artifact-verify-") as temporary:
        root = Path(temporary)
        manifest = _load_remote_manifest(remote, snapshot, root, runner)
        checked = 0
        for entry in manifest["files"]:
            target = root / f"object-{checked}"
            runner("copyto", _remote_join(remote, str(entry["object"])), str(target))
            if target.stat().st_size != int(entry["size"]) or _sha256(target) != entry["sha256"]:
                raise ArtifactBackupError(f"remote artifact failed verification: {entry['path']}")
            checked += 1
        return {"snapshot_id": snapshot, "verified_files": checked,
                "database_backup_sha256": manifest["database_backup_sha256"], "complete": True}


def restore_backup(
    remote: str,
    snapshot: str,
    destination: Path,
    *,
    database_backup_sha256: str,
    runner: Any = _run_rclone,
) -> dict[str, Any]:
    if not SHA256_RE.fullmatch(database_backup_sha256):
        raise ArtifactBackupError("target PostgreSQL snapshot SHA-256 is required")
    target = destination.resolve()
    if target.exists() and any(target.iterdir()):
        raise ArtifactBackupError("restore target must be absent or empty; live artifact trees are never overwritten")
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{target.name}-restore-", dir=target.parent))
    try:
        with tempfile.TemporaryDirectory(prefix="hansard-artifact-restore-manifest-") as temporary:
            manifest = _load_remote_manifest(remote, snapshot, Path(temporary), runner)
        if manifest.get("database_backup_sha256") != database_backup_sha256:
            raise ArtifactBackupError("artifact backup does not match the supplied PostgreSQL snapshot checksum")
        seen_paths: set[str] = set()
        for entry in manifest["files"]:
            relative = PurePosixPath(str(entry["path"]))
            if relative.is_absolute() or ".." in relative.parts:
                raise ArtifactBackupError("manifest contains a path outside restore root")
            if str(relative) in seen_paths:
                raise ArtifactBackupError("manifest contains duplicate artifact paths")
            seen_paths.add(str(relative))
            output = stage.joinpath(*relative.parts)
            output.parent.mkdir(parents=True, exist_ok=True)
            runner("copyto", _remote_join(remote, str(entry["object"])), str(output))
            if output.stat().st_size != int(entry["size"]) or _sha256(output) != entry["sha256"]:
                raise ArtifactBackupError(f"restored artifact failed verification: {entry['path']}")
        if target.exists() and any(target.iterdir()):
            raise ArtifactBackupError("restore target became non-empty during restore")
        if target.exists():
            target.rmdir()
        os.replace(stage, target)
        return {"snapshot_id": snapshot, "restored_files": len(manifest["files"]),
                "database_backup_sha256": database_backup_sha256, "destination": str(target)}
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    backup = sub.add_parser("backup")
    backup.add_argument("--source", type=Path, default=Path(os.getenv("HANSARD_CAMPAIGN_ARTIFACT_ROOT", "/var/lib/hansard-campaign-artifacts")))
    backup.add_argument("--remote", default=os.getenv("HANSARD_ARTIFACT_BACKUP_REMOTE"))
    backup.add_argument("--database-backup-sha256", required=True)
    backup.add_argument("--snapshot-id")
    verify = sub.add_parser("verify")
    verify.add_argument("--remote", default=os.getenv("HANSARD_ARTIFACT_BACKUP_REMOTE"))
    verify.add_argument("--snapshot-id", required=True)
    restore = sub.add_parser("restore")
    restore.add_argument("--remote", default=os.getenv("HANSARD_ARTIFACT_BACKUP_REMOTE"))
    restore.add_argument("--snapshot-id", required=True)
    restore.add_argument("--database-backup-sha256", required=True)
    restore.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    if not args.remote:
        parser.error("off-host destination missing; set HANSARD_ARTIFACT_BACKUP_REMOTE")
    try:
        if args.action == "backup":
            result = backup_artifacts(args.source, remote=args.remote,
                                      database_backup_sha256=args.database_backup_sha256,
                                      snapshot_id=args.snapshot_id)
        elif args.action == "verify":
            result = verify_backup(args.remote, args.snapshot_id)
        else:
            result = restore_backup(args.remote, args.snapshot_id, args.destination,
                                    database_backup_sha256=args.database_backup_sha256)
    except ArtifactBackupError as exc:
        parser.error(str(exc))
    print(json.dumps(result, sort_keys=True, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
