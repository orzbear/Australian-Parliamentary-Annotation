"""Administrator-operated Phase 3 account CLI."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import secrets
import string
import sys
from typing import Any

import psycopg
from psycopg.rows import dict_row

from hansard_annotator.web.auth.models import Principal
from hansard_annotator.web.auth.service import (
    change_password,
    create_user,
    revoke_all_sessions,
    set_user_enabled,
)
from hansard_annotator.web.config import WebSettings
from hansard_annotator.web.projects.service import create_project, transition_project
from hansard_annotator.web.tasks.schemas import SelectionCriteria
from hansard_annotator.web.tasks.service import generate_batch


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="hansard-web")
    commands = result.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create-user")
    create.add_argument("username")
    create.add_argument("--display-name", required=True)
    create.add_argument("--email")
    create.add_argument("--admin", action="store_true")
    create.add_argument("--no-force-change", action="store_true")
    create.add_argument("--generate-password", action="store_true")
    create.add_argument("--password-env")
    reset = commands.add_parser("reset-password")
    reset.add_argument("username")
    reset.add_argument("--generate-password", action="store_true")
    reset.add_argument("--password-env")
    for name in ("disable-user", "enable-user", "revoke-sessions"):
        command = commands.add_parser(name)
        command.add_argument("username")
    commands.add_parser("list-users")
    seed = commands.add_parser("seed-development")
    seed.add_argument("--admin-username", required=True)
    seed.add_argument("--project-slug", default="phase3-interface-pilot")
    seed.add_argument("--project-name", default="Phase 3 interface pilot")
    seed.add_argument("--task-count", type=int, default=50)
    seed.add_argument("--seed", type=int, default=20260724)
    prototype = commands.add_parser("seed-conference-prototype")
    prototype.add_argument("--admin-username", required=True)
    prototype.add_argument("--task-count", type=int, default=50)
    prototype.add_argument("--seed", type=int, default=20260810)
    return result


def _password(arguments: argparse.Namespace, minimum: int) -> tuple[str, bool]:
    if arguments.password_env:
        value = os.getenv(arguments.password_env, "")
        if not value:
            raise ValueError(f"{arguments.password_env} is empty or missing")
        return value, False
    if arguments.generate_password:
        alphabet = string.ascii_letters + string.digits + "-_!@"
        value = "".join(secrets.choice(alphabet) for _ in range(max(minimum, 20)))
        return value, True
    first = getpass.getpass("Password: ")
    second = getpass.getpass("Confirm password: ")
    if first != second:
        raise ValueError("passwords do not match")
    return first, False


def _user(settings: WebSettings, username: str) -> dict[str, Any]:
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        row = connection.execute(
            "SELECT id,public_id,username,status FROM users WHERE lower(username)=lower(%s)",
            (username,),
        ).fetchone()
    if row is None:
        raise ValueError("user does not exist")
    return dict(row)


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        settings = WebSettings.from_environment()
        if arguments.command == "create-user":
            password, generated = _password(
                arguments, settings.minimum_password_length
            )
            user = create_user(
                settings,
                arguments.username,
                arguments.display_name,
                password,
                email=arguments.email,
                admin=arguments.admin,
                must_change_password=not arguments.no_force_change,
            )
            output: dict[str, object] = {
                "ok": True,
                "username": user["username"],
                "public_id": user["public_id"],
                "admin": arguments.admin,
            }
            if generated:
                output["temporary_password"] = password
            _json(output)
            return 0
        if arguments.command == "list-users":
            with psycopg.connect(
                settings.database.psycopg_url, row_factory=dict_row
            ) as connection:
                rows = connection.execute(
                    """
                    SELECT public_id,username,email,display_name,status,
                           must_change_password,last_login_at
                    FROM users ORDER BY lower(username)
                    """
                ).fetchall()
            _json([dict(row) for row in rows])
            return 0
        if arguments.command == "seed-development":
            if settings.environment == "production":
                raise ValueError("development seeding is forbidden in production")
            admin_row = _admin_principal(
                settings, arguments.admin_username
            )
            with psycopg.connect(
                settings.database.psycopg_url, row_factory=dict_row
            ) as connection:
                existing = connection.execute(
                    "SELECT id,status FROM projects WHERE slug=%s",
                    (arguments.project_slug,),
                ).fetchone()
                pins = connection.execute(
                    """
                    SELECT c.id AS corpus_id,pr.id AS run_id,asv.id AS schema_id
                    FROM corpora c
                    JOIN preprocessing_runs pr
                      ON pr.id=c.current_preprocessing_run_id
                    JOIN annotation_schemas ans
                      ON ans.slug='australian_policy_annotation'
                    JOIN annotation_schema_versions asv
                      ON asv.annotation_schema_id=ans.id AND asv.version='0.1.0'
                    WHERE c.slug='australian-house-representatives-hansard'
                      AND pr.run_name='phase1-full-20260723-v4'
                      AND pr.pipeline_version='1.0.0'
                    """
                ).fetchone()
            if pins is None:
                raise ValueError("accepted corpus and draft Phase 3 schema must be loaded")
            if existing is None:
                project = create_project(
                    settings,
                    admin_row,
                    slug=arguments.project_slug,
                    name=arguments.project_name,
                    description="Development-only browser and workflow acceptance.",
                    mode="development",
                    corpus_id=pins["corpus_id"],
                    preprocessing_run_id=pins["run_id"],
                    annotation_schema_version_id=pins["schema_id"],
                )
                project_id = int(str(project["id"]))
                batch = generate_batch(
                    settings,
                    admin_row,
                    project_id,
                    name="Deterministic interface acceptance",
                    criteria=SelectionCriteria(
                        minimum_words=50,
                        maximum_words=1200,
                        exclude_orphan_continuations=True,
                        limit=arguments.task_count,
                        seed=arguments.seed,
                    ),
                )
                transition_project(settings, admin_row, project_id, "active")
            else:
                project_id = int(existing["id"])
                batch = {"reused": True}
            _json(
                {
                    "ok": True,
                    "project_id": project_id,
                    "project_slug": arguments.project_slug,
                    "batch_reused": batch.get("reused", False),
                    "development_only": True,
                }
            )
            return 0
        if arguments.command == "seed-conference-prototype":
            if settings.environment == "production":
                raise ValueError("development seeding is forbidden in production")
            admin_row = _admin_principal(settings, arguments.admin_username)
            with psycopg.connect(
                settings.database.psycopg_url, row_factory=dict_row
            ) as connection:
                pins = connection.execute(
                    """
                    SELECT c.id AS corpus_id,pr.id AS run_id,
                           general.id AS general_schema_id,
                           aukus.id AS aukus_schema_id
                    FROM corpora c
                    JOIN preprocessing_runs pr
                      ON pr.id=c.current_preprocessing_run_id
                    JOIN annotation_schemas general_schema
                      ON general_schema.slug='australian_policy_annotation'
                    JOIN annotation_schema_versions general
                      ON general.annotation_schema_id=general_schema.id
                     AND general.version='0.2.0'
                    JOIN annotation_schemas aukus_schema
                      ON aukus_schema.slug='australian_aukus_screen'
                    JOIN annotation_schema_versions aukus
                      ON aukus.annotation_schema_id=aukus_schema.id
                     AND aukus.version='0.1.0'
                    WHERE c.slug='australian-house-representatives-hansard'
                      AND pr.run_name='phase1-full-20260723-v4'
                      AND pr.pipeline_version='1.0.0'
                    """
                ).fetchone()
                projects = {
                    row["slug"]: dict(row)
                    for row in connection.execute(
                        """
                        SELECT id,slug,status FROM projects
                        WHERE slug IN (
                          'conference-general-pass','conference-aukus-pass'
                        )
                        """
                    ).fetchall()
                }
            if pins is None:
                raise ValueError("conference prototype schemas must be loaded first")
            general = projects.get("conference-general-pass")
            if general is None:
                created_general = create_project(
                    settings,
                    admin_row,
                    slug="conference-general-pass",
                    name="Conference general policy pass",
                    description=(
                        "Streamlined policy-by-default general-domain annotation."
                    ),
                    mode="development",
                    corpus_id=pins["corpus_id"],
                    preprocessing_run_id=pins["run_id"],
                    annotation_schema_version_id=pins["general_schema_id"],
                )
                general_id = int(str(created_general["id"]))
                generate_batch(
                    settings,
                    admin_row,
                    general_id,
                    name="Conference general sample",
                    criteria=SelectionCriteria(
                        minimum_words=50,
                        maximum_words=1200,
                        exclude_orphan_continuations=True,
                        limit=arguments.task_count,
                        seed=arguments.seed,
                    ),
                )
                transition_project(settings, admin_row, general_id, "active")
            else:
                general_id = int(str(general["id"]))
            aukus = projects.get("conference-aukus-pass")
            if aukus is None:
                created_aukus = create_project(
                    settings,
                    admin_row,
                    slug="conference-aukus-pass",
                    name="Conference AUKUS second pass",
                    description=(
                        "AUKUS-only screen derived from submitted AU12 general labels."
                    ),
                    mode="development",
                    corpus_id=pins["corpus_id"],
                    preprocessing_run_id=pins["run_id"],
                    annotation_schema_version_id=pins["aukus_schema_id"],
                    source_project_id=general_id,
                )
                aukus_id = int(str(created_aukus["id"]))
                transition_project(settings, admin_row, aukus_id, "active")
            else:
                aukus_id = int(str(aukus["id"]))
            _json(
                {
                    "ok": True,
                    "general_project_id": general_id,
                    "aukus_project_id": aukus_id,
                    "general_task_count_requested": arguments.task_count,
                    "aukus_tasks": (
                        "Generate after AU12 general submissions are available"
                    ),
                    "development_only": True,
                }
            )
            return 0
        user = _user(settings, arguments.username)
        if arguments.command == "reset-password":
            password, generated = _password(
                arguments, settings.minimum_password_length
            )
            change_password(
                settings,
                int(str(user["id"])),
                password,
                actor_user_id=int(str(user["id"])),
                force_change=True,
            )
            output = {"ok": True, "username": user["username"]}
            if generated:
                output["temporary_password"] = password
            _json(output)
        elif arguments.command == "disable-user":
            user_id = int(str(user["id"]))
            set_user_enabled(settings, user_id, False, user_id)
            _json({"ok": True, "username": user["username"], "status": "disabled"})
        elif arguments.command == "enable-user":
            user_id = int(str(user["id"]))
            set_user_enabled(settings, user_id, True, user_id)
            _json({"ok": True, "username": user["username"], "status": "active"})
        elif arguments.command == "revoke-sessions":
            count = revoke_all_sessions(settings, int(str(user["id"])), None)
            _json({"ok": True, "username": user["username"], "revoked": count})
        return 0
    except Exception as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1


def _json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str, sort_keys=True))


def _admin_principal(settings: WebSettings, username: str) -> Principal:
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        row = connection.execute(
            """
            SELECT u.id,u.public_id,u.username,u.display_name,u.must_change_password
            FROM users u JOIN user_global_roles ugr ON ugr.user_id=u.id
            JOIN global_roles gr ON gr.id=ugr.global_role_id
            WHERE lower(u.username)=lower(%s) AND u.status='active'
              AND gr.role_key='admin'
            """,
            (username,),
        ).fetchone()
    if row is None:
        raise ValueError("active administrator does not exist")
    return Principal(
        user_id=row["id"],
        public_id=row["public_id"],
        username=row["username"],
        display_name=row["display_name"],
        must_change_password=row["must_change_password"],
        is_admin=True,
        session_id=0,
        csrf_token="cli-not-a-browser-session",
    )


if __name__ == "__main__":
    raise SystemExit(main())
