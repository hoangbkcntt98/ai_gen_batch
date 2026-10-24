"""Reusable Google Sheets reader."""
from __future__ import annotations

from pathlib import Path
from typing import Any

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]


class GoogleSheetsService:
    def __init__(self, credentials_path: Path) -> None:
        self.credentials_path = Path(credentials_path)

    def read_spreadsheet(self, spreadsheet_id: str) -> tuple[str, list[tuple[str, list[list[str]]]]]:
        try:
            from google.oauth2.service_account import Credentials
            from googleapiclient.discovery import build
        except ImportError as err:
            raise RuntimeError(
                "Google API dependencies missing. Install requirements.txt."
            ) from err
        credentials = Credentials.from_service_account_file(
            str(self.credentials_path), scopes=SCOPES
        )
        service = build("sheets", "v4", credentials=credentials, cache_discovery=False)
        metadata = service.spreadsheets().get(
            spreadsheetId=spreadsheet_id,
            fields="properties(title),sheets(properties(title))",
        ).execute()
        title = metadata.get("properties", {}).get("title", spreadsheet_id)
        tabs = []
        for item in metadata.get("sheets", []):
            tab_name = item["properties"]["title"]
            rows = service.spreadsheets().values().get(
                spreadsheetId=spreadsheet_id,
                range="'" + tab_name.replace("'", "''") + "'",
                valueRenderOption="FORMATTED_VALUE",
            ).execute().get("values", [])
            tabs.append((tab_name, rows))
        return title, tabs
