"""Environment-only Phase 2 database configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from hansard_annotator.db.exceptions import DatabaseConfigurationError


@dataclass(frozen=True)
class DatabaseSettings:
    url: str
    processed_data_root: Path
    batch_size: int = 2_000

    @classmethod
    def from_environment(cls) -> DatabaseSettings:
        url = os.environ.get("HANSARD_DATABASE_URL", "").strip()
        if not url:
            raise DatabaseConfigurationError("HANSARD_DATABASE_URL is required")
        if not url.startswith(("postgresql+psycopg://", "postgresql://")):
            raise DatabaseConfigurationError("HANSARD_DATABASE_URL must use PostgreSQL")
        root = Path(os.environ.get("HANSARD_PROCESSED_DATA_ROOT", "data/processed"))
        batch = int(os.environ.get("HANSARD_IMPORT_BATCH_SIZE", "2000"))
        if batch < 1 or batch > 50_000:
            raise DatabaseConfigurationError("HANSARD_IMPORT_BATCH_SIZE must be 1..50000")
        return cls(url=url, processed_data_root=root.resolve(), batch_size=batch)

    @property
    def psycopg_url(self) -> str:
        return self.url.replace("postgresql+psycopg://", "postgresql://", 1)
