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
python spreadsheet_anki_creator.py --input words.csv kanji.tsv "GOOGLE_SHEETS_URL" --dry-run
```

`--input` accepts multiple CSV/TSV files and Google Sheets URLs; repeat
`--input` or pass multiple sources after one flag. `--spreadsheet-id` also
accepts multiple IDs. These source types can be mixed in one run.
`--credentials PATH` overrides the service-account JSON path. `--dry-run` checks rows and Anki
metadata without creating notes. `--skip-images` and `--skip-tts` disable those
generators for that run.

## Sync AnkiWeb with local Anki

Keep Anki and AnkiConnect server running, then run:

```bash
./anki_sync.sh
```

This triggers Anki's normal sync through AnkiConnect. It is bidirectional;
Anki decides whether to download, upload, or resolve conflicts. It does not
force a download-only operation. Optional flags: `--config PATH` and
`--timeout SECONDS` (default 1800; increase for large collections), for example
`./anki_sync.sh --timeout 3600`.

## Sync Anki notes to PostgreSQL

Upsert notes from configured Word and Kanji note types into `anki_word` and
`anki_kanji`:

```bash
python -m helpers.sync_anki_notes_to_postgres
```

The helper uses `pgsql_*` settings in `config.json` and matches note fields to
same-named table columns. Both tables need an `anki_note_id` column. Each run
deletes all rows from both tables, then inserts matching Anki notes in one
transaction. If import fails, PostgreSQL rolls back the deletes too.
Optional flags: `--config PATH` and `--limit N`. `--limit` still clears both
tables, so use it only when intentionally creating a partial snapshot.

## Process existing notes

Fill missing word fields, images, and audio:

```bash
python -m batch.anki_notes --limit 10
```

Skip image creation while still filling other fields and audio:

```bash
python -m batch.anki_notes --skip-images
```

Anki sync before and after this batch is disabled by default. Enable either
point independently under `services.anki_notes` in `config.json`:
`sync_before_processing` and `sync_after_processing`.

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

Run AnkiWeb sync every 5 minutes, with a 30-minute timeout per sync:

```bash
./watcher.sh start helpers/sync_ankiweb_to_local.py --interval 5m --timeout 1800
./watcher.sh status helpers/sync_ankiweb_to_local.py
./watcher.sh stop helpers/sync_ankiweb_to_local.py
```

The 5-minute interval starts after each sync finishes. The sync watcher lock is
`batch_logs/sync_ankiweb_to_local.lock`. Do not run `./anki_sync.sh` or another
sync command at the same time; those commands do not use this watcher lock.

## My WorKFlow

Start each watcher once. Replace `GOOGLE_SHEETS_URL` with the source URL; use
`--input path/to/file.csv` for a CSV/TSV file instead:

```bash
./watcher.sh start spreadsheet_anki_creator.py --interval 5m --input "GOOGLE_SHEETS_URL" --skip-images --skip-tts --defer-word-generation
./watcher.sh start batch/anki_notes.py --interval 5m --identity word --skip-images --skip-sync
./watcher.sh start batch/images.py --interval 5m --skip-sync
./watcher.sh start helpers/sync_ankiweb_to_local.py --interval 5m --timeout 1800
```

Spreadsheet watcher creates new Word notes without AI field generation, image,
audio, or sync. `anki_notes.py` fills missing Word fields and audio, but skips
images and sync. `images.py` generates images without syncing; the separate
AnkiWeb watcher handles sync. Each watcher has its own lock and waits 5 minutes
after its job finishes before next run. Check or stop each watcher with
`./watcher.sh status SCRIPT.py` or `./watcher.sh stop SCRIPT.py`.

## Media and logs

For OCI uploads, set `services.image_generator.upload_provider` to `oci` and
configure its pre-authenticated URL in `image_base_path`:

```bash
python -m helpers.upload_media_to_oci
```

Optional flags: `--config PATH` and `--media-dir PATH`.

Download images under the configured OCI PAR prefix into `media_dir`:

```bash
python -m helpers.download_oci_to_media_dir
```

The PAR must allow listing and reading objects. Existing local files are
preserved; pass `--overwrite` to replace them. The configured prefix is
removed from local paths, so a PAR prefix ending in `/images/` downloads
`images/file.png` as `media_dir/file.png`. Optional flags: `--config PATH` and
`--media-dir PATH`. Keep PAR URLs private because they grant access to OCI
objects.

If download fails with `OCI listing failed: HTTP 404`, create a new PAR for
the bucket with object read access and **Enable Object Listing** (`ListObjects`).
Use the new PAR URL in `services.image_generator.image_base_path`; listing
permission cannot be added to an existing PAR. If uploads also use this URL,
grant the required write access too. Check that the PAR is not expired or
revoked and that its bucket, namespace, and region are correct.

Convert OCI PAR URLs in existing Anki `Image` fields to local Anki media:

```bash
python -m helpers.convert_anki_image_par_links --dry-run
python -m helpers.convert_anki_image_par_links
```

The helper only removes the configured PAR base URL from matching `src`
values, leaving each filename, then updates the note. It does not download or
copy images; ensure those files already exist in Anki's media folder. `--limit`
restricts the number of notes processed.

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
