"""Replace configured PostgreSQL word and kanji tables with Anki notes."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any

from services import AnkiConnector
from services.job_manager import _qualified_table, connect_pgsql, table_names


def _quote_column(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", name):
        raise ValueError(f"Unsupported PostgreSQL column name: {name!r}")
    return f'"{name}"'


def _note_target(
    note: dict[str, Any], fields: dict[str, str], config: dict[str, Any]
) -> tuple[str, str] | None:
    model_name = str(note.get("modelName", "")).strip()
    note_types = config.get("anki_import", {}).get("note_types", {})
    for identity in ("word", "kanji"):
        configured_model = str(note_types.get(identity, "")).strip()
        if configured_model and configured_model == model_name:
            table = table_names(config)[identity]
            field_name = "Word" if identity == "word" else "Kanji"
            if fields.get(field_name, "").strip():
                return identity, table
            return None

    if fields.get("Word", "").strip():
        return "word", table_names(config)["word"]
    if fields.get("Kanji", "").strip():
        return "kanji", table_names(config)["kanji"]
    return None


def _flatten_fields(note: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for name, raw_value in note.get("fields", {}).items():
        value = raw_value.get("value", "") if isinstance(raw_value, dict) else raw_value
        result[str(name)] = str(value or "")
    return result


def _table_columns(connection: Any, schema: str, table: str) -> set[str]:
    qualified = _qualified_table(schema, table)
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT * FROM {qualified} LIMIT 0")
        return {
            str(column.name if hasattr(column, "name") else column[0])
            for column in cursor.description or []
        }


def sync_notes(config: dict[str, Any], limit: int | None = None) -> dict[str, int]:
    schema = str(config.get("pgsql_schema", "public"))
    tables = table_names(config)
    connector = AnkiConnector(
        str(config.get("anki_import", {}).get("api_url", "http://127.0.0.1:8765"))
    )
    notes = connector.all_notes(limit=limit)
    connection = connect_pgsql(config)
    synced = skipped = inserted = updated = 0
    try:
        columns_by_table = {
            table: _table_columns(connection, schema, table)
            for table in tables.values()
        }
        for table in dict.fromkeys(tables.values()):
            with connection.cursor() as cursor:
                cursor.execute(f"DELETE FROM {_qualified_table(schema, table)}")

        for note in notes:
            fields = _flatten_fields(note)
            target = _note_target(note, fields, config)
            if target is None:
                skipped += 1
                continue
            _identity, table = target
            columns = columns_by_table[table]
            if "anki_note_id" not in columns:
                raise ValueError(
                    f'{schema}.{table} must contain an "anki_note_id" column.'
                )
            note_id = int(note["noteId"])
            values = {"anki_note_id": note_id}
            values.update(
                {
                    field: value
                    for field, value in fields.items()
                    if field in columns and field != "anki_note_id"
                }
            )
            names = list(values)
            table_ref = _qualified_table(schema, table)
            with connection.cursor() as cursor:
                insert_columns = ", ".join(_quote_column(name) for name in names)
                placeholders = ", ".join(["%s"] * len(names))
                cursor.execute(
                    f"INSERT INTO {table_ref} ({insert_columns}) "
                    f"VALUES ({placeholders})",
                    tuple(values[name] for name in names),
                )
                inserted += 1
            synced += 1
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "scanned": len(notes),
        "synced": synced,
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "config.json",
    )
    parser.add_argument("--limit", type=int, help="Process at most this many notes.")
    args = parser.parse_args(argv)
    try:
        config = json.loads(args.config.read_text(encoding="utf-8-sig"))
        if not isinstance(config, dict):
            raise ValueError("Config must be a JSON object.")
        result = sync_notes(config, limit=args.limit)
        print(
            f"Scanned: {result['scanned']}; synced: {result['synced']}; "
            f"inserted: {result['inserted']}; updated: {result['updated']}; "
            f"skipped: {result['skipped']}."
        )
        return 0
    except Exception as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
