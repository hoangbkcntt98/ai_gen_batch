"""Create one missing JLPT N1 grammar example video per run.

Requirements: Python 3.11+, ``requests``, and ``psycopg[binary]``.
Configure batch settings and credentials in ``config.json``.
Environment variables can override ``database_url``, ``base_url``, and
``api_key`` from config.

Safe preview:
    python3 grammar_example_video_batch.py --dry-run

Run one job:
    python3 grammar_example_video_batch.py

The dry run reads PostgreSQL and prints the selected example and prompt. It does
not update the database, call MUSE2API, or copy media.
"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

from helper.google_drive_helper import upload_to_google_drive
from helper.video_generation_helper import generate_video, normalize_api_root


CONFIG_FILE = Path(__file__).with_name("config.json")
PROJECT_DIR = CONFIG_FILE.parent
DATABASE_NAME = None
SCHEMA = None
PATTERNS_TABLE = None
EXAMPLES_TABLE = None
TABLE = None
MEDIA_DIR = None
OUTPUT_ROOT = None
RETRY_DELAY = None
ADVISORY_LOCK_NAMESPACE = None
ADVISORY_LOCK_ID = None
BATCH_CONFIG = None
VIDEO_ID_TYPES = {}

LOGGER = logging.getLogger("grammar_example_video_batch")


def require_env(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Required environment variable is missing: {name}")
    return value


def load_config():
    try:
        with CONFIG_FILE.open(encoding="utf-8-sig") as config_file:
            config = json.load(config_file)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read config file {CONFIG_FILE}: {exc}") from exc

    batch = config.get("grammar_video_batch")
    if not isinstance(batch, dict):
        raise ValueError("config.json must define a grammar_video_batch object")
    required = (
        "output_dir",
        "delete_source_after_copy",
        "duration",
        "size",
        "task_poll_interval_seconds",
        "task_timeout_seconds",
        "media_wait_seconds",
        "media_poll_interval_seconds",
        "http_connect_timeout_seconds",
        "http_read_timeout_seconds",
        "retry_delay_minutes",
        "database_name",
        "schema",
        "patterns_table",
        "examples_table",
        "videos_table",
        "advisory_lock_namespace",
        "advisory_lock_id",
    )
    missing = [key for key in required if key not in batch]
    if missing:
        raise ValueError(
            "Missing grammar_video_batch config values: " + ", ".join(missing)
        )
    if not isinstance(batch.get("upload_to_google_drive", False), bool):
        raise ValueError(
            "grammar_video_batch.upload_to_google_drive must be true or false"
        )

    global DATABASE_NAME, SCHEMA, PATTERNS_TABLE, EXAMPLES_TABLE, TABLE
    global MEDIA_DIR, OUTPUT_ROOT, RETRY_DELAY
    global ADVISORY_LOCK_NAMESPACE, ADVISORY_LOCK_ID, BATCH_CONFIG
    global VIDEO_ID_TYPES
    BATCH_CONFIG = batch
    DATABASE_NAME = batch["database_name"]
    SCHEMA = batch["schema"]
    PATTERNS_TABLE = batch["patterns_table"]
    EXAMPLES_TABLE = batch["examples_table"]
    TABLE = batch["videos_table"]
    media_dir = batch.get("media_dir")
    MEDIA_DIR = Path(media_dir) if media_dir else None
    OUTPUT_ROOT = Path(batch["output_dir"])
    RETRY_DELAY = f"{batch['retry_delay_minutes']} minutes"
    ADVISORY_LOCK_NAMESPACE = batch["advisory_lock_namespace"]
    ADVISORY_LOCK_ID = batch["advisory_lock_id"]
    return config


def make_video_prompt(pattern, example_ja, example_vi, note):
    return (
        "2D anime educational video for Japanese JLPT N1 grammar pattern "
        f"{pattern}. Clearly act out this exact example in a natural, "
        f"easy-to-understand context: {example_ja}. "
        f"Meaning for the learner: {example_vi}. Context note: {note}. "
        "Use expressive anime characters, a clear beginning-middle-end visual "
        "story, smooth camera movement, warm cinematic lighting, vertical "
        "social-media composition, no logos, no watermark. Show the Japanese "
        f"example as a clean subtitle: {example_ja}. Add Japanese voiceover "
        f"reading only this exact Japanese sentence: {example_ja}. Do not "
        "speak, narrate, or include any Vietnamese voiceover; the Vietnamese "
        "meaning and context note are visual guidance only and must not be "
        "read aloud."
    )


def connect_database(database_url):
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError(
            "PostgreSQL driver missing. Install with: "
            "python3 -m pip install 'psycopg[binary]'"
        ) from exc

    parsed_database = unquote(urlparse(database_url).path.lstrip("/"))
    if parsed_database != DATABASE_NAME:
        raise ValueError(
            f"DATABASE_URL must target database {DATABASE_NAME!r}; "
            f"got {parsed_database!r}"
        )
    return psycopg.connect(database_url)


def ensure_table(connection, create=True):
    global VIDEO_ID_TYPES
    with connection.cursor() as cursor:
        if create:
            cursor.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
            cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {SCHEMA}.{TABLE} (
                    id BIGSERIAL PRIMARY KEY,
                    pattern_id TEXT NOT NULL,
                    example_id TEXT NOT NULL,
                    pattern TEXT NOT NULL,
                    example_ja TEXT NOT NULL,
                    example_vi TEXT NOT NULL DEFAULT '',
                    task_id TEXT,
                    video_url TEXT,
                    local_path TEXT,
                    video_filename TEXT,
                    status TEXT NOT NULL,
                    error TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            for column_definition in (
                "id BIGSERIAL",
                "pattern_id TEXT",
                "example_id TEXT",
                "pattern TEXT",
                "example_ja TEXT",
                "example_vi TEXT DEFAULT ''",
                "task_id TEXT",
                "video_url TEXT",
                "local_path TEXT",
                "video_filename TEXT",
                "status TEXT",
                "error TEXT",
                "created_at TIMESTAMPTZ DEFAULT NOW()",
                "updated_at TIMESTAMPTZ DEFAULT NOW()",
            ):
                cursor.execute(
                    f"ALTER TABLE {SCHEMA}.{TABLE} "
                    f"ADD COLUMN IF NOT EXISTS {column_definition}"
                )
            cursor.execute(
                f"""
                CREATE UNIQUE INDEX IF NOT EXISTS
                    grammar_example_videos_pattern_example_uidx
                ON {SCHEMA}.{TABLE} (pattern_id, example_id)
                """
            )
        else:
            cursor.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.tables
                    WHERE table_schema = %s AND table_name = %s
                )
                """,
                (SCHEMA, TABLE),
            )
            table_exists = cursor.fetchone()[0]
        if create:
            table_exists = True
        cursor.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = %s
                  AND table_name = %s
                  AND column_name = 'note'
            )
            """,
            (SCHEMA, EXAMPLES_TABLE),
        )
        has_example_note = cursor.fetchone()[0]
        if has_example_note:
            note_expression = "COALESCE(e.note, '')"
        else:
            cursor.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema = %s
                      AND table_name = %s
                      AND column_name = 'note'
                )
                """,
                (SCHEMA, PATTERNS_TABLE),
            )
            note_expression = "COALESCE(p.note, '')" if cursor.fetchone()[0] else "''"
        cursor.execute(
            """
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = %s
              AND table_name = %s
              AND column_name IN ('pattern_id', 'example_id')
            """,
            (SCHEMA, TABLE),
        )
        VIDEO_ID_TYPES = dict(cursor.fetchall())
    connection.commit()
    return note_expression, table_exists


def acquire_advisory_lock(connection):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_try_advisory_lock(%s, %s)",
            (ADVISORY_LOCK_NAMESPACE, ADVISORY_LOCK_ID),
        )
        acquired = cursor.fetchone()[0]
    connection.commit()
    return acquired


def select_candidate(connection, note_expression, table_exists=True):
    if table_exists:
        eligibility = f"""
          AND (
              NOT EXISTS (
                  SELECT 1
                  FROM {SCHEMA}.{TABLE} AS v
                  WHERE v.pattern_id::text = p.id::text
                    AND v.example_id::text = e.id::text
              )
              OR EXISTS (
                  SELECT 1
                  FROM {SCHEMA}.{TABLE} AS v
                  WHERE v.pattern_id::text = p.id::text
                    AND v.example_id::text = e.id::text
                    AND v.status = 'failed'
                    AND v.updated_at <= NOW() - INTERVAL '{RETRY_DELAY}'
              )
          )
          AND NOT EXISTS (
              SELECT 1
              FROM {SCHEMA}.{TABLE} AS v
              WHERE v.pattern_id::text = p.id::text
                AND v.example_id::text = e.id::text
                AND v.status IN ('completed', 'processing')
          )
        """
    else:
        eligibility = ""
    query = f"""
        SELECT
            p.id AS pattern_id,
            p.pattern::text AS pattern,
            e.id AS example_id,
            e.example_ja::text AS example_ja,
            COALESCE(e.example_vi::text, '') AS example_vi,
            {note_expression}::text AS note
        FROM {SCHEMA}.{EXAMPLES_TABLE} AS e
        JOIN {SCHEMA}.{PATTERNS_TABLE} AS p ON p.id = e.pattern_id
        WHERE e.display_order = 1
        {eligibility}
        ORDER BY p.id, e.id
        LIMIT 1
        FOR UPDATE OF e SKIP LOCKED
    """
    with connection.cursor() as cursor:
        cursor.execute(query)
        row = cursor.fetchone()
    if row is None:
        connection.commit()
        return None
    fields = ("pattern_id", "pattern", "example_id", "example_ja", "example_vi", "note")
    item = dict(zip(fields, row))
    for column in ("pattern_id", "example_id"):
        if VIDEO_ID_TYPES.get(column) in {"smallint", "integer", "bigint"}:
            item[column] = int(item[column])
        elif VIDEO_ID_TYPES.get(column) in {"text", "character varying", "character"}:
            item[column] = str(item[column])
    item["pattern"] = item["pattern"] or ""
    item["example_ja"] = item["example_ja"] or ""
    item["example_vi"] = item["example_vi"] or ""
    item["note"] = item["note"] or ""
    return item


def mark_processing(connection, item):
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            INSERT INTO {SCHEMA}.{TABLE} AS existing (
                pattern_id, example_id, pattern, example_ja, example_vi,
                task_id, video_url, local_path, video_filename, status, error,
                created_at, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, NULL, NULL, NULL, NULL, 'processing',
                    NULL, NOW(), NOW())
            ON CONFLICT (pattern_id, example_id) DO UPDATE SET
                pattern = EXCLUDED.pattern,
                example_ja = EXCLUDED.example_ja,
                example_vi = EXCLUDED.example_vi,
                task_id = NULL,
                video_url = NULL,
                local_path = NULL,
                video_filename = NULL,
                status = 'processing',
                error = NULL,
                updated_at = NOW()
            WHERE existing.status = 'failed'
              AND existing.updated_at <= NOW() - INTERVAL '{RETRY_DELAY}'
            RETURNING id
            """,
            (
                item["pattern_id"],
                item["example_id"],
                item["pattern"],
                item["example_ja"],
                item["example_vi"],
            ),
        )
        result = cursor.fetchone()
    connection.commit()
    return result is not None


def update_status(connection, item, status, **values):
    allowed = {"task_id", "video_url", "local_path", "video_filename", "error"}
    updates = {key: value for key, value in values.items() if key in allowed}
    assignments = ["status = %s", "updated_at = NOW()"]
    params = [status]
    for key, value in updates.items():
        assignments.append(f"{key} = %s")
        params.append(value)
    params.extend((item["pattern_id"], item["example_id"]))
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            UPDATE {SCHEMA}.{TABLE}
            SET {", ".join(assignments)}
            WHERE pattern_id::text = %s::text
              AND example_id::text = %s::text
            """,
            [*params[:-2], str(params[-2]), str(params[-1])],
        )
    connection.commit()


def resolve_project_path(configured_path):
    path = Path(configured_path).expanduser()
    return path if path.is_absolute() else PROJECT_DIR / path

def upload_video_to_drive(result, drive_config):
    folder_id = drive_config.get("folder_id")
    if not folder_id:
        raise ValueError("Set google_drive.folder_id in config.json")

    return upload_to_google_drive(
        result["local_path"],
        folder_id,
        resolve_project_path(
            drive_config.get("service_account_file", "account_service.json")
        ),
        auth_type=drive_config.get("auth_type", "oauth"),
        oauth_client_file=resolve_project_path(
            drive_config.get("oauth_client_file", "client.json")
        ),
        token_file=resolve_project_path(
            drive_config.get("token_file", "token.json")
        ),
    )

def process_item(connection, item, api_root, api_key, drive_config):
    prompt = make_video_prompt(
        item["pattern"],
        item["example_ja"],
        item["example_vi"],
        item["note"],
    )
    LOGGER.info(
        "pattern_id=%s pattern=%s example_id=%s status=processing",
        item["pattern_id"],
        item["pattern"],
        item["example_id"],
    )
    if not mark_processing(connection, item):
        LOGGER.info(
            "pattern_id=%s example_id=%s status=idle reason=record_changed",
            item["pattern_id"],
            item["example_id"],
        )
        return False

    task_id = None
    try:
        def record_task(task_id_value):
            nonlocal task_id
            task_id = task_id_value
            update_status(connection, item, "processing", task_id=task_id)
            LOGGER.info(
                "pattern_id=%s pattern=%s example_id=%s task_id=%s status=processing",
                item["pattern_id"],
                item["pattern"],
                item["example_id"],
                task_id,
            )

        result = generate_video(
            prompt,
            item,
            api_root,
            api_key,
            MEDIA_DIR,
            OUTPUT_ROOT,
            duration=BATCH_CONFIG["duration"],
            size=BATCH_CONFIG["size"],
            task_poll_interval=BATCH_CONFIG["task_poll_interval_seconds"],
            task_timeout=BATCH_CONFIG["task_timeout_seconds"],
            media_wait_timeout=BATCH_CONFIG["media_wait_seconds"],
            media_poll_interval=BATCH_CONFIG["media_poll_interval_seconds"],
            request_timeout=(
                BATCH_CONFIG["http_connect_timeout_seconds"],
                BATCH_CONFIG["http_read_timeout_seconds"],
            ),
            delete_source_after_copy=BATCH_CONFIG["delete_source_after_copy"],
            on_task_created=record_task,
        )
        update_status(
            connection,
            item,
            "completed",
            task_id=result["task_id"],
            video_url=result["video_url"],
            local_path=result["local_path"],
            video_filename=result["video_filename"],
            error=None,
        )
        LOGGER.info(
            "pattern_id=%s pattern=%s example_id=%s task_id=%s status=completed "
            "local_path=%s",
            item["pattern_id"],
            item["pattern"],
            item["example_id"],
            task_id,
            result["local_path"],
        )
        if BATCH_CONFIG.get("upload_to_google_drive", False):
            try:
                uploaded = upload_video_to_drive(result, drive_config)
                LOGGER.info(
                    "pattern_id=%s example_id=%s task_id=%s "
                    "drive_status=uploaded drive_file_id=%s drive_name=%s "
                    "drive_url=%s",
                    item["pattern_id"],
                    item["example_id"],
                    task_id,
                    uploaded.get("id"),
                    uploaded.get("name"),
                    uploaded.get("webViewLink"),
                )
            except Exception:
                LOGGER.exception(
                    "pattern_id=%s example_id=%s task_id=%s "
                    "drive_status=failed; video remains completed",
                    item["pattern_id"],
                    item["example_id"],
                    task_id,
                )
        else:
            LOGGER.info(
                "pattern_id=%s example_id=%s drive_status=skipped reason=disabled",
                item["pattern_id"],
                item["example_id"],
            )
        return True
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        try:
            update_status(connection, item, "failed", task_id=task_id, error=error)
        except Exception:
            LOGGER.exception(
                "Could not mark failed: pattern_id=%s example_id=%s task_id=%s",
                item["pattern_id"],
                item["example_id"],
                task_id,
            )
        LOGGER.error(
            "pattern_id=%s pattern=%s example_id=%s task_id=%s status=failed error=%s",
            item["pattern_id"],
            item["pattern"],
            item["example_id"],
            task_id,
            error,
        )
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Select one eligible example and print its prompt without writing or uploading",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    connection = None
    lock_acquired = False
    try:
        config = load_config()
        database_url = os.environ.get("DATABASE_URL") or config.get("database_url")
        if not database_url:
            raise RuntimeError(
                "Set database_url in config.json or DATABASE_URL in environment"
            )
        base_url = os.environ.get("MUSE2API_BASE_URL") or config.get("base_url")
        api_root = normalize_api_root(base_url) if base_url else None
        api_key = None
        if not args.dry_run:
            api_key = os.environ.get("MUSE2API_KEY") or config.get("api_key")
            if not api_key:
                raise RuntimeError(
                    "Set api_key in config.json or MUSE2API_KEY in environment"
                )
        connection = connect_database(database_url)
        lock_acquired = acquire_advisory_lock(connection)
        if not lock_acquired:
            LOGGER.info("status=idle reason=another_batch_process_holds_lock")
            return 0

        note_expression, table_exists = ensure_table(
            connection,
            create=not args.dry_run,
        )
        item = select_candidate(connection, note_expression, table_exists)
        if item is None:
            LOGGER.info("status=idle reason=no_eligible_example")
            return 0

        prompt = make_video_prompt(
            item["pattern"],
            item["example_ja"],
            item["example_vi"],
            item["note"],
        )
        if args.dry_run:
            LOGGER.info(
                "pattern_id=%s pattern=%s example_id=%s status=dry_run",
                item["pattern_id"],
                item["pattern"],
                item["example_id"],
            )
            print(prompt)
            connection.rollback()
            return 0

        process_item(
            connection,
            item,
            api_root,
            api_key,
            config.get("google_drive", {}),
        )
        return 0
    except Exception:
        LOGGER.exception("Batch failed")
        return 1
    finally:
        if connection is not None:
            if lock_acquired:
                try:
                    connection.rollback()
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "SELECT pg_advisory_unlock(%s, %s)",
                            (ADVISORY_LOCK_NAMESPACE, ADVISORY_LOCK_ID),
                        )
                    connection.commit()
                except Exception:
                    LOGGER.exception("Could not release PostgreSQL advisory lock")
            connection.close()


if __name__ == "__main__":
    sys.exit(main())
