"""Reusable MUSE2API video generation and local media handling."""

import logging
import os
import shutil
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse, urlunparse

import requests

LOGGER = logging.getLogger(__name__)


def normalize_api_root(base_url):
    root = base_url.strip().rstrip("/")
    if not root:
        raise ValueError("MUSE2API_BASE_URL cannot be empty")
    return root if root.endswith("/v1") else f"{root}/v1"


def safe_filename_part(value):
    value = "".join(
        char if char.isalnum() or char in "._-" else "_"
        for char in str(value)
    ).strip("._")
    return value or "pattern"


def response_error(response):
    try:
        payload = response.json()
    except ValueError:
        return response.text[:1000]
    error = payload.get("error")
    return str(error if error is not None else payload)[:1000]


def create_video_task(session, api_root, prompt, duration, size, request_timeout):
    response = session.post(
        f"{api_root}/videos",
        json={"prompt": prompt, "duration": duration, "size": size},
        timeout=request_timeout,
    )
    if not response.ok:
        raise RuntimeError(
            f"MUSE2API create task failed ({response.status_code}): "
            f"{response_error(response)}"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("MUSE2API create task returned invalid JSON") from exc
    task_id = payload.get("id") or payload.get("task_id")
    if not task_id:
        raise RuntimeError(f"MUSE2API response has no id/task_id: {payload}")
    return str(task_id)


def wait_for_video_task(
    session, api_root, task_id, poll_interval, timeout, request_timeout
):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = session.get(
            f"{api_root}/videos/{task_id}",
            timeout=request_timeout,
        )
        if not response.ok:
            raise RuntimeError(
                f"MUSE2API poll failed ({response.status_code}): "
                f"{response_error(response)}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError("MUSE2API poll returned invalid JSON") from exc
        status = str(payload.get("status", "")).lower()
        LOGGER.info("task_id=%s status=%s", task_id, status or "unknown")
        if status in {"completed", "succeeded"}:
            return payload
        if status in {"failed", "error"}:
            error = payload.get("error") or payload.get("message") or payload
            raise RuntimeError(f"MUSE2API task {status}: {error}")
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(poll_interval, remaining))
    raise TimeoutError(f"MUSE2API task timed out after {timeout} seconds")


def extract_video_result(payload):
    result = payload.get("result")
    if isinstance(result, list):
        result = result[0] if result else {}
    if not isinstance(result, dict):
        result = {}
    filename = result.get("filename")
    raw_url = result.get("url") or payload.get("url")
    if not filename and raw_url:
        filename = Path(urlparse(str(raw_url)).path).name
    if not filename:
        raise RuntimeError("Completed task response has no result filename or URL")
    filename = Path(unquote(str(filename))).name
    if filename in {"", ".", ".."}:
        raise RuntimeError("Completed task returned an unsafe or empty filename")
    return filename, raw_url


def normalize_media_url(raw_url, api_root):
    if not raw_url:
        return None
    raw_url = str(raw_url)
    parsed = urlparse(raw_url)
    if parsed.scheme and parsed.netloc:
        path = parsed.path.replace("/v1/v1/", "/v1/")
        if path.endswith("/v1/v1"):
            path = path[:-3]
        return urlunparse(parsed._replace(path=path))
    relative_path = raw_url.lstrip("/")
    if relative_path.startswith("v1/"):
        relative_path = relative_path[3:]
    return urljoin(f"{api_root}/", relative_path)


def wait_for_media(filename, media_dir, timeout, poll_interval):
    source = Path(media_dir) / filename
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if source.is_file():
            return source
        time.sleep(min(poll_interval, max(0, deadline - time.monotonic())))
    raise TimeoutError(f"Media file did not appear: {source}")


def copy_media(source, output_root, item, run_date=None, delete_source=False):
    source = Path(source)
    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    run_date = run_date or datetime.now().strftime("%y%m%d")
    basename = (
        f"{safe_filename_part(item['pattern_id'])}_"
        f"{safe_filename_part(item['pattern'])}_{run_date}.mp4"
    )
    destination = output_root / basename
    temporary = destination.with_name(destination.name + ".tmp")
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
        if delete_source and source.resolve() != destination.resolve():
            source.unlink()
    finally:
        if temporary.exists():
            temporary.unlink()
    return destination


def download_media(session, raw_url, api_root, output_root, item, request_timeout):
    media_url = normalize_media_url(raw_url, api_root)
    if not media_url:
        raise RuntimeError("Completed task has no media URL and media_dir is unset")
    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    run_date = datetime.now().strftime("%y%m%d")
    basename = (
        f"{safe_filename_part(item['pattern_id'])}_"
        f"{safe_filename_part(item['pattern'])}_{run_date}.mp4"
    )
    destination = output_root / basename
    temporary = destination.with_name(destination.name + ".tmp")
    try:
        with session.get(
            media_url, timeout=request_timeout, stream=True
        ) as response:
            if not response.ok:
                raise RuntimeError(
                    f"MUSE2API media download failed ({response.status_code}): "
                    f"{response_error(response)}"
                )
            with temporary.open("wb") as output_file:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        output_file.write(chunk)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return destination


def generate_video(
    prompt,
    item,
    api_root,
    api_key,
    media_dir,
    output_root,
    duration,
    size,
    task_poll_interval,
    task_timeout,
    media_wait_timeout,
    media_poll_interval,
    request_timeout,
    delete_source_after_copy,
    on_task_created=None,
):
    """Generate video, wait for local media, copy it, and return result metadata."""
    with requests.Session() as session:
        session.headers.update(
            {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
        )
        task_id = create_video_task(
            session, api_root, prompt, duration, size, request_timeout
        )
        if on_task_created:
            on_task_created(task_id)
        payload = wait_for_video_task(
            session,
            api_root,
            task_id,
            task_poll_interval,
            task_timeout,
            request_timeout,
        )
        filename, raw_url = extract_video_result(payload)
        if media_dir:
            source = wait_for_media(
                filename, media_dir, media_wait_timeout, media_poll_interval
            )
            destination = copy_media(
                source,
                output_root,
                item,
                delete_source=delete_source_after_copy,
            )
        else:
            destination = download_media(
                session, raw_url, api_root, output_root, item, request_timeout
            )
    return {
        "task_id": task_id,
        "video_url": normalize_media_url(raw_url, api_root),
        "local_path": str(destination),
        "video_filename": destination.name,
    }
