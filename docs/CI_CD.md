# CI/CD policy

## Source-of-truth workflow

The Git repository is the source of truth for application and deployment code. Production
data is not. Raw XML, processed artifacts, environment files, database dumps, exports and
credentials remain outside Git.

Use this flow for every change:

1. create or reuse a short-lived feature branch;
2. make and test the change locally;
3. commit and push the branch;
4. require the `CI` workflow to pass in a pull request;
5. review and merge to `main`;
6. deploy the exact reviewed `main` commit;
7. take a database backup before migrations and run production verification afterward.

Changing local files or pushing to GitHub does not alter the live domain. Deployment is a
separate, auditable operation.

## Continuous integration

`.github/workflows/ci.yml` runs on pull requests to `main` and again after accepted changes
are merged into `main`. This avoids duplicate push and pull-request runs for feature branches.
It uses read-only repository permissions and immutable commit pins for GitHub-maintained
actions. It builds browser assets, runs lint/type/unit checks, exercises migrations on
PostgreSQL 16, validates the production Compose/Caddy configuration, and builds the production
image. It never receives production credentials or corpus data.

Configure `main` branch protection to require the `CI / quality-and-production-build` check
before merging. Keep the repository private while licensing/publication decisions remain
unresolved.

## Production deployment

Production deployment remains manual during the private conference pilot. Use the reviewed
runbook in `docs/PILOT_DEPLOYMENT_RUNBOOK.md`: package an explicit allowlist, verify its
checksum, back up PostgreSQL, migrate explicitly, recreate only the Hansard web/Caddy
services, and run `deploy/scripts/verify.sh`. Never transfer `.env.production` through Git or
replace the PostgreSQL volume.

Do not store the owner's passphrase-protected root SSH key in GitHub. Before enabling a
GitHub deployment workflow, create a dedicated VPS deploy identity, restrict it to the
Hansard deployment path/command, pin the VPS host key, store its private key as a GitHub
secret, restrict deployment to `main`, use a `workflow_dispatch` manual trigger and a
single-production concurrency group. Do not install a self-hosted Actions runner on the VPS.

Automatic deployment on every push is deliberately not enabled for the pilot. A failed test,
accidental push, or compromised dependency must not immediately modify the research service
or its database.
