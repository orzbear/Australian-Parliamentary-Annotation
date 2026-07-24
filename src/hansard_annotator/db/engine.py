"""SQLAlchemy engine creation without credential logging."""

from __future__ import annotations

from sqlalchemy import Engine, create_engine

from hansard_annotator.db.config import DatabaseSettings


def create_database_engine(settings: DatabaseSettings) -> Engine:
    return create_engine(
        settings.url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 10},
    )
