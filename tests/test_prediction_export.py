from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from fastapi.routing import APIRoute

from hansard_annotator.db.config import DatabaseSettings
from hansard_annotator.web.auth.models import Principal
from hansard_annotator.web.config import WebSettings
from hansard_annotator.web.dependencies import require_admin
from hansard_annotator.web.research_exports import service as export_service
from hansard_annotator.web.research_exports.routes import router as export_router

POLICY_FIELDS = [
    "run_id",
    "provider",
    "configured_model",
    "prompt_version",
    "prompt_sha256",
    "canonical_schema_sha256",
    "prediction_contract_sha256",
    "provider_schema_sha256",
    "generation_config",
    "execution_mode",
    "record_set_sha256",
    "artifact_sha256",
    "provider_job_id",
    "run_imported_at",
    "prediction_id",
    "record_id",
    "speaker_turn_id",
    "turn_key",
    "speech_date",
    "chamber",
    "speaker_name_raw",
    "is_non_policy",
    "primary_australian_domain",
    "secondary_australian_domains",
    "reasoning",
    "returned_model",
    "created_at",
]
HOSTILITY_FIELDS = [
    *POLICY_FIELDS[:21],
    "prediction_task",
    "hostility_present",
    "target",
    "evidence_spans",
    "partisan_hostility",
    *POLICY_FIELDS[24:],
]


def _principal(*, admin: bool = True) -> Principal:
    return Principal(
        user_id=1,
        public_id=UUID(int=1),
        username="researcher",
        display_name="Researcher",
        must_change_password=False,
        is_admin=admin,
        session_id=1,
        csrf_token="csrf",
    )


def _settings() -> WebSettings:
    return WebSettings(
        database=DatabaseSettings(
            url="postgresql://test:test@localhost/test",
            processed_data_root="data/processed",
        ),
        environment="test",
        session_secret="prediction-export-test-secret-at-least-thirty-two",
        cookie_secure=False,
        allowed_hosts=("testserver",),
        base_url="http://testserver",
        idle_minutes=60,
        absolute_hours=12,
        minimum_password_length=12,
        lockout_failures=2,
        lockout_minutes=15,
        log_level="INFO",
    )


def _run(task: str) -> dict[str, Any]:
    return {
        "id": 7,
        "public_id": "run-public-id",
        "prediction_task": task,
        "provider": "gemini",
        "configured_model": "gemini-2.5-flash",
        "returned_model": "gemini-2.5-flash",
        "prompt_version": "hostility-prompt-v2" if task == "hostility" else "policy-v2",
        "prompt_sha256": "a" * 64,
        "canonical_schema_sha256": "b" * 64,
        "prediction_contract_sha256": "c" * 64,
        "provider_schema_sha256": "d" * 64,
        "generation_config": {"thinking_budget": 0},
        "execution_mode": "batch",
        "record_set_sha256": "e" * 64,
        "artifact_sha256": "f" * 64,
        "provider_job_id": "batches/test",
        "imported_at": datetime(2026, 11, 2, tzinfo=UTC),
    }


def _prediction(task: str, values: dict[str, Any]) -> dict[str, Any]:
    policy = task == "policy"
    return {
        "prediction_id": "prediction-public-id",
        "record_id": "turn:turn-key",
        "speaker_turn_id": 42,
        "prediction_task": task,
        "prediction_values": values,
        "turn_key": "turn-key",
        "speech_date": "2026-01-01",
        "chamber": "House",
        "speaker_name_raw": "Example Speaker",
        "is_non_policy": False if policy else None,
        "primary_australian_domain": "AU01" if policy else None,
        "secondary_australian_domains": ["AU02"] if policy else None,
        "reasoning": "Stored reasoning",
        "returned_model": "gemini-2.5-flash",
        "created_at": datetime(2026, 11, 1, tzinfo=UTC),
    }


class _Result:
    def __init__(self, one: dict[str, Any] | None = None, many: list[dict[str, Any]] | None = None):
        self.one = one
        self.many = many or []

    def fetchone(self) -> dict[str, Any] | None:
        return self.one

    def fetchall(self) -> list[dict[str, Any]]:
        return self.many


class _ReadOnlyConnection:
    def __init__(self, run: dict[str, Any], predictions: list[dict[str, Any]]) -> None:
        self.run = run
        self.predictions = predictions
        self.statements: list[str] = []

    def __enter__(self) -> _ReadOnlyConnection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, statement: str, _parameters: object = None) -> _Result:
        self.statements.append(statement)
        normalized = statement.lstrip().upper()
        if normalized.startswith("SET TRANSACTION READ ONLY"):
            return _Result()
        if "FROM MODEL_PREDICTION_RUNS" in normalized:
            return _Result(one=self.run)
        if "FROM MODEL_PREDICTIONS" in normalized:
            return _Result(many=self.predictions)
        raise AssertionError(f"unexpected (possibly mutating) SQL: {statement}")


def _export(
    monkeypatch: pytest.MonkeyPatch,
    run: dict[str, Any],
    predictions: list[dict[str, Any]],
) -> tuple[bytes, _ReadOnlyConnection]:
    connection = _ReadOnlyConnection(run, predictions)
    monkeypatch.setattr(export_service.psycopg, "connect", lambda *_args, **_kwargs: connection)
    download = export_service.prediction_export(_settings(), _principal(), "run-public-id")
    assert download.filename == "ai-predictions-run-public-id.csv"
    return download.content, connection


def test_policy_export_header_and_values_remain_byte_compatible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = {
        "is_non_policy": False,
        "primary_australian_domain": "AU01",
        "secondary_australian_domains": ["AU02"],
    }
    content, connection = _export(monkeypatch, _run("policy"), [_prediction("policy", values)])
    rows = list(csv.DictReader(io.StringIO(content.decode("utf-8"))))
    assert content.splitlines()[0].decode() == ",".join(POLICY_FIELDS)
    assert list(rows[0]) == POLICY_FIELDS
    assert rows[0]["is_non_policy"] == "False"
    assert rows[0]["primary_australian_domain"] == "AU01"
    assert rows[0]["secondary_australian_domains"] == '["AU02"]'
    assert rows[0]["reasoning"] == "Stored reasoning"
    assert rows[0]["run_imported_at"] == "2026-11-02T00:00:00+00:00"
    assert list(rows[0].values()) == [
        "run-public-id",
        "gemini",
        "gemini-2.5-flash",
        "policy-v2",
        "a" * 64,
        "b" * 64,
        "c" * 64,
        "d" * 64,
        '{"thinking_budget":0}',
        "batch",
        "e" * 64,
        "f" * 64,
        "batches/test",
        "2026-11-02T00:00:00+00:00",
        "prediction-public-id",
        "turn:turn-key",
        "42",
        "turn-key",
        "2026-01-01",
        "House",
        "Example Speaker",
        "False",
        "AU01",
        '["AU02"]',
        "Stored reasoning",
        "gemini-2.5-flash",
        "2026-11-01T00:00:00+00:00",
    ]
    assert all(
        statement.lstrip().upper().startswith(("SET TRANSACTION READ ONLY", "SELECT"))
        for statement in connection.statements
    )


@pytest.mark.parametrize(
    ("presence", "target", "spans", "expected_partisan"),
    [
        ("YES", "OPPOSING_PARTISAN_TARGET", True, "True"),
        ("YES", "OTHER_POLITICAL_TARGET", True, "False"),
        ("NO", "N/A", False, "False"),
        ("UNSURE", "OPPOSING_PARTISAN_TARGET", True, "False"),
    ],
)
def test_hostility_export_serializes_structured_values_without_policy_columns(
    monkeypatch: pytest.MonkeyPatch,
    presence: str,
    target: str,
    spans: bool,
    expected_partisan: str,
) -> None:
    speech = 'The words are "liars, cruel\nand false".'
    evidence = '"liars, cruel\nand false"'
    start = speech.index(evidence)
    evidence_spans = (
        [{"start_offset": start, "end_offset": start + len(evidence), "text": evidence}]
        if spans
        else []
    )
    values = {
        "hostility_present": presence,
        "target": target,
        "evidence_spans": evidence_spans,
    }
    content, connection = _export(
        monkeypatch, _run("hostility"), [_prediction("hostility", values)]
    )
    text = content.decode("utf-8")
    rows = list(csv.DictReader(io.StringIO(text)))
    assert text.splitlines()[0].split(",") == HOSTILITY_FIELDS
    assert list(rows[0]) == HOSTILITY_FIELDS
    assert "is_non_policy" not in rows[0]
    assert "primary_australian_domain" not in rows[0]
    assert "secondary_australian_domains" not in rows[0]
    assert rows[0]["prediction_task"] == "hostility"
    assert rows[0]["hostility_present"] == presence
    assert rows[0]["target"] == target
    assert rows[0]["partisan_hostility"] == expected_partisan
    assert json.loads(rows[0]["evidence_spans"]) == evidence_spans
    assert rows[0]["reasoning"] == "Stored reasoning"
    assert len(list(csv.reader(io.StringIO(text)))[1]) == len(HOSTILITY_FIELDS)
    assert all(
        statement.lstrip().upper().startswith(("SET TRANSACTION READ ONLY", "SELECT"))
        for statement in connection.statements
    )
    if spans:
        encoded_evidence = json.dumps(
            evidence_spans, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        cell = io.StringIO(newline="")
        csv.writer(cell, lineterminator="").writerow([encoded_evidence])
        assert cell.getvalue() in text


@pytest.mark.parametrize(
    ("run_task", "row_task", "values", "policy"),
    [
        (
            "unknown",
            "unknown",
            {"x": 1},
            False,
        ),
        (
            "hostility",
            "policy",
            {"hostility_present": "NO", "target": "N/A", "evidence_spans": []},
            False,
        ),
        (
            "hostility",
            "hostility",
            {"hostility_present": "NO", "target": "N/A", "evidence_spans": []},
            True,
        ),
        (
            "hostility",
            "hostility",
            {"hostility_present": "YES", "target": "N/A", "evidence_spans": []},
            False,
        ),
    ],
)
def test_export_rejects_unknown_or_inconsistent_task_data(
    monkeypatch: pytest.MonkeyPatch,
    run_task: str,
    row_task: str,
    values: dict[str, Any],
    policy: bool,
) -> None:
    if policy:
        values = {
            "is_non_policy": True,
            "primary_australian_domain": None,
            "secondary_australian_domains": [],
        }
    connection = _ReadOnlyConnection(_run(run_task), [_prediction(row_task, values)])
    monkeypatch.setattr(export_service.psycopg, "connect", lambda *_args, **_kwargs: connection)
    with pytest.raises(ValueError):
        export_service.prediction_export(_settings(), _principal(), "run-public-id")


def test_export_permission_and_route_authorization_remain_intact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_connect(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("non-admin access must be rejected before database access")

    monkeypatch.setattr(export_service.psycopg, "connect", unexpected_connect)
    with pytest.raises(PermissionError, match="administrator"):
        export_service.prediction_export(_settings(), _principal(admin=False), "run-id")
    route = next(
        route
        for route in export_router.routes
        if isinstance(route, APIRoute) and route.path == "/exports/predictions/{run_id}.csv"
    )
    assert any(dependency.call is require_admin for dependency in route.dependant.dependencies)
