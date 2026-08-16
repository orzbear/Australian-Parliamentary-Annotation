# Approval-gated GitHub production deployment

This deployment is manually triggered, restricted to `main`, serialized, and requires an
explicit confirmation phrase. It packages the exact selected commit, checks its SHA-256 on
the VPS, builds a commit-tagged image, backs up PostgreSQL, runs migrations explicitly,
recreates only the Hansard web/Caddy services, and verifies application and database
readiness. It does not modify the Hermes Compose project or delete Docker volumes.

## 1. Merge the deployment workflow

The workflow itself must pass CI and be merged through the normal pull request before it can
deploy from `main`. Do not run it from the feature branch.

## 2. Create a dedicated key on the owner's PC

Create a new key used only by GitHub Actions. It must not replace the owner's existing SSH key:

```powershell
ssh-keygen -t ed25519 -f "$env:USERPROFILE\.ssh\id_ed25519_hansard_github" -C "github-actions-hansard-deploy"
```

Because an unattended workflow cannot type a passphrase, leave this dedicated key's
passphrase empty. Its risk is constrained by the unprivileged VPS account and single allowed
sudo command installed below. Keep the private key out of the repository and chat.

After this change is merged, copy the public key and the two reviewed bootstrap files to a
temporary root-owned VPS directory:

```powershell
ssh -i "$env:USERPROFILE\.ssh\id_ed25519_hansard_vps" root@187.52.115.175 "mkdir -p /root/hansard-cd-bootstrap && chmod 700 /root/hansard-cd-bootstrap"
scp -i "$env:USERPROFILE\.ssh\id_ed25519_hansard_vps" `
  "$env:USERPROFILE\.ssh\id_ed25519_hansard_github.pub" `
  deploy/host/bootstrap-github-deployer.sh `
  deploy/host/hansard-release `
  root@187.52.115.175:/root/hansard-cd-bootstrap/
```

Then run the bootstrap as root:

```bash
sh /root/hansard-cd-bootstrap/bootstrap-github-deployer.sh \
  /root/hansard-cd-bootstrap/id_ed25519_hansard_github.pub
```

The bootstrap creates `hansard-deploy`, disables forwarding and PTY allocation for the key,
gives it a private upload directory, and permits only `/usr/local/sbin/hansard-release` through
passwordless sudo. The production environment file remains root-only.

## 3. Pin the VPS host identity

On the VPS, record the authoritative Ed25519 fingerprint:

```bash
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
```

On the owner's PC, collect the public host-key line and verify that its fingerprint matches:

```powershell
ssh-keyscan -t ed25519 187.52.115.175 | Set-Content "$env:TEMP\hansard-known-hosts"
ssh-keygen -lf "$env:TEMP\hansard-known-hosts"
```

Do not save the scanned line until the fingerprints match. This prevents the workflow from
silently connecting to an impostor host.

## 4. Configure GitHub deployment secrets

For a private personal repository, open **Settings -> Secrets and variables -> Actions** and
add the following repository secrets:

- `DEPLOY_HOST`: `187.52.115.175`
- `DEPLOY_USER`: `hansard-deploy`
- `DEPLOY_SSH_KEY`: the complete dedicated private-key file
- `DEPLOY_KNOWN_HOSTS`: the verified `ssh-ed25519` host-key line

The application/database secrets stay only in `/opt/hansard-annotator/.env.production` on the
VPS. They are not GitHub secrets because the deployment command does not need to read them.

GitHub currently limits required-reviewer protection for private repositories to qualifying
Enterprise plans. On a private Free, Pro or Team repository, the approval gate is therefore
the deliberate **Run workflow** action plus the typed confirmation. If the repository has
Enterprise environment protection, add `environment: production` to the job, put these four
secrets in that environment, restrict it to `main`, require a reviewer, and prevent
self-review.

## 5. First connection test

Before a real deployment, verify the new identity from the owner's PC. It should log in as the
unprivileged user, and `sudo -l` should list only the release command:

```powershell
ssh -i "$env:USERPROFILE\.ssh\id_ed25519_hansard_github" hansard-deploy@187.52.115.175 "id; sudo -l"
```

## 6. Deploy an approved commit

Wait for CI on `main` to pass. Open **Actions -> Deploy production -> Run workflow**, select
`main`, type `deploy-production`, and submit. This is the production approval action; no push
or merge can start it automatically. Never deploy a SHA other than the reviewed green `main`
commit.

GitHub serializes production deployments. The VPS additionally takes a host lock, verifies the
archive checksum and allowlist, rejects links/path traversal, and refuses to reuse an existing
release directory. Every successful SHA is recorded in
`/var/log/hansard-annotator-deployments.log`; releases remain under
`/opt/hansard-annotator/releases/` for diagnosis. No automatic release deletion is enabled.

## 7. Recovery boundary

The workflow does not automatically reverse a database migration. If verification fails, do
not delete volumes or retry blindly. Keep PostgreSQL running, inspect the deployment log and
Hansard container logs, and follow the recovery section of `docs/PILOT_DEPLOYMENT_RUNBOOK.md`.
The pre-migration logical backup remains in the configured backup directory.
