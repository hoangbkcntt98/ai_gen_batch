"""Generate Kanji and Word note data from PostgreSQL using a chat API."""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}")
KANJI_FIELDS = (
    "MeaningJP", "MeaningVI", "Chinese", "OnYomi", "KunYomi", "Radical",
    "RadicalName", "RadicalMeaning", "PhoneticPart",
    *(f"Vocabulary{i}_{lang}" for i in range(1, 7) for lang in ("JP", "VN")),
    *(f"Example{i}_{lang}" for i in range(1, 7) for lang in ("JP", "VN")),
    "Mnemonic", "ImagePrompt", "RelatedKanji", "Notes",
)
WORD_FIELDS = (
    "Reading", "MeaningSource", "MeaningDestination", "Usage",
    *(f"Example{i}_{lang}" for i in range(1, 7) for lang in ("Source", "Destination")),
    "ImagePrompt", "Notes", "Chineses",
)


def setup_logging() -> logging.Logger:
    log_path = Path(__file__).resolve().with_suffix(".log")
    logger = logging.getLogger("word_kanji_generate")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"
        ))
        logger.addHandler(handler)
    return logger

def _identifier(value: Any, label: str) -> str:
    name = str(value or "").strip()
    if not _IDENTIFIER_RE.fullmatch(name):
        raise ValueError(f"{label} must be an ASCII identifier of 1-63 characters.")
    return name


def _table(schema: str, name: str) -> str:
    return f'"{_identifier(schema, "pgsql_schema")}"."{_identifier(name, "table")}"'


def connect_pgsql(config: dict[str, Any]) -> Any:
    params: dict[str, Any] = {}
    for key, target in (
        ("pgsql_host", "host"),
        ("pgsql_user", "user"),
        ("pgsql_password", "password"),
        ("pgsql_database", "dbname"),
    ):
        value = config.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Missing configuration: {key}.")
        params[target] = value if target == "password" else value.strip()
    port = int(config.get("pgsql_port", 5432))
    connect_timeout = int(config.get("pgsql_connect_timeout", 15))
    sslmode = str(config.get("pgsql_ssl_mode", "disable")).strip().lower()
    if not 1 <= port <= 65535 or connect_timeout < 1:
        raise ValueError("pgsql_port must be 1-65535 and pgsql_connect_timeout must be positive.")
    if sslmode not in {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}:
        raise ValueError("Invalid pgsql_ssl_mode.")
    params["port"] = port
    params["connect_timeout"] = connect_timeout
    params["sslmode"] = sslmode
    ca_file = str(config.get("pgsql_ssl_ca", "")).strip()
    if ca_file:
        params["sslrootcert"] = ca_file
    try:
        import psycopg
    except ImportError as err:
        raise RuntimeError(
            "Missing psycopg. Install standalone dependencies with "
            "`python -m pip install -r requirements.txt`."
        ) from err
    return psycopg.connect(**params)


def prompt_for(mode: str, source: str, required_vocabulary: str, config: dict[str, Any]) -> str:
    level = str(config.get("user_level", "N3")).upper()
    if level not in {"N1", "N2", "N3", "N4", "N5"}:
        level = "N3"
    if mode == "kanji":
        return (
            f"Create one accurate Japanese kanji learning note for {source!r}. "
            f"Learner level: JLPT {level}. RequiredVocabulary: {required_vocabulary!r}. "
            "Return only a JSON object with these keys: "
            + ", ".join(KANJI_FIELDS)
            + ". Give concise Japanese and Vietnamese meanings, standard readings and radical. "
            "Create 1-6 vocabulary pairs and matching Japanese/Vietnamese example pairs; "
            "if RequiredVocabulary is nonempty, make it Vocabulary1_JP and explain it "
            "in Vocabulary1_VN. Examples must cover all listed vocabulary. Chinese is "
            "uppercase Sino-Vietnamese reading. ImagePrompt must describe a cute, simple "
            "2D Japanese everyday-life illustration of RequiredVocabulary and its meaning, "
            "pastel colors, no text or watermark."
        )
    source_language = str(config.get("source_language", "Japanese")).strip()
    destination_language = str(config.get("destination_language", "Vietnamese")).strip()
    return (
        f"Create one accurate vocabulary learning note for {source!r}. "
        f"Source language: {source_language}; destination language: {destination_language}; "
        f"learner level: JLPT {level}. Return only a JSON object with these keys: "
        + ", ".join(WORD_FIELDS)
        + ". Include a natural reading, concise meanings, usage as short HTML, and 1-6 "
        "natural example pairs in the two languages. ImagePrompt must describe a cute, "
        "simple 2D Japanese everyday-life illustration of the word and its destination "
        "meaning, pastel colors, no text or watermark. Chineses is uppercase Sino-Vietnamese "
        "reading only when applicable; otherwise empty."
    )


def request_data(mode: str, source: str, required_vocabulary: str, config: dict[str, Any]) -> dict[str, str]:
    batch_config = config.get("batch", {}).get("word_kanji_generate", {})
    base = str(batch_config.get("chat_api_base", config.get("chat_api_base", ""))).strip().rstrip("/")
    model = str(batch_config.get("chat_model", config.get("chat_model", ""))).strip()
    api_key = str(batch_config.get("chat_api_key", config.get("chat_api_key", ""))).strip()
    if not base or not model:
        raise RuntimeError(
            "Configure batch.word_kanji_generate.chat_api_base and chat_model in config.json."
        )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Return only one valid JSON object."},
            {"role": "user", "content": prompt_for(mode, source, required_vocabulary, config)},
        ],
        "temperature": float(batch_config.get("temperature", 0.2)),
        "response_format": {"type": "json_object"},
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        timeout = int(batch_config.get("timeout_seconds", config.get("timeout_seconds", 120)))
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        detail = err.read().decode("utf-8", errors="replace")
        if "response_format" not in detail and err.code not in {400, 422}:
            raise RuntimeError(f"Chat API HTTP {err.code}: {detail}") from err
        payload.pop("response_format", None)
        request = urllib.request.Request(
            base + "/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    content = body["choices"][0]["message"]["content"]
    if isinstance(content, list):
        content = "".join(str(item.get("text", "")) for item in content if isinstance(item, dict))
    text = str(content).strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
    result = json.loads(text)
    if not isinstance(result, dict):
        raise RuntimeError("Chat API response must be a JSON object.")
    fields = KANJI_FIELDS if mode == "kanji" else WORD_FIELDS
    values: dict[str, str] = {}
    for field in fields:
        value = result.get(field)
        if value is None:
            values[field] = ""
        elif isinstance(value, str):
            values[field] = value
        else:
            raise RuntimeError(f"Chat API field {field!r} must be a string or null.")
    return values


def generate_table(
    connection: Any,
    config: dict[str, Any],
    mode: str,
    limit: int | None,
    overwrite: bool,
    skip_fields: list[str],
    dry_run: bool,
) -> tuple[int, int, int]:
    schema = _identifier(config.get("pgsql_schema", "public"), "pgsql_schema")
    table_name = _identifier(
        config.get(f"pgsql_{mode}_table", f"anki_{mode}"),
        f"pgsql_{mode}_table",
    )
    table = _table(schema, table_name)
    source_field = "Kanji" if mode == "kanji" else "Word"
    fields = KANJI_FIELDS if mode == "kanji" else WORD_FIELDS
    control = "RequiredVocabulary" if mode == "kanji" else None
    selected = [f'"{source_field}"'] + ([f'"{control}"'] if control else [])
    where = f'COALESCE(BTRIM("{source_field}"), \'\') <> \'\''
    validated_skip_fields = [
        _identifier(field, f"batch.word_kanji_generate.skip_fields.{mode}")
        for field in skip_fields
    ]
    for field in validated_skip_fields:
        where += f' AND COALESCE(BTRIM("{field}"), \'\') = \'\''
    if not overwrite:
        where += " AND (" + " OR ".join(
            f'COALESCE(BTRIM("{field}"), \'\') = \'\'' for field in fields
        ) + ")"
    query = f'SELECT "id", {", ".join(selected)} FROM {table} WHERE {where} ORDER BY "id"'
    params: tuple[Any, ...] = ()
    if limit is not None:
        query += " LIMIT %s"
        params = (limit,)
    with connection.cursor() as cursor:
        cursor.execute(query, params)
        rows = cursor.fetchall()
    connection.commit()
    if dry_run:
        print(f"{mode}: {len(rows)} note(s) eligible.")
        logging.getLogger("word_kanji_generate").info(
            "%s dry-run: %d note(s) eligible", mode, len(rows)
        )
        return len(rows), 0, 0

    logger = logging.getLogger("word_kanji_generate")
    updated = errors = 0
    assignments = ", ".join(f'"{field}" = %s' for field in fields)
    update = f'UPDATE {table} SET {assignments}, "updated_at" = CURRENT_TIMESTAMP WHERE "id" = %s'
    for index, row in enumerate(rows, 1):
        row_id, source = int(row[0]), str(row[1] or "").strip()
        required = str(row[2] or "").strip() if control else ""
        try:
            generated = request_data(mode, source, required, config)
            if not overwrite:
                field_list = ", ".join('"' + field + '"' for field in fields)
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"SELECT {field_list} FROM {table} WHERE \"id\" = %s",
                        (row_id,),
                    )
                    existing = cursor.fetchone()
                generated = {
                    field: (str(old or "") if str(old or "").strip() else generated[field])
                    for field, old in zip(fields, existing)
                }
            with connection.cursor() as cursor:
                cursor.execute(update, (*[generated[field] for field in fields], row_id))
            connection.commit()
            updated += 1
            print(f"[{index}/{len(rows)}] {mode} {source}: updated")
            logger.info("[%d/%d] %s id=%d source=%r updated", index, len(rows), mode, row_id, source)
        except Exception as err:
            connection.rollback()
            errors += 1
            print(f"[{index}/{len(rows)}] {mode} {source}: ERROR: {err}", file=sys.stderr)
            logger.exception("[%d/%d] %s id=%d source=%r failed", index, len(rows), mode, row_id, source)
    return len(rows), updated, errors


def run(config: dict[str, Any], modes: tuple[str, ...], limit: int | None, overwrite: bool, dry_run: bool) -> int:
    logger = logging.getLogger("word_kanji_generate")
    logger.info(
        "Run started: modes=%s limit=%s overwrite=%s dry_run=%s",
        ",".join(modes), limit, overwrite, dry_run,
    )
    connection = connect_pgsql(config)
    try:
        total = updated = errors = 0
        remaining = limit
        for mode in modes:
            if remaining == 0:
                break
            configured_skip_fields = config.get("batch", {}).get(
                "word_kanji_generate", {}
            ).get("skip_fields", {})
            if isinstance(configured_skip_fields, dict):
                skip_fields = configured_skip_fields.get(mode, [])
            else:
                skip_fields = configured_skip_fields
            if not isinstance(skip_fields, list):
                raise ValueError(
                    f"batch.word_kanji_generate.skip_fields.{mode} must be a JSON array."
                )
            found, changed, failed = generate_table(
                connection, config, mode, remaining, overwrite, skip_fields, dry_run
            )
            total += found
            updated += changed
            errors += failed
            if remaining is not None:
                remaining -= found
        if not dry_run:
            print(f"Eligible: {total}; updated: {updated}; errors: {errors}.")
        logger.info("Run finished: eligible=%d updated=%d errors=%d", total, updated, errors)
        return 1 if errors else 0
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(Path(__file__).with_name("config.json")))
    parser.add_argument("--type", choices=("word", "kanji", "all"), default="all")
    parser.add_argument("--limit", type=int)
    overwrite_group = parser.add_mutually_exclusive_group()
    overwrite_group.add_argument("--overwrite", dest="overwrite", action="store_true")
    overwrite_group.add_argument("--no-overwrite", dest="overwrite", action="store_false")
    parser.set_defaults(overwrite=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    logger = setup_logging()
    try:
        with open(args.config, encoding="utf-8-sig") as stream:
            config = json.load(stream)
        if not isinstance(config, dict):
            raise ValueError("Config must be a JSON object.")
        batch = config.get("batch", {}).get("word_kanji_generate", {})
        configured_limit = batch.get("limit")
        limit_value = args.limit if args.limit is not None else configured_limit
        limit = None if limit_value in (None, "") else int(limit_value)
        if limit is not None and limit < 1:
            raise ValueError("limit must be a positive integer or null.")
        configured_overwrite = batch.get("overwrite_existing_fields", False)
        if not isinstance(configured_overwrite, bool):
            raise ValueError(
                "batch.word_kanji_generate.overwrite_existing_fields must be true or false."
            )
        overwrite = configured_overwrite if args.overwrite is None else args.overwrite
        types = ("word", "kanji") if args.type == "all" else (args.type,)
        return run(config, types, limit, overwrite, args.dry_run)
    except Exception as err:
        print(f"Error: {err}", file=sys.stderr)
        logger.exception("Run failed: %s", err)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
