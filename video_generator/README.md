# AI Generator

Python scripts for asynchronous image and video generation with the OpenAI SDK and Requests.

## Files
- `config.json`: API settings (`base_url`, `api_key`, prompts, and model).
- `main.py`: Main script for image generation (`muse-image`) and video generation (`/videos`).

## Setup
1. Install dependencies:
   ```bash
   python -m pip install -r requirements.txt
   ```
2. Set your `base_url` and `api_key` in `config.json`.

## Run
```bash
python main.py
```

## Run in the background on Linux

From the `video_generator` directory, run:

```bash
nohup python -u main.py > video.log 2>&1 &
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
