"""Generate and store images for individual Anki notes."""
from __future__ import annotations

import base64
from html import escape
import json
import mimetypes
import re
import threading
from urllib.parse import quote
import urllib.request
from pathlib import Path
from typing import Any

try:
    from helpers.image_validation import validate_image_data
except ImportError:
    from ..helpers.image_validation import validate_image_data

_DRIVE_AUTH_LOCK = threading.Lock()

def _image_tag(location: str) -> str:
    return f'<img src="{escape(location, quote=True)}">'

def request_image(prompt: str, config: dict[str, Any]) -> tuple[bytes, str]:
    settings = config.get("services", {}).get("image_generator", {})
    model = str(settings.get("image_model", config.get("image_model", ""))).strip()
    if not model:
        raise RuntimeError("Missing ai.image.model.")
    base = str(settings.get("image_api_base", config.get("image_api_base", ""))).strip().rstrip("/")
    api_key = str(settings.get("image_api_key", config.get("image_api_key", ""))).strip()
    if not base:
        raise RuntimeError("Missing ai.image.api_base.")
    timeout = int(settings.get("timeout_seconds", config.get("timeout_seconds", 120)))
    payload = {
        "model": model,
        "prompt": prompt,
        "size": str(settings.get("image_size", config.get("image_size", "1024x1024"))),
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
    batch_config = config.get("services", {}).get(
        "image_generator", config.get("batch", {}).get("image_generate", {})
    )
    provider = str(batch_config.get("upload_provider", "local")).strip().lower()
    if provider not in {"local", "oci", "drive"}:
        raise ValueError(
            "services.image_generator.upload_provider must be local, oci, or drive."
        )
    base_path = str(batch_config.get("image_base_path", "")).strip()
    if provider != "local" and not base_path:
        raise ValueError(
            f"services.image_generator.image_base_path is required for {provider}."
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
    settings = config.get("services", {}).get("image_generator", {})
    timeout = int(settings.get("timeout_seconds", config.get("timeout_seconds", 120)))
    if provider == "oci":
        return _upload_oci(image_base_path, filename, data, timeout)
    return _upload_drive(image_base_path, filename, data, config_path, timeout)



class ImageGenerator:
    def __init__(self, config: dict[str, Any], config_path: Path | None = None) -> None:
        self.config = config
        self.config_path = config_path or Path(__file__).resolve().parents[1] / "config.json"

    def generate_for_note(
        self,
        prompt: str,
        identity: str,
        note_id: int,
        media_dir: Path,
    ) -> str:
        """Generate and store one image for an Anki note, returning its HTML field value."""
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
