"""Rename completed batch image files and update their batch_jobs results."""
from __future__ import annotations

import argparse
from html import escape
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

try:
    from .batch_image_generate import (
        _identifier,
        _qualified_table,
        connect_pgsql,
        table_for_object_type,
        table_names,
    )
except ImportError:
    from batch_image_generate import (
        _identifier,
        _qualified_table,
        connect_pgsql,
        table_for_object_type,
        table_names,
    )


def _new_result(old_result: str, record_id: int) -> str:
    filename = Path(old_result).name
    stem = Path(filename).stem
    extension = Path(filename).suffix
    if not extension or "_" not in stem:
        raise ValueError(f"Unexpected result filename format: {old_result!r}.")
    if stem.endswith(f"_{record_id}"):
        return filename
    prefix = stem.split("_", 1)[0]
    if not prefix:
        raise ValueError(f"Unexpected result filename format: {old_result!r}.")
    return f"{prefix}_{record_id}{extension}"


def _image_directory(config: dict[str, Any], config_path: Path) -> Path:
    batch_config = config.get("batch", {}).get("image_generate", {})
    configured = batch_config.get("image_base_path")
    if configured is None or configured == "":
        configured = batch_config.get("media_dir", "")
    if not isinstance(configured, str):
        raise ValueError("batch.image_generate.image_base_path must be a string.")
    value = configured.strip()
    if not value:
        raise ValueError(
            "Set batch.image_generate.image_base_path or media_dir in config.json."
        )
    parsed = urlparse(value)
    if parsed.scheme.lower() in {"http", "https"}:
        raise ValueError(
            "Remote image rename is unsupported."
        )
    directory = Path(value).expanduser()
    if not directory.is_absolute():
        directory = (config_path.resolve().parent / directory).resolve()
    return directory


def _remote_image_location(
    provider: str, image_base_path: str, result: Any
) -> str:
    value = str(result or "").strip()
    if not value:
        raise ValueError("Completed image result is empty.")
    parsed = urlparse(value)
    if parsed.scheme.lower() in {"http", "https"}:
        return value
    if provider == "oci":
        base_path = image_base_path.strip()
        if not base_path:
            raise ValueError(
                "batch.image_generate.image_base_path is required for OCI."
            )
        return f"{base_path.rstrip('/')}/{quote(value.lstrip('/'), safe='/')}"
    raise ValueError(
        f"Completed {provider} image result must be a URL, got {value!r}."
    )


def _image_tag(location: str) -> str:
    return f'<img src="{escape(location, quote=True)}">'


def run(
    config: dict[str, Any],
    config_path: Path,
    temp_path: Path,
    check_log_path: Path,
    check_images: bool,
    replace_note_image: bool,
) -> int:
    schema = _identifier(config.get("pgsql_schema", "public"), "pgsql_schema")
    table = _qualified_table(
        schema,
        _identifier(config.get("pgsql_batch_job_table", "batch_jobs"), "pgsql_batch_job_table"),
    )
    tables = table_names(config)
    image_field = _identifier(config.get("image_field", "Image"), "image_field")
    connection = connect_pgsql(config)
    moved: list[tuple[Path, Path]] = []
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f'SELECT "batch_job_id", "id", "result", "object_type" FROM {table} '
                'WHERE "status" = %s AND "result" IS NOT NULL AND BTRIM("result") <> \'\' '
                'ORDER BY "batch_job_id" FOR UPDATE',
                ("completed",),
            )
            rows = cursor.fetchall()

        batch_config = config.get("batch", {}).get("image_generate", {})
        provider = str(batch_config.get("upload_provider", "local")).strip().lower()
        if provider not in {"local", "oci", "drive"}:
            raise ValueError(
                "batch.image_generate.upload_provider must be local, oci, or drive."
            )
        if provider != "local":
            if check_images:
                raise ValueError("--check-images only supports local upload_provider.")
            if replace_note_image:
                with connection.cursor() as cursor:
                    for _, record_id, result, object_type in rows:
                        source_table_name = table_for_object_type(str(object_type), tables)
                        source_table = _qualified_table(schema, source_table_name)
                        cursor.execute(
                            f'UPDATE {source_table} SET "{image_field}" = %s '
                            'WHERE "anki_note_id" = %s',
                            (
                                _image_tag(
                                    _remote_image_location(
                                        provider,
                                        str(batch_config.get("image_base_path", "")),
                                        result,
                                    )
                                ),
                                int(record_id),
                            ),
                        )
                        if cursor.rowcount != 1:
                            raise RuntimeError(
                                f"Expected one {source_table_name} record for "
                                f"anki_note_id={record_id}; updated {cursor.rowcount}."
                            )
                connection.commit()
                print(f"Replaced Image field for {len(rows)} remote images.")
            else:
                print("Remote image storage does not support local file renaming.")
            return 0

        directory = _image_directory(config, config_path)
        checks = [
            (row, directory / Path(str(row[2])).name)
            for row in rows
        ]
        if check_images:
            check_log_path.write_text(
                "".join(
                    f"{image_path} {'OK' if image_path.is_file() else 'NG'}\n"
                    for _, image_path in checks
                ),
                encoding="utf-8",
            )
        existing_rows = [row for row, image_path in checks if image_path.is_file()]
        missing_rows = [row for row, image_path in checks if not image_path.is_file()]
        missing_count = len(rows) - len(existing_rows)
        if check_images:
            print(f"Existing image files: {len(existing_rows)}/{len(rows)}.")
            print(f"File check log: {check_log_path}")
            if missing_count:
                print(f"Marked {missing_count} records with missing image files as error.")

        changes: list[tuple[int, int, str, str, str, Path, Path]] = []
        targets: set[Path] = set()
        for batch_job_id, record_id, old_result, object_type in existing_rows:
            old_name = str(old_result)
            new_name = _new_result(old_name, int(record_id))
            source = directory / Path(old_name).name
            destination = directory / new_name
            if destination in targets:
                raise ValueError(f"Multiple records map to the same target: {destination}")
            if destination.exists() and destination != source:
                raise FileExistsError(f"Target image already exists: {destination}")
            targets.add(destination)
            changes.append(
                (
                    int(batch_job_id),
                    int(record_id),
                    str(object_type),
                    old_name,
                    new_name,
                    source,
                    destination,
                )
            )

        temp_path.write_text(
            "".join(f"({old!r}, {new!r})\n" for _, _, _, old, new, _, _ in changes),
            encoding="utf-8",
        )
        for _, _, _, _, _, source, destination in changes:
            if source != destination:
                source.rename(destination)
                moved.append((source, destination))

        with connection.cursor() as cursor:
            cursor.executemany(
                f'UPDATE {table} SET "result" = %s WHERE "batch_job_id" = %s',
                [(new_name, batch_job_id) for batch_job_id, _, _, _, new_name, _, _ in changes],
            )
            if check_images and missing_rows:
                cursor.executemany(
                    f'UPDATE {table} SET "status" = %s, "result" = NULL '
                    'WHERE "batch_job_id" = %s',
                    [("error", int(row[0])) for row in missing_rows],
                )
            if replace_note_image:
                for _, record_id, object_type, _, new_name, _, _ in changes:
                    source_table_name = table_for_object_type(object_type, tables)
                    source_table = _qualified_table(schema, source_table_name)
                    cursor.execute(
                        f'UPDATE {source_table} SET "{image_field}" = %s '
                        'WHERE "anki_note_id" = %s',
                        (_image_tag(str(directory / new_name)), record_id),
                    )
                    if cursor.rowcount != 1:
                        raise RuntimeError(
                            f"Expected one {source_table_name} record for anki_note_id={record_id}; "
                            f"updated {cursor.rowcount}."
                        )
        connection.commit()
        print(f"Updated {len(changes)} completed records.")
        if replace_note_image:
            print(f"Replaced Image field for {len(changes)} notes.")
        print(f"Mapping: {temp_path}")
        return 0
    except Exception:
        connection.rollback()
        for source, destination in reversed(moved):
            try:
                destination.rename(source)
            except OSError as rollback_error:
                print(f"Could not restore {destination}: {rollback_error}", file=sys.stderr)
        raise
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"))
    parser.add_argument("--temp", type=Path, default=Path(__file__).with_name("temp.txt"))
    parser.add_argument(
        "--check-log",
        type=Path,
        default=Path(__file__).with_name("check_images.log"),
    )
    parser.add_argument(
        "--check-images",
        action="store_true",
        help="Write file existence checks to check_images.log.",
    )
    parser.add_argument(
        "--replace-note-image",
        action="store_true",
        help="Replace each matching note's Image field with an HTML img tag.",
    )
    args = parser.parse_args(argv)
    try:
        config = json.loads(args.config.read_text(encoding="utf-8-sig"))
        if not isinstance(config, dict):
            raise ValueError("Config must be a JSON object.")
        return run(
            config,
            args.config,
            args.temp,
            args.check_log,
            args.check_images,
            args.replace_note_image,
        )
    except Exception as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
