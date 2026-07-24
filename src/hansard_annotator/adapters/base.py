"""Small typed contracts for internal source adapters."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol, runtime_checkable

from hansard_annotator.adapters.capabilities import AdapterCapabilities
from hansard_annotator.corpus.models import DiscoveredFile
from hansard_annotator.corpus.pipeline import PipelineOptions, PipelineOutcome


@runtime_checkable
class SourceAdapter(Protocol):
    adapter_key: str
    adapter_version: str
    source_format_key: str
    source_format_version: str
    supported_languages: tuple[str, ...]
    supported_chambers: tuple[str, ...]

    def describe_provenance(self) -> dict[str, str | None]: ...

    def describe_capabilities(self) -> AdapterCapabilities: ...


@runtime_checkable
class DiscoveryAdapter(SourceAdapter, Protocol):
    def discover_sources(
        self, root: Path, *, scope: str = "full"
    ) -> Sequence[DiscoveredFile]: ...


@runtime_checkable
class ValidationAdapter(SourceAdapter, Protocol):
    def validate_source(self, path: Path, root: Path) -> DiscoveredFile: ...


@runtime_checkable
class PreprocessingAdapter(SourceAdapter, Protocol):
    def preprocess(self, options: PipelineOptions) -> PipelineOutcome: ...
