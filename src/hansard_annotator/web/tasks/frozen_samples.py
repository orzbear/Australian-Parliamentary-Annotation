"""Validated, zero-resampling imports of frozen research samples."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Literal

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, model_validator

from hansard_annotator.web.audit.service import record_event
from hansard_annotator.web.auth.models import Principal
from hansard_annotator.web.config import WebSettings
from hansard_annotator.web.projects.permissions import MANAGER_ROLES, require_project_role


class FrozenTurn(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sample_order: int = Field(ge=1)
    turn_key: str = Field(min_length=1, max_length=64)
    primary_domain: str = Field(pattern=r"^AU(?:0[1-9]|1[0-4])$")


class FrozenSample(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"]
    sample_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    purpose: Literal[
        "HOSTILITY_DEVELOPMENT",
        "HOSTILITY_HELDOUT_VALIDATION",
        "HOSTILITY_LUNA_CONFIRMATION",
    ]
    version: str | None = None
    sample_version: str | None = None
    sample_role: Literal["development", "heldout_validation", "confirmation"] | None = None
    source_frame_path: str | None = None
    eligible_population: list[str] | None = None
    development_sample_exclusion: str | None = None
    algorithm_version: str | None = None
    source_frame_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_canonical_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    seed: int
    allocation: dict[str, int]
    selection_algorithm: str
    sample_size: Literal[100]
    sample_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: str
    turns: list[FrozenTurn] = Field(min_length=100, max_length=100)

    @model_validator(mode="after")
    def validate_frozen_membership(self) -> FrozenSample:
        if not self.version and not self.sample_version:
            raise ValueError("frozen sample must declare a version")
        if self.version and self.sample_version and self.version != self.sample_version:
            raise ValueError("frozen sample version fields disagree")
        keys = [turn.turn_key for turn in self.turns]
        orders = [turn.sample_order for turn in self.turns]
        if len(set(keys)) != self.sample_size:
            raise ValueError("frozen sample contains duplicate turn keys")
        if orders != list(range(1, self.sample_size + 1)):
            raise ValueError("frozen sample orders must be contiguous and ordered")
        actual: dict[str, int] = {}
        for turn in self.turns:
            actual[turn.primary_domain] = actual.get(turn.primary_domain, 0) + 1
        if actual != self.allocation:
            raise ValueError("frozen sample allocation does not match its turns")
        if selection_sha256(self.turns) != self.sample_sha256:
            raise ValueError("frozen sample SHA-256 does not match its turns")
        return self

    @property
    def effective_version(self) -> str:
        return self.sample_version or self.version or ""


def selection_sha256(turns: list[FrozenTurn]) -> str:
    canonical = "\n".join(
        f"{turn.sample_order}|{turn.turn_key}|{turn.primary_domain}" for turn in turns
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def load_frozen_sample(path: Path, expected_sample_id: str | None = None) -> FrozenSample:
    sample = FrozenSample.model_validate_json(path.read_text(encoding="utf-8"))
    if expected_sample_id is not None and sample.sample_id != expected_sample_id:
        raise ValueError(f"expected frozen sample {expected_sample_id}, got {sample.sample_id}")
    return sample


def map_frozen_turn_ids(keys: list[str], rows: list[dict[str, object]]) -> list[int]:
    by_key = {str(row["turn_key"]): int(str(row["id"])) for row in rows}
    missing = [key for key in keys if key not in by_key]
    if missing:
        raise ValueError(f"frozen sample has {len(missing)} missing turn keys: {missing[:5]}")
    return [by_key[key] for key in keys]


def create_batch_from_frozen_sample(
    settings: WebSettings,
    principal: Principal,
    project_id: int,
    sample: FrozenSample,
    *,
    batch_name: str | None = None,
) -> dict[str, Any]:
    name = (batch_name or sample.sample_id).strip()
    criteria = {
        "type": "frozen_sample",
        "schema_version": sample.schema_version,
        "sample_id": sample.sample_id,
        "sample_version": sample.effective_version,
        "sample_sha256": sample.sample_sha256,
        "sample_size": sample.sample_size,
    }
    keys = [turn.turn_key for turn in sample.turns]
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        require_project_role(connection, principal, project_id, MANAGER_ROLES)
        project = connection.execute(
            "SELECT status,preprocessing_run_id FROM projects WHERE id=%s FOR UPDATE",
            (project_id,),
        ).fetchone()
        if project is None or project["status"] == "archived":
            raise ValueError("archived or missing project cannot generate tasks")
        existing = connection.execute(
            "SELECT id,public_id,name,status,selected_count,selection_sha256,"
            "selection_criteria FROM batches WHERE project_id=%s AND name=%s",
            (project_id, name),
        ).fetchone()
        if existing is not None:
            if existing["selection_criteria"] != criteria:
                raise ValueError("batch name already exists with a different frozen sample")
            stored_keys = [
                str(row["turn_key"])
                for row in connection.execute(
                    "SELECT st.turn_key FROM tasks t JOIN speaker_turns st "
                    "ON st.id=t.speaker_turn_id WHERE t.batch_id=%s ORDER BY t.id",
                    (existing["id"],),
                )
            ]
            if stored_keys != keys:
                raise ValueError("existing frozen-sample batch membership has changed")
            return {**dict(existing), "reused": True}
        rows = connection.execute(
            "SELECT st.id,st.turn_key FROM speaker_turns st "
            "JOIN reconstruction_runs rr ON rr.id=st.reconstruction_run_id "
            "WHERE rr.preprocessing_run_id=%s AND st.turn_key=ANY(%s)",
            (project["preprocessing_run_id"], keys),
        ).fetchall()
        turn_ids = map_frozen_turn_ids(keys, [dict(row) for row in rows])
        conflicts = connection.execute(
            "SELECT st.turn_key FROM tasks t JOIN speaker_turns st "
            "ON st.id=t.speaker_turn_id WHERE t.project_id=%s AND st.turn_key=ANY(%s)",
            (project_id, keys),
        ).fetchall()
        if conflicts:
            raise ValueError("frozen sample turns already exist in another project batch")
        batch = connection.execute(
            "INSERT INTO batches (project_id,name,status,selection_criteria,random_seed,"
            "requested_count,selected_count,selection_sha256,created_by_user_id,finalised_at) "
            "VALUES (%s,%s,'generated',%s,%s,%s,%s,%s,%s,now()) "
            "RETURNING id,public_id,name,status,selected_count,selection_sha256",
            (
                project_id,
                name,
                Jsonb(criteria),
                sample.seed,
                sample.sample_size,
                sample.sample_size,
                sample.sample_sha256,
                principal.user_id,
            ),
        ).fetchone()
        assert batch is not None
        task_rows = connection.execute(
            "INSERT INTO tasks (project_id,batch_id,speaker_turn_id,priority) "
            "SELECT %s,%s,candidate.turn_id,-candidate.ordinal::integer "
            "FROM unnest(%s::bigint[]) WITH ORDINALITY candidate(turn_id,ordinal) "
            "ORDER BY candidate.ordinal RETURNING id",
            (project_id, batch["id"], turn_ids),
        ).fetchall()
        if len(task_rows) != sample.sample_size:
            raise RuntimeError("frozen sample task insertion count mismatch")
        connection.cursor().executemany(
            "INSERT INTO assignments (task_id,status) VALUES (%s,'assigned')",
            [(row["id"],) for row in task_rows],
        )
        record_event(
            connection,
            "frozen_sample_batch_created",
            "batch",
            actor_user_id=principal.user_id,
            entity_public_id=batch["public_id"],
            project_id=project_id,
            metadata=criteria,
        )
        return {**dict(batch), "reused": False}
