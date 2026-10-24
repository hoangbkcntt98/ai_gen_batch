# Anki Image Generator

Python tools for creating Anki notes from spreadsheets and filling missing
word, image, and audio fields on existing notes.

## Prerequisites

- **AnkiConnect server:** Clone and run
  [anki-connect-server](https://github.com/hoangbkcntt98/anki-connect-server).
  Keep Anki available and configure its API URL in `anki_import.api_url`.
- **Image generation:** Clone and run
  [muse2api](https://github.com/hoangbkcntt98/muse2api) before using workflows
  that generate images. Configure its API endpoint in
  `services.image_generator.image_api_base`.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp config.example.json config.json
```

Edit `config.json` with AnkiConnect URL, note types, API credentials, media
directory, and database settings. Keep this file and `account_service.json`
private; do not commit credentials.

For Google Sheets, share the spreadsheet with the service account in
`account_service.json`. The importer also accepts local CSV and TSV files.

## AnkiConnect

Anki must be running with AnkiConnect available at the URL configured under
`anki_import.api_url` (default `http://127.0.0.1:8765`).

```bash
./anki_connect_server.sh start
./anki_connect_server.sh status
./anki_connect_server.sh stop
```

The server script uses paths configured inside `anki_connect_server.sh`; update
those paths for your machine before use.

## Import spreadsheet

Rows need `Word` or `Kanji`; `Tags` uses semicolons. A tag matching an existing
Anki deck selects that deck.

```bash
python spreadsheet_anki_creator.py --input spreadsheet_export.txt --dry-run
python spreadsheet_anki_creator.py --input spreadsheet_export.txt --sync
python spreadsheet_anki_creator.py --input "GOOGLE_SHEETS_URL" --skip-images --skip-tts
```

Use `--spreadsheet-id ID` to read Sheets directly. `--credentials PATH`
overrides the service-account JSON path. `--dry-run` checks rows and Anki
metadata without creating notes. `--skip-images` and `--skip-tts` disable those
generators for that run.

## Process existing notes

Fill missing word fields, images, and audio:

```bash
python -m batch.anki_notes --limit 10
```

Generate images for existing notes with blank `Image` and non-empty
`ImagePrompt`:

```bash
python -m batch.images --limit 100 --step 10
```

`--limit` counts eligible notes. `--step` sets images per chunk. Image batch
syncs Anki before scanning and after each chunk.

## Watcher

Run a Python job repeatedly. Default interval is 30 minutes; pass script options
after watcher options:

```bash
./watcher.sh start batch/images.py --limit 100 --step 10
./watcher.sh status batch/images.py
./watcher.sh pid batch/images.py
./watcher.sh stop batch/images.py
```

Override interval with `--interval`, for example `--interval 60s` or
`--interval 5m`. Python output goes to `batch_logs/{name}.log`; watcher events
go to `batch_logs/{name}.watcher.log`. The watcher uses a per-script
`batch_logs/{name}.lock` to prevent overlapping runs. Use `--` before script
arguments that could be confused with watcher options.

## Media and logs

For OCI uploads, set `services.image_generator.upload_provider` to `oci` and
configure its pre-authenticated URL in `image_base_path`:

```bash
python -m helpers.upload_media_to_oci
```

Optional flags: `--config PATH` and `--media-dir PATH`.

Delete top-level `batch_logs/*.log` files every 12 hours by running:

```bash
nohup ./cleanup_batch_logs.sh >/dev/null 2>&1 &
```

The cleanup script waits 12 hours before its first deletion. It leaves lock
files, PID files, and logs inside subdirectories untouched.

## Tests

```bash
python -m unittest discover -s tests
```
