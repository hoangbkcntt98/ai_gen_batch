import json
from pathlib import Path

from google_drive_helper import (
    upload_folder_to_google_drive,
    upload_to_google_drive,
)

PROJECT_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = PROJECT_DIR / "config.json"


def resolve_project_path(configured_path):
    path = Path(configured_path).expanduser()
    return path if path.is_absolute() else PROJECT_DIR / path


def main():
    with CONFIG_FILE.open("r", encoding="utf-8-sig") as config_file:
        config = json.load(config_file)

    drive_config = config.get("google_drive", {})
    folder_id = drive_config.get("folder_id")
    if not folder_id:
        raise ValueError("Set google_drive.folder_id in config.json")

    auth_type = drive_config.get("auth_type", "oauth")
    service_account_file = resolve_project_path(
        drive_config.get("service_account_file", "account_service.json")
    )
    oauth_client_file = resolve_project_path(
        drive_config.get("oauth_client_file", "client.json")
    )
    token_file = resolve_project_path(
        drive_config.get("token_file", "token.json")
    )
    upload_folder = drive_config.get("upload_folder")
    upload_files = drive_config.get("upload_files", [])
    if bool(upload_folder) == bool(upload_files):
        raise ValueError(
            "Set either google_drive.upload_folder or google_drive.upload_files "
            "in config.json, but not both."
        )

    if upload_folder:
        results = upload_folder_to_google_drive(
            resolve_project_path(upload_folder),
            folder_id,
            service_account_file,
            auth_type=auth_type,
            oauth_client_file=oauth_client_file,
            token_file=token_file,
        )
    else:
        results = [
            upload_to_google_drive(
                resolve_project_path(file_path),
                folder_id,
                service_account_file,
                auth_type=auth_type,
                oauth_client_file=oauth_client_file,
                token_file=token_file,
            )
            for file_path in upload_files
        ]

    for result in results:
        print(f"Uploaded {result['name']} (ID: {result['id']})")


if __name__ == "__main__":
    main()
