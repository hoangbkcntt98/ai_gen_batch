"""Copy image files from the configured media directory to OCI Object Storage."""
from __future__ import annotations

import argparse
import json
import mimetypes
from pathlib import Path
import sys
from urllib.parse import quote

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}


def upload_media(config: dict, config_path: Path, media_override: Path | None) -> int:
    batch_config = config.get("services", {}).get(
        "image_generator", config.get("batch", {}).get("image_generate", {})
    )
    if str(batch_config.get("upload_provider", "local")).strip().lower() != "oci":
        raise ValueError("services.image_generator.upload_provider must be oci.")
    base_url = str(batch_config.get("image_base_path", "")).strip()
    if not base_url.startswith(("https://", "http://")):
        raise ValueError(
            "services.image_generator.image_base_path must be an OCI pre-authenticated request URL."
        )

    configured_dir = str(batch_config.get("media_dir", "")).strip()
    directory = media_override or (Path(configured_dir) if configured_dir else None)
    if directory is None:
        raise ValueError("Set services.image_generator.media_dir or pass --media-dir.")
    directory = directory.expanduser()
    if not directory.is_absolute():
        directory = (config_path.resolve().parent / directory).resolve()
    if not directory.is_dir():
        raise ValueError(f"Media directory not found: {directory}")

    try:
        import requests
    except ImportError as err:
        raise RuntimeError(
            "Missing requests. Install dependencies with "
            "`python -m pip install -r requirements.txt`."
        ) from err

    images = sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
    )
    timeout = int(config.get("timeout_seconds", 120))
    for image_path in images:
        url = f"{base_url.rstrip('/')}/{quote(image_path.name)}"
        response = requests.put(
            url,
            data=image_path.read_bytes(),
            headers={
                "Content-Type": mimetypes.guess_type(image_path.name)[0]
                or "application/octet-stream"
            },
            timeout=timeout,
        )
        if response.status_code not in {200, 201}:
            raise RuntimeError(
                f"OCI upload failed for {image_path.name}: HTTP "
                f"{response.status_code}: {response.text[:1000]}"
            )
        print(f"Uploaded: {image_path.name}")
    print(f"OCI upload complete: {len(images)} image(s) from {directory}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"))
    parser.add_argument("--media-dir", type=Path, help="Override services.image_generator.media_dir.")
    args = parser.parse_args(argv)
    try:
        config = json.loads(args.config.read_text(encoding="utf-8-sig"))
        if not isinstance(config, dict):
            raise ValueError("Config must be a JSON object.")
        return upload_media(config, args.config, args.media_dir)
    except Exception as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
