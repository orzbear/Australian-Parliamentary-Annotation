# Web information architecture

The application represents the overall **Australian Hansard Policy Annotation** research
system. A technical `projects` route and database entity remain for compatibility, but that
entity is only one Human Annotation workspace and is not the whole research project.

## Top-level modules

- **Dashboard** — personal queues and current system activity.
- **Human Annotation** — direct human coding of Hansard speaker turns. Existing `/projects`
  bookmarks remain valid.
- **AI Annotation** — administrator-controlled campaign planning, provider execution,
  recovery, validation, and prediction import.
- **AI Review** — human confirmation, correction, skipping, and codebook flagging for
  immutable model predictions.
- **Codebook & Taxonomy** — reviewer-readable policy gate, canonical AU01–AU14 and
  AU_OTHER_REVIEW definitions, secondary guidance, boundary/precedence rules, hashes, and
  allowlisted source downloads.
- **Exports** — administrator research-output hub that invokes existing Human Annotation,
  prediction, review-audit, and codebook services.
- **Administration** — account and system audit functions.

Navigation follows existing authorization. Reviewers see Human Annotation, AI Review, and
the read-only codebook. Administrators additionally see AI Annotation, Exports, and
Administration.

## Frozen codebook view

The Codebook & Taxonomy page loads the strict repository taxonomy and annotation-schema
seeds using the same product seed loader used by the application. It separately validates
the canonical V2 prompt context and assembled frozen prompt hash. This preserves the
distinction between taxonomy, schema, and prompt artifacts.

There are no edit actions. Administrator downloads are fixed, controlled endpoints for the
canonical taxonomy YAML, schema YAML, and frozen context JSON. No arbitrary path parameter
is accepted.

## Export types and provenance

Human Annotation exports link to the existing canonical project exporter. AI prediction
CSV files contain immutable prediction rows plus run identity and configuration provenance.
AI Review exports call `hansard_annotator.llm_eval.review_audit`, producing:

- full review results CSV;
- edited cases CSV;
- codebook-flagged cases CSV;
- review summary JSON;
- audit Markdown;
- a deterministic ZIP containing those five files.

The full CSV is available for an incomplete run and is explicitly a partial raw export.
Completed audit artifacts require zero unreviewed predictions. Acceptance is described as
human confirmation in an assisted workflow, never automatically as benchmark accuracy.
Exports retain separate AI prediction, terminal review action, linked annotation revision,
and codebook flag fields.

Research exports require the global admin role and CSRF-protected POST requests. Codebook
reading remains available to authenticated reviewers; canonical downloads are restricted.
Generated responses use fixed safe filenames, `no-store`, `nosniff`, and an explicit UTC
export-timestamp response header.
