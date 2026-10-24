"""Create Anki notes from CSV/TSV files or Google Sheets."""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from models import ImportResult, NoteDraft
from services import (
    AnkiConnector,
    GoogleSheetsService,
    ImageGenerator,
    TTSGenerator,
    WordGenerator,
)


def _split_tags(value: str) -> list[str]:
    return [tag.strip() for tag in value.split(";") if tag.strip()]


def _read_file(path: Path) -> tuple[str, list[tuple[str, list[list[str]]]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        sample = source.read(4096)
        source.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters="\t,")
        except csv.Error:
            dialect = csv.excel_tab
        rows = [row for row in csv.reader(source, dialect) if any(cell.strip() for cell in row)]
    return path.stem, [(path.stem, rows)]


def _read_spreadsheet(
    input_path: str | Path | None,
    spreadsheet_id: str | None,
    credentials_path: Path,
) -> tuple[str, list[tuple[str, list[list[str]]]]]:
    if input_path is not None:
        input_value = str(input_path)
        parsed = urlparse(input_value)
        if parsed.scheme in {"http", "https"}:
            match = re.fullmatch(r"/spreadsheets/d/([^/]+)(?:/.*)?", parsed.path)
            if parsed.netloc != "docs.google.com" or not match:
                raise ValueError("--input URL must be a Google Sheets URL.")
            if spreadsheet_id:
                raise ValueError("Do not combine a Google Sheets --input URL with --spreadsheet-id.")
            spreadsheet_id = match.group(1)
            input_path = None
        else:
            input_path = Path(input_value)
    if bool(input_path) == bool(spreadsheet_id):
        raise ValueError("Set exactly one of --input or --spreadsheet-id.")
    if input_path:
        return _read_file(input_path)
    return GoogleSheetsService(credentials_path).read_spreadsheet(str(spreadsheet_id))


def _dict_rows(
    tabs: list[tuple[str, list[list[str]]]],
) -> list[tuple[str, int, dict[str, str]]]:
    output = []
    for tab_name, rows in tabs:
        if not rows:
            continue
        headers = [str(value).strip() for value in rows[0]]
        for row_number, values in enumerate(rows[1:], start=2):
            mapped = {
                header: str(values[index]).strip() if index < len(values) else ""
                for index, header in enumerate(headers)
                if header
            }
            if any(mapped.values()):
                output.append((tab_name, row_number, mapped))
    return output


def _note_fields(row: dict[str, str], model_fields: list[str]) -> dict[str, str]:
    fields = {name: "" for name in model_fields}
    for source_name, value in row.items():
        if source_name == "Tags":
            continue
        target = source_name if source_name in fields else source_name.replace("_", "")
        if target in fields and value:
            fields[target] = value
    return fields


def _identity(row: dict[str, str]) -> str:
    if row.get("Word", "").strip():
        return "word"
    if row.get("Kanji", "").strip():
        return "kanji"
    raise ValueError("Row must contain non-empty Word or Kanji.")


def create_notes(
    config: dict[str, Any],
    rows: list[tuple[str, int, dict[str, str]]],
    config_path: Path,
    dry_run: bool = False,
    skip_images: bool | None = None,
    skip_tts: bool | None = None,
) -> ImportResult:
    import_config = config.get("anki_import", {})
    note_types = import_config.get("note_types", {})
    connector = AnkiConnector(str(import_config.get("api_url", "http://127.0.0.1:8765")))
    word_generator = WordGenerator(config)
    tts_generator = TTSGenerator(config)
    image_generator = ImageGenerator(config, config_path)
    creator_settings = config.get("services", {}).get("spreadsheet_anki_creator", {})
    skip_images = creator_settings.get("skip_images", False) if skip_images is None else skip_images
    skip_tts = creator_settings.get("skip_tts", False) if skip_tts is None else skip_tts
    deck_names = connector.call("deckNames")
    result = ImportResult()
    media_setting = str(
        config.get("services", {}).get(
            "image_generator", config.get("batch", {}).get("image_generate", {})
        ).get("media_dir", "")
    ).strip()
    media_dir = Path(media_setting).expanduser() if media_setting else Path.cwd()
    if not media_dir.is_absolute():
        media_dir = (config_path.resolve().parent / media_dir).resolve()

    for sheet, row_number, row in rows:
        try:
            identity = _identity(row)
            note_type = connector.note_type_for(identity, note_types)
            model_fields = connector.call("modelFieldNames", {"modelName": note_type})
            tags = _split_tags(row.get("Tags", ""))
            deck = connector.deck_for_tags(tags, deck_names)
            draft = NoteDraft(
                sheet=sheet,
                row_number=row_number,
                deck=deck,
                note_type=note_type,
                fields=_note_fields(row, model_fields),
                tags=tags,
                source_row=row,
            )
            if dry_run:
                result.skipped += 1
                print(
                    f"DRY-RUN {sheet} row {row_number}: "
                    f"deck={deck!r} model={note_type!r} fields={len(draft.fields)}"
                )
                continue

            word_generator.generate_missing_fields(
                draft.source_row,
                draft.fields,
                model_fields,
                allow_new_examples=True,
            )
            note_id = connector.add_note(
                draft.deck, draft.note_type, draft.fields, draft.tags
            )
            if note_id is None:
                result.skipped += 1
                print(f"SKIP duplicate: {sheet} row {row_number}")
                continue

            result.created += 1
            changed_fields: dict[str, str] = {}
            if not skip_tts:
                try:
                    before = dict(draft.fields)
                    tts_generator.generate_audio_fields(
                        draft.fields,
                        model_fields,
                        connector.store_media_file,
                        note_id=int(note_id),
                        identity=draft.fields.get("Word" if identity == "word" else "Kanji", identity),
                    )
                    changed_fields.update(
                        {key: value for key, value in draft.fields.items() if value != before[key]}
                    )
                except Exception as err:
                    result.failures.append(f"{sheet} row {row_number}: TTS: {err}")

            image_field = str(config.get("services", {}).get(
                "image_generator", {}
            ).get("image_field", config.get("image_field", "Image")))
            prompt = draft.fields.get("ImagePrompt", "").strip()
            if (
                not skip_images
                and image_field in model_fields
                and prompt
                and not draft.fields.get(image_field, "").strip()
            ):
                try:
                    draft.fields[image_field] = image_generator.generate_for_note(
                        prompt, identity, int(note_id), media_dir
                    )
                    changed_fields[image_field] = draft.fields[image_field]
                except Exception as err:
                    result.failures.append(f"{sheet} row {row_number}: image: {err}")
            if changed_fields:
                connector.update_note_fields(int(note_id), changed_fields)
            print(f"CREATED {sheet} row {row_number}: note={note_id} deck={deck!r}")
        except Exception as err:
            result.failures.append(f"{sheet} row {row_number}: {err}")
            print(f"ERROR {sheet} row {row_number}: {err}", file=sys.stderr)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"))
    parser.add_argument("--input", help="CSV/TSV file or Google Sheets URL.")
    parser.add_argument("--spreadsheet-id", help="Read spreadsheet through Google Sheets API.")
    parser.add_argument(
        "--credentials",
        type=Path,
        help="Google service-account JSON; defaults to account_service.json beside config.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--sync", action="store_true", help="Sync Anki after note creation.")
    parser.add_argument("--skip-images", action="store_true", help="Skip image generation.")
    parser.add_argument("--skip-tts", action="store_true", help="Skip audio generation.")
    args = parser.parse_args(argv)
    try:
        config = json.loads(args.config.read_text(encoding="utf-8-sig"))
        if not isinstance(config, dict):
            raise ValueError("Config must be a JSON object.")
        credentials = args.credentials or args.config.resolve().parent / "account_service.json"
        title, tabs = _read_spreadsheet(args.input, args.spreadsheet_id, credentials)
        rows = _dict_rows(tabs)
        print(f"Spreadsheet: {title}; rows: {len(rows)}")
        result = create_notes(
            config,
            rows,
            args.config,
            dry_run=args.dry_run,
            skip_images=True if args.skip_images else None,
            skip_tts=True if args.skip_tts else None,
        )
        print(
            f"Created: {result.created}; skipped: {result.skipped}; "
            f"errors: {result.failed}."
        )
        for failure in result.failures:
            print(f"FAILURE: {failure}", file=sys.stderr)
        if args.sync and not args.dry_run and result.created:
            connector = AnkiConnector(
                str(config.get("anki_import", {}).get("api_url", "http://127.0.0.1:8765"))
            )
            connector.sync()
            print("Anki sync complete.")
        return 1 if result.failed else 0
    except Exception as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
