"""Trigger Anki's normal AnkiWeb sync through AnkiConnect."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from services import AnkiConnector


def sync_from_ankiweb(config: dict[str, Any], timeout: int = 1800) -> Any:
    api_url = str(
        config.get("anki_import", {}).get("api_url", "http://127.0.0.1:8765")
    )
    return AnkiConnector(api_url).sync(timeout=timeout)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "config.json",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=1800,
        help="HTTP read timeout in seconds (default: 1800).",
    )
    args = parser.parse_args(argv)
    try:
        if args.timeout < 1:
            raise ValueError("--timeout must be positive.")
        config = json.loads(args.config.read_text(encoding="utf-8-sig"))
        if not isinstance(config, dict):
            raise ValueError("Config must be a JSON object.")
        sync_from_ankiweb(config, timeout=args.timeout)
        print("Anki sync complete.")
        return 0
    except Exception as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
