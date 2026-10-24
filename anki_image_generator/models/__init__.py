"""Reusable data models shared by command-line tools and services."""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ImageJob:
    job_id: int
    object_type: str
    note_id: int


@dataclass
class NoteDraft:
    sheet: str
    row_number: int
    deck: str
    note_type: str
    fields: dict[str, str]
    tags: list[str] = field(default_factory=list)
    source_row: dict[str, str] = field(default_factory=dict)


@dataclass
class ImportResult:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def failed(self) -> int:
        return len(self.failures)

    def as_dict(self) -> dict[str, Any]:
        return {
            "created": self.created,
            "updated": self.updated,
            "skipped": self.skipped,
            "failed": self.failed,
            "failures": list(self.failures),
        }

@dataclass
class AnkiNotesResult:
    processed: int = 0
    succeeded: int = 0
    failed: int = 0
