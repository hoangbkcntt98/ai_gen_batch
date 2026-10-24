"""Generate and store images for queued Anki notes."""
from __future__ import annotations

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
    from models import ImageJob
    from services.job_manager import (
        _identifier, _qualified_table, table_for_object_type, table_names, JobManager,
    )
    from helpers.image_validation import validate_image_data
except ImportError:
    from ..models import ImageJob
    from .job_manager import (
        _identifier, _qualified_table, table_for_object_type, table_names, JobManager,
    )
    from ..helpers.image_validation import validate_image_data

_DRIVE_AUTH_LOCK = threading.Lock()

def _image_tag(location: str) -> str:
    return f'<img src="{escape(location, quote=True)}">'

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
    job: ImageJob,
    config: dict[str, Any],
    tables: dict[str, str],
    media_dir: Path,
    config_path: Path,
) -> tuple[str, str]:
    job_id, object_type, note_id = job.job_id, job.object_type, job.note_id
    schema = str(config.get("pgsql_schema", "public")).strip()
    job_table = _qualified_table(
        schema,
        _identifier(
            config.get("pgsql_batch_job_table", "batch_job"),
            "pgsql.batch_job_table",
        ),
    )
    connection = JobManager(config).connect()
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


class ImageGenerator:
    def __init__(self, config: dict[str, Any], config_path: Path | None = None) -> None:
        self.config = config
        self.config_path = config_path or Path(__file__).resolve().parents[1] / "config.json"

    def process(self, job: ImageJob, media_dir: Path) -> tuple[str, str]:
        return process_job(job, self.config, table_names(self.config), media_dir, self.config_path)

    def generate_for_note(
        self,
        prompt: str,
        identity: str,
        note_id: int,
        media_dir: Path,
    ) -> str:
        """Generate and store one image outside PostgreSQL batch queue."""
        provider, image_base_path = _image_provider(self.config)
        data, extension = request_image(prompt, self.config)
        filename = _filename(identity, note_id, extension)
        location = _store_image(
            provider,
            image_base_path,
            filename,
            data,
            media_dir,
            self.config_path,
            self.config,
        )
        return _image_tag(location)

    def run(
        self, media_dir: Path, limit: int | None = None, dry_run: bool = False,
        workers: int = 1, recovery: bool = False, clean_error: bool = True,
    ) -> int:
        return run(
            self.config, limit, media_dir, dry_run, workers, recovery, clean_error, self.config_path
        )

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
    job_manager = JobManager(config)
    tables = job_manager.tables
    job_type = str(config.get("image_batch_job_type", "image_generate")).strip()
    if not job_type:
        raise ValueError("batch.image_generate.job_type must not be empty.")
    _image_provider(config)
    connection = job_manager.connect()
    try:
        image_field = _identifier(config.get("image_field", "Image"), "ai.image.field")
        skip_existing_image = bool(config.get("skip_existing_image", True))
        if dry_run:
            eligible = job_manager.count_eligible(
                connection, job_type, image_field, skip_existing_image
            )
            total = eligible if limit is None else min(eligible, limit)
            print(f"Eligible prompt notes: {eligible}")
            print(f"Notes to process this run: {total}")
            return 0
        if clean_error and not recovery:
            deleted = job_manager.clean_errors(connection)
            print(f"Error history rows deleted: {deleted}")
        elif clean_error and recovery:
            print("Error cleanup skipped during recovery mode.")
        pid = os.getpid()
        if recovery:
            jobs = job_manager.recover(connection, job_type, pid, limit)
            print(f"History jobs recovered: {len(jobs)}")
        else:
            jobs = job_manager.create_history(
                connection,
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
                    ImageJob(*job),
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


