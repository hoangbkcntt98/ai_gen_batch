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
`batch.image_generate.media_dir` to Anki's `collection.media` directory for
local storage. `batch.image_generate.upload_provider` supports `local` (default),
`oci`, and `drive`. Local uploads save generated images in `media_dir`.
For OCI, set `batch.image_generate.image_base_path` to an Object Storage
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
`batch.image_generate.clean_error` defaults to `true` and deletes all `error`
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
`batch.word_kanji_generate.chat_api_base`, `chat_model`, and `chat_api_key` in
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

During import, the configured `batch.word_kanji_generate` chat API fills empty
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
Voice, rate, volume, pitch, and timeout can be set under `batch.tts`; defaults
use `ja-JP-NanamiNeural`. `--sync` runs AnkiConnect sync after note creation
and audio storage complete successfully. `--dry-run` reports planned tool
fields but does not call generation APIs. Install dependencies with
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

For local storage, run `python setting_image_to_notes.py` to rename completed
image files and update their `result` values. Local directory comes from
`batch.image_generate.image_base_path` when set, otherwise `media_dir`. The
script records `(old_result, new_result)` pairs in `temp.txt` before updating
the database. For OCI and Drive, remote rename is unsupported; passing
`--replace-note-image` updates note `Image` fields with an HTML `<img>` tag.
For local storage, tag `src` uses the renamed image filename, as Anki expects
for files in `collection.media`. For OCI and Drive,
tag `src` uses the stored remote URL; OCI filename-only `result` values are
combined with `image_base_path`, while existing full URLs remain unchanged.

## Run in the background on Linux

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
