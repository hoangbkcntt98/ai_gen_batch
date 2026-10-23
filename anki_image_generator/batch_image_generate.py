"""Queue prompts from Anki tables and generate their images."""
from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import escape
import json
import mimetypes
import os
import re
import sys
import threading
from urllib.parse import quote
import urllib.request
from pathlib import Path
from typing import Any

try:
    from .image_validation import validate_image_data
except ImportError:
    from image_validation import validate_image_data

_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}")
_DEFAULT_TABLES = {"kanji": "anki_kanji", "word": "anki_word"}
_DRIVE_AUTH_LOCK = threading.Lock()


def _image_tag(location: str) -> str:
    return f'<img src="{escape(location, quote=True)}">'


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


def effective_limit(config_limit: Any, cli_limit: int | None) -> int | None:
    value = cli_limit if cli_limit is not None else config_limit
    if value is None or value == "":
        return None
    limit = int(value)
    if limit < 1:
        raise ValueError("Image generation limit must be a positive integer or null.")
    return limit


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


def request_image(prompt: str, config: dict[str, Any]) -> tuple[bytes, str]:
    model = str(config.get("image_model", "")).strip()
    if not model:
        raise RuntimeError("Missing ai.image.model.")
    base = str(config.get("image_api_base", "")).strip().rstrip("/")
    api_key = str(config.get("image_api_key", "")).strip()
    if not base:
        raise RuntimeError("Missing ai.image.api_base.")
    timeout = int(config.get("timeout_seconds", 120))
    payload = {
        "model": model,
        "prompt": prompt,
        "size": str(config.get("image_size", "1024x1024")),
        "n": 1,
        "response_format": "b64_json",
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(
        base + "/images/generations",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = json.loads(response.read().decode("utf-8"))
    items = body.get("data")
    if not isinstance(items, list) or not items:
        raise RuntimeError("Image API returned no data.")
    item = items[0]
    data: bytes
    if item.get("b64_json"):
        encoded = re.sub(r"\s+", "", str(item["b64_json"]))
        if encoded.startswith("data:") and "," in encoded:
            encoded = encoded.split(",", 1)[1]
        data = base64.b64decode(encoded, validate=True)
    elif item.get("url"):
        download = urllib.request.Request(str(item["url"]), method="GET")
        with urllib.request.urlopen(download, timeout=timeout) as response:
            data = response.read()
    else:
        raise RuntimeError("Image API response has neither b64_json nor url.")
    image_format = validate_image_data(data, "Image API response")
    return data, image_format.extension


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

def _finish_job(
    connection: Any,
    job_table: str,
    job_id: int,
    status: str,
    result: str,
) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            f'UPDATE {job_table} SET "status" = %s, "ended_at" = CURRENT_TIMESTAMP, '
            '"response" = CASE WHEN %s = \'error\' THEN %s ELSE NULL END, '
            '"result" = CASE WHEN %s = \'error\' THEN NULL ELSE %s END '
            'WHERE "batch_job_id" = %s',
            (status, status, result[:4000], status, result[:4000], job_id),
        )
    connection.commit()


def _filename(value: str, row_id: int, extension: str) -> str:
    safe_value = re.sub(
        r"[^\w\u3040-\u30ff\u3400-\u9fff-]+",
        "_",
        value,
        flags=re.UNICODE,
    ).strip("_")[:30] or "item"
    return f"{safe_value}_{row_id}{extension}"


def _write_image(path: Path, data: bytes) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def _image_provider(config: dict[str, Any]) -> tuple[str, str]:
    batch_config = config.get("batch", {}).get("image_generate", {})
    provider = str(batch_config.get("upload_provider", "local")).strip().lower()
    if provider not in {"local", "oci", "drive"}:
        raise ValueError(
            "batch.image_generate.upload_provider must be local, oci, or drive."
        )
    base_path = str(batch_config.get("image_base_path", "")).strip()
    if provider != "local" and not base_path:
        raise ValueError(
            f"batch.image_generate.image_base_path is required for {provider}."
        )
    return provider, base_path


def _upload_oci(base_url: str, filename: str, data: bytes, timeout: int) -> str:
    try:
        import requests
    except ImportError as err:
        raise RuntimeError(
            "Missing requests. Install standalone dependencies with "
            "`python -m pip install -r requirements.txt`."
        ) from err
    url = f"{base_url.rstrip('/')}/{quote(filename)}"
    response = requests.put(url, data=data, timeout=timeout)
    if response.status_code not in {200, 201}:
        raise RuntimeError(
            f"OCI upload failed: HTTP {response.status_code}: {response.text[:1000]}"
        )
    return url


def _upload_drive(
    folder_id: str, filename: str, data: bytes, config_path: Path, timeout: int
) -> str:
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaIoBaseUpload
    except ImportError as err:
        raise RuntimeError(
            "Missing Google Drive dependencies. Install standalone dependencies with "
            "`python -m pip install -r requirements.txt`."
        ) from err
    import io

    client_path = config_path.resolve().parent / "client.json"
    token_path = config_path.resolve().parent / "token.json"
    if not client_path.is_file():
        raise RuntimeError(f"Google Drive OAuth file not found: {client_path}")
    scopes = ["https://www.googleapis.com/auth/drive"]
    with _DRIVE_AUTH_LOCK:
        credentials = (
            Credentials.from_authorized_user_file(str(token_path), scopes)
            if token_path.is_file()
            else None
        )
        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        if not credentials or not credentials.valid:
            flow = InstalledAppFlow.from_client_secrets_file(str(client_path), scopes)
            credentials = flow.run_local_server(port=0, timeout_seconds=timeout)
            token_path.write_text(credentials.to_json(), encoding="utf-8")

    service = build("drive", "v3", credentials=credentials, cache_discovery=False)
    uploaded = service.files().create(
        body={"name": filename, "parents": [folder_id]},
        media_body=MediaIoBaseUpload(
            io.BytesIO(data),
            mimetype=mimetypes.guess_type(filename)[0] or "application/octet-stream",
        ),
        fields="id",
    ).execute()
    file_id = uploaded["id"]
    service.permissions().create(
        fileId=file_id,
        body={"type": "anyone", "role": "reader"},
    ).execute()
    return f"https://drive.google.com/uc?export=view&id={quote(file_id)}"


def _store_image(
    provider: str,
    image_base_path: str,
    filename: str,
    data: bytes,
    media_dir: Path,
    config_path: Path,
    config: dict[str, Any],
) -> str:
    if provider == "local":
        media_dir.mkdir(parents=True, exist_ok=True)
        _write_image(media_dir / filename, data)
        return filename
    timeout = int(config.get("timeout_seconds", 120))
    if provider == "oci":
        return _upload_oci(image_base_path, filename, data, timeout)
    return _upload_drive(image_base_path, filename, data, config_path, timeout)


def process_job(
    job: tuple[int, str, int],
    config: dict[str, Any],
    tables: dict[str, str],
    media_dir: Path,
    config_path: Path,
) -> tuple[str, str]:
    job_id, object_type, note_id = job
    schema = str(config.get("pgsql_schema", "public")).strip()
    job_table = _qualified_table(
        schema,
        _identifier(
            config.get("pgsql_batch_job_table", "batch_job"),
            "pgsql.batch_job_table",
        ),
    )
    connection = connect_pgsql(config)
    try:
        image_field = _identifier(config.get("image_field", "Image"), "ai.image.field")
        table = table_for_object_type(object_type, tables)
        mode = "kanji" if table == tables["kanji"] else "word"
        source_field = "RequiredVocabulary" if mode == "kanji" else "Word"
        source_table = _qualified_table(schema, table)
        with connection.cursor() as cursor:
            cursor.execute(
                f'SELECT "id", "ImagePrompt", "{image_field}", "{source_field}" '
                f'FROM {source_table} WHERE "anki_note_id" = %s',
                (note_id,),
            )
            rows = cursor.fetchall()
        if not rows:
            raise RuntimeError(f"Source row {table}.anki_note_id={note_id} was not found.")
        if len(rows) > 1:
            raise RuntimeError(
                f"Multiple rows in {table} have anki_note_id={note_id}; cannot choose a source row."
            )
        source_id, prompt, existing_image, source_value = rows[0]
        if bool(config.get("skip_existing_image", True)) and str(existing_image or "").strip():
            _finish_job(connection, job_table, job_id, "completed", "Skipped: image already exists.")
            return "skipped", "image already exists"
        if not str(prompt or "").strip():
            raise RuntimeError(f"{table}.ImagePrompt is empty.")

        provider, image_base_path = _image_provider(config)
        data, extension = request_image(str(prompt).strip(), config)
        filename = _filename(str(source_value or mode), note_id, extension)
        image_location = _store_image(
            provider, image_base_path, filename, data, media_dir, config_path, config
        )
        image_html = _image_tag(image_location)
        with connection.cursor() as cursor:
            cursor.execute(
                f'UPDATE {source_table} SET "{image_field}" = %s, '
                '"updated_at" = CURRENT_TIMESTAMP WHERE "id" = %s',
                (image_html, source_id),
            )
            if not cursor.rowcount:
                raise RuntimeError(f"Could not update {table}.id={source_id}.")
        _finish_job(connection, job_table, job_id, "completed", image_location)
        return "completed", image_location
    except Exception as err:
        connection.rollback()
        _finish_job(connection, job_table, job_id, "error", str(err))
        return "error", str(err)
    finally:
        connection.close()


def run(
    config: dict[str, Any],
    limit: int | None,
    media_dir: Path,
    dry_run: bool = False,
    step: int = 1,
    recovery: bool = False,
    clean_error: bool = True,
    config_path: Path | None = None,
) -> int:
    schema = _identifier(config.get("pgsql_schema", "public"), "pgsql.schema")
    tables = table_names(config)
    job_table = _qualified_table(
        schema, _identifier(config.get("pgsql_batch_job_table", "batch_job"), "pgsql.batch_job_table")
    )
    job_type = str(config.get("image_batch_job_type", "image_generate")).strip()
    if not job_type:
        raise ValueError("batch.image_generate.job_type must not be empty.")
    _image_provider(config)
    connection = connect_pgsql(config)
    try:
        image_field = _identifier(config.get("image_field", "Image"), "ai.image.field")
        skip_existing_image = bool(config.get("skip_existing_image", True))
        if dry_run:
            eligible = count_eligible_prompt_rows(
                connection,
                job_table,
                schema,
                tables,
                job_type,
                image_field,
                skip_existing_image,
            )
            total = eligible if limit is None else min(eligible, limit)
            print(f"Eligible prompt notes: {eligible}")
            print(f"Notes to process this run: {total}")
            return 0
        if clean_error and not recovery:
            deleted = clean_error_jobs(connection, job_table)
            print(f"Error history rows deleted: {deleted}")
        elif clean_error and recovery:
            print("Error cleanup skipped during recovery mode.")
        pid = os.getpid()
        if recovery:
            jobs = recover_jobs(connection, job_table, job_type, pid, limit)
            print(f"History jobs recovered: {len(jobs)}")
        else:
            jobs = create_history_jobs(
                connection,
                job_table,
                schema,
                tables,
                job_type,
                image_field,
                skip_existing_image,
                limit,
                pid,
            )
            print(f"History jobs created: {len(jobs)}")
        print(f"Processing notes: {len(jobs)}")
        if not jobs:
            return 0
        succeeded = skipped = errors = 0
        with ThreadPoolExecutor(max_workers=step) as executor:
            futures = {
                executor.submit(
                    process_job,
                    job,
                    config,
                    tables,
                    media_dir,
                    config_path or Path(__file__).resolve().with_name("config.json"),
                ): job
                for job in jobs
            }
            results = {
                futures[future]: future.result()
                for future in as_completed(futures)
            }
        for index, job in enumerate(jobs, start=1):
            status, result = results[job]
            if status == "completed":
                succeeded += 1
            elif status == "skipped":
                skipped += 1
            else:
                errors += 1
                print(f"Error job {job[0]}: {result}", file=sys.stderr)
            print(f"[{index}/{len(jobs)}] {status}: job {job[0]} ({job[1]}:{job[2]})")
        print(f"Completed: {succeeded}; skipped: {skipped}; error: {errors}.")
        print(f"Media: {media_dir}")
        return 1 if errors else 0
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    batch_dir = Path(__file__).resolve().parent
    default_config = batch_dir / "config.json"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=default_config)
    parser.add_argument("--limit", type=int, help="Override batch.image_generate.limit.")
    parser.add_argument("--step", type=int, default=1, help="Run this many jobs concurrently.")
    parser.add_argument("--recovery", action="store_true", help="Retry eligible error jobs.")
    clean_group = parser.add_mutually_exclusive_group()
    clean_group.add_argument(
        "--clean-error",
        dest="clean_error",
        action="store_true",
        help="Delete all error history rows before a normal run.",
    )
    clean_group.add_argument(
        "--no-clean-error",
        dest="clean_error",
        action="store_false",
        help="Keep error history rows.",
    )
    parser.set_defaults(clean_error=None)
    parser.add_argument("--media-dir", type=Path, help="Override Anki collection.media directory.")
    parser.add_argument("--dry-run", action="store_true", help="Count eligible prompt notes without changing data.")
    args = parser.parse_args(argv)
    try:
        raw_config = json.loads(args.config.read_text(encoding="utf-8-sig"))
        if not isinstance(raw_config, dict):
            raise ValueError("Config must be a JSON object.")
        config = raw_config
        batch_config = config.get("batch", {}).get("image_generate", {})
        limit = effective_limit(batch_config.get("limit"), args.limit)
        clean_error = args.clean_error
        if clean_error is None:
            clean_error = batch_config.get("clean_error", True)
        if not isinstance(clean_error, bool):
            raise ValueError("batch.image_generate.clean_error must be true or false.")
        if args.step < 1:
            raise ValueError("--step must be a positive integer.")
        provider, _image_base_path = _image_provider(config)
        configured_media_dir = str(batch_config.get("media_dir", "")).strip()
        media_value = args.media_dir or (
            Path(configured_media_dir) if configured_media_dir else None
        )
        if media_value is None:
            if args.dry_run or provider != "local":
                media_value = Path.cwd()
            else:
                raise ValueError(
                    "Set batch.image_generate.media_dir to Anki's collection.media directory "
                    "or pass --media-dir."
                )
        media_dir = media_value.expanduser()
        if not media_dir.is_absolute():
            media_dir = (args.config.resolve().parent / media_dir).resolve()
        config["image_batch_job_type"] = batch_config.get("job_type", "image_generate")
        return run(
            config,
            limit,
            media_dir,
            args.dry_run,
            args.step,
            args.recovery,
            clean_error,
            args.config,
        )
    except Exception as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
