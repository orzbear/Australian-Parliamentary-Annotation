"""CLI entry point for the repeat-safe Phase 4E orchestration tick."""

from __future__ import annotations

import argparse
import json
import time

import psycopg

from hansard_annotator.web.campaigns.phase4e import orchestration_tick
from hansard_annotator.web.config import WebSettings


def _active_campaign_ids(settings: WebSettings) -> list[str]:
    with psycopg.connect(settings.database.psycopg_url) as connection:
        rows = connection.execute(
            """
            SELECT public_id::text FROM ai_annotation_campaigns
            WHERE orchestration_mode='CHUNKED' AND authorized_at IS NOT NULL
              AND orchestration_status IN ('RUNNING','MAIN_COMPLETE')
            ORDER BY id
            """
        ).fetchall()
    return [str(row[0]) for row in rows]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Advance a Phase 4E campaign")
    parser.add_argument("campaign_id", nargs="?", help="optional logical campaign public UUID")
    parser.add_argument("--watch", action="store_true", help="run ticks continuously")
    parser.add_argument("--interval-seconds", type=float, default=15.0)
    arguments = parser.parse_args(argv)
    settings = WebSettings.from_environment()
    while True:
        campaign_ids = (
            [arguments.campaign_id]
            if arguments.campaign_id
            else _active_campaign_ids(settings)
        )
        for campaign_id in campaign_ids:
            result = orchestration_tick(settings, campaign_id)
            print(json.dumps(result.__dict__, sort_keys=True), flush=True)
        if not arguments.watch:
            return 0
        time.sleep(max(1.0, arguments.interval_seconds))


if __name__ == "__main__":
    raise SystemExit(main())
