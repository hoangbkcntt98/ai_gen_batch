# Standalone image batch

Requires [muse2api](https://github.com/czg86389-hub/muse2api).

This folder runs independently of the Anki add-on repository. Copy the entire
folder and edit `config.json`. Create a virtual environment, install dependencies,
and run:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python batch_image_generate.py --dry-run
python batch_image_generate.py
```

On Windows PowerShell, use `.venv\Scripts\Activate.ps1` to activate the
environment after creating it with `py -m venv .venv`. If `venv` is unavailable
on Debian/Ubuntu, install the `python3-full` package first.

Set PostgreSQL and image API values in `config.json`. Set
`services.image_generator.media_dir` to Anki's `collection.media` directory for
local storage. `services.image_generator.upload_provider` supports `local` (default),
`oci`, and `drive`. Local uploads save generated images in `media_dir`.
For OCI, set `services.image_generator.image_base_path` to an Object Storage
pre-authenticated request URL with object-create permission. For Google Drive,
set it to the destination folder ID and place OAuth Desktop client credentials
in `client.json` beside `config.json`. First Drive run opens Google OAuth flow
and saves refresh credentials to `token.json`; keep both files private.
Drive uploads receive public reader permission so Anki can load their image URLs.
Remote providers store full image URLs in `batch_jobs.result`; note `Image`
fields embed those URLs. Normal remote generation does not write to `media_dir`.
`run_batch.sh` starts with two task options: `1) anki_image_generate` runs the
normal batch, and `2) upload media_dir images to OCI` copies existing `.png`,
`.jpg`, `.jpeg`, `.webp`, `.gif`, or `.bmp` files from `media_dir` to the
configured OCI request URL immediately, without config prompts or image
generation. Option 2 uses default `config.json`; set `CONFIG` or `MEDIA_DIR`
environment variables to override paths. It needs `upload_provider: "oci"` and
uses `image_base_path`.
Generated image names use the existing sanitized word/kanji prefix plus
`anki_note_id`, for example `語彙_123456.png`. Set top-level `log_path` to the
base directory for timestamped batch log folders; relative paths resolve from
the config file's directory.
The script finds non-empty `ImagePrompt` values in both configured note tables,
inserts one history row per eligible note, then generates images in the same
run. A matching `completed` or `processing` history row prevents another
attempt. With `skip_existing_image: true`, notes that already have an image are
also ignored. `error` rows can be retried with `--recovery`.
`limit: null` processes all eligible prompts; use `--limit N` to cap a run.
Use `--step N` to process up to N jobs concurrently (default: 1). Use
`--recovery` to retry `error` history rows only when their
`(object_type, anki_note_id)` has no `completed` row. `batch_jobs.id` stores the
source row's `anki_note_id`; workers resolve that value back to the source row
before updating it. New and recovered jobs receive a
`locked_by_task_id` stores Python process PID. Job outcomes and
successful file names are stored in `result`; error messages are stored in
`response`.
`services.image_generator.clean_error` defaults to `true` and deletes all `error`
history rows before a normal run. The `run_batch.sh` prompt can override it.
Cleanup is skipped during `--recovery` so error rows remain available to retry.
`run_batch.sh` also prompts `Set Images If Completed` (default `true`); it runs
`setting_image_to_notes.py --replace-note-image` after the batch exits, including
when some jobs fail. This post-step is skipped for dry runs. It only processes
completed rows for the configured image job type and ignores completed jobs
whose result says the existing image was skipped.

The `batch_job` queue table must already exist with the documented schema.
Manual queue inserts are not required.

## Generate word and kanji note fields

`word_kanji_generate.py` fills missing learning fields in both configured
`anki_word` and `anki_kanji` tables through an OpenAI-compatible chat API. Set
`services.word_generator.chat_api_base`, `chat_model`, and `chat_api_key` in
`config.json` to a chat-completions endpoint; image API settings are separate.
The config supports a total `limit`, `overwrite_existing_fields`, per-table
`skip_fields`, `temperature`, and `timeout_seconds`.

Preview eligible rows, then run generation:

```bash
.venv/bin/python word_kanji_generate.py --dry-run
.venv/bin/python word_kanji_generate.py --type all
```

Use `--type word` or `--type kanji` to run one table, `--limit N` to override
the configured total limit, and `--overwrite` / `--no-overwrite` to override
the config setting. Existing generated fields remain unchanged unless overwrite
is enabled. A row listed by `skip_fields` is excluded when any listed field is
non-empty.

## Import spreadsheet rows into Anki

Set `anki_import.note_types.word` and `anki_import.note_types.kanji` in
`config.json` to existing Anki note type names. Export the Google Sheet, start
the local AnkiConnect server, then preview:

```bash
python export_google_sheet.py
./anki_connect_server.sh start
python import_spreadsheet_to_anki.py --dry-run
```

Importer reads every tab in `spreadsheet_export.txt`. Rows with a non-empty
`Word` use configured word note type; rows with a non-empty `Kanji` use
configured kanji note type. Exact tag matching against existing Anki deck names
selects destination deck; row fails if no deck tag or multiple deck tags match.
The deck-matching tag remains attached to note along with other tags. Matching
Anki fields are imported directly when header matches; for example,
`Example1_Source` maps to same-named field. If no exact field exists, importer
tries `_Source` aliases such as `Meaning_Source` to `MeaningSource` or `Meaning`.
`Tags` values split on semicolons. Use `--field-map
'{"Meaning_Source":"MeaningSource"}'` to override field mapping. Import skips
notes Anki identifies as duplicates. After import, script prints current note
counts for every deck and collection total. Add `--sync` to sync the collection
with AnkiWeb after a successful import:

```bash
python import_spreadsheet_to_anki.py --sync
```

During import, the configured `services.word_generator` chat API fills empty
text-learning fields supported by each note type, including `Reading`,
`MeaningDestination`, `Usage`, `Notes`, Chinese readings, and applicable
example/vocabulary fields. It receives spreadsheet and mapped note values as
context and returns JSON keyed by missing Anki field names. Existing values
remain unchanged. Word `ImagePrompt` uses the configured prompt template;
kanji `ImagePrompt` is chat-generated. `Image` and `*_Audio` are tool-generated
fields. Importer generates supported audio fields with Edge TTS, stores each
MP3 through AnkiConnect `storeMediaFile`, then writes `[sound:...]` into note
fields. Word example audio uses `ExampleN_Source`, kanji example audio uses
`ExampleN_JP`, and reading/vocabulary audio uses its matching text field.
Configure `services.tts_generator.api_base`, `model`, `api_key`, and `timeout_seconds` in
`config.json`. The importer calls the configured `/audio/speech` endpoint,
stores MP3 through AnkiConnect `storeMediaFile`, then writes `[sound:...]` into
note fields. New-note filenames use the returned Anki note ID. For example, the
model can be `edge-tts/ja-JP-KeitaNeural`; keep API keys private. Word example
audio uses `ExampleN_Source`, kanji example audio uses `ExampleN_JP`, and
reading/vocabulary audio uses its matching text field. `--sync` runs after note
creation and audio storage succeed. `--dry-run` reports planned tool fields but
does not call generation APIs. Install dependencies with
`python -m pip install -r requirements.txt`.

On duplicate notes, importer still creates and fills only missing audio fields
without `--update-existing`; other note fields remain untouched. `--sync`
syncs those audio updates too.
Use `--regenerate-audio` to replace existing audio fields and media files:

```bash
python import_spreadsheet_to_anki.py --regenerate-audio --sync
```

Use `--update-existing` to fill missing mapped fields on matching notes from a
previous import. It only fills empty fields and leaves existing field content
unchanged:

```bash
python import_spreadsheet_to_anki.py --update-existing --sync
```

The matching decks and note types must already exist in Anki. Anki must be able
to access AnkiWeb for sync to succeed.

## Reusable Python services

CLI scripts remain supported. Business APIs are also available as reusable
classes:

- `models.ImageJob`, `models.NoteDraft`, and `models.ImportResult`
- `services.ImageGenerator` for queued image generation/storage
- `services.WordGenerator` for missing Chat API fields
- `services.TTSGenerator` for configured speech API audio
- `services.AnkiConnector` for AnkiConnect actions, deck mapping, and sync
- `services.GoogleSheetsService` for reading spreadsheet tabs

Example: `from services import AnkiConnector, WordGenerator`.

### Test services offline

Tests use mocks; they do not call AnkiConnect, Google, Chat/TTS/Image APIs, or PostgreSQL.

```bash
python test_services.py                 # all services
python test_services.py anki             # one service
python test_services.py google
python test_services.py image
python test_services.py jobs
python test_services.py tts
python test_services.py word
```

Equivalent unittest discovery: `python -m unittest discover -s tests -v`.

## Create Anki notes from a spreadsheet

`spreadsheet_anki_creator.py` reads CSV/TSV exports or Google Sheets, maps
`Word`/`Kanji` rows and `Tags` to configured note types/decks, generates missing
text fields, creates the note, then fills missing audio and image fields.
Configure `anki_import.note_types`, Chat/TTS/Image API settings, and
`services.image_generator.media_dir` in `config.json`.

```bash
python spreadsheet_anki_creator.py --input spreadsheet_export.txt --dry-run
python spreadsheet_anki_creator.py --input spreadsheet_export.txt --sync
python spreadsheet_anki_creator.py --spreadsheet-id YOUR_SHEET_ID --sync
python spreadsheet_anki_creator.py --input "https://docs.google.com/spreadsheets/d/YOUR_SHEET_ID/edit" --dry-run
python spreadsheet_anki_creator.py --input spreadsheet_export.txt --skip-images --skip-tts
```

Google Sheets reads use `account_service.json` beside the config by default;
override with `--credentials`. Dry-run reads rows and queries Anki metadata but
does not generate content, create notes, or sync. `--sync` syncs after note and
media updates complete. `--skip-images` and `--skip-tts` disable those generators;
defaults can also be set with `services.spreadsheet_anki_creator.skip_images` and
`services.spreadsheet_anki_creator.skip_tts`.

## Complete fields on existing Anki notes

`batch/anki_notes.py` fetches existing notes through AnkiConnect, then runs word,
image, and TTS generation in order. Only notes with changed fields get a
`batch_jobs` history row; skipped notes do not. Failed generation attempts also
get an error history row. Failures are recorded in the log and do not stop later
notes. Logs record `status=updated`
with `filled_fields` and `created_fields`, `status=skipped` when no fields changed,
or `status=error` on failure. This batch updates existing notes; it does not create
new Anki notes. Set
`services.anki_notes.log_dir` to choose log location.
`--limit N` counts notes whose fields actually changed; complete notes do not
consume the limit.

```bash
python -m batch.anki_notes --limit 10
```

For local storage, run `python setting_image_to_notes.py` to rename completed
image files and update their `result` values. Local directory comes from
`services.image_generator.image_base_path` when set, otherwise `media_dir`. The
script records `(old_result, new_result)` pairs in `temp.txt` before updating
the database. For OCI and Drive, remote rename is unsupported; passing
`--replace-note-image` updates note `Image` fields with an HTML `<img>` tag.
For local storage, tag `src` uses the renamed image filename, as Anki expects
for files in `collection.media`. For OCI and Drive,
tag `src` uses the stored remote URL; OCI filename-only `result` values are
combined with `image_base_path`, while existing full URLs remain unchanged.

## Run in the background on Linux

Generate missing images for existing Anki notes:

```bash
python -m batch.images --limit 100 --step 10
```

`limit` caps eligible notes; `step` groups image generation into chunks. Both
can be set in `services.image_generator` (`limit: null` means no cap). The
batch skips notes with a non-empty `Image`, missing `ImagePrompt`, or missing
`Word`/`Kanji`. It syncs Anki before scanning and after each chunk.

Run any Python script repeatedly with a watcher:

```bash
./watcher.sh start batch/anki_notes.py --interval 60s
./watcher.sh status batch/anki_notes.py
./watcher.sh pid batch/anki_notes.py
./watcher.sh stop batch/anki_notes.py
```

Watcher runs script immediately, then waits given interval after each run
finishes. It does not overlap runs. Interval accepts seconds (`60s`), minutes
(`5m`), or hours (`1h`). Python stdout/stderr goes to
`batch_logs/anki_notes.log`; watcher events and exit codes go to
`batch_logs/anki_notes.watcher.log`; PID goes to
`batch_logs/anki_notes.watcher.pid`. The active Python child PID is stored in
`batch_logs/anki_notes.python.pid`. The watcher runs from the project root and
adds that root to `PYTHONPATH`, so scripts under `batch/` can import
`helpers`, `services`, and `models`. A per-script `batch_logs/{filename}.lock`
uses an OS file lock to prevent overlapping runs; a locked run is logged as
`SKIP` and retried after the next interval. Add script arguments after `--`.

From this folder:

```bash
nohup .venv/bin/python -u batch_image_generate.py > batch.log 2>&1 &
echo $! > batch.pid
```

Use `nohup .venv/bin/python -u batch_image_generate.py --limit 20 > batch.log 2>&1 &`
to cap a run. Follow logs with `tail -f batch.log`; stop with
`kill "$(cat batch.pid)"`.

To check every 60 seconds and start image generation and word/kanji generation
when neither corresponding batch is active:

```bash
./batch_image_watchdog.sh start
./batch_image_watchdog.sh status
./batch_image_watchdog.sh stop
```

The watchdog checks immediately, then every 60 seconds by default. It runs
`run_batch.sh` for images and `.venv/bin/python word_kanji_generate.py --type all`
for word/kanji fields. Word/kanji output goes to
`batch_logs/word_kanji_generate.log`. Set
`WATCH_INTERVAL_SECONDS` when starting it to change the interval, for example
`WATCH_INTERVAL_SECONDS=300 ./batch_image_watchdog.sh start` for five minutes.
Its output goes to `batch_logs/watchdog.log`. Stopping the watchdog does not
stop an image batch already running.

## Run in the background on Windows (PowerShell)

From this folder, start the batch without keeping the terminal open:

```powershell
$job = Start-Process -FilePath (Join-Path $PWD ".venv\Scripts\python.exe") `
  -ArgumentList @("-u", "batch_image_generate.py") `
  -WorkingDirectory $PWD `
  -RedirectStandardOutput (Join-Path $PWD "batch.log") `
  -RedirectStandardError (Join-Path $PWD "batch-error.log") `
  -WindowStyle Hidden -PassThru
$job.Id | Set-Content batch.pid
```

Use `-ArgumentList @("-u", "batch_image_generate.py", "--limit", "20")` to cap a run.
Check progress in `batch.log` and errors in `batch-error.log`. Stop the process
with `Stop-Process -Id (Get-Content batch.pid)`.
