"""Database queue and history operations for image-generation jobs."""
from __future__ import annotations

import re
from typing import Any

_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}")
_DEFAULT_TABLES = {"kanji": "anki_kanji", "word": "anki_word"}

def _identifier(value: Any, label: str) -> str:
    name = str(value or "").strip()
    if not _IDENTIFIER_RE.fullmatch(name):
        raise ValueError(f"{label} must be an ASCII identifier of 1-63 characters.")
    return name


def _qualified_table(schema: str, table: str) -> str:
    return f'"{_identifier(schema, "pgsql_schema")}"."{_identifier(table, "table")}"'


def table_names(config: dict[str, Any]) -> dict[str, str]:
    tables = {
        "kanji": _identifier(config.get("pgsql_kanji_table", _DEFAULT_TABLES["kanji"]), "pgsql.kanji_table"),
        "word": _identifier(config.get("pgsql_word_table", _DEFAULT_TABLES["word"]), "pgsql.word_table"),
    }
    if tables["kanji"] == tables["word"]:
        raise ValueError("pgsql.kanji_table and pgsql.word_table must be different.")
    return tables


def table_for_object_type(object_type: str, tables: dict[str, str]) -> str:
    normalized = str(object_type or "").strip().casefold()
    aliases = {
        "kanji": tables["kanji"],
        "anki_kanji": tables["kanji"],
        tables["kanji"].casefold(): tables["kanji"],
        "word": tables["word"],
        "anki_word": tables["word"],
        tables["word"].casefold(): tables["word"],
    }
    try:
        return aliases[normalized]
    except KeyError as err:
        raise ValueError(f"Unsupported batch job object_type: {object_type!r}.") from err


def connect_pgsql(config: dict[str, Any]) -> Any:
    options: dict[str, Any] = {}
    for key, parameter in (
        ("pgsql_host", "host"),
        ("pgsql_user", "user"),
        ("pgsql_password", "password"),
        ("pgsql_database", "dbname"),
    ):
        value = config.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Missing configuration: {key}.")
        options[parameter] = value if parameter == "password" else value.strip()
    port = int(config.get("pgsql_port", 5432))
    timeout = int(config.get("pgsql_connect_timeout", 15))
    if not 1 <= port <= 65535 or timeout < 1:
        raise ValueError("pgsql_port must be 1-65535 and pgsql_connect_timeout must be positive.")
    ssl_mode = str(config.get("pgsql_ssl_mode", "verify-full")).strip().lower()
    if ssl_mode not in {"disable", "require", "verify-ca", "verify-full"}:
        raise ValueError("pgsql_ssl_mode must be disable, require, verify-ca, or verify-full.")
    options.update(port=port, connect_timeout=timeout, sslmode=ssl_mode)
    ssl_ca = str(config.get("pgsql_ssl_ca", "")).strip()
    if ssl_ca:
        options["sslrootcert"] = ssl_ca
    try:
        import psycopg
    except ImportError as err:
        raise RuntimeError(
            "Missing psycopg. Install standalone dependencies with "
            "`python -m pip install -r requirements.txt`."
        ) from err
    return psycopg.connect(**options)


def claim_jobs(
    connection: Any,
    job_table: str,
    job_type: str,
    limit: int | None,
) -> list[tuple[int, str, int]]:
    with connection.cursor() as cursor:
        query = (
            f'SELECT "batch_job_id", "object_type", "id" FROM {job_table} '
            'WHERE "status" = %s AND "job_type" = %s '
            'ORDER BY "batch_job_id"'
        )
        parameters: tuple[Any, ...] = ("pending", job_type)
        if limit is not None:
            query += " LIMIT %s"
            parameters += (limit,)
        query += " FOR UPDATE SKIP LOCKED"
        cursor.execute(query, parameters)
        rows = cursor.fetchall()
        if rows:
            cursor.executemany(
                f'UPDATE {job_table} SET "status" = %s, "started_at" = CURRENT_TIMESTAMP, '
                '"ended_at" = NULL, "response" = NULL, "result" = NULL '
                'WHERE "batch_job_id" = %s',
                [("processing", int(row[0])) for row in rows],
            )
    connection.commit()
    return [(int(row[0]), str(row[1]), int(row[2])) for row in rows]


def recover_jobs(
    connection: Any,
    job_table: str,
    job_type: str,
    pid: int,
    limit: int | None,
) -> list[tuple[int, str, int]]:
    try:
        with connection.cursor() as cursor:
            query = (
                f'SELECT job."batch_job_id", job."object_type", job."id" '
                f'FROM {job_table} job WHERE job."status" = %s AND job."job_type" = %s '
                f'AND NOT EXISTS (SELECT 1 FROM {job_table} completed '
                'WHERE completed."job_type" = job."job_type" '
                'AND completed."object_type" = job."object_type" AND completed."id" = job."id" '
                'AND completed."status" = %s) ORDER BY job."batch_job_id"'
            )
            parameters: tuple[Any, ...] = ("error", job_type, "completed")
            if limit is not None:
                query += " LIMIT %s"
                parameters += (limit,)
            query += " FOR UPDATE OF job SKIP LOCKED"
            cursor.execute(query, parameters)
            rows = cursor.fetchall()
            if rows:
                cursor.executemany(
                    f'UPDATE {job_table} SET "status" = %s, "started_at" = CURRENT_TIMESTAMP, '
                    '"ended_at" = NULL, "response" = NULL, "result" = NULL, '
                    '"locked_by_task_id" = %s '
                    'WHERE "batch_job_id" = %s',
                    [("processing", pid, int(row[0])) for row in rows],
                )
        connection.commit()
        return [(int(row[0]), str(row[1]), int(row[2])) for row in rows]
    except Exception:
        connection.rollback()
        raise

def clean_error_jobs(connection: Any, job_table: str) -> int:
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f'DELETE FROM {job_table} WHERE "status" = %s',
                ("error",),
            )
            deleted = int(cursor.rowcount)
        connection.commit()
        return deleted
    except Exception:
        connection.rollback()
        raise

def _eligible_source_query(
    source_table: str,
    job_table: str,
    image_field: str,
    skip_existing_image: bool,
    job_type: str,
    object_type: str,
) -> str:
    conditions = ['COALESCE(BTRIM(source."ImagePrompt"), \'\') <> \'\'']
    conditions.append('source."anki_note_id" IS NOT NULL')
    if skip_existing_image:
        conditions.append(f'COALESCE(BTRIM(source."{image_field}"), \'\') = \'\'')
    conditions.append(
        f'NOT EXISTS (SELECT 1 FROM {job_table} history '
        'WHERE history."job_type" = %s AND history."object_type" = %s '
        'AND history."id" = source."anki_note_id" '
        'AND history."status" IN (\'completed\', \'processing\'))'
    )
    return " AND ".join(conditions)

def _source_tables(tables: dict[str, str]) -> tuple[tuple[str, str, str], ...]:
    return (
        ("kanji", tables["kanji"], "kanji"),
        ("word", tables["word"], "word"),
    )

def count_eligible_prompt_rows(
    connection: Any,
    job_table: str,
    schema: str,
    tables: dict[str, str],
    job_type: str,
    image_field: str,
    skip_existing_image: bool,
) -> int:
    total = 0
    for _mode, table_name, object_type in _source_tables(tables):
        source_table = _qualified_table(schema, table_name)
        conditions = _eligible_source_query(
            source_table, job_table, image_field, skip_existing_image, job_type, object_type
        )
        with connection.cursor() as cursor:
            cursor.execute(
                f'SELECT COUNT(*) FROM {source_table} source WHERE {conditions}',
                (job_type, object_type),
            )
            total += int(cursor.fetchone()[0])
    return total

def create_history_jobs(
    connection: Any,
    job_table: str,
    schema: str,
    tables: dict[str, str],
    job_type: str,
    image_field: str,
    skip_existing_image: bool,
    limit: int | None,
    pid: int,
) -> list[tuple[int, str, int]]:
    image_field = _identifier(image_field, "ai.image.field")
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"{job_table}:{job_type}",),
            )
            jobs: list[tuple[int, str, int]] = []
            remaining = limit
            for _mode, table_name, object_type in _source_tables(tables):
                if remaining == 0:
                    break
                source_table = _qualified_table(schema, table_name)
                conditions = _eligible_source_query(
                    source_table,
                    job_table,
                    image_field,
                    skip_existing_image,
                    job_type,
                    object_type,
                )
                query = (
                    f'INSERT INTO {job_table} '
                    '("job_type", "object_type", "id", "status", "started_at", "locked_by_task_id") '
                    f'SELECT %s, %s, source."anki_note_id", %s, CURRENT_TIMESTAMP, %s '
                    f'FROM {source_table} source '
                    f"WHERE {conditions} ORDER BY source.\"id\""
                )
                parameters: tuple[Any, ...] = (
                    job_type,
                    object_type,
                    "processing",
                    pid,
                    job_type,
                    object_type,
                )
                if remaining is not None:
                    query += " LIMIT %s"
                    parameters += (remaining,)
                query += ' RETURNING "batch_job_id", "object_type", "id"'
                cursor.execute(query, parameters)
                new_jobs = cursor.fetchall()
                jobs.extend(
                    (int(row[0]), str(row[1]), int(row[2]))
                    for row in new_jobs
                )
                if remaining is not None:
                    remaining -= len(new_jobs)
        connection.commit()
        return jobs
    except Exception:
        connection.rollback()
        raise


class JobManager:
    """Own database configuration and queue operations for image jobs."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.schema = _identifier(config.get("pgsql_schema", "public"), "pgsql.schema")
        self.tables = table_names(config)
        self.job_table = _qualified_table(
            self.schema,
            _identifier(
                config.get("pgsql_batch_job_table", "batch_job"),
                "pgsql.batch_job_table",
            ),
        )

    def connect(self) -> Any:
        return connect_pgsql(self.config)

    def count_eligible(
        self,
        connection: Any,
        job_type: str,
        image_field: str,
        skip_existing_image: bool,
    ) -> int:
        return count_eligible_prompt_rows(
            connection,
            self.job_table,
            self.schema,
            self.tables,
            job_type,
            image_field,
            skip_existing_image,
        )

    def create_history(
        self,
        connection: Any,
        job_type: str,
        image_field: str,
        skip_existing_image: bool,
        limit: int | None,
        pid: int,
    ) -> list[tuple[int, str, int]]:
        return create_history_jobs(
            connection,
            self.job_table,
            self.schema,
            self.tables,
            job_type,
            image_field,
            skip_existing_image,
            limit,
            pid,
        )

    def claim_pending(
        self,
        connection: Any,
        job_type: str,
        limit: int | None,
    ) -> list[tuple[int, str, int]]:
        return claim_jobs(connection, self.job_table, job_type, limit)

    def recover(
        self, connection: Any, job_type: str, pid: int, limit: int | None
    ) -> list[tuple[int, str, int]]:
        return recover_jobs(connection, self.job_table, job_type, pid, limit)

    def clean_errors(self, connection: Any) -> int:
        return clean_error_jobs(connection, self.job_table)
