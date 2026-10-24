"""Replace PAR URLs in Anki Image fields with local filenames."""
from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath
import re
import sys
from typing import Any
from urllib.parse import unquote, urlsplit

from services import AnkiConnector

SRC_ATTRIBUTE = re.compile(r"""(\bsrc\s*=\s*)(["'])(.*?)\2""", re.IGNORECASE)


def _image_base_url(config: dict[str, Any]) -> str:
    settings = config.get("services", {}).get("image_generator", {})
    base_url = str(settings.get("image_base_path", "")).strip()
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(
            "services.image_generator.image_base_path must be an OCI PAR URL."
        )
    return base_url.rstrip("/") + "/"


def _is_under_base(url: str, base_url: str) -> bool:
    candidate, base = urlsplit(url), urlsplit(base_url)
    return (
        candidate.scheme == base.scheme
        and candidate.netloc == base.netloc
        and candidate.path.startswith(base.path)
    )


def _filename_from_url(url: str) -> str:
    name = PurePosixPath(unquote(urlsplit(url).path)).name
    if not name or name in {".", ".."}:
        raise ValueError("Image URL has no valid filename.")
    return name


def convert_notes(
    config: dict[str, Any],
    dry_run: bool = False,
    limit: int | None = None,
) -> dict[str, int]:
    base_url = _image_base_url(config)
    settings = config.get("services", {}).get("image_generator", {})
    api_url = str(config.get("anki_import", {}).get(
        "api_url", "http://127.0.0.1:8765"
    ))
    image_field = str(settings.get("image_field", "Image"))
    connector = AnkiConnector(api_url)
    notes = connector.all_notes(limit=limit)
    updated = skipped = failed = 0

    for note in notes:
        fields = note.get("fields", {})
        field = fields.get(image_field, {})
        old_value = str(field.get("value", "")) if isinstance(field, dict) else ""
        replacements: dict[str, str] = {}

        def localize(match: re.Match[str]) -> str:
            url = match.group(3)
            if not _is_under_base(url, base_url):
                return match.group(0)
            filename = _filename_from_url(url)
            replacements[url] = filename
            return f"{match.group(1)}{match.group(2)}{filename}{match.group(2)}"

        new_value = SRC_ATTRIBUTE.sub(localize, old_value)
        if new_value != old_value:
            if not dry_run:
                connector.update_note_fields(int(note["noteId"]), {image_field: new_value})
            updated += 1
            print(
                f"{'Would convert' if dry_run else 'Converted'}: "
                f"note={note.get('noteId')} images={len(replacements)}"
            )
        else:
            skipped += 1

    print(
        f"Image link conversion complete: notes={len(notes)}; "
        f"updated={updated}; skipped={skipped}; failed={failed}."
    )
    return {
        "notes": len(notes),
        "updated": updated,
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
    parser.add_argument("--limit", type=int, help="Process at most this many notes.")
    parser.add_argument(
        "--dry-run", action="store_true", help="Preview changes without updating notes."
    )
    args = parser.parse_args(argv)
    try:
        config = json.loads(args.config.read_text(encoding="utf-8-sig"))
        if not isinstance(config, dict):
            raise ValueError("Config must be a JSON object.")
        result = convert_notes(config, dry_run=args.dry_run, limit=args.limit)
        return 1 if result["failed"] else 0
    except Exception as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
