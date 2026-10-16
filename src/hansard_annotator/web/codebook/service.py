"""Strict, read-only access to the canonical production codebook artifacts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from hansard_annotator.llm_eval.operations import (
    FROZEN_CANONICAL_SCHEMA_SHA256,
    FROZEN_PROMPT_SHA256,
    FROZEN_PROMPT_VERSION,
)
from hansard_annotator.llm_eval.prompt import PROMPT_V2, build_system_prompt, prompt_hash
from hansard_annotator.product.seeds import (
    load_annotation_schema_seed,
    load_taxonomy_seed,
)

ROOT = Path(__file__).parents[4]
TAXONOMY_PATH = ROOT / "config/taxonomies/australian_policy_domains/0.1.0.yaml"
SCHEMA_PATH = ROOT / "config/annotation_schemas/australian_policy_annotation/0.2.0.yaml"
CONTEXT_PATH = (
    ROOT
    / "scripts/conference-codebook-pilot-01-ai-codebook-input-d1d13e895705"
    / "CODEBOOK_CONTEXT.json"
)
DownloadKind = Literal["taxonomy-yaml", "schema-yaml", "context-json"]


@dataclass(frozen=True)
class CanonicalDownload:
    filename: str
    media_type: str
    content: bytes


def production_codebook() -> dict[str, Any]:
    """Load the exact strict seeds and the context used to assemble frozen V2."""
    taxonomy = load_taxonomy_seed(TAXONOMY_PATH)
    schema = load_annotation_schema_seed(SCHEMA_PATH)
    context_value = json.loads(CONTEXT_PATH.read_text(encoding="utf-8"))
    if not isinstance(context_value, dict):
        raise ValueError("canonical codebook context must be an object")
    if schema.content_sha256 != FROZEN_CANONICAL_SCHEMA_SHA256:
        raise ValueError("canonical schema no longer matches frozen production hash")
    context_schema = context_value.get("schema")
    if not isinstance(context_schema, dict) or (
        context_schema.get("content_sha256") != schema.content_sha256
    ):
        raise ValueError("frozen prompt context and canonical schema disagree")
    if prompt_hash(build_system_prompt(context_value, FROZEN_PROMPT_VERSION)) != (
        FROZEN_PROMPT_SHA256
    ):
        raise ValueError("canonical context no longer assembles the frozen production prompt")
    return {
        "taxonomy": taxonomy,
        "schema": schema,
        "context": context_value,
        "prompt_sections": _prompt_sections(PROMPT_V2),
        "production": {
            "label": "FROZEN PRODUCTION CODEBOOK / CONFIGURATION",
            "prompt_version": FROZEN_PROMPT_VERSION,
            "prompt_sha256": FROZEN_PROMPT_SHA256,
            "canonical_schema_sha256": FROZEN_CANONICAL_SCHEMA_SHA256,
            "taxonomy_sha256": taxonomy.content_sha256,
            "editable": False,
        },
    }


def _prompt_sections(prompt: str) -> list[dict[str, str]]:
    sections: list[dict[str, str]] = []
    current_title = "Policy and Non-Policy framing"
    current_lines: list[str] = []
    for line in prompt.strip().splitlines():
        if line.startswith("STEP "):
            if current_lines:
                sections.append({"title": current_title, "body": "\n".join(current_lines).strip()})
            heading, _, remainder = line.partition(". ")
            current_title = heading
            current_lines = [remainder]
        else:
            current_lines.append(line)
    if current_lines:
        sections.append({"title": current_title, "body": "\n".join(current_lines).strip()})
    return sections


def canonical_download(kind: DownloadKind) -> CanonicalDownload:
    """Return only an allowlisted canonical source; never accept a filesystem path."""
    production_codebook()
    choices = {
        "taxonomy-yaml": CanonicalDownload(
            "australian-policy-domains-0.1.0.yaml",
            "application/yaml; charset=utf-8",
            TAXONOMY_PATH.read_bytes(),
        ),
        "schema-yaml": CanonicalDownload(
            "australian-policy-annotation-0.2.0.yaml",
            "application/yaml; charset=utf-8",
            SCHEMA_PATH.read_bytes(),
        ),
        "context-json": CanonicalDownload(
            "frozen-v2-codebook-context.json",
            "application/json; charset=utf-8",
            CONTEXT_PATH.read_bytes(),
        ),
    }
    try:
        return choices[kind]
    except KeyError:
        raise ValueError("unsupported canonical codebook download") from None
