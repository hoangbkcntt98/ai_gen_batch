# Standalone image batch

This folder runs independently of the Anki add-on repository. Copy the whole
folder, edit `config.json`, then install the dependency and run:

```powershell
python -m pip install -r requirements.txt
python batch_image_generate.py --dry-run
python batch_image_generate.py
```

Set PostgreSQL and image API values in `config.json`. Set
`batch.image_generate.media_dir` to Anki's `collection.media` directory.
The script finds non-empty `ImagePrompt` values in both configured note tables,
inserts one history row per eligible note, then generates images in the same
run. A matching `completed` or `processing` history row prevents another
attempt. With `skip_existing_image: true`, notes that already have an image are
also ignored. `error` rows can be retried and create a new history row.
`limit: null` processes all eligible prompts; use `--limit N` to cap a run.

The `batch_job` queue table must already exist with the documented schema.
Manual queue inserts are not required.
