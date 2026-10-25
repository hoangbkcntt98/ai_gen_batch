# AI Generator

Python scripts for asynchronous image and video generation with the OpenAI SDK and Requests.

## Files
- `config.json`: API settings (`base_url`, `api_key`, prompts, and model).
- `main.py`: Main script for image generation (`muse-image`) and video generation (`/videos`).
- `helper/google_drive_helper.py`: Reusable Google Drive upload helpers.
- `helper/video_generation_helper.py`: Reusable MUSE2API video generation and media helpers.
- `helper/upload_to_gg_drive.py`: Config-driven Google Drive uploader.

## Setup
1. Create and activate a virtual environment:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```
   On Debian/Ubuntu, if `venv` is unavailable, install it with
   `sudo apt install python3-venv`.
2. Install dependencies inside the virtual environment:
   ```bash
   python -m pip install -r requirements.txt
   ```
3. Set your `base_url` and `api_key` in `config.json`.
4. Copy `config.example.json` as a reference. Edit `grammar_video_batch` in
   `config.json` to set output directory, media directory, video settings, and
   polling/retry limits. Set `database_url` there or provide `DATABASE_URL` as
   an environment variable. Environment values override config.

Run one missing JLPT N1 example video with:

```bash
python grammar_example_video_batch.py --dry-run
python grammar_example_video_batch.py
```

Batch output goes to `grammar_video_batch.output_dir`; filenames include run
date (`{pattern_id}_{pattern}_example1_YYYYMMDD.mp4`).

## Upload files or a folder to Google Drive

Uploader uses OAuth as your Google account by default, so uploads go to your
personal Drive quota. Put the Desktop OAuth client file at `client.json`, set
`google_drive.folder_id` in `config.json`, then choose one upload mode. First
run opens Google sign-in and saves `token.json`; later runs reuse that token.
Keep `client.json` and `token.json` private. Set `google_drive.auth_type` to
`service_account` only when uploading to a Shared Drive.

For individual files, set:

```json
"upload_folder": "",
"upload_files": ["output/video.mp4", "output/image.png"]
```

For a folder and its contents recursively, set:

```json
"upload_folder": "output",
"upload_files": []
```

Run `python helper/upload_to_gg_drive.py` while the virtual environment is
active. Folder uploads preserve nested directories and create a new top-level
folder inside the configured Drive folder.

## Run
```bash
python main.py
```

## Run in the background on Linux

From the `video_generator` directory, run:

```bash
nohup .venv/bin/python -u main.py > video.log 2>&1 &
echo $! > video.pid
```

Follow the log with `tail -f video.log`. Stop the process with
`kill "$(cat video.pid)"`.

## Run in the background on Windows (PowerShell)

Open PowerShell in the `video_generator` directory. Start the script in the
background and save its logs:

```powershell
$job = Start-Process -FilePath python `
  -ArgumentList @("-u", "main.py") `
  -WorkingDirectory $PWD `
  -RedirectStandardOutput (Join-Path $PWD "video.log") `
  -RedirectStandardError (Join-Path $PWD "video-error.log") `
  -WindowStyle Hidden -PassThru
$job.Id | Set-Content video.pid
```

Follow `video.log` for output and check `video-error.log` for errors. Stop the
process with:

```powershell
Stop-Process -Id (Get-Content video.pid)
```
