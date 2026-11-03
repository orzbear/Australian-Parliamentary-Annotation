from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import os
import threading
import zipfile
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from fastapi import HTTPException
from psycopg.types.json import Jsonb
from starlette.requests import Request

from hansard_annotator.db.config import DatabaseSettings
from hansard_annotator.web.auth.models import Principal
from hansard_annotator.web.campaigns.service import production_configuration
from hansard_annotator.web.config import (
    DEFAULT_EXPORT_MIN_FREE_BYTES,
    MINIMUM_EXPORT_MIN_FREE_BYTES,
    WebSettings,
)
from hansard_annotator.web.research_exports import canonical_ai as canonical_ai_module
from hansard_annotator.web.research_exports import routes as export_routes
from hansard_annotator.web.research_exports.canonical_ai import (
    CANONICAL_EXPORT_DIR_PREFIX,
    FIELDS,
    OUTCOME_NO_USABLE_TEXT,
    OUTCOME_PREDICTION,
    OUTCOME_PROVIDER_REFUSAL,
    PARQUET_SCHEMA,
    CanonicalAIExport,
    CanonicalAIExportError,
    _canonical_query,
    _configuration_parameters,
    _csv_row,
    _output_row,
    _write_deterministic_zip,
    check_export_disk_space,
    cleanup_canonical_export_work_dir,
)


def _raw(**overrides: object) -> dict[str, object]:
    configuration = production_configuration()
    value: dict[str, object] = {
        "corpus_public_id": "00000000-0000-0000-0000-000000000001",
        "corpus_slug": "house-hansard",
        "corpus_name": "House Hansard",
        "turn_id": 1,
        "turn_key": "stable-turn",
        "speech_date": date(2026, 1, 1),
        "chamber": "House of Representatives",
        "source_file": "2026/2026-01-01.xml",
        "source_url": "https://example.test/turn",
        "turn_sequence": 1,
        "speaker_id_raw": "speaker-1",
        "speaker_name_raw": "Example Speaker",
        "major_heading_original": "Debate",
        "minor_heading_original": "Matter",
        "business_type": "Debate",
        "calculated_word_count": 2,
        "warning_codes": [],
        "text_clean": "Useful speech",
        "text_raw": "Useful speech",
        "usable_text": True,
        "prediction_public_id": None,
        "prediction_task": "policy",
        "prediction_match_count": None,
        "abstention_public_id": None,
        "abstention_match_count": None,
        "provider": configuration["provider"],
        "configured_model": configuration["configured_model"],
        "run_returned_model": configuration["configured_model"],
        "prompt_version": configuration["prompt_version"],
        "prompt_sha256": configuration["prompt_sha256"],
        "canonical_schema_sha256": configuration["canonical_schema_sha256"],
        "prediction_contract_sha256": configuration["prediction_contract_sha256"],
        "provider_schema_sha256": configuration["provider_schema_sha256"],
        "generation_config": dict(configuration["generation_config"]),
        "execution_mode": configuration["execution_mode"],
        "latest_flagged_at": None,
    }
    value.update(overrides)
    return value


def test_canonical_query_is_corpus_first_and_exactly_configuration_pinned() -> None:
    query = _canonical_query()
    parameters = _configuration_parameters()
    configuration = production_configuration()
    assert list(PARQUET_SCHEMA.names) == FIELDS
    assert "FROM speaker_turns turn" in query
    assert "LEFT JOIN matching_predictions" in query
    assert "LEFT JOIN matching_abstentions" in query
    assert "generation_config=%s" in query
    assert "execution_mode=%s AND status='imported'" in query
    assert "prediction_task='policy'" in query
    assert "SELECT prediction.*" not in query
    assert "SELECT abstention.*" not in query
    assert "raw_provider_response" not in query
    assert 'ORDER BY turn.speech_date,source.relative_path COLLATE "C"' in query
    assert parameters == (
        "gemini",
        "gemini-3.1-flash-lite",
        "phase4a-policy-v2",
        "a41027b001f10b61074c196996606c86b5dac4c0788ae185a05ff68fcf3b7cc6",
        "9c94bab9cd2976559c79b1c31991aef4138a2071a5d3b3d6186a26448f117126",
        "99cafa6c473293dc076793ee6baeca44b6961d7cb11de8f9174517defc4a0879",
        configuration["provider_schema_sha256"],
        parameters[7],
        "batch",
    )
    assert isinstance(parameters[7], Jsonb)
    assert parameters[7].obj == {"thinking_level": "minimal"}


def test_outcomes_never_synthesize_abstention_or_no_text_classification() -> None:
    reviewed_at = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    prediction = _output_row(
        _raw(
            prediction_public_id="00000000-0000-0000-0000-000000000010",
            prediction_match_count=1,
            is_non_policy=False,
            primary_australian_domain="AU07",
            secondary_australian_domains=["AU08"],
            reasoning="An immutable model explanation.",
            prediction_artifact_reference="data/predictions/batch-1/predictions.jsonl#L1",
            latest_review_public_id="00000000-0000-0000-0000-000000000099",
            latest_review_status="edited",
            latest_reviewed_at=reviewed_at,
            latest_review_annotation_version_id=42,
            latest_review_note="Human reviewer changed domain to AU12.",
            latest_flag_note="Check boundary with defence.",
            latest_flagged_at=reviewed_at,
        ),
        1,
    )
    assert prediction["outcome_status"] == OUTCOME_PREDICTION
    assert prediction["is_non_policy"] is False
    assert prediction["primary_australian_domain"] == "AU07"
    assert prediction["secondary_australian_domains"] == ["AU08"]
    assert prediction["reasoning"] == "An immutable model explanation."
    assert prediction["thinking_level"] == "minimal"
    assert prediction["provider_block_reason"] is None
    assert prediction["refusal_provenance_json"] is None
    assert prediction["latest_review_status"] == "edited"
    assert prediction["review_edited"] is True
    assert prediction["latest_review_annotation_version_id"] == 42
    assert prediction["latest_review_note"] == "Human reviewer changed domain to AU12."
    assert prediction["has_codebook_flag"] is True
    assert prediction["latest_codebook_flag_note"] == "Check boundary with defence."

    refusal_payload = {
        "resolution": "PROVIDER_REFUSAL",
        "reason": "PROHIBITED_CONTENT",
        "attempts": [{"provider_response": {"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}}}],
    }
    refusal = _output_row(
        _raw(
            abstention_public_id="00000000-0000-0000-0000-000000000020",
            abstention_match_count=1,
            resolution_type="PROVIDER_REFUSAL",
            resolution_reason="PROHIBITED_CONTENT",
            refusal_provenance=refusal_payload,
            abstention_artifact_reference="data/campaigns/c1/provider_refusals.jsonl",
            is_non_policy=False,
            primary_australian_domain="AU01",
            secondary_australian_domains=["AU02"],
            reasoning="Must not leak.",
        ),
        2,
    )
    assert refusal["outcome_status"] == OUTCOME_PROVIDER_REFUSAL
    assert refusal["is_non_policy"] is None
    assert refusal["primary_australian_domain"] is None
    assert refusal["secondary_australian_domains"] is None
    assert refusal["reasoning"] is None
    assert refusal["provider_block_reason"] == "PROHIBITED_CONTENT"
    assert json.loads(refusal["refusal_provenance_json"]) == refusal_payload
    assert refusal["artifact_reference"] == "data/campaigns/c1/provider_refusals.jsonl"
    assert refusal["thinking_level"] == "minimal"
    assert refusal["review_edited"] is False

    no_text = _output_row(
        _raw(
            text_clean="",
            text_raw="   ",
            usable_text=False,
            provider=None,
            configured_model=None,
            run_returned_model=None,
            prompt_version=None,
            prompt_sha256=None,
            canonical_schema_sha256=None,
            prediction_contract_sha256=None,
            provider_schema_sha256=None,
            generation_config=None,
            execution_mode=None,
        ),
        3,
    )
    assert no_text["outcome_status"] == OUTCOME_NO_USABLE_TEXT
    assert no_text["is_non_policy"] is None
    assert no_text["primary_australian_domain"] is None
    assert no_text["secondary_australian_domains"] is None
    assert no_text["reasoning"] is None
    assert no_text["prediction_run_id"] is None
    assert no_text["provider"] is None
    assert no_text["thinking_level"] is None
    assert no_text["provider_block_reason"] is None
    assert no_text["refusal_provenance_json"] is None


def test_output_rows_can_reuse_the_export_pinned_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configuration = production_configuration()
    raw = _raw(prediction_public_id="prediction", prediction_match_count=1)
    expected = _output_row(raw, 1)
    expected_parameters = _configuration_parameters(configuration)

    def unexpected_configuration_lookup() -> dict[str, object]:
        raise AssertionError("configuration should be reused for each export row")

    monkeypatch.setattr(
        canonical_ai_module, "production_configuration", unexpected_configuration_lookup
    )
    actual = _output_row(raw, 1, configuration)
    assert actual == expected
    actual_parameters = _configuration_parameters(configuration)
    assert actual_parameters[:7] == expected_parameters[:7]
    assert actual_parameters[7].obj == expected_parameters[7].obj  # type: ignore[union-attr]
    assert actual_parameters[8:] == expected_parameters[8:]


def test_canonical_export_streams_one_read_only_query_into_bounded_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    row_count = 73
    statements: list[str] = []
    row_template = _raw(
        prediction_public_id="prediction-0",
        prediction_task="policy",
        prediction_match_count=1,
        is_non_policy=False,
        primary_australian_domain="AU01",
        secondary_australian_domains=["AU02"],
        reasoning="Immutable synthetic prediction.",
        prediction_returned_model=production_configuration()["configured_model"],
        run_public_id="00000000-0000-0000-0000-000000000002",
        run_artifact_sha256="a" * 64,
        run_record_set_sha256="b" * 64,
        provider_job_id="synthetic-batch",
        run_imported_at=datetime(2026, 1, 3, tzinfo=UTC),
    )

    class _Result:
        def fetchone(self) -> dict[str, object]:
            return {
                "public_id": "00000000-0000-0000-0000-000000000001",
                "slug": "synthetic-house",
                "name": "Synthetic House",
                "turn_count": row_count,
            }

    class _Cursor:
        itersize = 0

        def __enter__(self) -> _Cursor:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, statement: str, _parameters: object) -> None:
            statements.append(statement)
            assert statement.lstrip().startswith("WITH matching_runs")
            assert self.itersize == 16

        def __iter__(self) -> Iterator[dict[str, object]]:
            for order in range(1, row_count + 1):
                row = dict(row_template)
                row.update(
                    turn_id=order,
                    turn_key=f"turn-{order:04d}",
                    turn_sequence=order,
                    prediction_public_id=f"prediction-{order}",
                )
                yield row

    class _Connection:
        def __enter__(self) -> _Connection:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, statement: str, _parameters: object = None) -> _Result:
            statements.append(statement)
            normalized = statement.lstrip().upper()
            assert normalized.startswith(
                ("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY", "SELECT")
            )
            return _Result()

        def cursor(self, *, name: str) -> _Cursor:
            assert name == "canonical_full_corpus_ai_export"
            return _Cursor()

    monkeypatch.setattr(
        canonical_ai_module.psycopg, "connect", lambda *_args, **_kwargs: _Connection()
    )
    monkeypatch.setattr(canonical_ai_module, "ROW_BATCH_SIZE", 16)
    monkeypatch.setattr(canonical_ai_module, "check_export_disk_space", lambda *_args: 1)
    output = canonical_ai_module.create_canonical_full_corpus_export(
        _test_settings(tmp_path / "exports"),
        _admin_principal(),
        "00000000-0000-0000-0000-000000000001",
        tmp_path / "streamed-export",
        generated_at=datetime(2026, 1, 4, tzinfo=UTC),
    )

    with output.csv_path.open(encoding="utf-8", newline="") as source:
        csv_rows = list(csv.DictReader(source))
    parquet_rows = pq.read_table(output.parquet_path).to_pylist()  # type: ignore[no-untyped-call]
    assert len(csv_rows) == len(parquet_rows) == row_count
    assert [row["turn_key"] for row in csv_rows] == [
        row["turn_key"] for row in parquet_rows
    ]
    assert [row["corpus_order"] for row in parquet_rows] == list(
        range(1, row_count + 1)
    )
    assert output.manifest["row_count"] == row_count
    assert output.manifest["outcome_counts"][OUTCOME_PREDICTION] == row_count
    assert len(statements) == 3  # read-only setup, metadata SELECT, one cursor SELECT
    assert statements[0].lstrip().upper().startswith("SET TRANSACTION ISOLATION LEVEL")
    assert output.manifest["files"][output.csv_path.name]["sha256"] == hashlib.sha256(
        output.csv_path.read_bytes()
    ).hexdigest()


@pytest.mark.parametrize(
    "row,message",
    [
        (_raw(), "no production outcome"),
        (
            _raw(
                prediction_public_id="prediction",
                prediction_match_count=2,
            ),
            "multiple production predictions",
        ),
        (
            _raw(
                abstention_public_id="refusal",
                abstention_match_count=2,
                resolution_type="PROVIDER_REFUSAL",
                resolution_reason="PROHIBITED_CONTENT",
            ),
            "multiple production refusals",
        ),
        (
            _raw(
                prediction_public_id="prediction",
                prediction_match_count=1,
                abstention_public_id="refusal",
                abstention_match_count=1,
            ),
            "prediction/refusal conflict",
        ),
        (
            _raw(
                text_clean="",
                text_raw="",
                usable_text=False,
                prediction_public_id="prediction",
                prediction_match_count=1,
            ),
            "no-text turn has a model outcome",
        ),
        (
            _raw(
                prediction_public_id="prediction",
                prediction_match_count=1,
                configured_model="gemini-2.5-flash",
            ),
            "non-canonical configured_model",
        ),
        (
            _raw(
                prediction_public_id="prediction",
                prediction_match_count=1,
                prompt_version="phase4a-policy-v1",
            ),
            "non-canonical prompt_version",
        ),
        (
            _raw(
                prediction_public_id="prediction",
                prediction_match_count=1,
                generation_config={"thinking_budget": 0},
            ),
            "non-canonical generation_config",
        ),
        (
            _raw(
                prediction_public_id="prediction",
                prediction_match_count=1,
                prediction_task="hostility",
            ),
            "non-policy prediction task",
        ),
    ],
)
def test_incomplete_duplicate_or_conflicting_layers_fail_closed(
    row: dict[str, object], message: str
) -> None:
    with pytest.raises(CanonicalAIExportError, match=message):
        _output_row(row, 1)


def test_completed_corpus_accounting_shape_reconciles_160577_turns() -> None:
    total_turns = 160_577
    expected_predictions = 160_567
    expected_refusals = 8
    expected_no_usable_text = 2

    counts = {
        OUTCOME_PREDICTION: 0,
        OUTCOME_PROVIDER_REFUSAL: 0,
        OUTCOME_NO_USABLE_TEXT: 0,
    }
    pred_template = _raw(
        prediction_public_id="00000000-0000-0000-0000-000000000010",
        prediction_match_count=1,
        is_non_policy=False,
        primary_australian_domain="AU01",
        secondary_australian_domains=[],
        reasoning="Policy turn.",
    )
    refusal_template = _raw(
        abstention_public_id="00000000-0000-0000-0000-000000000020",
        abstention_match_count=1,
        resolution_type="PROVIDER_REFUSAL",
        resolution_reason="PROHIBITED_CONTENT",
        refusal_provenance={"resolution": "PROVIDER_REFUSAL", "reason": "PROHIBITED_CONTENT"},
    )
    no_text_template = _raw(
        text_clean="",
        text_raw="",
        usable_text=False,
        provider=None,
        configured_model=None,
        run_returned_model=None,
        prompt_version=None,
        prompt_sha256=None,
        canonical_schema_sha256=None,
        prediction_contract_sha256=None,
        provider_schema_sha256=None,
        generation_config=None,
        execution_mode=None,
    )

    for order in range(1, total_turns + 1):
        if order <= expected_predictions:
            raw = {**pred_template, "turn_id": order, "turn_key": f"turn-{order:06d}"}
        elif order <= expected_predictions + expected_refusals:
            raw = {**refusal_template, "turn_id": order, "turn_key": f"turn-{order:06d}"}
        else:
            raw = {**no_text_template, "turn_id": order, "turn_key": f"turn-{order:06d}"}
        out = _output_row(raw, order)
        counts[str(out["outcome_status"])] += 1
        if order in (1, expected_predictions + 1, total_turns):
            rendered = _csv_row(out)
            assert set(rendered.keys()) == set(FIELDS)

    assert counts == {
        OUTCOME_PREDICTION: expected_predictions,
        OUTCOME_PROVIDER_REFUSAL: expected_refusals,
        OUTCOME_NO_USABLE_TEXT: expected_no_usable_text,
    }
    assert sum(counts.values()) == total_turns


def test_zip_bytes_are_deterministic_for_identical_files(tmp_path: Path) -> None:
    first = tmp_path / "a.csv"
    second = tmp_path / "manifest.json"
    first.write_bytes(b"header\nvalue\n")
    second.write_bytes(b'{"generated_at":"fixed"}\n')
    one = tmp_path / "one.zip"
    two = tmp_path / "two.zip"
    _write_deterministic_zip(one, [second, first])
    _write_deterministic_zip(two, [first, second])
    assert one.read_bytes() == two.read_bytes()
    with zipfile.ZipFile(one) as archive:
        assert archive.namelist() == ["a.csv", "manifest.json"]


def test_research_export_ui_has_separate_canonical_action() -> None:
    template = Path(
        "src/hansard_annotator/web/templates/exports/index.html"
    ).read_text(encoding="utf-8")
    assert "Canonical Full-Corpus AI Export" in template
    assert "/exports/canonical-ai/{{ corpus.public_id }}.zip" in template
    assert "never replaces the immutable AI result" in template


def _test_settings(export_work_root: Path, **overrides: object) -> WebSettings:
    defaults: dict[str, object] = {
        "database": DatabaseSettings(
            url="postgresql+psycopg://hansard:test@127.0.0.1:54321/hansard_test",
            processed_data_root=Path("data/processed"),
        ),
        "environment": "test",
        "session_secret": "x" * 32,
        "cookie_secure": False,
        "allowed_hosts": ("testserver", "localhost"),
        "base_url": "http://testserver",
        "idle_minutes": 60,
        "absolute_hours": 12,
        "minimum_password_length": 12,
        "lockout_failures": 5,
        "lockout_minutes": 15,
        "log_level": "INFO",
        "export_work_root": export_work_root,
        "export_min_free_bytes": DEFAULT_EXPORT_MIN_FREE_BYTES,
    }
    defaults.update(overrides)
    return WebSettings(**defaults)  # type: ignore[arg-type]


def _admin_principal() -> Principal:
    from uuid import UUID

    return Principal(
        user_id=1,
        public_id=UUID("00000000-0000-0000-0000-000000000099"),
        username="admin",
        display_name="Admin",
        must_change_password=False,
        is_admin=True,
        session_id=1,
        csrf_token="csrf-token",
    )


def _dummy_request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/exports/canonical-ai/00000000-0000-0000-0000-000000000001.zip",
            "headers": [(b"x-csrf-token", b"csrf-token")],
        }
    )


def test_web_settings_requires_explicit_non_tmp_export_work_root_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HANSARD_APP_ENV", "production")
    monkeypatch.setenv("HANSARD_SESSION_SECRET", "s" * 40)
    monkeypatch.setenv("HANSARD_COOKIE_SECURE", "true")
    monkeypatch.setenv("HANSARD_ALLOWED_HOSTS", "annotator.polisde.tech")
    monkeypatch.setenv(
        "HANSARD_DATABASE_URL",
        "postgresql+psycopg://u:p@postgres:5432/hansard",
    )
    monkeypatch.delenv("HANSARD_EXPORT_WORK_ROOT", raising=False)
    monkeypatch.delenv("HANSARD_EXPORT_MIN_FREE_BYTES", raising=False)

    with pytest.raises(ValueError, match="HANSARD_EXPORT_WORK_ROOT must be explicitly set"):
        WebSettings.from_environment()

    monkeypatch.setenv("HANSARD_EXPORT_WORK_ROOT", "/tmp/exports")
    with pytest.raises(ValueError, match="disk-backed path outside /tmp"):
        WebSettings.from_environment()

    monkeypatch.setenv("HANSARD_EXPORT_WORK_ROOT", "/var/lib/hansard-export-work")
    monkeypatch.setenv("HANSARD_EXPORT_MIN_FREE_BYTES", str(64 * 1024 * 1024))
    with pytest.raises(ValueError, match="HANSARD_EXPORT_MIN_FREE_BYTES"):
        WebSettings.from_environment()

    monkeypatch.delenv("HANSARD_EXPORT_MIN_FREE_BYTES", raising=False)
    configured = WebSettings.from_environment()
    assert configured.export_work_root == Path("/var/lib/hansard-export-work")
    assert configured.export_min_free_bytes == DEFAULT_EXPORT_MIN_FREE_BYTES


def test_free_space_guard_rejects_low_disk_space_and_64mb_thresholds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(CanonicalAIExportError, match="must be at least"):
        check_export_disk_space(tmp_path, 64 * 1024 * 1024)

    monkeypatch.setattr(
        "hansard_annotator.web.research_exports.canonical_ai.shutil.disk_usage",
        lambda _path: os.statvfs_result((0, 0, 0, 0, 0, 0, 0, 0, 0, 0))  # pragma: no cover
        if False
        else type("Usage", (), {"total": 100, "used": 90, "free": 64 * 1024 * 1024})(),
    )
    with pytest.raises(CanonicalAIExportError, match="insufficient free disk space"):
        check_export_disk_space(tmp_path, MINIMUM_EXPORT_MIN_FREE_BYTES)


def test_download_canonical_ai_uses_configured_work_root_and_cleans_up_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    default_temp_dir = tmp_path / "default-temp"
    default_temp_dir.mkdir(parents=True)
    monkeypatch.setattr("tempfile.tempdir", str(default_temp_dir))
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(default_temp_dir))

    work_root = tmp_path / "hansard-export-work"
    campaign_root = tmp_path / "campaign-artifacts"
    campaign_root.mkdir(parents=True)
    persisted_artifact = campaign_root / "campaign-1.jsonl"
    persisted_artifact.write_text("{}", encoding="utf-8")

    sibling_export = work_root / f"{CANONICAL_EXPORT_DIR_PREFIX}concurrent"
    sibling_export.mkdir(parents=True)
    sibling_file = sibling_export / "canonical_full_corpus_ai.zip"
    sibling_file.write_bytes(b"concurrent-in-progress")

    settings = _test_settings(work_root)
    created_dirs: list[Path] = []

    async def _noop_csrf(
        _request: Request, _principal: Principal, _settings: WebSettings
    ) -> None:
        return None

    def _fake_export(
        _settings: WebSettings,
        _principal: Principal,
        _corpus_id: str,
        output_directory: Path,
    ) -> CanonicalAIExport:
        created_dirs.append(output_directory)
        assert output_directory.resolve().parent == work_root.resolve()
        assert output_directory.resolve().parent != default_temp_dir.resolve()
        assert output_directory.name.startswith(CANONICAL_EXPORT_DIR_PREFIX)
        assert list(default_temp_dir.iterdir()) == []
        csv_path = output_directory / "canonical_full_corpus_ai.csv"
        parquet_path = output_directory / "canonical_full_corpus_ai.parquet"
        manifest_path = output_directory / "manifest.json"
        zip_path = output_directory / "canonical_full_corpus_ai.zip"
        for file_path in (csv_path, parquet_path, manifest_path, zip_path):
            file_path.write_bytes(b"ok")
        return CanonicalAIExport(
            directory=output_directory,
            csv_path=csv_path,
            parquet_path=parquet_path,
            manifest_path=manifest_path,
            zip_path=zip_path,
            manifest={"corpus": {"slug": "house-hansard"}},
        )

    monkeypatch.setattr(export_routes, "enforce_csrf", _noop_csrf)
    monkeypatch.setattr(
        export_routes, "create_canonical_full_corpus_export", _fake_export
    )

    response = asyncio.run(
        export_routes.download_canonical_ai(
            _dummy_request(),
            "00000000-0000-0000-0000-000000000001",
            settings,
            _admin_principal(),
        )
    )
    assert len(created_dirs) == 1
    request_dir = created_dirs[0]
    assert request_dir.exists()
    assert Path(response.path) == request_dir / "canonical_full_corpus_ai.zip"
    assert response.background is not None

    asyncio.run(response.background())

    assert not request_dir.exists()
    assert sibling_export.exists()
    assert sibling_file.read_bytes() == b"concurrent-in-progress"
    assert persisted_artifact.exists()
    assert list(default_temp_dir.iterdir()) == []


def test_concurrent_canonical_downloads_use_isolated_work_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work_root = tmp_path / "concurrent-export-work"
    work_root.mkdir()
    settings = _test_settings(work_root)
    barrier = threading.Barrier(2)
    directories: list[Path] = []

    async def _noop_csrf(
        _request: Request, _principal: Principal, _settings: WebSettings
    ) -> None:
        return None

    def _concurrent_export(
        _settings: WebSettings,
        _principal: Principal,
        _corpus_id: str,
        output_directory: Path,
    ) -> CanonicalAIExport:
        directories.append(output_directory)
        barrier.wait(timeout=10)
        csv_path = output_directory / "canonical_full_corpus_ai.csv"
        parquet_path = output_directory / "canonical_full_corpus_ai.parquet"
        manifest_path = output_directory / "manifest.json"
        zip_path = output_directory / "canonical_full_corpus_ai.zip"
        for path in (csv_path, parquet_path, manifest_path, zip_path):
            path.write_bytes(output_directory.name.encode())
        return CanonicalAIExport(
            output_directory,
            csv_path,
            parquet_path,
            manifest_path,
            zip_path,
            {"corpus": {"slug": output_directory.name}},
        )

    monkeypatch.setattr(export_routes, "enforce_csrf", _noop_csrf)
    monkeypatch.setattr(export_routes, "check_export_disk_space", lambda *_args: 1)
    monkeypatch.setattr(
        export_routes, "create_canonical_full_corpus_export", _concurrent_export
    )

    async def _download_both() -> tuple[object, object]:
        return await asyncio.gather(
            export_routes.download_canonical_ai(
                _dummy_request(), "corpus-one", settings, _admin_principal()
            ),
            export_routes.download_canonical_ai(
                _dummy_request(), "corpus-two", settings, _admin_principal()
            ),
        )

    first, second = asyncio.run(_download_both())
    assert len(directories) == 2 and directories[0] != directories[1]
    assert all(directory.parent == work_root for directory in directories)
    assert Path(first.path).parent != Path(second.path).parent  # type: ignore[attr-defined]
    for response in (first, second):
        assert response.background is not None  # type: ignore[attr-defined]
        asyncio.run(response.background())  # type: ignore[attr-defined]
    assert list(work_root.iterdir()) == []


@pytest.mark.parametrize(
    ("raised_error", "expected_status"),
    [
        (CanonicalAIExportError("incomplete layer"), 409),
        (OSError(28, "No space left on device"), 409),
        (LookupError("corpus not found"), 404),
        (PermissionError("admin required"), 403),
    ],
)
def test_download_canonical_ai_exception_cleans_only_request_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raised_error: Exception,
    expected_status: int,
) -> None:
    work_root = tmp_path / "hansard-export-work"
    sibling_export = work_root / f"{CANONICAL_EXPORT_DIR_PREFIX}sibling"
    sibling_export.mkdir(parents=True)
    sibling_marker = sibling_export / "in_progress.tmp"
    sibling_marker.write_bytes(b"keep-me")

    settings = _test_settings(work_root)
    created_dirs: list[Path] = []

    async def _noop_csrf(
        _request: Request, _principal: Principal, _settings: WebSettings
    ) -> None:
        return None

    def _failing_export(
        _settings: WebSettings,
        _principal: Principal,
        _corpus_id: str,
        output_directory: Path,
    ) -> CanonicalAIExport:
        created_dirs.append(output_directory)
        (output_directory / "partial.csv").write_bytes(b"partial")
        raise raised_error

    monkeypatch.setattr(export_routes, "enforce_csrf", _noop_csrf)
    monkeypatch.setattr(
        export_routes, "create_canonical_full_corpus_export", _failing_export
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            export_routes.download_canonical_ai(
                _dummy_request(),
                "00000000-0000-0000-0000-000000000001",
                settings,
                _admin_principal(),
            )
        )

    assert exc_info.value.status_code == expected_status
    assert len(created_dirs) == 1
    assert not created_dirs[0].exists()
    assert sibling_export.exists()
    assert sibling_marker.read_bytes() == b"keep-me"


def test_download_canonical_ai_fails_before_export_when_disk_space_is_low(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work_root = tmp_path / "hansard-export-work"
    settings = _test_settings(work_root)

    async def _noop_csrf(
        _request: Request, _principal: Principal, _settings: WebSettings
    ) -> None:
        return None

    monkeypatch.setattr(export_routes, "enforce_csrf", _noop_csrf)
    monkeypatch.setattr(
        "hansard_annotator.web.research_exports.canonical_ai.shutil.disk_usage",
        lambda _path: type(
            "Usage", (), {"total": 100, "used": 90, "free": 64 * 1024 * 1024}
        )(),
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            export_routes.download_canonical_ai(
                _dummy_request(),
                "00000000-0000-0000-0000-000000000001",
                settings,
                _admin_principal(),
            )
        )

    assert exc_info.value.status_code == 409
    assert "insufficient free disk space" in str(exc_info.value.detail)
    assert list(work_root.iterdir()) == []


def test_cleanup_helper_refuses_paths_outside_work_root(tmp_path: Path) -> None:
    work_root = tmp_path / "hansard-export-work"
    work_root.mkdir()
    outside = tmp_path / f"{CANONICAL_EXPORT_DIR_PREFIX}outside"
    outside.mkdir()
    wrong_prefix = work_root / "campaign-artifacts"
    wrong_prefix.mkdir()

    with pytest.raises(ValueError, match="refusing to remove"):
        cleanup_canonical_export_work_dir(work_root, work_root)
    with pytest.raises(ValueError, match="refusing to remove"):
        cleanup_canonical_export_work_dir(outside, work_root)
    with pytest.raises(ValueError, match="refusing to remove"):
        cleanup_canonical_export_work_dir(wrong_prefix, work_root)

    assert outside.exists()
    assert wrong_prefix.exists()
