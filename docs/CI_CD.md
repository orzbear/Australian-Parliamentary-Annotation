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

Production deployment remains manual and approval-gated during the private conference pilot.
After its one-time restricted-identity setup, `.github/workflows/deploy-production.yml`
requires an explicit GitHub Actions dispatch and confirmation phrase, then implements the
reviewed procedure in `docs/GITHUB_PRODUCTION_DEPLOYMENT.md`: package an exact `main` commit
with an explicit allowlist, verify its checksum, back up PostgreSQL, migrate explicitly,
recreate only the Hansard web/Caddy services, and run product verification. Never transfer
`.env.production` through Git or replace the PostgreSQL volume.

Do not store the owner's passphrase-protected root SSH key in GitHub. The setup creates a
dedicated VPS identity restricted to the Hansard upload directory and release command, pins
the VPS host key, stores its private key as a GitHub Actions secret, restricts deployment to
`main`, uses a `workflow_dispatch` manual trigger with explicit confirmation, and serializes
production deployments. Do not install a self-hosted Actions runner on the VPS. Required
reviewers can be added when the private repository's GitHub plan supports that protection.

Automatic deployment on every push is deliberately not enabled for the pilot. A failed test,
accidental push, or compromised dependency must not immediately modify the research service
or its database.
