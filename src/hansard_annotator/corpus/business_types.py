"""Exact, versioned business-type mapping."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hansard_annotator.corpus.config import file_sha256, load_yaml
from hansard_annotator.corpus.hashing import normalise_unicode


@dataclass(frozen=True)
class BusinessTypeRule:
    heading: str
    business_type: str
    is_procedural: bool | None
    is_ceremonial: bool | None
    is_question_time: bool | None
    is_division_related: bool | None


@dataclass(frozen=True)
class BusinessTypeConfig:
    version: str
    sha256: str
    rules: dict[str, BusinessTypeRule]

    def lookup(self, heading: str | None) -> BusinessTypeRule | None:
        if heading is None:
            return None
        return self.rules.get(normalise_heading(heading))


def normalise_heading(value: str) -> str:
    return " ".join(normalise_unicode(value).split())


def load_business_type_config(path: Path) -> BusinessTypeConfig:
    raw = load_yaml(path)
    if raw.get("mapping_type") != "exact":
        raise ValueError("Phase 1 only supports exact business-type mappings")
    rules: dict[str, BusinessTypeRule] = {}
    for item in raw["mappings"]:
        typed: dict[str, Any] = item
        rule = BusinessTypeRule(
            heading=str(typed["heading"]),
            business_type=str(typed["business_type"]),
            is_procedural=typed.get("is_procedural"),
            is_ceremonial=typed.get("is_ceremonial"),
            is_question_time=typed.get("is_question_time"),
            is_division_related=typed.get("is_division_related"),
        )
        key = normalise_heading(rule.heading)
        if key in rules:
            raise ValueError(f"duplicate business heading: {key}")
        rules[key] = rule
    return BusinessTypeConfig(
        version=str(raw["version"]),
        sha256=file_sha256(path),
        rules=rules,
    )

