"""AnkiConnect client and reusable deck/note helpers."""
from __future__ import annotations

import json
from typing import Any

import requests


class AnkiConnector:
    def __init__(
        self,
        api_url: str = "http://127.0.0.1:8765",
        session: requests.Session | None = None,
        api_version: int = 6,
    ) -> None:
        self.api_url = api_url
        self.session = session or requests.Session()
        self.api_version = api_version

    def call(self, action: str, params: dict[str, Any] | None = None, timeout: int = 60):
        response = self.session.post(
            self.api_url,
            json={
                "action": action,
                "version": self.api_version,
                "params": params or {},
            },
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("error"):
            raise RuntimeError(f"AnkiConnect {action} failed: {payload['error']}")
        return payload.get("result")

    def note_type_for(self, identity: str, configured_types: dict[str, str]) -> str:
        note_type = str(configured_types.get(identity.lower(), "")).strip()
        if not note_type:
            raise ValueError(f"No note type configured for {identity!r}.")
        return note_type

    def deck_for_tags(self, tags: list[str], known_decks: list[str]) -> str:
        matches = [tag for tag in tags if tag in known_decks]
        if len(matches) != 1:
            reason = "no matching deck tag" if not matches else "multiple matching deck tags"
            raise ValueError(f"{reason}; tags={tags}")
        return matches[0]

    def sync(self, timeout: int = 300):
        return self.call("sync", timeout=timeout)

    def store_media_file(self, filename: str, encoded_data: str, timeout: int = 60):
        return self.call(
            "storeMediaFile",
            {"filename": filename, "data": encoded_data},
            timeout=timeout,
        )

    def add_note(
        self,
        deck: str,
        note_type: str,
        fields: dict[str, str],
        tags: list[str] | None = None,
    ):
        return self.call(
            "addNote",
            {
                "note": {
                    "deckName": deck,
                    "modelName": note_type,
                    "fields": fields,
                    "tags": tags or [],
                    "options": {"allowDuplicate": False},
                }
            },
        )

    def has_note_field_value(
        self, note_type: str, field_name: str, value: str
    ) -> bool:
        """Check exact field value among notes of one model, across decks."""
        query = f'note:"{note_type}"'
        note_ids = self.call("findNotes", {"query": query})
        for offset in range(0, len(note_ids), 200):
            notes = self.call(
                "notesInfo", {"notes": note_ids[offset : offset + 200]}
            )
            if any(
                note.get("modelName") == note_type
                and note.get("fields", {}).get(field_name, {}).get("value", "").strip()
                == value.strip()
                for note in notes
            ):
                return True
        return False

    def update_note_fields(self, note_id: int, fields: dict[str, str]):
        return self.call(
            "updateNoteFields",
            {"note": {"id": note_id, "fields": fields}},
        )

    def note_counts_by_deck(self) -> list[tuple[str, int]]:
        counts = []
        for deck_name in self.call("deckNames"):
            query = f"deck:{json.dumps(deck_name, ensure_ascii=False)}"
            note_ids = self.call("findNotes", {"query": query})
            counts.append((deck_name, len(note_ids)))
        return counts

    def all_notes(
        self, chunk_size: int = 200, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """Fetch every note with full fields, model name, and deck metadata."""
        if chunk_size < 1:
            raise ValueError("chunk_size must be positive.")
        if limit is not None and limit < 1:
            raise ValueError("limit must be positive or None.")
        note_ids = self.call("findNotes", {"query": ""})
        if limit is not None:
            note_ids = note_ids[:limit]
        notes: list[dict[str, Any]] = []
        for offset in range(0, len(note_ids), chunk_size):
            chunk = note_ids[offset : offset + chunk_size]
            notes.extend(self.call("notesInfo", {"notes": chunk}))
        return notes
