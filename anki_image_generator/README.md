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
`batch.image_generate.media_dir` to Anki's `collection.media` directory.
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

The `batch_job` queue table must already exist with the documented schema.
Manual queue inserts are not required.

## Run in the background on Linux

From this folder:

```bash
nohup .venv/bin/python -u batch_image_generate.py > batch.log 2>&1 &
echo $! > batch.pid
```

Use `nohup .venv/bin/python -u batch_image_generate.py --limit 20 > batch.log 2>&1 &`
to cap a run. Follow logs with `tail -f batch.log`; stop with
`kill "$(cat batch.pid)"`.

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
