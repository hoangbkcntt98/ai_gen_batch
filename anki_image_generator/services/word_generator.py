"""Reusable adapter for generating missing Word/Kanji note fields."""
from __future__ import annotations

import json
import re
from typing import Any

import requests

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





class WordGenerator:
    """Generate absent text fields using configured Chat API.

    Existing prompt and field policy stay centralized in the legacy importer
    during migration, while callers use this stable service interface.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    def generate_missing_fields(
        self,
        row: dict[str, str],
        fields: dict[str, str],
        model_fields: list[str],
    ) -> dict[str, str]:
        return generate_missing_fields(row, fields, model_fields, self.config)
