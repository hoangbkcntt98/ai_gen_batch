"""Generate missing word fields, images, and audio on existing Anki notes."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from helpers.logger import log
from models import AnkiNotesResult
from services import AnkiConnector, ImageGenerator, JobManager, TTSGenerator, WordGenerator


class AnkiNotesBatch:
    """Reusable sequential processor for existing Anki notes."""

    def __init__(self, config: dict[str, Any], config_path: Path | None = None) -> None:
        self.config = config
        self.config_path = config_path or Path(__file__).resolve().parents[1] / "config.json"
        import_config = config.get("anki_import", {})
        self.connector = AnkiConnector(
            str(import_config.get("api_url", "http://127.0.0.1:8765"))
        )
        self.word_creator = WordGenerator(config)
        self.image_creator = ImageGenerator(config, self.config_path)
        self.tts_creator = TTSGenerator(config)
        self.job_manager = JobManager(config)
        self.settings = config.get("services", {}).get("anki_notes", {})
        self.default_limit = self.settings.get("limit")
        configured_log_dir = self.settings.get("log_dir", config.get("log_path", "batch_logs"))
        self.log_dir = Path(configured_log_dir).expanduser()
        if not self.log_dir.is_absolute():
            self.log_dir = (self.config_path.resolve().parent / self.log_dir).resolve()

    def _identity(self, note: dict[str, Any], fields: dict[str, str]) -> tuple[str, str]:
        for field in ("Word", "Kanji"):
            if fields.get(field, "").strip():
                return field, fields[field].strip()
        model_name = str(note.get("modelName", "")).strip()
        note_types = self.config.get("anki_import", {}).get("note_types", {})
        for identity, configured_model in note_types.items():
            if str(configured_model).strip() == model_name and identity.lower() in {"word", "kanji"}:
                field = "Word" if identity.lower() == "word" else "Kanji"
                value = fields.get(field, "").strip()
                if value:
                    return field, value
        raise ValueError(f"Note {note.get('noteId')} has no non-empty Word or Kanji field.")

    def process(self, limit: int | None = None) -> AnkiNotesResult:
        """Process at most `limit` notes; use configured limit when omitted."""
        selected_limit = self.default_limit if limit is None else limit
        if selected_limit is not None:
            if isinstance(selected_limit, bool) or not isinstance(selected_limit, int):
                raise ValueError("limit must be a positive integer or None.")
            if selected_limit < 1:
                raise ValueError("limit must be a positive integer or None.")
        notes = self.connector.all_notes(limit=selected_limit)
        result = AnkiNotesResult(processed=len(notes))
        job_type = str(self.settings.get("job_type", "anki_notes")).strip() or "anki_notes"

        for note in notes:
            note_id = int(note["noteId"])
            model_name = str(note.get("modelName", ""))
            raw_fields = note.get("fields", {})
            fields = {
                name: str(value.get("value", "")) if isinstance(value, dict) else str(value or "")
                for name, value in raw_fields.items()
            }
            display = fields.get("Word", "").strip() or fields.get("Kanji", "").strip() or "?"
            job_id: int | None = None
            try:
                identity_field, identity_value = self._identity(note, fields)
                before = dict(fields)
                model_fields = list(fields)

                self.word_creator.generate_missing_fields(
                    {identity_field: identity_value, **fields},
                    fields,
                    model_fields,
                )

                image_config = self.config.get("services", {}).get("image_generator", {})
                image_field = str(image_config.get("image_field", "Image"))
                prompt = fields.get("ImagePrompt", "").strip()
                if image_field in fields and prompt and not fields.get(image_field, "").strip():
                    media_dir = Path(str(image_config.get("media_dir", "") or ".")).expanduser()
                    if not media_dir.is_absolute():
                        media_dir = (self.config_path.resolve().parent / media_dir).resolve()
                    fields[image_field] = self.image_creator.generate_for_note(
                        prompt, identity_value, note_id, media_dir
                    )

                self.tts_creator.generate_audio_fields(
                    fields,
                    model_fields,
                    self.connector.store_media_file,
                    note_id=note_id,
                    identity=identity_value,
                )

                changed_fields = {
                    key: value for key, value in fields.items()
                    if value != before.get(key, "")
                }
                if changed_fields:
                    job_id = self.job_manager.create_anki_note_job(
                        note_id, model_name, job_type
                    )
                    self.connector.update_note_fields(note_id, changed_fields)
                created_fields = sorted(
                    field for field in changed_fields
                    if field == image_field or field.endswith("_Audio")
                )
                filled_fields = sorted(set(changed_fields) - set(created_fields))

                note_result = {
                    "anki_note_id": note_id,
                    identity_field.lower(): identity_value,
                    "image": fields.get(image_field, ""),
                    "audio": {
                        key: value for key, value in fields.items()
                        if key.endswith("_Audio") and value.strip()
                    },
                }
                if job_id is not None:
                    self.job_manager.finish_anki_note_job(
                        job_id, "completed", result=note_result
                    )
                result.succeeded += 1
                status = "updated" if changed_fields else "skipped"
                details = []
                if filled_fields:
                    details.append(f"filled_fields={','.join(filled_fields)}")
                if created_fields:
                    details.append(f"created_fields={','.join(created_fields)}")
                if not changed_fields:
                    details.append("reason=no_missing_fields")
                log(
                    "anki_notes",
                    self.log_dir,
                    f"note={note_id} {identity_field.lower()}={display!r} "
                    f"status={status} {' '.join(details)}".rstrip(),
                    "INFO",
                )
            except Exception as err:
                result.failed += 1
                if job_id is None:
                    try:
                        job_id = self.job_manager.create_anki_note_job(
                            note_id, model_name, job_type
                        )
                    except Exception as job_err:
                        err = RuntimeError(f"{err}; job creation failed: {job_err}")
                if job_id is not None:
                    try:
                        self.job_manager.finish_anki_note_job(
                            job_id, "error", response=str(err)
                        )
                    except Exception as job_err:
                        err = RuntimeError(f"{err}; job update failed: {job_err}")
                log(
                    "anki_notes",
                    self.log_dir,
                    f"note={note_id} item={display!r} status=error error={err}",
                    "ERROR",
                )
        return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=Path(__file__).resolve().parents[1] / "config.json"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum notes to process; overrides services.anki_notes.limit.",
    )
    args = parser.parse_args(argv)
    try:
        config = json.loads(args.config.read_text(encoding="utf-8-sig"))
        if not isinstance(config, dict):
            raise ValueError("Config must be a JSON object.")
        result = AnkiNotesBatch(config, args.config).process(limit=args.limit)
        print(
            f"Processed: {result.processed}; succeeded: {result.succeeded}; "
            f"failed: {result.failed}."
        )
        return 1 if result.failed else 0
    except Exception as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
