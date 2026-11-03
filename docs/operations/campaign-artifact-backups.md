# Campaign artifact off-host backups

The campaign artifact volume is separate from PostgreSQL. Back it up only after a
PostgreSQL dump has completed and its SHA-256 is known. The artifact manifest
records that checksum to bind the two snapshots. The tool refuses to report a
backup without an explicit off-host rclone remote and does not read environment
files or runtime secrets from the application configuration.

## Configure an external destination

Configure an `rclone` remote on the operator host using the storage provider's
normal credential procedure. Do not put credentials in the repository, Compose
environment, or artifact volume. Set `HANSARD_ARTIFACT_BACKUP_REMOTE` to a path
such as `remote:bucket/hansard/campaign-artifacts`. The destination must be
outside the VPS and protected with access controls and lifecycle policies that
retain old snapshot manifests and content-addressed objects.

The application image does not bundle rclone. After a release containing this
tool is built, run the image with the host's rclone executable and protected
rclone config mounted read-only. Mount the campaign artifact volume read-only
and use the export work volume for temporary verification files. The production
deployment currently has no external destination configured, so backup is not
operational until the external remote and credentials are configured.

## Backup

First use the existing PostgreSQL backup workflow and calculate the resulting
dump checksum. Then run:

```sh
docker run --rm --read-only \
  --mount type=volume,src=hansard-annotator_hansard-campaign-artifacts,dst=/artifacts,readonly \
  --mount type=volume,src=hansard-annotator_hansard-export-work,dst=/work \
  --mount type=bind,src=/usr/bin/rclone,dst=/usr/local/bin/rclone,readonly \
  --mount type=bind,src=/etc/rclone/hansard.conf,dst=/run/secrets/rclone.conf,readonly \
  --env RCLONE_CONFIG=/run/secrets/rclone.conf \
  --env HANSARD_ARTIFACT_BACKUP_REMOTE=remote:bucket/hansard/campaign-artifacts \
  --env TMPDIR=/work \
  hansard-annotator:<deployed-image-tag> \
  python -m hansard_annotator.llm_eval.campaign_artifact_backup backup \
  --source /artifacts \
  --database-backup-sha256 <sha256-of-postgres-dump> \
  --snapshot-id <operator-chosen-unique-id>
```

The volume names above use the Compose project name `hansard-annotator`; confirm
the actual Docker volume names with `docker volume ls` before running. Ensure
the mounted rclone config is readable by the container's non-root UID 10001.
Replace the remote, image tag, snapshot ID, and dump checksum with verified
operator values.

Files are copied to immutable SHA-256 object names and reused on later runs.
Each unique snapshot manifest is published last. A manifest is the completion
marker; temporary/orphan objects from an interrupted run are not a backup.
Concurrent runs on the same host are serialized by an exclusive lock. Never
delete the lock automatically after an interrupted process; inspect its PID and
the remote snapshot state first.

## Verify and restore

Verification downloads each referenced object to a temporary directory and
checks its size and SHA-256 without modifying remote data:

```sh
docker run --rm --read-only \
  --mount type=volume,src=hansard-annotator_hansard-campaign-artifacts,dst=/artifacts,readonly \
  --mount type=volume,src=hansard-annotator_hansard-export-work,dst=/work \
  --mount type=bind,src=/usr/bin/rclone,dst=/usr/local/bin/rclone,readonly \
  --mount type=bind,src=/etc/rclone/hansard.conf,dst=/run/secrets/rclone.conf,readonly \
  --env RCLONE_CONFIG=/run/secrets/rclone.conf \
  --env TMPDIR=/work \
  hansard-annotator:<deployed-image-tag> \
  python -m hansard_annotator.llm_eval.campaign_artifact_backup verify \
  --remote remote:bucket/hansard/campaign-artifacts --snapshot-id <snapshot-id>
```

Restore requires the exact checksum of the matching PostgreSQL dump and an
absent or empty isolated destination. It never targets the live artifact root
unless an operator explicitly supplies that path; do not do so for a test.

```sh
docker run --rm --read-only \
  --mount type=volume,src=hansard-annotator_restore-test,dst=/restore-test \
  --mount type=volume,src=hansard-annotator_hansard-export-work,dst=/work \
  --mount type=bind,src=/usr/bin/rclone,dst=/usr/local/bin/rclone,readonly \
  --mount type=bind,src=/etc/rclone/hansard.conf,dst=/run/secrets/rclone.conf,readonly \
  --env RCLONE_CONFIG=/run/secrets/rclone.conf \
  --env TMPDIR=/work \
  hansard-annotator:<deployed-image-tag> \
  python -m hansard_annotator.llm_eval.campaign_artifact_backup restore \
  --remote remote:bucket/hansard/campaign-artifacts \
  --snapshot-id <snapshot-id> \
  --database-backup-sha256 <matching-postgres-dump-sha256> \
  --destination /restore-test/<snapshot-id>
```

The tool validates every restored file before publishing the tree. Restore the
matching PostgreSQL dump only into an isolated database and compare campaign
artifact references there. No production restore is performed by these
commands.
