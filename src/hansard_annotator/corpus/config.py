"""Versioned configuration loading and hashing."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class SafetyLimits:
    max_file_bytes: int
    max_elements: int
    max_depth: int
    max_individual_text_chars: int


@dataclass(frozen=True)
class ReconstructionConfig:
    version: str
    chamber: str
    expected_source_url_marker: str
    collective_speaker_allowlist: tuple[str, ...]
    collective_interruption_patterns: dict[str, str]
    collective_interruption_reason_code: str
    unknown_speaker_ids: tuple[str, ...]
    unknown_speaker_names: tuple[str, ...]
    safety_limits: SafetyLimits
    sha256: str


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as source:
        value = yaml.safe_load(source)
    if not isinstance(value, dict):
        raise ValueError(f"configuration root must be a mapping: {path}")
    return value


def load_reconstruction_config(path: Path) -> ReconstructionConfig:
    raw = load_yaml(path)
    limits = raw["safety_limits"]
    return ReconstructionConfig(
        version=str(raw["version"]),
        chamber=str(raw["chamber"]),
        expected_source_url_marker=str(raw["expected_source_url_marker"]),
        collective_speaker_allowlist=tuple(raw["collective_speaker_allowlist"]),
        collective_interruption_patterns={
            str(key): str(value)
            for key, value in raw["collective_interruption_patterns"].items()
        },
        collective_interruption_reason_code=str(
            raw["collective_interruption_reason_code"]
        ),
        unknown_speaker_ids=tuple(str(value) for value in raw["unknown_speaker_ids"]),
        unknown_speaker_names=tuple(
            str(value) for value in raw["unknown_speaker_names"]
        ),
        safety_limits=SafetyLimits(
            max_file_bytes=int(limits["max_file_bytes"]),
            max_elements=int(limits["max_elements"]),
            max_depth=int(limits["max_depth"]),
            max_individual_text_chars=int(limits["max_individual_text_chars"]),
        ),
        sha256=file_sha256(path),
    )

