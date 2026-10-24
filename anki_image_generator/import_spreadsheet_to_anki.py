#!/usr/bin/env python3
"""Import exported spreadsheet tabs into Anki through AnkiConnect."""

import argparse
import base64
import csv
import hashlib
import html
import io
import json
import re
import sys
import uuid
from pathlib import Path

import requests

API_VERSION = 6
DEFAULT_API_URL = "http://127.0.0.1:8765"
DEFAULT_EXPORT = Path(__file__).resolve().with_name("spreadsheet_export.txt")
DEFAULT_CONFIG = Path(__file__).resolve().with_name("config.json")


def parse_export(path: Path):
    text = path.read_text(encoding="utf-8-sig")
    sections = []
    current_title = None
    current_lines = []

    for line in text.splitlines(keepends=True):
        if line.startswith("=== ") and line.rstrip().endswith(" ==="):
            if current_title is not None:
                sections.append((current_title, "".join(current_lines)))
            current_title = line.rstrip()[4:-4]
            current_lines = []
        elif current_title is not None:
            current_lines.append(line)

    if current_title is not None:
        sections.append((current_title, "".join(current_lines)))

    for title, contents in sections:
        rows = list(csv.reader(io.StringIO(contents), delimiter="\t"))
        rows = [row for row in rows if any(cell.strip() for cell in row)]
        if not rows:
            continue
        headers = [header.strip() for header in rows[0]]
        for row_number, row in enumerate(rows[1:], start=2):
            row += [""] * (len(headers) - len(row))
            yield title, row_number, dict(zip(headers, row[: len(headers)]))


def call_api(session, api_url, action, params=None, timeout=60):
    response = session.post(
        api_url,
        json={"action": action, "version": API_VERSION, "params": params or {}},
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("error"):
        raise RuntimeError(f"AnkiConnect {action} failed: {payload['error']}")
    return payload.get("result")


def deck_note_counts(session, api_url):
    deck_names = call_api(session, api_url, "deckNames")
    counts = []
    for deck_name in deck_names:
        query = f"deck:{json.dumps(deck_name, ensure_ascii=False)}"
        note_ids = call_api(session, api_url, "findNotes", {"query": query})
        counts.append((deck_name, len(note_ids)))
    return counts


def find_matching_notes(session, api_url, deck, note_type, unique_field, unique_value):
    query = (
        f"deck:{json.dumps(deck, ensure_ascii=False)} "
        f"note:{json.dumps(note_type, ensure_ascii=False)} "
        f"{unique_field}:{json.dumps(unique_value, ensure_ascii=False)}"
    )
    note_ids = call_api(session, api_url, "findNotes", {"query": query})
    info_list = call_api(session, api_url, "notesInfo", {"notes": note_ids})
    return [
        info for info in info_list
        if info.get("modelName") == note_type
        and info.get("fields", {}).get(unique_field, {}).get("value") == unique_value
    ]


def map_fields(row, model_fields, explicit_mapping):
    mapping = {}
    for source in row:
        if source == "Tags":
            continue
        if source in model_fields:
            target = source
        elif source.endswith("_Source") and source[:-7] in model_fields:
            target = source[:-7]
        elif source.endswith("_Source") and source.replace("_Source", "Source") in model_fields:
            target = source.replace("_Source", "Source")
        else:
            target = source
        if target in model_fields:
            mapping[source] = target
    if explicit_mapping:
        mapping.update(explicit_mapping)

    fields = {}
    for source, target in mapping.items():
        if source in row and target in model_fields:
            fields[target] = row[source]
    return fields


def build_image_prompt(word, meaning):
    return (
        "Create a Japanese vocabulary illustration in classic Irasutoya-inspired style: "
        "cute, simple 2D Japanese cartoon, soft rounded outlines, flat pastel colors, "
        "minimal shading, expressive characters, and a clear visual action representing "
        f'"{word}" ("{meaning}"). Include a detailed but clean, contextually relevant '
        "background with recognizable objects and everyday Japanese scenery to help "
        "learners remember the word. Friendly educational illustration, no text, no watermark."
    )


WORD_CHAT_FIELDS = {
    "Reading", "MeaningSource", "MeaningDestination", "Usage", "Notes", "Chineses",
    *(f"Example{index}_{language}" for index in range(1, 7) for language in ("Source", "Destination")),
}
KANJI_CHAT_FIELDS = {
    "RequiredVocabulary", "MeaningJP", "MeaningVI", "OnYomi", "KunYomi",
    "Radical", "RadicalName", "RadicalMeaning", "PhoneticPart", "Mnemonic", "ImagePrompt",
    "RelatedKanji", "Notes", "Chinese",
    *(f"Example{index}_{language}" for index in range(1, 7) for language in ("JP", "VN")),
    *(f"Vocabulary{index}_{language}" for index in range(1, 7) for language in ("JP", "VN")),
}

def generate_missing_fields(row, fields, model_fields, config):
    identity = next((name for name in ("Word", "Kanji") if row.get(name, "").strip()), None)
    eligible = WORD_CHAT_FIELDS if identity == "Word" else KANJI_CHAT_FIELDS
    missing = [
        name for name in model_fields
        if name in eligible and not fields.get(name, "").strip()
    ]
    if identity == "Word":
        missing = [
            name for name in missing
            if name not in {"MeaningSource"} and not name.endswith("_Source")
            and (
                not re.fullmatch(r"Example\d+_Destination", name)
                or bool(fields.get(name.replace("_Destination", "_Source"), "").strip())
            )
        ]

    if missing:
        batch_config = config.get("batch", {}).get("word_kanji_generate", {})
        base = str(batch_config.get("chat_api_base", config.get("chat_api_base", ""))).strip().rstrip("/")
        model = str(batch_config.get("chat_model", config.get("chat_model", ""))).strip()
        api_key = str(batch_config.get("chat_api_key", config.get("chat_api_key", ""))).strip()
        if not base or not model:
            raise RuntimeError(
                "Configure batch.word_kanji_generate.chat_api_base and chat_model in config.json."
            )
        source_language = str(config.get("source_language", "Japanese")).strip()
        destination_language = str(config.get("destination_language", "Vietnamese")).strip()
        descriptions = {
            "Reading": "Japanese kana reading",
            "MeaningSource": f"concise meaning in {source_language}",
            "MeaningDestination": f"concise meaning in {destination_language}",
            "Usage": "short usage explanation, HTML allowed",
            "Notes": "concise useful learner note",
            "Chineses": "uppercase Sino-Vietnamese reading, or empty when inapplicable",
            "Chinese": "uppercase Sino-Vietnamese reading",
            "MeaningJP": "concise Japanese meaning",
            "MeaningVI": "concise Vietnamese meaning",
            "OnYomi": "standard on-yomi readings",
            "KunYomi": "standard kun-yomi readings",
            "Radical": "radical character",
            "RadicalName": "radical name",
            "RadicalMeaning": "radical meaning",
            "PhoneticPart": "phonetic component, or empty when inapplicable",
            "Mnemonic": "concise mnemonic",
            "ImagePrompt": "detailed Irasutoya-inspired Japanese learning illustration prompt, no text or watermark",
            "RelatedKanji": "related kanji",
            "RequiredVocabulary": "Japanese vocabulary containing this kanji",
        }
        for index in range(1, 7):
            descriptions[f"Example{index}_Source"] = f"natural {source_language} example"
            descriptions[f"Example{index}_Destination"] = f"translation into {destination_language} of matching example"
            descriptions[f"Example{index}_JP"] = "natural Japanese example"
            descriptions[f"Example{index}_VN"] = "Vietnamese translation of matching example"
            descriptions[f"Vocabulary{index}_JP"] = "Japanese vocabulary containing the kanji"
            descriptions[f"Vocabulary{index}_VN"] = "Vietnamese meaning of matching vocabulary"
        context = {
            "identity": identity,
            "spreadsheet_values": {
                key: value for key, value in row.items()
                if key != "Tags" and value.strip()
            },
            "existing_fields": {
                key: value for key, value in fields.items() if value.strip()
            },
        }
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": "Return only one valid JSON object. Use only requested keys with string values. Preserve supplied source data.",
                },
                {
                    "role": "user",
                    "content": (
                        f"Generate missing fields for this Japanese {identity.lower()} note. "
                        f"Return exactly these keys: {', '.join(missing)}. "
                        f"Field descriptions: {json.dumps({key: descriptions.get(key, key) for key in missing}, ensure_ascii=False)}. "
                        "Use accurate, natural, concise learning content. Translate matching example pairs; "
                        "do not replace supplied values. Use empty string only when genuinely inapplicable.\n"
                        f"Context: {json.dumps(context, ensure_ascii=False)}"
                    ),
                },
            ],
            "temperature": float(batch_config.get("temperature", 0.2)),
            "response_format": {"type": "json_object"},
        }
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        timeout = int(
            batch_config.get("timeout_seconds", config.get("timeout_seconds", 120))
        )
        response = requests.post(
            base + "/chat/completions", json=payload, headers=headers, timeout=timeout
        )
        if response.status_code in (400, 422) and "response_format" in response.text:
            payload.pop("response_format", None)
            response = requests.post(
                base + "/chat/completions",
                json=payload,
                headers=headers,
                timeout=timeout,
            )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        if isinstance(content, list):
            content = "".join(
                str(item.get("text", ""))
                for item in content
                if isinstance(item, dict)
            )
        generated_text = re.sub(
            r"^```(?:json)?\s*|\s*```$", "", str(content).strip(), flags=re.I
        )
        generated = json.loads(generated_text)
        if not isinstance(generated, dict):
            raise RuntimeError("Chat API response must be a JSON object.")
        for field in missing:
            value = generated.get(field)
            if value is None:
                value = ""
            if not isinstance(value, str):
                raise RuntimeError(f"Chat API field {field!r} must be a string.")
            fields[field] = value.strip()

    if identity == "Word" and "ImagePrompt" in model_fields and not fields.get("ImagePrompt", "").strip():
        meaning = (
            fields.get("MeaningDestination", "").strip()
            or row.get("Meaning_Source", "").strip()
            or "its common meaning"
        )
        fields["ImagePrompt"] = build_image_prompt(
            row.get("Word", "").strip(), meaning
        )
    return fields


def generation_plan(row, fields, model_fields):
    identity = next((name for name in ("Word", "Kanji") if row.get(name, "").strip()), None)
    eligible = WORD_CHAT_FIELDS if identity == "Word" else KANJI_CHAT_FIELDS
    chat = [name for name in model_fields if name in eligible and not fields.get(name, "").strip()]
    if identity == "Word":
        chat = [
            name for name in chat
            if name not in {"MeaningSource"} and not name.endswith("_Source")
            and (
                not re.fullmatch(r"Example\d+_Destination", name)
                or bool(fields.get(name.replace("_Destination", "_Source"), "").strip())
            )
        ]
    prompt = ["ImagePrompt"] if (
        identity == "Word" and "ImagePrompt" in model_fields
        and not fields.get("ImagePrompt", "").strip()
    ) else []
    tools = [
        name for name in model_fields
        if (name == "Image" or name.endswith("_Audio"))
        and not fields.get(name, "").strip()
    ]
    return chat, prompt, tools


def audio_source_field(audio_field):
    example = re.fullmatch(r"Example(\d+)_Audio", audio_field)
    if example:
        number = example.group(1)
        return (f"Example{number}_JP", f"Example{number}_Source")
    if audio_field == "Reading_Audio":
        return ("Reading", "Word")
    if audio_field == "RequiredVocabulary_Audio":
        return ("RequiredVocabulary", "Kanji")
    return ()


def plain_text(value):
    value = re.sub(r"<(?:br\b[^>]*|/p\s*|/div\s*)>", " ", value, flags=re.I)
    return html.unescape(re.sub(r"<[^>]+>", "", value)).strip()


def generate_audio_fields(fields, model_fields, config, session, api_url, force=False):
    batch_config = config.get("batch", {}).get("tts", {})
    voice = str(batch_config.get("voice", "ja-JP-NanamiNeural")).strip()
    rate = str(batch_config.get("rate", "+0%")).strip()
    volume = str(batch_config.get("volume", "+0%")).strip()
    pitch = str(batch_config.get("pitch", "+0Hz")).strip()
    timeout = int(batch_config.get("timeout_seconds", 120))
    for audio_field in model_fields:
        if (
            not audio_field.endswith("_Audio")
            or (fields.get(audio_field, "").strip() and not force)
        ):
            continue
        text_field = next(
            (name for name in audio_source_field(audio_field) if fields.get(name, "").strip()),
            None,
        )
        if not text_field:
            continue
        text = plain_text(fields[text_field])
        if not text:
            continue
        try:
            import edge_tts
        except ImportError as err:
            raise RuntimeError(
                "Missing edge-tts. Install it in active Python environment."
            ) from err

        digest = hashlib.sha256(
            f"{voice}\0{audio_field}\0{text}".encode("utf-8")
        ).hexdigest()[:20]
        # Never point a regenerated sound tag at potentially stale media.
        filename = f"anki_{audio_field.lower()}_{digest}_{uuid.uuid4().hex[:8]}.mp3"
        communicate = edge_tts.Communicate(
            text, voice, rate=rate, volume=volume, pitch=pitch
        )
        audio_data = bytearray()
        import asyncio

        async def collect_audio():
            async for chunk in communicate.stream():
                if chunk.get("type") == "audio":
                    audio_data.extend(chunk["data"])

        asyncio.run(collect_audio())
        if not audio_data:
            raise RuntimeError(f"TTS returned empty audio for {audio_field}.")
        call_api(
            session,
            api_url,
            "storeMediaFile",
            {
                "filename": filename,
                "data": base64.b64encode(audio_data).decode("ascii"),
            },
            timeout=timeout,
        )
        fields[audio_field] = f"[sound:{filename}]"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create Anki notes from spreadsheet_export.txt through AnkiConnect."
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_EXPORT)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--api-url", help="Override configured AnkiConnect URL.")
    parser.add_argument(
        "--field-map",
        help='Optional JSON object mapping spreadsheet headers to Anki fields, e.g. \'{"Meaning_Source":"Meaning"}\'.',
    )
    parser.add_argument(
        "--sync",
        action="store_true",
        help="Sync the collection with AnkiWeb after successful note creation.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and preview rows without creating notes.",
    )
    parser.add_argument(
        "--update-existing",
        action="store_true",
        help="Fill empty mapped fields on matching existing notes; never overwrite non-empty fields.",
    )
    parser.add_argument(
        "--regenerate-audio",
        action="store_true",
        help="Regenerate audio fields even when they already contain sound tags.",
    )
    args = parser.parse_args()

    if not args.input.is_file():
        print(f"Input file not found: {args.input}", file=sys.stderr)
        return 2

    try:
        if not args.config.is_file():
            raise RuntimeError(f"Config file not found: {args.config}")
        config = json.loads(args.config.read_text(encoding="utf-8"))
        import_config = config.get("anki_import", {})
        api_url = args.api_url or import_config.get("api_url", DEFAULT_API_URL)
        note_types = import_config.get("note_types", {})
        if not isinstance(note_types, dict):
            raise ValueError("config.anki_import.note_types must be an object.")
        explicit_mapping = json.loads(args.field_map) if args.field_map else None
        if explicit_mapping is not None and not isinstance(explicit_mapping, dict):
            raise ValueError("--field-map must be a JSON object.")

        session = requests.Session()
        version = call_api(session, api_url, "version")
        known_decks = call_api(session, api_url, "deckNames")
        model_cache = {}

        notes = []
        for sheet, row_number, row in parse_export(args.input):
            identity_field = next(
                (name for name in ("Word", "Kanji") if row.get(name, "").strip()),
                None,
            )
            if identity_field is None:
                continue
            note_type = str(note_types.get(identity_field.lower(), "")).strip()
            if not note_type:
                raise RuntimeError(
                    f"Missing config.anki_import.note_types.{identity_field.lower()} "
                    f"for {sheet} row {row_number}."
                )
            if note_type not in model_cache:
                model_fields = call_api(
                    session, api_url, "modelFieldNames", {"modelName": note_type}
                )
                if not model_fields:
                    raise RuntimeError(f"Model has no fields or was not found: {note_type}")
                model_cache[note_type] = model_fields
            model_fields = model_cache[note_type]
            fields = map_fields(row, model_fields, explicit_mapping)
            if not any(value.strip() for value in fields.values()):
                continue
            tags = [tag.strip() for tag in row.get("Tags", "").split(";") if tag.strip()]
            matching_decks = [tag for tag in tags if tag in known_decks]
            if len(matching_decks) != 1:
                reason = (
                    "no matching deck tag"
                    if not matching_decks
                    else "multiple matching deck tags"
                )
                raise RuntimeError(f"{sheet} row {row_number}: {reason}; tags={tags}")
            deck = matching_decks[0]
            notes.append((sheet, row_number, deck, note_type, fields, tags, row))

        if not notes:
            print("No importable rows found.")
            return 0

        if args.dry_run:
            print(f"AnkiConnect API version: {version}")
            print(f"Prepared notes: {len(notes)}")
            for sheet, row_number, deck, note_type, fields, tags, row in notes[:10]:
                chat_fields, prompt_fields, tool_fields = generation_plan(
                    row, fields, model_cache[note_type]
                )
                print(
                    f"  {sheet} row {row_number}: deck={deck!r} "
                    f"model={note_type!r} fields={list(fields)} tags={tags}; "
                    f"chat={chat_fields}; prompt={prompt_fields}; tool={tool_fields}"
                )
            return 0

        created = 0
        skipped = 0
        updated = 0
        failures = []
        for sheet, row_number, deck, note_type, fields, tags, row in notes:
            try:
                model_fields = model_cache[note_type]
                unique_field = model_fields[0]
                if unique_field not in fields or not fields[unique_field].strip():
                    raise ValueError(
                        f"First model field {unique_field!r} is missing or empty."
                    )
                can_add = call_api(
                    session,
                    api_url,
                    "canAddNotes",
                    {
                        "notes": [
                            {
                                "deckName": deck,
                                "modelName": note_type,
                                "fields": fields,
                            }
                        ]
                    },
                )
                if can_add and not can_add[0] and not args.update_existing:
                    matched = find_matching_notes(
                        session, api_url, deck, note_type,
                        unique_field, fields[unique_field],
                    )
                    filled_audio = False
                    for info in matched:
                        info_fields = info.get("fields", {})
                        existing_fields = {
                            name: str(info_fields.get(name, {}).get("value", ""))
                            for name in model_fields
                        }
                        for name, value in fields.items():
                            if name in model_fields and not existing_fields.get(name, "").strip():
                                existing_fields[name] = value
                        empty_audio_fields = {
                            name for name in model_fields
                            if name.endswith("_Audio")
                            and (
                                args.regenerate_audio
                                or not existing_fields.get(name, "").strip()
                            )
                        }
                        if not empty_audio_fields:
                            continue
                        for name in empty_audio_fields:
                            existing_fields[name] = ""
                        generate_audio_fields(
                            existing_fields, model_fields, config, session, api_url,
                            force=args.regenerate_audio,
                        )
                        audio_updates = {
                            name: existing_fields[name]
                            for name in empty_audio_fields
                            if existing_fields.get(name, "").strip()
                        }
                        if audio_updates:
                            call_api(
                                session,
                                api_url,
                                "updateNoteFields",
                                {"note": {"id": info["noteId"], "fields": audio_updates}},
                            )
                            filled_audio = True
                    if filled_audio:
                        updated += 1
                        print(
                            f"Filled missing audio on duplicate: "
                            f"{sheet} row {row_number}: {fields[unique_field]}"
                        )
                    else:
                        skipped += 1
                        print(
                            f"Skip duplicate (audio complete or source missing): "
                            f"{sheet} row {row_number}: {fields[unique_field]}"
                        )
                    continue
                fields = generate_missing_fields(row, fields, model_fields, config)
                generate_audio_fields(
                    fields, model_fields, config, session, api_url,
                    force=args.regenerate_audio,
                )
                if can_add and not can_add[0]:
                    if args.update_existing:
                        matched = find_matching_notes(
                            session, api_url, deck, note_type,
                            unique_field, fields[unique_field],
                        )
                        filled_any = False
                        for info in matched:
                            fields_to_fill = {
                                name: value
                                for name, value in fields.items()
                                if value.strip()
                                and (
                                    not info.get("fields", {}).get(name, {}).get("value", "").strip()
                                    or (args.regenerate_audio and name.endswith("_Audio"))
                                )
                            }
                            if fields_to_fill:
                                call_api(
                                    session,
                                    api_url,
                                    "updateNoteFields",
                                    {
                                        "note": {
                                            "id": info["noteId"],
                                            "fields": fields_to_fill,
                                        }
                                    },
                                )
                                filled_any = True
                        if filled_any:
                            updated += 1
                            print(
                                f"Filled empty fields: {sheet} row {row_number}: "
                                f"{fields[unique_field]}"
                            )
                            continue
                    skipped += 1
                    print(
                        f"Skip duplicate: {sheet} row {row_number}: "
                        f"{fields[unique_field]}"
                    )
                    continue
                note_id = call_api(
                    session,
                    api_url,
                    "addNote",
                    {
                        "note": {
                            "deckName": deck,
                            "modelName": note_type,
                            "fields": fields,
                            "tags": tags,
                            "options": {"allowDuplicate": False},
                        }
                    },
                )
                if note_id is None:
                    skipped += 1
                    print(
                        f"Skip duplicate: {sheet} row {row_number}: "
                        f"{fields[unique_field]}"
                    )
                else:
                    created += 1
            except Exception as err:
                failures.append(f"{sheet} row {row_number}: {err}")

        print(
            f"Created: {created}; updated existing: {updated}; "
            f"skipped duplicates: {skipped}; failed: {len(failures)}"
        )
        for failure in failures:
            print(f"ERROR {failure}", file=sys.stderr)

        if args.sync and not failures:
            try:
                call_api(session, api_url, "sync", timeout=300)
                print("AnkiWeb sync completed.")
            except Exception as err:
                failures.append(f"AnkiWeb sync failed: {err}")
                print(f"AnkiWeb sync failed: {err}", file=sys.stderr)
        elif args.sync:
            print("Sync skipped because some notes failed.", file=sys.stderr)

        try:
            print("Current note counts by deck:")
            counts = deck_note_counts(session, api_url)
            for deck_name, count in counts:
                print(f"  {deck_name}: {count}")
            print(f"  Total: {sum(count for _, count in counts)}")
        except Exception as err:
            failures.append(f"Could not retrieve deck note counts: {err}")
            print(f"Could not retrieve deck note counts: {err}", file=sys.stderr)

        return 1 if failures else 0
    except Exception as err:
        print(f"Import failed: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
