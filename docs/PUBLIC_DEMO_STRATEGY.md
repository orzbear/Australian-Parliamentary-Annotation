# Public demo strategy

Publish application source separately from the private full corpus. Demonstrate the
pipeline with the existing small synthetic XML fixtures and, later, a small synthetic or
formally verified redistributable corpus. Never copy the full Hansard archive or accepted
Phase 1 outputs into a demo directory.

A public package should include reproducible environment, migration, seed-validation,
and demo-loading instructions. Replace passwords and hostnames with placeholders and
scan output for secrets and absolute local paths. Demo labels, users, and screenshots
should be fictional or demonstrably safe to redistribute. Screenshots belong to a later
UI phase and must not expose unpublished annotations.

Make synthetic status conspicuous, retain deterministic checksums, and keep source-code
licensing separate from corpus and taxonomy permissions.
