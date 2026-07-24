"""Explicit non-executable source-adapter capabilities."""

from dataclasses import dataclass


@dataclass(frozen=True)
class AdapterCapabilities:
    discovery: bool = False
    source_validation: bool = False
    preprocessing: bool = False
    direct_database_import: bool = False
    canonical_export: bool = False
    incremental_update: bool = False
