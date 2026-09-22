"""
Movie Bot:
Google Drive original
-> FFmpeg multi-resolution
-> VCDN Watch Online
-> Streamtape Download Links
-> screenshots + 9:16 thumbnail
-> Gemini title/description/labels
-> Blogger post (draft by default)
-> delete original Google Drive file after successful post

Runs on GitHub Actions.
All settings come from environment variables.
"""

import html
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from google import genai
from google.auth.transport.requests import Request
from google.genai import types
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload


# ============================================================
# SETTINGS
# ============================================================

CLIENT_ID = os.environ["GOOGLE_CLIENT_ID"]
CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]
REFRESH_TOKEN = os.environ["GOOGLE_REFRESH_TOKEN"]
GEMINI_KEY = os.environ["GEMINI_API_KEY"]
BLOG_ID = os.environ["BLOG_ID"]
INPUT_FOLDER = os.environ["DRIVE_INPUT_FOLDER_ID"]

# ---------------- VCDN ----------------

VCDN_API_KEY = os.environ["VCDN_API_KEY"].strip()
VCDN_API_HOST = "cdn.vcdn.me"

# ---------------- STREAMTAPE ----------------

STREAMTAPE_LOGIN = os.environ["STREAMTAPE_LOGIN"].strip()
STREAMTAPE_KEY = os.environ["STREAMTAPE_KEY"].strip()
STREAMTAPE_API = "https://api.streamtape.com"

# Maximum time Streamtape conversion is allowed to take.
STREAMTAPE_TIMEOUT_MINUTES = int(
    os.environ.get("STREAMTAPE_TIMEOUT_MINUTES", "30")
)

# ---------------- General ----------------

GEMINI_MODEL = os.environ.get(
    "GEMINI_MODEL",
    "gemini-3.6-flash"
)

RESOLUTIONS = [
    int(x)
    for x in os.environ.get(
        "RESOLUTIONS",
        "480,720,1080"
    ).split(",")
    if x.strip()
]

MAX_VIDEOS = int(
    os.environ.get("MAX_VIDEOS", "1")
)

PUBLISH = (
    os.environ.get("PUBLISH", "false").lower() == "true"
)

LANGUAGE_HINT = os.environ.get(
    "LANGUAGE_HINT", ""
).strip()

AUDIO_MINUTES = int(
    os.environ.get("AUDIO_MINUTES", "10")
)

SCREENSHOTS = int(
    os.environ.get("SCREENSHOTS", "6")
)

WAIT_SECONDS = int(
    os.environ.get("WAIT_SECONDS", "20")
)

DIRECTOR_NAME = os.environ.get(
    "DIRECTOR_NAME", ""
).strip()

CRF = {
    480: 24,
    720: 23,
    1080: 22,
    1440: 22,
    2160: 21,
}

WORK = Path("work")

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/blogger",
]


# ============================================================
# BASIC HELPERS
# ============================================================

def log(*a):
    print(*a, flush=True)


def retry(fn, tries=4):
    for i in range(tries):
        try:
            return fn()
        except Exception as e:
            if i == tries - 1:
                raise

            log(
                f"  retry {i + 1}/{tries - 1} after error: {e}"
            )

            time.sleep(5 * (i + 1))


def run(cmd):
    subprocess.run(cmd, check=True)


# ============================================================
# GOOGLE CLIENTS
# ============================================================

creds = Credentials(
    None,
    refresh_token=REFRESH_TOKEN,
    token_uri="https://oauth2.googleapis.com/token",
    client_id=CLIENT_ID,
    client_secret=CLIENT_SECRET,
    scopes=SCOPES,
)

creds.refresh(Request())

drive = build(
    "drive",
    "v3",
    credentials=creds,
    cache_discovery=False
)

blogger = build(
    "blogger",
    "v3",
    credentials=creds,
    cache_discovery=False
)

gclient = genai.Client(
    api_key=GEMINI_KEY
)


# ============================================================
# GOOGLE DRIVE HELPERS
# ============================================================

def ensure_folder(name):
    q = (
        f"'{INPUT_FOLDER}' in parents "
        f"and name='{name}' "
        "and mimeType='application/vnd.google-apps.folder' "
        "and trashed=false"
    )

    res = drive.files().list(
        q=q,
        fields="files(id)"
    ).execute()

    if res["files"]:
        return res["files"][0]["id"]

    body = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [INPUT_FOLDER],
    }

    return drive.files().create(
        body=body,
        fields="id"
    ).execute()["id"]


def list_videos():
    q = (
        f"'{INPUT_FOLDER}' in parents "
        "and mimeType contains 'video/' "
        "and trashed=false"
    )

    res = drive.files().list(
        q=q,
        fields="files(id,name,size)",
        orderBy="createdTime"
    ).execute()

    return res["files"]


def download(file_id, dest):
    req = drive.files().get_media(
        fileId=file_id
    )

    with open(dest, "wb") as fh:
        dl = MediaIoBaseDownload(
            fh,
            req,
            chunksize=64 * 1024 * 1024
        )

        done = False

        while not done:
            status, done = retry(
                dl.next_chunk
            )

            if status:
                log(
                    f"  download "
                    f"{int(status.progress() * 100)}%"
                )


def upload_public(path, parent, mime):
    media = MediaFileUpload(
        path,
        mimetype=mime,
        resumable=True,
        chunksize=64 * 1024 * 1024
    )

    req = drive.files().create(
        body={
            "name": os.path.basename(path),
            "parents": [parent]
        },
        media_body=media,
        fields="id"
    )

    resp = None

    while resp is None:
        _, resp = retry(
            req.next_chunk
        )

    fid = resp["id"]

    retry(
        lambda: drive.permissions().create(
            fileId=fid,
            body={
                "type": "anyone",
                "role": "reader"
            }
        ).execute()
    )

    return fid


def delete_original_drive_file(file_id):
    """
    Permanently delete the original source file
    only after all processing and Blogger publishing
    have succeeded.
    """

    log(
        "Deleting original Google Drive file..."
    )

    retry(
        lambda: drive.files().delete(
            fileId=file_id
        ).execute()
    )

    log(
        "Original Google Drive file deleted."
    )


# ============================================================
# STREAMTAPE API
# ============================================================

STREAMTAPE_USER_AGENT = (
    "Mozilla/5.0 "
    "(Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 "
    "(KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


def _streamtape_api(path, params=None):
    """
    Streamtape API request.

    Official API:
    https://api.streamtape.com

    Most requests require:
    login
    key
    """

    params = dict(params or {})

    params["login"] = STREAMTAPE_LOGIN
    params["key"] = STREAMTAPE_KEY

    query = urllib.parse.urlencode(
        params,
        doseq=True
    )

    url = (
        f"{STREAMTAPE_API}{path}"
        f"?{query}"
    )

    def request():
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": STREAMTAPE_USER_AGENT,
            },
            method="GET",
        )

        try:
            with urllib.request.urlopen(
                req,
                timeout=120
            ) as resp:

                raw = resp.read().decode(
                    "utf-8",
                    "replace"
                )

                if not raw:
                    return {}

                data = json.loads(raw)

        except urllib.error.HTTPError as e:
            detail = e.read().decode(
                "utf-8",
                "replace"
            )

            raise RuntimeError(
                "Streamtape API HTTP "
                f"{e.code}: {detail}"
            ) from e

        except urllib.error.URLError as e:
            raise RuntimeError(
                f"Streamtape connection error: {e}"
            ) from e

        status = data.get("status")

        if status != 200:
            raise RuntimeError(
                "Streamtape API error "
                f"{status}: {data.get('msg')}"
            )

        return data

    return retry(
        request,
        tries=4
    )


def streamtape_account_test():
    """
    Verify login/key before processing a movie.
    """

    log(
        "Checking Streamtape API credentials..."
    )

    data = _streamtape_api(
        "/account/info"
    )

    result = data.get(
        "result",
        {}
    )

    log(
        "Streamtape account:",
        result.get("email", "OK")
    )


def _multipart_stream_upload(
    upload_url,
    file_path
):
    """
    Upload local file to Streamtape's upload URL
    using multipart/form-data without loading
    the whole video into RAM.
    """

    parsed = urllib.parse.urlsplit(
        upload_url
    )

    host = parsed.netloc

    path = parsed.path or "/"

    if parsed.query:
        path += "?" + parsed.query

    filename = os.path.basename(
        file_path
    )

    file_size = os.path.getsize(
        file_path
    )

    boundary = (
        "----MovieBotStreamtape"
        + str(int(time.time()))
    )

    prefix = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; '
        f'name="file1"; '
        f'filename="{filename.replace(chr(34), chr(39))}"\r\n'
        f"Content-Type: video/mp4\r\n\r\n"
    ).encode("utf-8")

    suffix = (
        f"\r\n--{boundary}--\r\n"
    ).encode("utf-8")

    total_length = (
        len(prefix)
        + file_size
        + len(suffix)
    )

    def upload():
        conn = http.client.HTTPSConnection(
            host,
            timeout=1800
        )

        try:
            conn.putrequest(
                "POST",
                path
            )

            conn.putheader(
                "Content-Type",
                f"multipart/form-data; boundary={boundary}"
            )

            conn.putheader(
                "Content-Length",
                str(total_length)
            )

            conn.putheader(
                "User-Agent",
                STREAMTAPE_USER_AGENT
            )

            conn.putheader(
                "Accept",
                "application/json"
            )

            conn.endheaders()

            conn.send(prefix)

            sent = 0
            last_log = -1

            with open(
                file_path,
                "rb"
            ) as fh:

                while True:
                    chunk = fh.read(
                        16 * 1024 * 1024
                    )

                    if not chunk:
                        break

                    conn.send(chunk)

                    sent += len(chunk)

                    pct = (
                        int(
                            sent * 100 / file_size
                        )
                        if file_size
                        else 100
                    )

                    if (
                        pct >= last_log + 10
                        or pct == 100
                    ):
                        log(
                            f"  Streamtape upload "
                            f"{pct}%"
                        )

                        last_log = pct

            conn.send(suffix)

            response = conn.getresponse()

            raw = response.read().decode(
                "utf-8",
                "replace"
            )

            if (
                response.status < 200
                or response.status >= 300
            ):
                raise RuntimeError(
                    "Streamtape file upload failed: "
                    f"HTTP {response.status}: {raw}"
                )

            try:
                return (
                    json.loads(raw)
                    if raw
                    else {}
                )
            except json.JSONDecodeError:
                return {
                    "raw": raw
                }

        finally:
            conn.close()

    return retry(
        upload,
        tries=3
    )


def _find_streamtape_file(
    filename,
    expected_size=None
):
    """
    Find uploaded file in Streamtape root folder.

    The API's listfolder response includes:
      name
      size
      link
      linkid
      convert
    """

    data = _streamtape_api(
        "/file/listfolder"
    )

    result = data.get(
        "result",
        {}
    )

    files = result.get(
        "files",
        []
    )

    candidates = [
        f for f in files
        if f.get("name") == filename
    ]

    if expected_size:
        exact = [
            f for f in candidates
            if int(f.get("size", 0) or 0)
            == int(expected_size)
        ]

        if exact:
            candidates = exact

    if not candidates:
        return None

    return candidates[-1]


def _streamtape_file_info(file_id):
    data = _streamtape_api(
        "/file/info",
        {
            "file": file_id
        }
    )

    result = data.get(
        "result",
        {}
    )

    return (
        result.get(str(file_id))
        or result.get(file_id)
        or {}
    )


def streamtape_upload(
    file_path,
    title
):
    """
    Upload one converted MP4 to Streamtape.

    Returns:
      {
        id,
        link,
        name,
        size
      }
    """

    filename = os.path.basename(
        file_path
    )

    file_size = os.path.getsize(
        file_path
    )

    if file_size <= 0:
        raise RuntimeError(
            "Streamtape upload file is empty."
        )

    log(
        f"Uploading {filename} to Streamtape "
        f"({human(file_size)})..."
    )

    # --------------------------------------------------------
    # STEP 1: Ask Streamtape for an upload URL
    # --------------------------------------------------------

    upload_data = _streamtape_api(
        "/file/ul",
        {
            "httponly": "false"
        }
    )

    result = upload_data.get(
        "result",
        {}
    )

    upload_url = result.get(
        "url"
    )

    if not upload_url:
        raise RuntimeError(
            "Streamtape did not return "
            f"an upload URL: {upload_data}"
        )

    log(
        "  Streamtape upload URL received."
    )

    # --------------------------------------------------------
    # STEP 2: Upload actual file
    # --------------------------------------------------------

    upload_result = _multipart_stream_upload(
        upload_url,
        file_path
    )

    log(
        "  Streamtape upload response:",
        str(upload_result)[:1000]
    )

    # --------------------------------------------------------
    # STEP 3: Try to obtain file ID from upload response
    # --------------------------------------------------------

    possible_id = None

    if isinstance(upload_result, dict):

        ur = upload_result.get(
            "result",
            upload_result
        )

        if isinstance(ur, dict):
            possible_id = (
                ur.get("id")
                or ur.get("file")
                or ur.get("fileid")
                or ur.get("file_id")
            )

    # --------------------------------------------------------
    # STEP 4: Wait until file appears in account
    # --------------------------------------------------------

    deadline = (
        time.time()
        + STREAMTAPE_TIMEOUT_MINUTES * 60
    )

    found = None

    while time.time() < deadline:

        if possible_id:
            try:
                info = _streamtape_file_info(
                    possible_id
                )

                if info:
                    found = {
                        "id": possible_id,
                        **info
                    }

                    break

            except Exception as e:
                log(
                    "  Streamtape file-info check:",
                    e
                )

        try:
            found = _find_streamtape_file(
                filename,
                file_size
            )

            if found:
                possible_id = (
                    found.get("linkid")
                    or found.get("id")
                )

                if possible_id:
                    break

        except Exception as e:
            log(
                "  Streamtape file-list check:",
                e
            )

        log(
            "  Waiting for Streamtape "
            "to register the uploaded file..."
        )

        time.sleep(8)

    if not found:
        raise RuntimeError(
            "Streamtape upload finished but "
            "the uploaded file could not be found "
            "through the API."
        )

    file_id = (
        found.get("linkid")
        or found.get("id")
        or possible_id
    )

    if not file_id:
        raise RuntimeError(
            f"Streamtape returned no file ID: {found}"
        )

    # --------------------------------------------------------
    # STEP 5: Wait for Streamtape conversion
    # --------------------------------------------------------

    log(
        "  Streamtape file ID:",
        file_id
    )

    converted = False
    stable_link = found.get("link")

    while time.time() < deadline:

        try:
            info = _streamtape_file_info(
                file_id
            )

            if info:

                converted = bool(
                    info.get("converted")
                )

                if not stable_link:
                    stable_link = info.get(
                        "link"
                    )

                log(
                    "  Streamtape converted:",
                    converted
                )

                if converted:
                    break

        except Exception as e:
            log(
                "  Streamtape conversion check:",
                e
            )

        time.sleep(10)

    if not converted:
        raise RuntimeError(
            "Streamtape did not finish converting "
            f"{filename} within "
            f"{STREAMTAPE_TIMEOUT_MINUTES} minutes."
        )

    # --------------------------------------------------------
    # STEP 6: Get stable public video link
    # --------------------------------------------------------

    if not stable_link:

        # Official Streamtape file-list responses
        # expose links in this form:
        #
        # https://streamtape.com/v/{file-id}/{name}

        safe_name = urllib.parse.quote(
            filename
        )

        stable_link = (
            "https://streamtape.com/v/"
            f"{file_id}/{safe_name}"
        )

    log(
        "  Streamtape ready:",
        stable_link
    )

    return {
        "id": file_id,
        "name": filename,
        "size": file_size,
        "link": stable_link,
    }


# ============================================================
# VCDN HELPERS
# ============================================================

VCDN_USER_AGENT = (
    "Mozilla/5.0 "
    "(Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 "
    "(KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


def _vcdn_auth_headers(content_type=None):
    headers = {
        "Authorization": f"Bearer {VCDN_API_KEY}",
        "X-API-Key": VCDN_API_KEY,
        "Accept": "application/json",
        "User-Agent": VCDN_USER_AGENT,
    }

    if content_type:
        headers["Content-Type"] = content_type

    return headers


def _vcdn_json(
    method,
    path,
    payload=None
):
    body = None

    headers = _vcdn_auth_headers()

    if payload is not None:
        body = json.dumps(
            payload
        ).encode("utf-8")

        headers["Content-Type"] = (
            "application/json"
        )

    def request():

        req = urllib.request.Request(
            f"https://{VCDN_API_HOST}{path}",
            data=body,
            headers=headers,
            method=method,
        )

        try:
            with urllib.request.urlopen(
                req,
                timeout=120
            ) as resp:

                raw = resp.read().decode(
                    "utf-8",
                    "replace"
                )

                return (
                    json.loads(raw)
                    if raw
                    else {}
                )

        except urllib.error.HTTPError as e:

            detail = e.read().decode(
                "utf-8",
                "replace"
            )

            raise RuntimeError(
                f"VCDN {method} {path} failed: "
                f"HTTP {e.code}: {detail}"
            ) from e

        except urllib.error.URLError as e:

            raise RuntimeError(
                f"VCDN connection failed "
                f"for {method} {path}: {e}"
            ) from e

    return retry(
        request,
        tries=4
    )


def _vcdn_upload_binary(
    upload_id,
    path,
    upload_url=None
):

    file_size = os.path.getsize(
        path
    )

    target = (
        upload_url
        or
        f"https://{VCDN_API_HOST}"
        f"/api/v1/upload/{upload_id}/chunk"
    )

    if target.startswith("https://"):

        parsed = urllib.parse.urlsplit(
            target
        )

        target_host = parsed.netloc

        target_path = (
            parsed.path
            or "/"
        )

        if parsed.query:
            target_path += (
                "?"
                + parsed.query
            )

    else:

        target_host = VCDN_API_HOST

        target_path = (
            target
            if target.startswith("/")
            else "/" + target
        )

    def upload():

        conn = http.client.HTTPSConnection(
            target_host,
            timeout=1800
        )

        try:

            conn.putrequest(
                "POST",
                target_path
            )

            headers = _vcdn_auth_headers(
                "application/octet-stream"
            )

            headers["Content-Length"] = (
                str(file_size)
            )

            for key, value in headers.items():
                conn.putheader(
                    key,
                    value
                )

            conn.endheaders()

            sent = 0
            last_log = -1

            with open(
                path,
                "rb"
            ) as fh:

                while True:

                    chunk = fh.read(
                        16 * 1024 * 1024
                    )

                    if not chunk:
                        break

                    conn.send(chunk)

                    sent += len(chunk)

                    pct = (
                        int(
                            sent * 100 / file_size
                        )
                        if file_size
                        else 100
                    )

                    if (
                        pct >= last_log + 10
                        or pct == 100
                    ):

                        log(
                            f"  VCDN upload "
                            f"{pct}%"
                        )

                        last_log = pct

            resp = conn.getresponse()

            raw = resp.read().decode(
                "utf-8",
                "replace"
            )

            if (
                resp.status < 200
                or resp.status >= 300
            ):

                raise RuntimeError(
                    "VCDN binary upload failed: "
                    f"HTTP {resp.status}: {raw}"
                )

            try:
                return (
                    json.loads(raw)
                    if raw
                    else {}
                )

            except json.JSONDecodeError:
                return {
                    "raw": raw
                }

        finally:
            conn.close()

    return retry(
        upload,
        tries=3
    )


def _multipart_header(
    boundary,
    title,
    filename,
    file_size
):

    safe_name = os.path.basename(
        filename
    ).replace('"', "'")

    safe_title = str(
        title
    ).replace('"', "'")

    prefix = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; '
        f'name="title"\r\n\r\n'
        f"{safe_title}\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; '
        f'name="file"; '
        f'filename="{safe_name}"\r\n'
        f"Content-Type: video/mp4\r\n\r\n"
    ).encode("utf-8")

    suffix = (
        f"\r\n--{boundary}--\r\n"
    ).encode("utf-8")

    return prefix, suffix


def _vcdn_direct_upload(
    path,
    title
):

    host = "api.vcdn.me"

    boundary = (
        "----MovieBotVCDNBoundary"
        "7MA4YWxkTrZu0gW"
    )

    file_size = os.path.getsize(
        path
    )

    prefix, suffix = _multipart_header(
        boundary,
        title,
        path,
        file_size
    )

    total_length = (
        len(prefix)
        + file_size
        + len(suffix)
    )

    def upload():

        conn = http.client.HTTPSConnection(
            host,
            timeout=1800
        )

        try:

            conn.putrequest(
                "POST",
                "/videos"
            )

            headers = _vcdn_auth_headers(
                f"multipart/form-data; "
                f"boundary={boundary}"
            )

            headers["Content-Length"] = (
                str(total_length)
            )

            for key, value in headers.items():
                conn.putheader(
                    key,
                    value
                )

            conn.endheaders()

            conn.send(prefix)

            sent = 0
            last_log = -1

            with open(
                path,
                "rb"
            ) as fh:

                while True:

                    chunk = fh.read(
                        16 * 1024 * 1024
                    )

                    if not chunk:
                        break

                    conn.send(chunk)

                    sent += len(chunk)

                    pct = (
                        int(
                            sent * 100 / file_size
                        )
                        if file_size
                        else 100
                    )

                    if (
                        pct >= last_log + 10
                        or pct == 100
                    ):

                        log(
                            f"  VCDN direct upload "
                            f"{pct}%"
                        )

                        last_log = pct

            conn.send(suffix)

            resp = conn.getresponse()

            raw = resp.read().decode(
                "utf-8",
                "replace"
            )

            if (
                resp.status < 200
                or resp.status >= 300
            ):

                raise RuntimeError(
                    "VCDN direct upload failed: "
                    f"HTTP {resp.status}: {raw}"
                )

            data = json.loads(
                raw
            )

            video_id = (
                data.get("id")
                or data.get("video_id")
            )

            embed_url = (
                data.get("embed_url")
                or data.get("embedUrl")
            )

            playback_url = (
                data.get("playback_url")
                or data.get("playbackUrl")
            )

            if (
                not embed_url
                and video_id
            ):
                embed_url = (
                    "https://embed.vcdn.me/"
                    f"{video_id}"
                )

            if (
                not video_id
                and not embed_url
            ):
                raise RuntimeError(
                    "VCDN direct upload returned "
                    "no usable video ID/embed URL: "
                    f"{data}"
                )

            log(
                "  VCDN video:",
                video_id or "unknown"
            )

            log(
                "  VCDN embed:",
                embed_url or "unknown"
            )

            if playback_url:
                log(
                    "  VCDN HLS:",
                    playback_url
                )

            return {
                "id": video_id,
                "embed_url": embed_url,
                "playback_url": playback_url,
                "status": data.get(
                    "status"
                ),
            }

        finally:
            conn.close()

    return retry(
        upload,
        tries=3
    )


def vcdn_upload(
    path,
    title
):

    log(
        f"Uploading {os.path.basename(path)} "
        "to VCDN..."
    )

    file_size = os.path.getsize(
        path
    )

    if file_size <= 0:
        raise RuntimeError(
            "VCDN upload file is empty."
        )

    try:

        log(
            "  VCDN file size:",
            file_size,
            "bytes"
        )

        return _vcdn_direct_upload(
            path,
            title
        )

    except Exception as direct_error:

        log(
            "  VCDN direct REST upload failed:",
            direct_error
        )

        try:

            init = _vcdn_json(
                "POST",
                "/api/v1/upload/init",
                {
                    "filename": os.path.basename(
                        path
                    ),
                    "title": title,
                    "size": file_size,
                }
            )

            upload_id = (
                init.get("upload_id")
                or init.get("uploadId")
            )

            upload_url = (
                init.get("upload_url")
                or init.get("uploadUrl")
            )

            if not upload_id:
                raise RuntimeError(
                    "VCDN init did not return "
                    f"upload ID: {init}"
                )

            log(
                "  VCDN chunk upload ID:",
                upload_id
            )

            if upload_url:
                log(
                    "  VCDN chunk upload URL:",
                    upload_url
                )

            _vcdn_upload_binary(
                upload_id,
                path,
                upload_url
            )

            complete = _vcdn_json(
                "POST",
                "/api/v1/upload/complete",
                {
                    "uploadId": upload_id
                }
            )

            video_id = (
                complete.get("id")
                or complete.get("video_id")
                or complete.get("videoId")
            )

            embed_url = (
                complete.get("embed_url")
                or complete.get("embedUrl")
            )

            playback_url = (
                complete.get("playback_url")
                or complete.get("playbackUrl")
            )

            status = complete.get(
                "status"
            )

            if not video_id:
                raise RuntimeError(
                    "VCDN complete returned "
                    f"no video ID: {complete}"
                )

            last_video = complete

            deadline = (
                time.time()
                + 10 * 60
            )

            while (
                time.time() < deadline
            ):

                if (
                    status
                    in (
                        "ready",
                        "processed",
                        "complete",
                        "completed",
                    )
                    and embed_url
                ):
                    break

                time.sleep(5)

                try:

                    info = _vcdn_json(
                        "GET",
                        "/api/v1/videos/"
                        + urllib.parse.quote(
                            str(video_id),
                            safe=""
                        )
                    )

                except Exception as poll_error:

                    log(
                        "  VCDN status check failed:",
                        poll_error
                    )

                    continue

                last_video = (
                    info
                    or last_video
                )

                status = (
                    info.get("status")
                    or status
                )

                embed_url = (
                    info.get("embed_url")
                    or info.get("embedUrl")
                    or embed_url
                )

                playback_url = (
                    info.get("playback_url")
                    or info.get("playbackUrl")
                    or playback_url
                )

                log(
                    "  VCDN processing status:",
                    status
                )

                if status in (
                    "ready",
                    "processed",
                    "complete",
                    "completed",
                ):
                    break

                if status in (
                    "failed",
                    "error",
                ):
                    raise RuntimeError(
                        "VCDN processing failed: "
                        f"{info}"
                    )

            if not embed_url:
                embed_url = (
                    "https://embed.vcdn.me/"
                    f"{video_id}"
                )

            if not embed_url:
                raise RuntimeError(
                    "VCDN returned no usable "
                    f"embed URL: {last_video}"
                )

            log(
                "  VCDN video:",
                video_id
            )

            log(
                "  VCDN embed:",
                embed_url
            )

            if playback_url:
                log(
                    "  VCDN HLS:",
                    playback_url
                )

            return {
                "id": video_id,
                "embed_url": embed_url,
                "playback_url": playback_url,
                "status": status,
            }

        except Exception as chunk_error:

            raise RuntimeError(
                "VCDN upload failed using "
                "both upload methods.\n"
                f"Direct REST error: "
                f"{direct_error}\n"
                f"Chunked API error: "
                f"{chunk_error}"
            ) from chunk_error


# ============================================================
# FFMPEG HELPERS
# ============================================================

def parse_fps(*vals):

    for v in vals:

        try:

            a, b = str(v).split("/")

            a = float(a)
            b = float(b)

            if b and a / b > 0:
                return a / b

        except Exception:
            pass

    return 30.0


def fmt_fps(x):

    r = round(x)

    return (
        str(r)
        if abs(x - r) < 0.05
        else f"{x:.2f}"
    )


def probe(path):

    out = subprocess.check_output([
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height,avg_frame_rate,r_frame_rate:"
        "format=duration",
        "-of",
        "json",
        path,
    ])

    d = json.loads(out)

    st = d["streams"][0]

    fps = parse_fps(
        st.get("avg_frame_rate"),
        st.get("r_frame_rate")
    )

    return (
        float(d["format"]["duration"]),
        int(st["width"]),
        int(st["height"]),
        fps
    )


def make_screenshots(
    src,
    dur,
    outdir
):

    files = []

    for i in range(SCREENSHOTS):

        t = (
            dur
            * (i + 1)
            / (SCREENSHOTS + 1)
        )

        p = str(
            outdir
            / f"shot_{i + 1}.jpg"
        )

        run([
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-ss",
            f"{t:.2f}",
            "-i",
            src,
            "-frames:v",
            "1",
            "-vf",
            "scale=1280:-2",
            "-q:v",
            "3",
            p,
        ])

        files.append(p)

    return files


def make_thumbnail(
    src,
    dur,
    w,
    h,
    outdir
):

    if w * 16 >= h * 9:

        ch = (
            h // 2 * 2
        )

        cw = (
            int(h * 9 / 16)
            // 2
            * 2
        )

    else:

        cw = (
            w // 2 * 2
        )

        ch = (
            int(w * 16 / 9)
            // 2
            * 2
        )

    p = str(
        outdir
        / "thumb_9x16.jpg"
    )

    run([
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-ss",
        f"{dur * 0.35:.2f}",
        "-i",
        src,
        "-frames:v",
        "1",
        "-vf",
        f"crop={cw}:{ch},scale=720:1280",
        "-q:v",
        "2",
        p,
    ])

    return p


def analysis_inputs(
    src,
    dur,
    outdir
):

    frames = []

    n = 12

    for i in range(n):

        t = (
            dur
            * (i + 1)
            / (n + 1)
        )

        p = (
            outdir
            / f"an_{i}.jpg"
        )

        run([
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-ss",
            f"{t:.2f}",
            "-i",
            src,
            "-frames:v",
            "1",
            "-vf",
            "scale=512:-2",
            "-q:v",
            "5",
            str(p),
        ])

        frames.append(
            p.read_bytes()
        )

    audio = (
        outdir
        / "an_audio.mp3"
    )

    start = dur * 0.10

    run([
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-ss",
        f"{start:.2f}",
        "-t",
        str(AUDIO_MINUTES * 60),
        "-i",
        src,
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-b:a",
        "32k",
        str(audio),
    ])

    return (
        frames,
        audio.read_bytes()
    )


def transcode(
    src,
    target,
    w,
    h,
    out
):

    crf = CRF.get(
        target,
        23
    )

    vf = (
        f"scale=-2:{target}"
        if w >= h
        else
        f"scale={target}:-2"
    )

    run([
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-stats",
        "-i",
        src,
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-vf",
        vf,
        "-pix_fmt",
        "yuv420p",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        str(crf),
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        out,
    ])


# ============================================================
# LABELS
# ============================================================

FALLBACK_LABELS = [
    "Bollywood Content",
    "Desi Junction",
    "Dual Audio",
    "Hindi Dubbed",
    "Hindi TV Shows",
    "Web Series",
    "WWE",
    "Hollywood Movies",
    "Malayalam Movies",
    "Marathi Movies",
    "Mobile Movies",
    "Multi Audio",
    "Pakistani Movies",
    "PC Games",
    "Pre Release",
    "Punjabi Movies",
    "Single Video Songs",
    "Tamil Movies",
    "Telugu Movies",
    "Trailers",
    "Uncategorized",
]

BLOCKED_LABEL = re.compile(
    r"18\+|adult|xxx|hevc|x265",
    re.I
)


def parse_labels(page):

    found = re.findall(
        r"/search/label/([^\"'?&#<>\s/]+)",
        page
    )

    labels = []
    seen = set()

    for f in found:

        name = urllib.parse.unquote_plus(
            f
        ).strip()

        if (
            name
            and name.lower() not in seen
        ):

            seen.add(
                name.lower()
            )

            labels.append(
                name
            )

    return labels


def get_site_labels():

    labels = []

    try:

        url = blogger.blogs().get(
            blogId=BLOG_ID
        ).execute()["url"]

        req = urllib.request.Request(
            url,
            headers={
                "User-Agent":
                    "Mozilla/5.0"
            }
        )

        page = urllib.request.urlopen(
            req,
            timeout=30
        ).read().decode(
            "utf-8",
            "ignore"
        )

        labels = parse_labels(
            page
        )

        log(
            f"  Found {len(labels)} "
            "labels on the blog"
        )

    except Exception as e:

        log(
            "  Could not read blog labels:",
            e
        )

    if len(labels) < 3:

        have = {
            l.lower()
            for l in labels
        }

        labels += [
            l
            for l in FALLBACK_LABELS
            if l.lower() not in have
        ]

    labels = [
        l
        for l in labels
        if not BLOCKED_LABEL.search(l)
    ]

    return labels[:60]


def pick_labels(
    raw,
    site_labels
):

    canon = {
        l.lower(): l
        for l in site_labels
    }

    out = []

    for r in raw or []:

        c = canon.get(
            str(r).strip().lower()
        )

        if (
            c
            and c not in out
        ):
            out.append(c)

    return out[:4]


# ============================================================
# GEMINI
# ============================================================

def as_paragraphs(v):

    if isinstance(v, list):

        return [
            str(x).strip()
            for x in v
            if str(x).strip()
        ]

    return [
        p.strip()
        for p in re.split(
            r"\n\s*\n",
            str(v or "")
        )
        if p.strip()
    ]


YEAR_RE = re.compile(
    r"(?<!\d)(19[5-9]\d|20[0-4]\d)(?!\d)"
)


def find_year(filename):

    m = YEAR_RE.search(
        Path(filename).stem
    )

    return (
        int(m.group(1))
        if m
        else None
    )


def clean_hint(filename):

    t = Path(filename).stem

    t = re.sub(
        r"[#@]\S+",
        " ",
        t
    )

    t = re.sub(
        r"[_.\-]+",
        " ",
        t
    )

    t = re.sub(
        r"[^\w\s]",
        " ",
        t,
        flags=re.UNICODE
    )

    t = re.sub(
        r"\s+",
        " ",
        t
    ).strip()

    no_year = re.sub(
        r"\s+",
        " ",
        YEAR_RE.sub(
            " ",
            t
        )
    ).strip()

    return no_year or t


def analyze(
    filename_hint,
    frames,
    audio_bytes,
    site_labels
):

    hint = clean_hint(
        filename_hint
    )

    year = find_year(
        filename_hint
    )

    prompt = f"""
You are a film writer. You are publishing an ORIGINAL film,
on its own director's film blog.

You get 12 frames spread across the film and an audio sample.

File name hint:
"{hint}"

Language hint:
"{LANGUAGE_HINT}"

Director name:
"{DIRECTOR_NAME}"

Rules:

- Write everything in your own words, in natural English.
- Never copy text from any website, film or review.
- Base it ONLY on what you can actually see and hear.
- If unsure, stay general.
- Never invent cast, crew, awards, festivals, ratings,
  box office or plot facts you cannot see.
- No piracy words such as:
  leaked, HD print, free download full movie,
  WEB-DL, dual audio, 300mb.
- The title must be a real film title of 1-6 words.
- No hashtags.
- No emojis.
- No year in title.

Return ONLY JSON with:

title
tagline
synopsis
review
themes
faq
genres
language
content_rating
tags
labels

Synopsis:
2 short paragraphs, about 120 words.

Review:
3-4 paragraphs, about 300 words.

Themes:
3-5 short phrases.

FAQ:
4 objects:
{{"q": "...", "a": "..."}}

Genres:
1-3 genres.

Language:
main spoken language.

Content rating:
one of:
General audience
Teen and above
Mature audience

Tags:
up to 6 short keywords.

Labels:
pick 1-4 categories ONLY from this exact list:

{json.dumps(site_labels)}
"""

    parts = [
        types.Part.from_bytes(
            data=b,
            mime_type="image/jpeg"
        )
        for b in frames
    ]

    parts.append(
        types.Part.from_bytes(
            data=audio_bytes,
            mime_type="audio/mp3"
        )
    )

    data = {}

    for model in dict.fromkeys([
        GEMINI_MODEL,
        "gemini-flash-latest"
    ]):

        try:

            resp = retry(
                lambda: gclient.models.generate_content(
                    model=model,
                    contents=[
                        prompt,
                        *parts
                    ],
                    config=types.GenerateContentConfig(
                        response_mime_type=
                            "application/json"
                    )
                ),
                tries=2
            )

            data = json.loads(
                re.sub(
                    r"^```json|```$",
                    "",
                    resp.text.strip()
                ).strip()
            )

            log(
                "  Gemini model used:",
                model
            )

            break

        except Exception as e:

            log(
                f"  Gemini model {model} failed:",
                e
            )

    if not data:
        log(
            "  Using fallback text."
        )

    faq = [
        f
        for f in (
            data.get("faq")
            or []
        )
        if (
            isinstance(f, dict)
            and f.get("q")
            and f.get("a")
        )
    ]

    return {
        "title":
            data.get("title")
            or hint
            or "Untitled Film",

        "tagline":
            data.get("tagline")
            or "",

        "synopsis":
            as_paragraphs(
                data.get("synopsis")
            )
            or [
                "An original film."
            ],

        "review":
            as_paragraphs(
                data.get("review")
            ),

        "themes":
            [
                str(t)
                for t in (
                    data.get("themes")
                    or []
                )
            ][:5],

        "faq":
            faq[:4],

        "genres":
            data.get("genres")
            or ["Drama"],

        "language":
            data.get("language")
            or LANGUAGE_HINT
            or "Unknown",

        "release_year":
            year,

        "content_rating":
            data.get(
                "content_rating"
            )
            or "General audience",

        "tags":
            data.get("tags")
            or [],

        "labels":
            pick_labels(
                data.get("labels"),
                site_labels
            ),
    }


# ============================================================
# HTML HELPERS
# ============================================================

def human(n):

    n = float(n)

    if n >= 1024 ** 3:

        return (
            f"{n / 1024 ** 3:.1f}GB"
        )

    return (
        f"{n / 1024 ** 2:.0f}MB"
    )


def fmt_runtime(sec):

    sec = int(sec)

    if sec < 60:
        return f"{sec} sec"

    m = sec // 60

    if m < 60:
        return f"{m} min"

    return (
        f"{m // 60} h "
        f"{m % 60} min"
    )


def img_url(fid):

    return (
        f"https://lh3.googleusercontent.com/d/{fid}"
    )


# ============================================================
# DOWNLOAD BUTTON TIMER
# ============================================================

TIMER_SCRIPT = """
<script>
(function () {

  var WAIT = %d;

  var btns =
    document.querySelectorAll(
      'a.mv-dl[data-url]'
    );

  for (var i = 0; i < btns.length; i++) {

    (function (b) {

      var label = b.innerHTML;
      var busy = false;

      b.addEventListener(
        'click',
        function (e) {

          e.preventDefault();

          if (busy) {
            return;
          }

          busy = true;

          var left = WAIT;

          b.style.opacity = '0.85';

          b.innerHTML =
            'Please wait '
            + left
            + ' seconds...';

          var t = setInterval(
            function () {

              left--;

              if (left > 0) {

                b.innerHTML =
                  'Please wait '
                  + left
                  + ' seconds...';

                return;
              }

              clearInterval(t);

              b.innerHTML =
                'Opening download...';

              window.location.href =
                b.getAttribute(
                  'data-url'
                );

              setTimeout(
                function () {

                  b.innerHTML = label;

                  b.style.opacity = '1';

                  busy = false;

                },
                6000
              );

            },
            1000
          );

        }
      );

    })(btns[i]);

  }

})();
</script>
""" % WAIT_SECONDS


# ============================================================
# BLOGGER HTML
# ============================================================

def build_html(
    meta,
    thumb_id,
    shot_ids,
    outputs,
    fps,
    dur,
    vcdn
):

    e = html.escape

    title = e(
        meta["title"]
    )

    year = meta[
        "release_year"
    ]

    ytxt = (
        f" ({year})"
        if year
        else ""
    )

    lang_known = (
        meta["language"]
        and meta["language"].lower()
        != "unknown"
    )

    lang = e(
        meta["language"]
    )

    lang_tag = (
        f' <span style="color:#f2f200">'
        f'{{{lang}}}</span>'
        if lang_known
        else ""
    )

    genres = ", ".join(
        e(g)
        for g in meta["genres"]
    )

    fps_txt = fmt_fps(
        fps
    )

    qualities = " - ".join(
        f"{h}p"
        for h, _, _ in outputs
    )

    sizes = " - ".join(
        human(size)
        for _, _, size in outputs
    )

    syn = [
        e(p)
        for p in meta["synopsis"]
    ]

    review = [
        e(p)
        for p in meta["review"]
    ]

    btn = (
        "display:block;"
        "width:260px;"
        "max-width:90%;"
        "margin:0 auto 28px;"
        "padding:18px 10px;"
        "text-align:center;"
        "color:#fff;"
        "font-weight:800;"
        "font-size:19px;"
        "text-decoration:none;"
        "cursor:pointer;"
        "background:"
        "linear-gradient("
        "90deg,#57a51c,#1f4fb4);"
        "box-shadow:"
        "0 8px 14px "
        "rgba(0,0,0,.45);"
    )

    head = (
        "text-align:center;"
        "color:#fff;"
        "font-size:21px;"
        "line-height:1.4;"
        "margin:28px 0 18px;"
        "font-weight:800"
    )

    hr = (
        '<hr style="border:0;'
        'border-top:1px solid '
        'rgba(255,255,255,.6);'
        'margin:22px 0"/>'
    )

    h3 = (
        '<h3 style="text-align:center">'
        '{}'
        '</h3>'
    )

    info = [
        f"<b>Movie Name:</b> {title}"
    ]

    if year:
        info.append(
            f"<b>Release Year:</b> {year}"
        )

    if DIRECTOR_NAME:
        info.append(
            f"<b>Directed by:</b> "
            f"{e(DIRECTOR_NAME)}"
        )

    if lang_known:
        info.append(
            f"<b>Language:</b> {lang}"
        )

    info += [
        f"<b>Runtime:</b> "
        f"{fmt_runtime(dur)}",

        f"<b>Genres:</b> "
        f"{genres}",

        f"<b>Content Advisory:</b> "
        f"{e(meta['content_rating'])}",

        f"<b>Quality:</b> "
        f"{qualities}",

        f"<b>Frame Rate:</b> "
        f"{fps_txt}fps",

        f"<b>Size:</b> "
        f"{sizes}",
    ]

    parts = [

        (
            '<div style="text-align:center">'
            f'<img src="{img_url(thumb_id)}" '
            f'alt="{title}" '
            'width="270" '
            'style="max-width:60%;'
            'height:auto;'
            'border-radius:8px"/>'
            '</div>'
        ),

        (
            '<p style="text-align:center">'
            f'<b>{title}{ytxt}</b>'
            + (
                f" - {lang} film"
                if lang_known
                else ""
            )
            + '</p>'
        ),
    ]

    if meta["tagline"]:

        parts.append(
            '<p style="text-align:center">'
            '<i>'
            f'{e(meta["tagline"])}'
            '</i>'
            '</p>'
        )

    parts.append(
        f"<p>{syn[0]}</p>"
    )

    parts.append(
        h3.format(
            "Movie Info"
        )
    )

    parts.append(
        "<p>"
        + "<br/>".join(info)
        + "</p>"
    )

    parts.append(
        h3.format(
            "Movie Synopsis / Plot"
        )
    )

    parts += [
        f"<p>{p}</p>"
        for p in syn
    ]

    # --------------------------------------------------------
    # VCDN WATCH ONLINE
    # --------------------------------------------------------

    parts.append(
        f"<h3>Watch {title} Online</h3>"
    )

    embed_url = vcdn[
        "embed_url"
    ]

    parts.append(
        '<div style="width:100%;'
        'max-width:100%;'
        'background:#000;'
        'border-radius:8px;'
        'overflow:hidden;'
        'margin:0 auto 24px">'
        f'<iframe src="{e(embed_url, quote=True)}" '
        'width="100%" '
        'height="420" '
        'frameborder="0" '
        'allow="autoplay; '
        'encrypted-media; '
        'picture-in-picture" '
        'allowfullscreen="true" '
        'style="border:0;'
        'display:block"></iframe>'
        '</div>'
    )

    parts.append(
        '<p style="text-align:center;'
        'font-size:13px;'
        'opacity:.8">'
        'Adaptive streaming player '
        'powered by VCDN.'
        '</p>'
    )

    # --------------------------------------------------------
    # REVIEW
    # --------------------------------------------------------

    if review:

        parts.append(
            h3.format(
                f"{title} - "
                "Film Review and Analysis"
            )
        )

        parts += [
            f"<p>{p}</p>"
            for p in review
        ]

    # --------------------------------------------------------
    # THEMES
    # --------------------------------------------------------

    if meta["themes"]:

        parts.append(
            h3.format(
                "Themes"
            )
        )

        parts.append(
            "<ul>"
            + "".join(
                f"<li>{e(t)}</li>"
                for t in meta["themes"]
            )
            + "</ul>"
        )

    # --------------------------------------------------------
    # SCREENSHOTS
    # --------------------------------------------------------

    parts.append(
        h3.format(
            "Screenshots"
        )
    )

    for fid in shot_ids:

        parts.append(
            '<p style="text-align:center">'
            f'<img src="{img_url(fid)}" '
            f'alt="{title} screenshot" '
            'style="max-width:100%;'
            'height:auto"/>'
            '</p>'
        )

    parts.append(
        hr
    )

    # --------------------------------------------------------
    # STREAMTAPE DOWNLOAD LINKS
    # --------------------------------------------------------

    parts.append(
        h3.format(
            "Download Links"
        )
    )

    for h, st, size in outputs:

        stream_url = st[
            "link"
        ]

        parts.append(
            f'<h4 style="{head}">'
            f'{title}{ytxt}{lang_tag} '
            f'{h}p x264 '
            f'{fps_txt}fps '
            f'[{human(size)}]'
            f'</h4>'
        )

        parts.append(
            f'<a class="mv-dl" '
            f'data-url="{e(stream_url, quote=True)}" '
            f'href="{e(stream_url, quote=True)}" '
            'rel="noopener" '
            f'style="{btn}">'
            '&#11015;&#9889;'
            'DOWNLOAD NOW'
            '&#9889;&#11015;'
            '</a>'
        )

    parts.append(
        hr
    )

    # --------------------------------------------------------
    # FAQ
    # --------------------------------------------------------

    if meta["faq"]:

        parts.append(
            h3.format(
                f"{title} - FAQ"
            )
        )

        for f in meta["faq"]:

            parts.append(
                f"<h4>"
                f"{e(str(f['q']))}"
                f"</h4>"
                f"<p>"
                f"{e(str(f['a']))}"
                f"</p>"
            )

    parts.append(
        '<
