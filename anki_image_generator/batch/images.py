"""Generate missing images for existing Anki notes."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from helpers.logger import log
from services import AnkiConnector, ImageGenerator


class ImagesBatch:
    """Find notes with blank Image fields and generate images in chunks."""

    def __init__(self, config: dict[str, Any], config_path: Path | None = None) -> None:
        self.config = config
        self.config_path = config_path or Path(__file__).resolve().parents[1] / "config.json"
        self.settings = config.get("services", {}).get("image_generator", {})
        import_settings = config.get("anki_import", {})
        self.connector = AnkiConnector(
            str(import_settings.get("api_url", "http://127.0.0.1:8765"))
        )
        self.image_generator = ImageGenerator(config, self.config_path)
        log_dir = Path(str(config.get("log_path", "batch_logs"))).expanduser()
        self.log_dir = (
            log_dir if log_dir.is_absolute()
            else (self.config_path.resolve().parent / log_dir).resolve()
        )

    @staticmethod
    def _fields(note: dict[str, Any]) -> dict[str, str]:
        raw_fields = note.get("fields", {})
        return {
            name: str(value.get("value", "")) if isinstance(value, dict) else str(value or "")
            for name, value in raw_fields.items()
        }

    def _media_dir(self) -> Path:
        configured = str(self.settings.get("media_dir", "") or ".")
        path = Path(configured).expanduser()
        if not path.is_absolute():
            path = self.config_path.resolve().parent / path
        return path.resolve()

    def process(
        self, limit: int | None = None, step: int | None = None
    ) -> dict[str, int]:
        limit = self.settings.get("limit") if limit is None else limit
        step = self.settings.get("step", 10) if step is None else step
        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or limit < 1
        ):
            raise ValueError("limit must be a positive integer or None.")
        if isinstance(step, bool) or not isinstance(step, int) or step < 1:
            raise ValueError("step must be a positive integer.")

        image_field = str(self.settings.get("image_field", "Image")).strip() or "Image"
        candidates: list[dict[str, Any]] = []
        self.connector.sync()
        notes = self.connector.all_notes()
        skipped_no_prompt = 0
        skipped_no_identity = 0

        for note in notes:
            fields = self._fields(note)
            if image_field not in fields or fields[image_field].strip():
                continue
            prompt = fields.get("ImagePrompt", "").strip()
            if not prompt:
                skipped_no_prompt += 1
                log(
                    "images",
                    self.log_dir,
                    f"note={note.get('noteId')} status=skipped reason=no_image_prompt",
                    "INFO",
                )
                continue
            identity = fields.get("Word", "").strip() or fields.get("Kanji", "").strip()
            if not identity:
                skipped_no_identity += 1
                log(
                    "images",
                    self.log_dir,
                    f"note={note.get('noteId')} status=skipped reason=no_word_or_kanji",
                    "WARNING",
                )
                continue
            candidates.append({
                "note_id": int(note["noteId"]),
                "identity": identity,
                "prompt": prompt,
            })

        candidate_count = len(candidates)
        if limit is not None:
            candidates = candidates[:limit]
        total_steps = (len(candidates) + step - 1) // step
        log(
            "images",
            self.log_dir,
            f"scan total_notes={len(notes)} candidates={candidate_count} "
            f"selected={len(candidates)} limit={limit if limit is not None else 'unlimited'} "
            f"step={step} skipped_no_prompt={skipped_no_prompt} "
            f"skipped_no_identity={skipped_no_identity}",
            "INFO",
        )

        processed = succeeded = failed = 0
        try:
            for offset in range(0, len(candidates), step):
                batch = candidates[offset : offset + step]
                step_number = offset // step + 1
                log(
                    "images",
                    self.log_dir,
                    f"step={step_number}/{total_steps} start "
                    f"notes={len(batch)}",
                    "INFO",
                )
                for item in batch:
                    note_id = item["note_id"]
                    identity = item["identity"]
                    try:
                        image_value = self.image_generator.generate_for_note(
                            item["prompt"],
                            identity,
                            note_id,
                            self._media_dir(),
                        )
                        self.connector.update_note_fields(note_id, {image_field: image_value})
                        processed += 1
                        succeeded += 1
                        log(
                            "images",
                            self.log_dir,
                            f"note={note_id} item={identity!r} status=created",
                            "INFO",
                        )
                    except Exception as err:
                        processed += 1
                        failed += 1
                        log(
                            "images",
                            self.log_dir,
                            f"note={note_id} item={identity!r} status=error error={err}",
                            "ERROR",
                        )
                self.connector.sync()
                log(
                    "images",
                    self.log_dir,
                    f"step={step_number}/{total_steps} finished",
                    "INFO",
                )
        except Exception:
            # Preserve any images completed before a batch-level failure.
            self.connector.sync()
            raise

        return {
            "scanned": len(notes),
            "candidates": candidate_count,
            "selected": len(candidates),
            "processed": processed,
            "succeeded": succeeded,
            "failed": failed,
            "skipped_no_prompt": skipped_no_prompt,
            "skipped_no_identity": skipped_no_identity,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=Path(__file__).resolve().parents[1] / "config.json"
    )
    parser.add_argument("--limit", type=int, default=None, help="Maximum eligible notes to process.")
    parser.add_argument(
        "--step", type=int, default=None,
        help="Images per chunk; defaults to services.image_generator.step.",
    )
    args = parser.parse_args(argv)
    try:
        config = json.loads(args.config.read_text(encoding="utf-8-sig"))
        if not isinstance(config, dict):
            raise ValueError("Config must be a JSON object.")
        result = ImagesBatch(config, args.config).process(args.limit, args.step)
        print(
            f"Scanned: {result['scanned']}; candidates: {result['candidates']}; "
            f"selected: {result['selected']}; processed: {result['processed']}; "
            f"succeeded: {result['succeeded']}; failed: {result['failed']}."
        )
        return 1 if result["failed"] else 0
    except Exception as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
