"""Generate Word/Kanji example audio without starting Anki.

Input: JSON snapshots or read-only PostgreSQL access. Output: media and field updates.
Never opens an Anki collection or writes back to PostgreSQL.
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
import html
import json
from pathlib import Path
import re
import sys
import tempfile
from typing import Any

try:
    from ..core.config import normalize_config
except ImportError:
    from core.config import normalize_config

from .tts_edge import (
    _generate_filename,
    _filename_audio_format,
    edge_tts_config_from_mapping,
    synthesize_audio_data,
)


@dataclass(frozen=True)
class AudioJob:
    index: int
    mode: str
    source_field: str
    source: str
    number: int
    text: str

    @property
    def audio_field(self) -> str:
        return f"Example{self.number}_Audio"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def pgsql_table_name(config: dict[str, Any]) -> str:
    parts = [
        str(config.get("pgsql_schema", "public")).strip(),
        str(config.get("pgsql_table", "anki_ai_notes")).strip(),
    ]
    if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", part) for part in parts):
        raise ValueError("pgsql_schema/pgsql_table must be identifiers of 1–63 ASCII characters.")
    return ".".join(f'"{part}"' for part in parts)


def connect_pgsql(config: dict[str, Any]) -> Any:
    options: dict[str, Any] = {}
    for key, parameter in (
        ("pgsql_host", "host"), ("pgsql_user", "user"),
        ("pgsql_password", "password"), ("pgsql_database", "dbname"),
    ):
        value = config.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Missing configuration: {key}.")
        options[parameter] = value if parameter == "password" else value.strip()
    port = int(config.get("pgsql_port", 5432))
    timeout = int(config.get("pgsql_connect_timeout", 15))
    if not 1 <= port <= 65535 or timeout < 1:
        raise ValueError("pgsql_port must be 1–65535; pgsql_connect_timeout must be positive.")
    ssl_mode = str(config.get("pgsql_ssl_mode", "verify-full")).strip().lower()
    if ssl_mode not in {"disable", "require", "verify-ca", "verify-full"}:
        raise ValueError("pgsql_ssl_mode must be disable, require, verify-ca, or verify-full.")
    options.update(port=port, connect_timeout=timeout, sslmode=ssl_mode, autocommit=True)
    ssl_ca = str(config.get("pgsql_ssl_ca", "")).strip()
    if ssl_ca:
        options["sslrootcert"] = ssl_ca
    try:
        import psycopg
    except ImportError as err:
        raise RuntimeError(
            "Missing psycopg. Run: python -m pip install -r requirements-tts.txt"
        ) from err
    return psycopg.connect(**options)


def load_pgsql_notes(config: dict[str, Any], mode: str) -> list[dict[str, Any]]:
    if mode not in {"both", "word", "kanji"}:
        raise ValueError("mode must be both, word, or kanji.")
    modes = ("word", "kanji") if mode == "both" else (mode,)
    names = [
        config.get(f"note_type_name_{item}", f"AI{item.title()}WithImage")
        for item in modes
    ]
    table = pgsql_table_name(config)
    connection = connect_pgsql(config)
    try:
        with connection.cursor() as cursor:
            cursor.execute("START TRANSACTION READ ONLY")
            cursor.execute(
                f"SELECT anki_note_id, anki_guid, note_type, fields_json "
                f"FROM {table} WHERE note_type IN ({', '.join(['%s'] * len(names))}) "
                "ORDER BY anki_note_id",
                tuple(names),
            )
            rows = cursor.fetchall()
        return [
            {
                "anki_note_id": row[0],
                "anki_guid": row[1],
                "note_type": row[2],
                "fields": row[3] if isinstance(row[3], dict) else json.loads(row[3]),
            }
            for row in rows
        ]
    finally:
        try:
            connection.rollback()
        finally:
            connection.close()


def collect_jobs(
    notes: list[dict[str, Any]], config: dict[str, Any], mode: str,
) -> list[AudioJob]:
    if not isinstance(notes, list):
        raise ValueError("Input JSON must be a list of notes.")
    type_modes = {
        str(config.get(f"note_type_name_{m}", f"AI{m.title()}WithImage")): m
        for m in ("word", "kanji")
    }
    jobs = []
    for index, note in enumerate(notes):
        if not isinstance(note, dict):
            raise ValueError(f"Note {index + 1}: expected an object.")
        note_mode = note.get("mode") or type_modes.get(note.get("note_type"))
        if note_mode not in ("word", "kanji"):
            raise ValueError(f"Note {index + 1}: set mode to word or kanji.")
        if mode != "both" and mode != note_mode:
            continue
        expected_type = str(config.get(
            f"note_type_name_{note_mode}", f"AI{note_mode.title()}WithImage",
        ))
        if note.get("note_type", expected_type) != expected_type:
            raise ValueError(f"Note {index + 1}: note_type does not match configured {note_mode} type.")
        if any(char in expected_type for char in "\r\n\t"):
            raise ValueError("Note type contains an invalid control character.")
        fields = note.get("fields")
        if not isinstance(fields, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in fields.items()
        ):
            raise ValueError(f"Note {index + 1}: fields must contain string values.")
        source_field = str(config.get(f"source_field_{note_mode}", note_mode.title()))
        source = fields.get(source_field, "")
        if not clean_text(source):
            raise ValueError(f"Note {index + 1}: {source_field} is empty.")
        suffix = "JP" if note_mode == "kanji" else "Source"
        for number in range(1, 7):
            text = clean_text(fields.get(f"Example{number}_{suffix}", ""))
            if text and not fields.get(f"Example{number}_Audio", "").strip():
                jobs.append(AudioJob(index, note_mode, source_field, source, number, text))
    return jobs


def clean_text(value: str) -> str:
    value = re.sub(r"<(?:br\b[^>]*|/p\s*|/div\s*)>", " ", value, flags=re.I)
    return html.unescape(re.sub(r"<[^>]+>", "", value)).strip()


def atomic_write(path: Path, data: bytes) -> None:
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(data)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def generate(
    notes: list[dict[str, Any]], jobs: list[AudioJob],
    config: dict[str, Any], output: Path,
) -> tuple[Path, dict[str, Any]]:
    media = output / "media"
    media.mkdir(parents=True, exist_ok=True)
    # Separate reports for each run: stale TSV files cannot enter a new import.
    run = Path(tempfile.mkdtemp(prefix="run-", dir=output))
    settings = edge_tts_config_from_mapping(config)
    report: dict[str, Any] = {"created": 0, "reused": 0, "updates": [], "failures": []}
    for job in jobs:
        filename = _generate_filename(
            job.text, settings["voice"], source_text=job.source,
            example_number=job.number,
            audio_format=_filename_audio_format(settings),
        )
        target = media / filename
        try:
            if target.is_file() and target.stat().st_size:
                report["reused"] += 1
            else:
                actual_name, data = synthesize_audio_data(
                    job.text, settings, source_text=job.source, example_number=job.number,
                )
                if actual_name != filename or not data:
                    raise RuntimeError("TTS returned an unexpected filename or empty audio.")
                atomic_write(target, data)
                report["created"] += 1
            note = notes[job.index]
            tag = f"[sound:{filename}]"
            note["fields"][job.audio_field] = tag
            report["updates"].append({
                "note_index": job.index,
                "anki_note_id": note.get("anki_note_id"),
                "anki_guid": note.get("anki_guid", ""),
                "note_type": note.get("note_type") or config.get(
                    f"note_type_name_{job.mode}", f"AI{job.mode.title()}WithImage",
                ),
                "mode": job.mode,
                "source_field": job.source_field,
                "source": job.source,
                "audio_field": job.audio_field,
                "value": tag,
                "filename": filename,
            })
        except Exception as err:
            report["failures"].append({
                "note_index": job.index, "audio_field": job.audio_field, "error": str(err),
            })
            print(f"Failed note {job.index + 1}, {job.audio_field}: {err}", file=sys.stderr)
        # Persist successful work after each example, including partial failures.
        atomic_write(run / "report.json", json.dumps(
            report, ensure_ascii=False, indent=2,
        ).encode("utf-8"))
    atomic_write(run / "notes.updated.json", json.dumps(
        notes, ensure_ascii=False, indent=2,
    ).encode("utf-8"))
    write_import_files(run, report["updates"])
    return run, report


def write_import_files(run: Path, updates: list[dict[str, Any]]) -> None:
    # One audio field per TSV avoids clearing unrelated/failed audio fields.
    groups: dict[tuple, list] = {}
    for update in updates:
        key = (update["mode"], update["audio_field"], bool(update["anki_guid"]))
        groups.setdefault(key, []).append(update)
    for (mode, field, has_guid), rows in groups.items():
        path = run / f"{mode}-{field}-{'guid' if has_guid else 'source'}.tsv"
        with path.open("w", encoding="utf-8", newline="") as stream:
            stream.write("#separator:tab\n#html:true\n")
            note_type = rows[0]["note_type"]
            if any(char in note_type for char in "\r\n\t"):
                raise ValueError("Note type contains an invalid control character.")
            stream.write(f"#notetype:{note_type}\n")
            if has_guid:
                stream.write("#guid column:3\n")
            writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
            for row in rows:
                values = [row["source"], row["value"]]
                if has_guid:
                    values.append(row["anki_guid"])
                writer.writerow(values)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path, help="JSON list of {mode, fields, anki_guid?}.")
    source.add_argument(
        "--pgsql", action="store_true", help="Read PostgreSQL snapshots; never write DB.",
    )
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"))
    parser.add_argument("--mode", choices=("both", "word", "kanji"), default="both")
    parser.add_argument("--output", type=Path, default=Path("tts_output"))
    parser.add_argument("--dry-run", action="store_true", help="Count jobs; no TTS or file writes.")
    args = parser.parse_args(argv)
    try:
        config = normalize_config(read_json(args.config))
        notes = load_pgsql_notes(config, args.mode) if args.pgsql else read_json(args.input)
        jobs = collect_jobs(notes, config, args.mode)
        print(f"Pending: {len(jobs)} examples across {len({job.index for job in jobs})} notes.")
        if args.dry_run or not jobs:
            return 0
        run, report = generate(notes, jobs, config, args.output.resolve())
        print(f"Created: {report['created']}; reused: {report['reused']}; "
              f"failed: {len(report['failures'])}.")
        print(f"Media: {args.output.resolve() / 'media'}")
        print(f"Reports / import TSV: {run}")
        return 1 if report["failures"] else 0
    except Exception as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
