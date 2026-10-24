"""Download image objects from OCI Object Storage into Anki's media directory."""
from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath
import sys
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import requests

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}


def _settings(config: dict[str, Any]) -> dict[str, Any]:
    settings = config.get("services", {}).get("image_generator", {})
    if str(settings.get("upload_provider", "local")).strip().lower() != "oci":
        raise ValueError("services.image_generator.upload_provider must be oci.")
    base_url = str(settings.get("image_base_path", "")).strip()
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(
            "services.image_generator.image_base_path must be an OCI PAR URL."
        )
    return settings


def _media_dir(
    settings: dict[str, Any], config_path: Path, override: Path | None
) -> Path:
    configured = str(settings.get("media_dir", "")).strip()
    directory = override or (Path(configured) if configured else None)
    if directory is None:
        raise ValueError("Set services.image_generator.media_dir or pass --media-dir.")
    directory = directory.expanduser()
    if not directory.is_absolute():
        directory = config_path.resolve().parent / directory
    return directory.resolve()


def _object_endpoint_and_prefix(base_url: str) -> tuple[str, str]:
    """Return PAR object endpoint and configured object-name prefix."""
    parsed = urlsplit(base_url)
    marker = "/o/"
    position = parsed.path.find(marker)
    if position < 0:
        raise ValueError("OCI PAR URL path must contain '/o/'.")

    endpoint_path = parsed.path[: position + len(marker)]
    prefix = parsed.path[position + len(marker) :]
    if prefix:
        prefix = prefix.rstrip("/") + "/"
    endpoint = urlunsplit(
        (parsed.scheme, parsed.netloc, endpoint_path, parsed.query, "")
    )
    return endpoint, prefix


def _list_objects(
    endpoint_url: str, prefix: str, timeout: int
) -> list[str]:
    """List objects under prefix; PAR must allow object listing."""
    object_names: list[str] = []
    start: str | None = None
    seen_starts: set[str] = set()

    while True:
        params: dict[str, str] = {"fields": "name"}
        if prefix:
            params["prefix"] = prefix
        if start:
            params["start"] = start
        try:
            response = requests.get(endpoint_url, params=params, timeout=timeout)
        except requests.RequestException as err:
            # Avoid printing request exception text: it may contain the PAR token.
            raise RuntimeError(f"OCI listing request failed ({type(err).__name__}).") from None
        if response.status_code != 200:
            if response.status_code == 404:
                raise RuntimeError(
                    "OCI listing failed: HTTP 404. Check that this PAR is valid, "
                    "not expired or revoked, and allows object listing."
                )
            raise RuntimeError(f"OCI listing failed: HTTP {response.status_code}.")
        try:
            payload = response.json()
        except ValueError as err:
            raise RuntimeError("OCI listing returned invalid JSON.") from err
        if not isinstance(payload, dict):
            raise RuntimeError("OCI listing response must be a JSON object.")
        items = payload.get("objects", [])
        if not isinstance(items, list):
            raise RuntimeError("OCI listing response has invalid 'objects' data.")
        object_names.extend(
            str(item["name"])
            for item in items
            if isinstance(item, dict) and item.get("name")
        )

        next_start = payload.get("nextStartWith")
        if not next_start or str(next_start) in seen_starts:
            break
        start = str(next_start)
        seen_starts.add(start)

    return object_names


def _safe_object_path(media_dir: Path, object_name: str) -> Path:
    relative = PurePosixPath(object_name)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ValueError(f"Unsafe OCI object name: {object_name!r}")
    root = media_dir.resolve()
    target = root.joinpath(*relative.parts)
    if not target.resolve().is_relative_to(root):
        raise ValueError(f"OCI object path escapes media directory: {object_name!r}")
    return target


def download_objects(
    config: dict[str, Any],
    config_path: Path,
    media_override: Path | None = None,
    overwrite: bool = False,
) -> dict[str, int]:
    settings = _settings(config)
    base_url = str(settings["image_base_path"]).strip()
    endpoint_url, prefix = _object_endpoint_and_prefix(base_url)
    timeout = int(settings.get("timeout_seconds", 120))
    media_dir = _media_dir(settings, config_path, media_override)
    media_dir.mkdir(parents=True, exist_ok=True)

    object_names = _list_objects(endpoint_url, prefix, timeout)
    downloaded = skipped = failed = 0
    for object_name in object_names:
        if Path(object_name).suffix.lower() not in IMAGE_EXTENSIONS:
            skipped += 1
            continue
        try:
            relative_name = (
                object_name[len(prefix) :]
                if prefix and object_name.startswith(prefix)
                else object_name
            )
            target = _safe_object_path(media_dir, relative_name)
            if target.exists() and not overwrite:
                skipped += 1
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            object_url = endpoint_url + quote(object_name, safe="/")
            try:
                response = requests.get(object_url, timeout=timeout)
            except requests.RequestException as err:
                raise RuntimeError(
                    f"download request failed ({type(err).__name__})"
                ) from None
            if response.status_code != 200:
                raise RuntimeError(f"HTTP {response.status_code}")
            temporary = target.with_name(target.name + ".download.tmp")
            temporary.write_bytes(response.content)
            temporary.replace(target)
            downloaded += 1
            print(f"Downloaded: {object_name}")
        except Exception as err:
            failed += 1
            print(f"Failed: {object_name}: {err}", file=sys.stderr)

    print(
        f"OCI download complete: listed={len(object_names)}; "
        f"downloaded={downloaded}; skipped={skipped}; failed={failed}; "
        f"media_dir={media_dir}"
    )
    return {
        "listed": len(object_names),
        "downloaded": downloaded,
        "skipped": skipped,
        "failed": failed,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "config.json",
    )
    parser.add_argument(
        "--media-dir",
        type=Path,
        help="Override services.image_generator.media_dir.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace files that already exist in media_dir.",
    )
    args = parser.parse_args(argv)
    try:
        config = json.loads(args.config.read_text(encoding="utf-8-sig"))
        if not isinstance(config, dict):
            raise ValueError("Config must be a JSON object.")
        result = download_objects(config, args.config, args.media_dir, args.overwrite)
        return 1 if result["failed"] else 0
    except Exception as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
