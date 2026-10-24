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

WORD_SCHEMA = (
    "Word", "Reading", "MeaningSource", "MeaningDestination", "Usage",
    *(f"Example{index}_{language}" for index in range(1, 7) for language in ("Source", "Destination")),
    "ImagePrompt", "Notes", "Chineses",
)
KANJI_SCHEMA = (
    "Kanji", "RequiredVocabulary", "MeaningJP", "MeaningVI", "Chinese", "OnYomi", "KunYomi",
    "Radical", "RadicalName", "RadicalMeaning", "PhoneticPart",
    *(f"Vocabulary{index}_{language}" for index in range(1, 7) for language in ("JP", "VN")),
    *(f"Example{index}_{language}" for index in range(1, 7) for language in ("JP", "VN")),
    "Mnemonic", "ImagePrompt", "RelatedKanji", "Notes",
)


def _call_json_api(prompt: str, config: dict[str, Any]) -> dict[str, Any]:
    batch_config = config.get("services", {}).get(
        "word_generator", config.get("batch", {}).get("word_kanji_generate", {})
    )
    base = str(batch_config.get("chat_api_base", config.get("chat_api_base", ""))).strip().rstrip("/")
    model = str(batch_config.get("chat_model", config.get("chat_model", ""))).strip()
    api_key = str(batch_config.get("chat_api_key", config.get("chat_api_key", ""))).strip()
    if not base or not model:
        raise RuntimeError(
            "Configure services.word_generator.chat_api_base and chat_model in config.json."
        )
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "Return only one valid JSON object. Use string values only. Do not return Markdown or text outside JSON.",
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": float(batch_config.get("temperature", 0.2)),
        "response_format": {"type": "json_object"},
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    timeout = int(batch_config.get("timeout_seconds", config.get("timeout_seconds", 120)))
    response = requests.post(
        base + "/chat/completions", json=payload, headers=headers, timeout=timeout
    )
    if response.status_code in (400, 422) and "response_format" in response.text:
        payload.pop("response_format", None)
        response = requests.post(
            base + "/chat/completions", json=payload, headers=headers, timeout=timeout
        )
    response.raise_for_status()
    content = response.json()["choices"][0]["message"]["content"]
    if isinstance(content, list):
        content = "".join(
            str(item.get("text", "")) for item in content if isinstance(item, dict)
        )
    generated_text = re.sub(
        r"^```(?:json)?\s*|\s*```$", "", str(content).strip(), flags=re.I
    )
    generated = json.loads(generated_text)
    if not isinstance(generated, dict):
        raise RuntimeError("Chat API response must be a JSON object.")
    return generated


def _generate_schema_fields(
    identity: str,
    row: dict[str, str],
    fields: dict[str, str],
    model_fields: list[str],
    config: dict[str, Any],
    allow_new_examples: bool = False,
) -> None:
    schema = WORD_SCHEMA if identity == "Word" else KANJI_SCHEMA
    value = row.get(identity, "").strip()
    settings = config.get("services", {}).get("word_generator", {})
    source_language = str(
        settings.get("source_language", config.get("source_language", "Japanese"))
    ).strip() or "Japanese"
    destination_language = (
        str(settings.get(
            "destination_language", config.get("destination_language", "Vietnamese")
        )).strip() or "Vietnamese"
    )
    user_level = str(settings.get("user_level", config.get("user_level", "N3"))).strip().upper()
    if user_level not in {"N1", "N2", "N3", "N4", "N5"}:
        user_level = "N3"
    required_vocabulary = (
        fields.get("RequiredVocabulary", "").strip()
        or row.get("RequiredVocabulary", "").strip()
    )
    bootstrap_pairs = not _has_existing_pair_source(identity, fields)
    output_fields = [
        name for name in schema
        if _should_generate_paired_field(
            identity, name, fields, allow_new_examples,
            bootstrap_pairs=bootstrap_pairs,
        )
    ]
    output_schema = {name: "" for name in output_fields}
    input_data = {
        "mode": identity.lower(),
        "value": value,
        "source_language": source_language,
        "destination_language": destination_language,
        "user_level": user_level,
        "required_vocabulary": required_vocabulary if identity == "Kanji" else "",
    }
    prompt = (
        "Bạn tạo dữ liệu Anki cho người học ngoại ngữ. Đọc INPUT, xử lý theo MODE, "
        "rồi chỉ trả về đúng một JSON object hợp lệ. Không trả Markdown, lời dẫn, "
        "hoặc nội dung ngoài JSON. Mọi giá trị phải là chuỗi; field không dùng hoặc "
        "không có dữ liệu thì để \"\".\n\n"
        f"INPUT\n{json.dumps(input_data, ensure_ascii=False)}\n\n"
        "Quy tắc chung:\n"
        "- Nếu user_level không thuộc N1, N2, N3, N4, N5, dùng N3.\n"
        "- Chỉ dùng field thuộc schema tương ứng với mode. Không thêm key.\n"
        "- Luôn trả đủ mọi key trong schema; field không áp dụng thì ghi \"\".\n"
        "- Nội dung chính xác, tự nhiên, phù hợp user_level. Không bịa từ nguyên hoặc thông tin không chắc chắn.\n"
    )
    if identity == "Word":
        example_rule = (
            "Tạo 1-6 cặp ví dụ đầy đủ cho note mới. "
            if allow_new_examples else
            (
                "Tạo tối đa 3 cặp ví dụ bao quát nghĩa phổ biến; "
                if bootstrap_pairs else
                "Chỉ dịch ví dụ source đã có. "
            )
            + "Tuân theo chính xác các field ví dụ được yêu cầu; không tự thêm key hay cặp khác. "
        )
        prompt += (
            f"\nMODE word: Phân tích value như từ/cụm từ trong {source_language}. "
            f"MeaningSource và Example*_Source dùng {source_language}; "
            f"MeaningDestination và Example*_Destination dùng {destination_language}. "
            f"{example_rule}Reading dùng cách đọc phù hợp source_language; "
            "với tiếng Nhật dùng hiragana. Usage mô tả ngắn cách dùng, sắc thái hoặc kết hợp từ; "
            "có thể dùng HTML đơn giản. Chỉ điền Chineses khi source_language là Japanese, "
            "destination_language là Vietnamese và từ có Hán tự; ghi âm Hán Việt IN HOA, "
            "trường hợp khác để trống. Ghi value nguyên dạng vào Word. "
            "ImagePrompt không tạo ở lượt này, luôn trả ImagePrompt là chuỗi rỗng.\n"
            f"Schema word: {json.dumps(output_schema, ensure_ascii=False)}"
        )
    else:
        example_rule = (
            "Tạo cặp ví dụ JP/VN đầy đủ cho note mới. "
            if allow_new_examples else
            (
                "Tạo tối đa 3 cặp ví dụ bao quát nghĩa phổ biến; "
                if bootstrap_pairs else
                "Chỉ dịch ví dụ JP đã có; "
            )
            + "Tuân theo chính xác các field ví dụ/từ vựng được yêu cầu; không tự thêm key hay cặp khác. "
        )
        prompt += (
            "\nMODE kanji: Phân tích đúng một chữ Kanji trong value. Tạo số từ vựng tối thiểu "
            "để đại diện cách đọc On/Kun thông dụng, tối đa 6 cặp. Nếu required_vocabulary "
            "không rỗng, ghi chính xác từ đó vào Vocabulary1_JP, kèm cách đọc trong ngoặc "
            "và nghĩa tiếng Việt ở Vocabulary1_VN; không thay bằng từ khác. Chỉ điền "
            "VocabularyN_VN khi VocabularyN_JP có dữ liệu. Nếu Vocabulary1_JP đã có dữ liệu, "
            "không tự tạo VocabularyN_JP với N > 1; chỉ dịch VocabularyN_JP đã có. Ví dụ dùng từ vựng "
            f"đã chọn, phù hợp user_level. {example_rule}"
            "Chinese là âm Hán Việt IN HOA; nhiều âm ngăn cách bằng ・. Mnemonic ngắn, "
            "dựa trên cấu trúc chữ; không bịa nguồn gốc Kanji. Ghi required_vocabulary nguyên dạng "
            "vào RequiredVocabulary. ImagePrompt không tạo ở lượt này, luôn trả ImagePrompt là chuỗi rỗng.\n"
            f"Schema kanji: {json.dumps(output_schema, ensure_ascii=False)}"
        )

    generated = _call_json_api(prompt, config)
    extra = set(generated) - set(schema)
    if extra:
        raise RuntimeError(f"Chat API returned fields outside the {identity} schema: {sorted(extra)}")
    for name, value_from_api in generated.items():
        if not isinstance(value_from_api, str):
            raise RuntimeError(f"Chat API field {name!r} must be a string.")
    for name in output_fields:
        if name == "ImagePrompt" or name not in model_fields:
            continue
        if not _should_generate_paired_field(
            identity, name, fields, allow_new_examples,
            bootstrap_pairs=bootstrap_pairs,
        ) or not _paired_source_present(identity, name, fields):
            continue
        if not fields.get(name, "").strip():
            fields[name] = generated.get(name, "").strip()
    if identity in model_fields and not fields.get(identity, "").strip():
        fields[identity] = value
    if identity == "Kanji" and "RequiredVocabulary" in model_fields:
        if not fields.get("RequiredVocabulary", "").strip() and required_vocabulary:
            fields["RequiredVocabulary"] = required_vocabulary


def _should_generate_paired_field(
    identity: str,
    field: str,
    fields: dict[str, str],
    allow_new_examples: bool = False,
    bootstrap_pairs: bool = False,
) -> bool:
    match = re.fullmatch(
        r"(Example|Vocabulary)(\d+)_(Source|Destination|JP|VN)", field
    )
    if not match:
        return True
    pair_type, number, part = match.groups()
    if not allow_new_examples:
        if bootstrap_pairs and pair_type == "Example":
            return int(number) <= 3
        if _has_existing_pair_source(identity, fields):
            if pair_type == "Example":
                source = "Source" if identity == "Word" else "JP"
                return part in {"Destination", "VN"} and bool(
                    fields.get(f"Example{number}_{source}", "").strip()
                )
            return pair_type == "Vocabulary" and part == "VN" and bool(
                fields.get(f"Vocabulary{number}_JP", "").strip()
            )
        return pair_type == "Example" and int(number) <= 3
    if identity == "Word":
        if pair_type != "Example":
            return True
        if allow_new_examples:
            return True
        return part == "Destination" and bool(
            fields.get(f"Example{number}_Source", "").strip()
        )
    if pair_type == "Example":
        if allow_new_examples:
            return True
        return part == "VN" and bool(fields.get(f"Example{number}_JP", "").strip())
    if int(number) > 1:
        if part == "JP":
            return not bool(fields.get("Vocabulary1_JP", "").strip())
        return part == "VN" and bool(fields.get(f"Vocabulary{number}_JP", "").strip())
    return part == "JP" or (
        part == "VN" and bool(fields.get(f"Vocabulary{number}_JP", "").strip())
    )


def _has_existing_pair_source(identity: str, fields: dict[str, str]) -> bool:
    suffix = "Source" if identity == "Word" else "JP"
    if any(
        fields.get(f"Example{index}_{suffix}", "").strip()
        for index in range(1, 7)
    ):
        return True
    return identity == "Kanji" and any(
        fields.get(f"Vocabulary{index}_JP", "").strip()
        for index in range(1, 7)
    )


def _paired_source_present(identity: str, field: str, fields: dict[str, str]) -> bool:
    match = re.fullmatch(
        r"(Example|Vocabulary)(\d+)_(Source|Destination|JP|VN)", field
    )
    if not match:
        return True
    pair_type, number, part = match.groups()
    if pair_type == "Example" and part in {"Destination", "VN"}:
        source = "Source" if identity == "Word" else "JP"
        return bool(fields.get(f"Example{number}_{source}", "").strip())
    if pair_type == "Vocabulary" and part == "VN":
        return bool(fields.get(f"Vocabulary{number}_JP", "").strip())
    return True


def generate_missing_fields(
    row,
    fields,
    model_fields,
    config,
    allow_new_examples=False,
    _follow_up_for_vocabulary=True,
):
    identity = next((name for name in ("Word", "Kanji") if row.get(name, "").strip()), None)
    if identity is None:
        raise ValueError("WordGenerator requires non-empty Word or Kanji in the source row.")

    bootstrap_pairs = not allow_new_examples and not _has_existing_pair_source(
        identity, fields
    )
    vocabulary_sources_before = {
        name: fields.get(name, "").strip()
        for name in model_fields
        if re.fullmatch(r"Vocabulary\d+_JP", name)
    }
    anchor_field = "Reading" if identity == "Word" else "MeaningJP"
    if not fields.get(anchor_field, "").strip():
        _generate_schema_fields(
            identity, row, fields, model_fields, config, allow_new_examples
        )

    eligible = WORD_CHAT_FIELDS if identity == "Word" else KANJI_CHAT_FIELDS
    missing = [
        name for name in model_fields
        if (
            name in eligible
            and not fields.get(name, "").strip()
            and _should_generate_paired_field(
                identity, name, fields, allow_new_examples,
                bootstrap_pairs=bootstrap_pairs,
            )
        )
    ]
    if identity == "Word":
        missing = [
            name for name in missing
            if name != "MeaningSource"
        ]

    if missing:
        batch_config = config.get("services", {}).get(
            "word_generator", config.get("batch", {}).get("word_kanji_generate", {})
        )
        base = str(batch_config.get("chat_api_base", config.get("chat_api_base", ""))).strip().rstrip("/")
        model = str(batch_config.get("chat_model", config.get("chat_model", ""))).strip()
        api_key = str(batch_config.get("chat_api_key", config.get("chat_api_key", ""))).strip()
        if not base or not model:
            raise RuntimeError(
                "Configure services.word_generator.chat_api_base and chat_model in config.json."
            )
        source_language = str(batch_config.get(
            "source_language", config.get("source_language", "Japanese")
        )).strip()
        destination_language = str(batch_config.get(
            "destination_language", config.get("destination_language", "Vietnamese")
        )).strip()
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
        example_prompt_rule = (
            "Create complete example pairs for this new note. "
            if allow_new_examples else
            (
                "Create up to three example pairs covering the common meanings. "
                if bootstrap_pairs else
                "Only translate existing source/JP examples; do not create source examples. "
            )
        )
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
                        "Use accurate, natural, concise learning content. Fill destination/translation fields "
                        "only when their matching source field has a value; never create a destination "
                        f"for an empty source. {example_prompt_rule}"
                        "If Vocabulary1_JP already has a value, do not create "
                        "VocabularyN_JP for N > 1 while filling missing fields. Translate matching example pairs; "
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
        ordered_missing = sorted(
            missing,
            key=lambda field: bool(
                re.fullmatch(
                    r"Example\d+_(Destination|VN)", field
                )
            ),
        )
        for field in ordered_missing:
            if not _should_generate_paired_field(
                identity, field, fields, allow_new_examples,
                bootstrap_pairs=bootstrap_pairs,
            ) or not _paired_source_present(identity, field, fields):
                continue
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
    if identity == "Kanji" and _follow_up_for_vocabulary:
        new_vocabulary_source = any(
            not previous and fields.get(name, "").strip()
            for name, previous in vocabulary_sources_before.items()
        )
        if new_vocabulary_source:
            generate_missing_fields(
                row, fields, model_fields, config,
                allow_new_examples=allow_new_examples,
                _follow_up_for_vocabulary=False,
            )
    return fields


def generation_plan(row, fields, model_fields):
    identity = next((name for name in ("Word", "Kanji") if row.get(name, "").strip()), None)
    if identity is None:
        raise ValueError("WordGenerator requires non-empty Word or Kanji in the source row.")
    eligible = WORD_CHAT_FIELDS if identity == "Word" else KANJI_CHAT_FIELDS
    anchor_field = "Reading" if identity == "Word" else "MeaningJP"
    if not fields.get(anchor_field, "").strip():
        schema = WORD_SCHEMA if identity == "Word" else KANJI_SCHEMA
        chat = [
            name for name in model_fields
            if (
                name in schema
                and name != "ImagePrompt"
                and not fields.get(name, "").strip()
                and _should_generate_paired_field(identity, name, fields)
            )
        ]
    else:
        chat = [
            name for name in model_fields
            if (
                name in eligible
                and not fields.get(name, "").strip()
                and _should_generate_paired_field(identity, name, fields)
            )
        ]
    if identity == "Word" and fields.get(anchor_field, "").strip():
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
        allow_new_examples: bool = False,
    ) -> dict[str, str]:
        return generate_missing_fields(
            row,
            fields,
            model_fields,
            self.config,
            allow_new_examples=allow_new_examples,
        )
