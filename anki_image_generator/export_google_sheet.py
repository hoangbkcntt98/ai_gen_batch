#!/usr/bin/env python3
"""Export every tab in a Google Spreadsheet to a tab-separated text file."""

import argparse
import csv
import io
import sys
from pathlib import Path

DEFAULT_SPREADSHEET_ID = "1xWrt8s9D7TJpsW59FaUpBpnRCm3hYobPa8-J5JEJ7oo"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]


def authorize(service_account_path: Path):
    try:
        from google.oauth2.service_account import Credentials
    except ImportError as err:
        raise RuntimeError(
            "Google API dependencies missing. Install with "
            "`python -m pip install -r requirements.txt`."
        ) from err

    return Credentials.from_service_account_file(
        str(service_account_path),
        scopes=SCOPES,
    )


def quote_sheet_title(title: str) -> str:
    return "'" + title.replace("'", "''") + "'"


def rows_as_tsv(rows) -> str:
    output = io.StringIO()
    writer = csv.writer(output, delimiter="\t", lineterminator="\n")
    for row in rows:
        writer.writerow(row)
    return output.getvalue()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export all tabs from a Google Spreadsheet as TSV text."
    )
    parser.add_argument("--spreadsheet-id", default=DEFAULT_SPREADSHEET_ID)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().with_name("spreadsheet_export.txt"),
    )
    parser.add_argument(
        "--credentials",
        type=Path,
        default=Path(__file__).resolve().with_name("account_service.json"),
    )
    args = parser.parse_args()

    if not args.credentials.is_file():
        print(f"Service account file not found: {args.credentials}", file=sys.stderr)
        return 2

    try:
        from googleapiclient.discovery import build

        credentials = authorize(args.credentials)
        service = build("sheets", "v4", credentials=credentials, cache_discovery=False)
        metadata = (
            service.spreadsheets()
            .get(
                spreadsheetId=args.spreadsheet_id,
                fields="properties(title),sheets(properties(title))",
            )
            .execute()
        )
        title = metadata.get("properties", {}).get("title", args.spreadsheet_id)
        lines = [f"Spreadsheet: {title}", f"ID: {args.spreadsheet_id}", ""]
        for sheet in metadata.get("sheets", []):
            sheet_title = sheet["properties"]["title"]
            values = (
                service.spreadsheets()
                .values()
                .get(
                    spreadsheetId=args.spreadsheet_id,
                    range=quote_sheet_title(sheet_title),
                    valueRenderOption="FORMATTED_VALUE",
                )
                .execute()
                .get("values", [])
            )
            lines.extend((f"=== {sheet_title} ===", rows_as_tsv(values), ""))

        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text("\n".join(lines), encoding="utf-8")
        print(f"Exported {len(metadata.get('sheets', []))} sheet(s) to {args.output}")
        return 0
    except Exception as err:
        print(f"Export failed: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
