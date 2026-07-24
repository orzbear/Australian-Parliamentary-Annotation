"""Adapter delegating to the accepted OpenAustralia/PublicWhip workflow."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from hansard_annotator.adapters.capabilities import AdapterCapabilities
from hansard_annotator.corpus.discovery import discover_source_files
from hansard_annotator.corpus.models import DiscoveredFile
from hansard_annotator.corpus.pipeline import (
    PipelineOptions,
    PipelineOutcome,
    run_pipeline,
)


class OpenAustraliaHansardAdapter:
    adapter_key = "openaustralia_publicwhip_xml"
    adapter_version = "1.0.0"
    source_format_key = "openaustralia_publicwhip_xml"
    source_format_version = "1"
    supported_languages = ("en",)
    supported_chambers = ("House of Representatives",)

    def describe_provenance(self) -> dict[str, str | None]:
        return {
            "source_description": (
                "OpenAustralia/PublicWhip-style normalised XML derived from "
                "Australian Parliament ParlInfo"
            ),
            "provenance_url": None,
            "licence_status": "pending_review",
            "redistribution_status": "pending_review",
        }

    def describe_capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            discovery=True,
            source_validation=True,
            preprocessing=True,
        )

    def discover_sources(
        self, root: Path, *, scope: str = "full"
    ) -> Sequence[DiscoveredFile]:
        if scope != "full":
            raise ValueError(f"unsupported discovery scope: {scope}")
        return discover_source_files(root)

    def validate_source(self, path: Path, root: Path) -> DiscoveredFile:
        relative = path.resolve().relative_to(root.resolve()).as_posix()
        matches = discover_source_files(root, one_file=relative)
        if len(matches) != 1:
            raise ValueError(f"source did not resolve uniquely: {relative}")
        return matches[0]

    def preprocess(self, options: PipelineOptions) -> PipelineOutcome:
        return run_pipeline(options)
