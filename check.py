import os

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

creds = Credentials(
    None,
    refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
    token_uri="https://oauth2.googleapis.com/token",
    client_id=os.environ["GOOGLE_CLIENT_ID"],
    client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
)
creds.refresh(Request())
drive = build("drive", "v3", credentials=creds, cache_discovery=False)
folder = os.environ["DRIVE_INPUT_FOLDER_ID"]
q = f"'{folder}' in parents and mimeType contains 'video/' and trashed=false"
res = drive.files().list(q=q, fields="files(id)", pageSize=1).execute()
found = "true" if res.get("files") else "false"
print("has_video =", found)
with open(os.environ["GITHUB_OUTPUT"], "a") as f:
    f.write(f"has_video={found}\n")
