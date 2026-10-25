import mimetypes
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials as UserCredentials
from google.oauth2.service_account import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
DEFAULT_SERVICE_ACCOUNT_FILE = "account_service.json"
DEFAULT_OAUTH_CLIENT_FILE = "client.json"
DEFAULT_TOKEN_FILE = "token.json"
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]


def create_drive_service(
    auth_type="oauth",
    service_account_file=DEFAULT_SERVICE_ACCOUNT_FILE,
    oauth_client_file=DEFAULT_OAUTH_CLIENT_FILE,
    token_file=DEFAULT_TOKEN_FILE,
):
    if auth_type == "service_account":
        credentials = Credentials.from_service_account_file(
            service_account_file,
            scopes=DRIVE_SCOPES,
        )
    elif auth_type == "oauth":
        token_path = Path(token_file)
        credentials = (
            UserCredentials.from_authorized_user_file(str(token_path), DRIVE_SCOPES)
            if token_path.is_file()
            else None
        )

        if credentials and credentials.expired and credentials.refresh_token:
            try:
                credentials.refresh(Request())
            except RefreshError:
                credentials = None

        if not credentials or not credentials.valid:
            flow = InstalledAppFlow.from_client_secrets_file(
                oauth_client_file,
                scopes=DRIVE_SCOPES,
            )
            credentials = flow.run_local_server(port=0)
            token_path.write_text(credentials.to_json(), encoding="utf-8")
    else:
        raise ValueError("google_drive.auth_type must be 'oauth' or 'service_account'")

    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def _create_drive_folder(drive, name, parent_id):
    return (
        drive.files()
        .create(
            body={
                "name": name,
                "mimeType": FOLDER_MIME_TYPE,
                "parents": [parent_id],
            },
            supportsAllDrives=True,
            fields="id,name,mimeType,webViewLink",
        )
        .execute()
    )


def _upload_file(drive, path, parent_id):
    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    media = MediaFileUpload(str(path), mimetype=mime_type, resumable=True)
    return (
        drive.files()
        .create(
            body={"name": path.name, "parents": [parent_id]},
            media_body=media,
            supportsAllDrives=True,
            fields="id,name,mimeType,webViewLink",
        )
        .execute()
    )


def upload_to_google_drive(
    file_path,
    folder_id,
    service_account_file=DEFAULT_SERVICE_ACCOUNT_FILE,
    drive=None,
    auth_type="oauth",
    oauth_client_file=DEFAULT_OAUTH_CLIENT_FILE,
    token_file=DEFAULT_TOKEN_FILE,
):
    """Upload one file into a Drive folder and return its metadata."""
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"File not found: {path}")
    if not folder_id:
        raise ValueError("folder_id is required")

    drive = drive or create_drive_service(
        auth_type=auth_type,
        service_account_file=service_account_file,
        oauth_client_file=oauth_client_file,
        token_file=token_file,
    )
    return _upload_file(drive, path, folder_id)


def upload_folder_to_google_drive(
    folder_path,
    folder_id,
    service_account_file=DEFAULT_SERVICE_ACCOUNT_FILE,
    auth_type="oauth",
    oauth_client_file=DEFAULT_OAUTH_CLIENT_FILE,
    token_file=DEFAULT_TOKEN_FILE,
):
    """Upload a local folder recursively, preserving its directory structure."""
    source = Path(folder_path)
    if not source.is_dir():
        raise NotADirectoryError(f"Folder not found: {source}")
    if not folder_id:
        raise ValueError("folder_id is required")

    drive = create_drive_service(
        auth_type=auth_type,
        service_account_file=service_account_file,
        oauth_client_file=oauth_client_file,
        token_file=token_file,
    )
    uploaded = []
    root = _create_drive_folder(drive, source.name, folder_id)
    uploaded.append(root)

    def upload_contents(local_dir, drive_parent_id):
        for child in sorted(local_dir.iterdir()):
            if child.is_dir():
                remote_folder = _create_drive_folder(drive, child.name, drive_parent_id)
                uploaded.append(remote_folder)
                upload_contents(child, remote_folder["id"])
            elif child.is_file():
                uploaded.append(_upload_file(drive, child, drive_parent_id))

    upload_contents(source, root["id"])
    return uploaded
