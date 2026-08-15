"""Typed authenticated-principal model."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class Principal:
    user_id: int
    public_id: UUID
    username: str
    display_name: str
    must_change_password: bool
    is_admin: bool
    session_id: int
    csrf_token: str
