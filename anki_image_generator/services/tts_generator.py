"""TTS API client for generating Anki audio fields."""
from __future__ import annotations

import base64
import hashlib
import html
import re
import uuid
from typing import Any, Callable

import requests


def _plain_text(value: str) -> str:
    value = re.sub(r"<(?:br\b[^>]*|/p\s*|/div\s*)>", " ", value, flags=re.I)
    return html.unescape(re.sub(r"<[^>]+>", "", value)).strip()


def _source_fields(audio_field: str) -> tuple[str, ...]:
    example = re.fullmatch(r"Example(\d+)_Audio", audio_field)
    if example:
        number = example.group(1)
        return (f"Example{number}_JP", f"Example{number}_Source")
    if audio_field == "Reading_Audio":
        return ("Reading", "Word")
    if audio_field == "RequiredVocabulary_Audio":
        return ("RequiredVocabulary", "Kanji")
    return ()


class TTSGenerator:
    def __init__(
        self,
        config: dict[str, Any],
        request_post: Callable[..., Any] = requests.post,
    ) -> None:
        self.config = config
        self.request_post = request_post

    def synthesize(self, text: str) -> bytes:
        settings = self.config.get("services", {}).get(
            "tts_generator", self.config.get("batch", {}).get("tts", {})
        )
        base = str(settings.get("api_base", "")).strip().rstrip("/")
        model = str(settings.get("model", "")).strip()
        api_key = str(settings.get("api_key", "")).strip()
        if not base or not model:
            raise RuntimeError(
                "Configure services.tts_generator.api_base and model in config.json."
            )
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        response = self.request_post(
            f"{base}/audio/speech",
            json={"model": model, "input": text},
            headers=headers,
            timeout=int(settings.get("timeout_seconds", 120)),
        )
        response.raise_for_status()
        audio = response.content
        if not audio:
            raise RuntimeError("TTS API returned empty audio.")
        return audio

    def generate_audio_fields(
        self,
        fields: dict[str, str],
        model_fields: list[str],
        store_media: Callable[[str, str], Any],
        force: bool = False,
        note_id: int | None = None,
        identity: str = "note",
    ) -> dict[str, str]:
        for audio_field in model_fields:
            if not audio_field.endswith("_Audio"):
                continue
            if fields.get(audio_field, "").strip() and not force:
                continue
            source_field = next(
                (name for name in _source_fields(audio_field) if fields.get(name, "").strip()),
                None,
            )
            if not source_field:
                continue
            text = _plain_text(fields[source_field])
            if not text:
                continue
            audio_data = self.synthesize(text)
            digest = hashlib.sha256(f"{audio_field}\0{text}".encode("utf-8")).hexdigest()[:12]
            prefix = str(identity or "note")
            filename = (
                f"{prefix}_{note_id}_{audio_field.lower()}.mp3"
                if note_id is not None
                else f"{prefix}_{audio_field.lower()}_{digest}_{uuid.uuid4().hex[:8]}.mp3"
            )
            store_media(filename, base64.b64encode(audio_data).decode("ascii"))
            fields[audio_field] = f"[sound:{filename}]"
        return fields
