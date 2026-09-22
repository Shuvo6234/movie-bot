"""
Movie Bot:
Google Drive source
-> FFmpeg multi-resolution
-> Streamtape: 480p/720p/1080p downloads
-> VCDN: highest quality watch online
-> Blogger post
-> delete original Google Drive source

Google Drive is used ONLY as temporary source storage.
No converted videos, screenshots, thumbnails, or _output files
are uploaded to Google Drive.
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

VCDN_API_KEY = os.environ["VCDN_API_KEY"].strip()
VCDN_API_HOST = "cdn.vcdn.me"

STREAMTAPE_LOGIN = os.environ["STREAMTAPE_LOGIN"].strip()
STREAMTAPE_KEY = os.environ["STREAMTAPE_KEY"].strip()
STREAMTAPE_API_HOST = "api.streamtape.com"

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
    os.environ.get(
        "PUBLISH",
        "false"
    ).lower() == "true"
)

LANGUAGE_HINT = os.environ.get(
    "LANGUAGE_HINT",
    ""
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
    "DIRECTOR_NAME",
    ""
).strip()

STREAMTAPE_FOLDER = os.environ.get(
    "STREAMTAPE_FOLDER",
    ""
).strip()

STREAMTAPE_WAIT_SECONDS = int(
    os.environ.get(
        "STREAMTAPE_WAIT_SECONDS",
        "1800"
    )
)

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
# LOGGING
# ============================================================

def log(message):
    print(
        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
        f"{message}",
        flush=True
    )


# ============================================================
# GENERIC RETRY
# ============================================================

def retry(fn, attempts=5, delay=5):
    last_error = None

    for attempt in range(1, attempts + 1):
        try:
            return fn()

        except Exception as e:
            last_error = e

            if attempt >= attempts:
                raise

            log(
                f"Retry {attempt}/{attempts - 1} "
                f"after error: {e}"
            )

            time.sleep(delay * attempt)

    raise last_error


# ============================================================
# COMMAND RUNNER
# ============================================================

def run(cmd, check=True):
    log("$ " + " ".join(map(str, cmd)))

    p = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True
    )

    if p.stdout:
        print(p.stdout)

    if check and p.returncode != 0:
        raise RuntimeError(
            f"Command failed with code {p.returncode}"
        )

    return p.stdout


# ============================================================
# GOOGLE CLIENTS
# ============================================================

def google_credentials():
    creds = Credentials(
        token=None,
        refresh_token=REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        scopes=SCOPES,
    )

    creds.refresh(Request())

    return creds


def google_clients():
    creds = google_credentials()

    drive = build(
        "drive",
        "v3",
        credentials=creds
    )

    blogger = build(
        "blogger",
        "v3",
        credentials=creds
    )

    return drive, blogger


# ============================================================
# DRIVE
# ============================================================

def list_videos(drive):
    q = (
        f"'{INPUT_FOLDER}' in parents "
        "and trashed = false "
        "and mimeType contains 'video/'"
    )

    result = drive.files().list(
        q=q,
        fields="files(id,name,mimeType,size,createdTime)",
        orderBy="createdTime"
    ).execute()

    return result.get("files", [])


def download(drive, file_id, dest):
    log(
        f"Downloading source from Google Drive: "
        f"{dest.name}"
    )

    request = drive.files().get_media(
        fileId=file_id
    )

    with open(dest, "wb") as fh:
        downloader = MediaIoBaseDownload(
            fh,
            request,
            chunksize=16 * 1024 * 1024
        )

        done = False

        while not done:
            status, done = downloader.next_chunk()

            if status:
                log(
                    f"Drive download: "
                    f"{status.progress() * 100:.1f}%"
                )

    return dest


def delete_drive_file(drive, file_id):
    log(
        "Deleting original Google Drive source..."
    )

    retry(
        lambda: drive.files().delete(
            fileId=file_id
        ).execute()
    )

    log(
        "Original Google Drive source deleted."
    )


# ============================================================
# STREAMTAPE
# ============================================================

def _streamtape_result(data):
    result = data.get("result")

    if isinstance(result, dict):
        return result

    return data


def _streamtape_api(
    path,
    params=None,
    timeout=120
):
    params = dict(params or {})

    params["login"] = STREAMTAPE_LOGIN
    params["key"] = STREAMTAPE_KEY

    query = urllib.parse.urlencode(
        params,
        doseq=True
    )

    url = (
        f"https://{STREAMTAPE_API_HOST}"
        f"{path}?{query}"
    )

    # IMPORTANT:
    # Never print this URL because it contains the API key.

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "MovieBot/1.0"
        },
        method="GET"
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=timeout
        ) as response:

            raw = response.read()

            if response.status < 200 or response.status >= 300:
                raise RuntimeError(
                    f"Streamtape API HTTP "
                    f"{response.status}"
                )

    except urllib.error.HTTPError as e:
        body = e.read().decode(
            "utf-8",
            errors="replace"
        )

        raise RuntimeError(
            f"Streamtape API HTTP {e.code}: "
            f"{body[:500]}"
        )

    except urllib.error.URLError as e:
        raise RuntimeError(
            f"Streamtape API connection error: {e}"
        )

    try:
        data = json.loads(
            raw.decode("utf-8")
        )

    except Exception:
        raise RuntimeError(
            "Streamtape API returned invalid JSON."
        )

    status = data.get("status")

    if status is not None:
        try:
            if int(status) != 200:
                raise RuntimeError(
                    f"Streamtape API error: "
                    f"{data}"
                )
        except ValueError:
            pass

    return data


def _streamtape_upload_url(path):
    params = {}

    if STREAMTAPE_FOLDER:
        params["folder"] = STREAMTAPE_FOLDER

    params["httponly"] = 0

    data = _streamtape_api(
        "/file/ul",
        params=params,
        timeout=120
    )

    result = _streamtape_result(data)

    upload_url = (
        result.get("url")
        or result.get("upload_url")
        or result.get("uploadUrl")
    )

    if not upload_url:
        raise RuntimeError(
            "Streamtape did not return an upload URL."
        )

    return upload_url


def _multipart_upload(
    upload_url,
    file_path
):
    """
    Upload the file directly to the temporary
    Streamtape upload URL.

    The API key is NOT sent here.
    """

    file_path = Path(file_path)

    boundary = (
        "----MovieBot"
        + os.urandom(16).hex()
    )

    filename = file_path.name

    header = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; '
        f'name="file1"; '
        f'filename="{filename}"\r\n'
        f"Content-Type: video/mp4\r\n"
        f"\r\n"
    ).encode("utf-8")

    footer = (
        f"\r\n--{boundary}--\r\n"
    ).encode("utf-8")

    parsed = urllib.parse.urlsplit(
        upload_url
    )

    host = parsed.hostname

    if not host:
        raise RuntimeError(
            "Invalid Streamtape upload URL."
        )

    port = parsed.port or 443

    path = parsed.path or "/"

    if parsed.query:
        path += "?" + parsed.query

    file_size = file_path.stat().st_size

    total_length = (
        len(header)
        + file_size
        + len(footer)
    )

    log(
        f"Uploading to Streamtape: "
        f"{filename} "
        f"({file_size / 1024 / 1024:.1f} MB)"
    )

    connection = http.client.HTTPSConnection(
        host,
        port,
        timeout=STREAMTAPE_WAIT_SECONDS
    )

    try:
        connection.putrequest(
            "POST",
            path
        )

        connection.putheader(
            "Content-Type",
            f"multipart/form-data; "
            f"boundary={boundary}"
        )

        connection.putheader(
            "Content-Length",
            str(total_length)
        )

        connection.putheader(
            "User-Agent",
            "MovieBot/1.0"
        )

        connection.endheaders()

        connection.send(header)

        sent = 0
        last_percent = -1

        with open(file_path, "rb") as fh:
            while True:
                chunk = fh.read(
                    16 * 1024 * 1024
                )

                if not chunk:
                    break

                connection.send(chunk)

                sent += len(chunk)

                percent = int(
                    sent * 100 / file_size
                )

                if (
                    percent >= last_percent + 10
                    or percent == 100
                ):
                    log(
                        f"Streamtape upload: "
                        f"{percent}%"
                    )

                    last_percent = percent

        connection.send(footer)

        response = connection.getresponse()

        body = response.read()

        if response.status < 200 or response.status >= 300:
            raise RuntimeError(
                f"Streamtape upload failed "
                f"HTTP {response.status}: "
                f"{body[:500]!r}"
            )

        try:
            data = json.loads(
                body.decode(
                    "utf-8",
                    errors="replace"
                )
            )

        except Exception:
            data = {}

        log(
            "Streamtape upload request completed."
        )

        return data

    finally:
        connection.close()


def streamtape_listfolder():
    params = {}

    if STREAMTAPE_FOLDER:
        params["folder"] = STREAMTAPE_FOLDER

    data = _streamtape_api(
        "/file/listfolder",
        params=params,
        timeout=120
    )

    result = _streamtape_result(data)

    if isinstance(result, dict):
        return result.get("files", []) or []

    return []


def streamtape_running_converts():
    data = _streamtape_api(
        "/file/runningconverts",
        timeout=120
    )

    result = _streamtape_result(data)

    if isinstance(result, list):
        return result

    if isinstance(result, dict):
        return (
            result.get("files")
            or result.get("converts")
            or result.get("items")
            or []
        )

    return []


def streamtape_find_existing(
    name,
    size
):
    try:
        files = streamtape_listfolder()

    except Exception as e:
        log(
            f"Could not check existing "
            f"Streamtape files: {e}"
        )

        return None

    for item in files:
        item_name = str(
            item.get("name", "")
        )

        if item_name != name:
            continue

        item_size = item.get("size")

        try:
            if item_size is not None:
                if int(item_size) != int(size):
                    continue
        except Exception:
            pass

        return item

    return None


def _streamtape_file_id(item):
    return (
        item.get("linkid")
        or item.get("id")
        or item.get("fileid")
        or item.get("file")
    )


def _streamtape_stable_link(item):
    link = (
        item.get("link")
        or item.get("url")
    )

    if link:
        return str(link)

    linkid = item.get("linkid")

    name = item.get("name")

    if linkid and name:
        return (
            "https://streamtape.com/v/"
            f"{urllib.parse.quote(str(linkid), safe='')}/"
            f"{urllib.parse.quote(str(name), safe='')}"
        )

    return None


def streamtape_file_info(file_id):
    if not file_id:
        return {}

    try:
        data = _streamtape_api(
            "/file/info",
            params={
                "file": file_id
            },
            timeout=120
        )

        return _streamtape_result(data)

    except Exception as e:
        log(
            f"Streamtape file info check failed: {e}"
        )

        return {}


def streamtape_wait_ready(
    name,
    size
):
    """
    Wait until Streamtape exposes a stable /v/
    video page.

    We never use a temporary dlticket URL
    in Blogger.
    """

    deadline = (
        time.time()
        + STREAMTAPE_WAIT_SECONDS
    )

    last_log = 0

    while time.time() < deadline:

        item = streamtape_find_existing(
            name,
            size
        )

        if item:
            stable_link = _streamtape_stable_link(
                item
            )

            file_id = _streamtape_file_id(
                item
            )

            if stable_link:

                # Check explicit running conversion.
                converting = False

                try:
                    converts = (
                        streamtape_running_converts()
                    )

                    for convert in converts:
                        convert_name = str(
                            convert.get(
                                "name",
                                ""
                            )
                        )

                        if convert_name == name:
                            progress = convert.get(
                                "progress"
                            )

                            status = str(
                                convert.get(
                                    "status",
                                    ""
                                )
                            ).lower()

                            if (
                                status
                                and status not in {
                                    "done",
                                    "ready",
                                    "complete",
                                    "completed",
                                    "success"
                                }
                            ):
                                converting = True

                            if progress is not None:
                                try:
                                    if float(progress) < 100:
                                        converting = True
                                except Exception:
                                    pass

                            break

                except Exception:
                    pass

                if not converting:
                    log(
                        f"Streamtape ready: {name}"
                    )

                    return {
                        "id": file_id,
                        "name": name,
                        "size": size,
                        "link": stable_link,
                    }

        now = time.time()

        if now - last_log >= 30:
            log(
                f"Waiting for Streamtape "
                f"to finish: {name}"
            )
            last_log = now

        time.sleep(10)

    raise TimeoutError(
        f"Streamtape did not become ready "
        f"within {STREAMTAPE_WAIT_SECONDS} "
        f"seconds: {name}"
    )


def streamtape_upload_and_wait(
    file_path
):
    file_path = Path(file_path)

    name = file_path.name
    size = file_path.stat().st_size

    # Prevent duplicate uploads if a previous
    # GitHub Action stopped after Streamtape upload.
    existing = streamtape_find_existing(
        name,
        size
    )

    if existing:
        existing_link = _streamtape_stable_link(
            existing
        )

        if existing_link:
            log(
                f"Existing Streamtape file found: "
                f"{name}"
            )

            return streamtape_wait_ready(
                name,
                size
            )

    upload_url = retry(
        lambda: _streamtape_upload_url(
            file_path
        ),
        attempts=4,
        delay=5
    )

    # Do NOT retry the actual multipart upload
    # automatically. Retrying could create duplicates.
    _multipart_upload(
        upload_url,
        file_path
    )

    return streamtape_wait_ready(
        name,
        size
    )


# ============================================================
# VCDN
# ============================================================

VCDN_USER_AGENT = "MovieBot/1.0"


def _vcdn_auth_headers():
    return {
        "Authorization": f"Bearer {VCDN_API_KEY}",
        "User-Agent": VCDN_USER_AGENT,
    }


def _vcdn_json(
    method,
    path,
    body=None,
    timeout=120
):
    url = (
        f"https://{VCDN_API_HOST}"
        f"{path}"
    )

    headers = _vcdn_auth_headers()
    headers["Content-Type"] = "application/json"

    data = None

    if body is not None:
        data = json.dumps(body).encode(
            "utf-8"
        )

    request = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method=method
    )

    with urllib.request.urlopen(
        request,
        timeout=timeout
    ) as response:

        raw = response.read()

        if not raw:
            return {}

        return json.loads(
            raw.decode("utf-8")
        )


def _vcdn_upload_binary(
    upload_url,
    upload_id,
    file_path
):
    file_path = Path(file_path)

    parsed = urllib.parse.urlsplit(
        upload_url
    )

    host = parsed.hostname

    if not host:
        raise RuntimeError(
            "Invalid VCDN binary upload URL."
        )

    path = parsed.path

    if parsed.query:
        path += "?" + parsed.query

    size = file_path.stat().st_size

    connection = http.client.HTTPSConnection(
        host,
        parsed.port or 443,
        timeout=3600
    )

    try:
        connection.putrequest(
            "PUT",
            path
        )

        headers = _vcdn_auth_headers()

        for key, value in headers.items():
            connection.putheader(
                key,
                value
            )

        connection.putheader(
            "Content-Type",
            "application/octet-stream"
        )

        connection.putheader(
            "Content-Length",
            str(size)
        )

        if upload_id:
            connection.putheader(
                "X-Upload-ID",
                str(upload_id)
            )

        connection.endheaders()

        sent = 0
        last_percent = -1

        with open(file_path, "rb") as fh:
            while True:
                chunk = fh.read(
                    16 * 1024 * 1024
                )

                if not chunk:
                    break

                connection.send(chunk)

                sent += len(chunk)

                percent = int(
                    sent * 100 / size
                )

                if (
                    percent >= last_percent + 10
                    or percent == 100
                ):
                    log(
                        f"VCDN upload: "
                        f"{percent}%"
                    )

                    last_percent = percent

        response = connection.getresponse()

        body = response.read()

        if response.status < 200 or response.status >= 300:
            raise RuntimeError(
                f"VCDN binary upload failed: "
                f"HTTP {response.status}: "
                f"{body[:500]!r}"
            )

        return body

    finally:
        connection.close()


def _multipart_header(
    boundary,
    file_path
):
    filename = Path(file_path).name

    return (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; '
        f'name="file"; '
        f'filename="{filename}"\r\n'
        f"Content-Type: video/mp4\r\n"
        f"\r\n"
    ).encode("utf-8")


def _vcdn_direct_upload(file_path):
    """
    Try the direct VCDN /videos upload.
    If VCDN rejects it because of payload size,
    caller can fall back to chunked upload.
    """

    boundary = (
        "----MovieBotVCDN"
        + os.urandom(16).hex()
    )

    header = _multipart_header(
        boundary,
        file_path
    )

    footer = (
        f"\r\n--{boundary}--\r\n"
    ).encode("utf-8")

    size = Path(file_path).stat().st_size

    total = (
        len(header)
        + size
        + len(footer)
    )

    connection = http.client.HTTPSConnection(
        VCDN_API_HOST,
        timeout=3600
    )

    try:
        connection.putrequest(
            "POST",
            "/videos"
        )

        headers = _vcdn_auth_headers()

        for key, value in headers.items():
            connection.putheader(
                key,
                value
            )

        connection.putheader(
            "Content-Type",
            f"multipart/form-data; "
            f"boundary={boundary}"
        )

        connection.putheader(
            "Content-Length",
            str(total)
        )

        connection.endheaders()

        connection.send(header)

        with open(file_path, "rb") as fh:
            while True:
                chunk = fh.read(
                    16 * 1024 * 1024
                )

                if not chunk:
                    break

                connection.send(chunk)

        connection.send(footer)

        response = connection.getresponse()

        body = response.read()

        if response.status < 200 or response.status >= 300:
            raise RuntimeError(
                f"VCDN direct upload HTTP "
                f"{response.status}: "
                f"{body[:500]!r}"
            )

        return json.loads(
            body.decode("utf-8")
        )

    finally:
        connection.close()


def vcdn_upload(file_path):
    """
    VCDN upload:
    1. Try direct /videos
    2. If it fails, use chunked upload
    3. Poll until ready
    """

    file_path = Path(file_path)

    log(
        f"Uploading highest quality to VCDN: "
        f"{file_path.name}"
    )

    try:
        direct = _vcdn_direct_upload(
            file_path
        )

        video_id = (
            direct.get("id")
            or direct.get("videoId")
            or direct.get("video_id")
            or (
                direct.get("result", {})
                if isinstance(
                    direct.get("result"),
                    dict
                )
                else {}
            ).get("id")
        )

        if video_id:
            log(
                f"VCDN direct upload accepted: "
                f"{video_id}"
            )

        else:
            raise RuntimeError(
                "VCDN direct upload returned "
                "no video ID."
            )

    except Exception as direct_error:
        log(
            "VCDN direct upload failed. "
            "Trying chunked upload..."
        )

        log(
            f"Direct upload reason: "
            f"{direct_error}"
        )

        init = _vcdn_json(
            "POST",
            "/api/v1/upload/init",
            {
                "filename": file_path.name,
                "filesize": file_path.stat().st_size,
            }
        )

        video_id = (
            init.get("videoId")
            or init.get("video_id")
            or init.get("id")
            or (
                init.get("data", {})
                if isinstance(
                    init.get("data"),
                    dict
                )
                else {}
            ).get("videoId")
        )

        upload_id = (
            init.get("uploadId")
            or init.get("upload_id")
            or (
                init.get("data", {})
                if isinstance(
                    init.get("data"),
                    dict
                )
                else {}
            ).get("uploadId")
        )

        upload_url = (
            init.get("uploadUrl")
            or init.get("upload_url")
            or (
                init.get("data", {})
                if isinstance(
                    init.get("data"),
                    dict
                )
                else {}
            ).get("uploadUrl")
        )

        if not video_id:
            raise RuntimeError(
                f"VCDN init did not return "
                f"video ID: {init}"
            )

        if not upload_url:
            upload_url = (
                f"https://{VCDN_API_HOST}"
                f"/api/v1/upload/{video_id}"
            )

        log(
            f"VCDN chunked upload initialized: "
            f"{video_id}"
        )

        _vcdn_upload_binary(
            upload_url,
            upload_id,
            file_path
        )

        _vcdn_json(
            "POST",
            "/api/v1/upload/complete",
            {
                "uploadId": upload_id,
                "upload_id": upload_id,
                "videoId": video_id,
                "video_id": video_id,
            }
        )

        log(
            "VCDN chunked upload completed."
        )

    # Poll VCDN
    deadline = time.time() + 1800

    while time.time() < deadline:

        data = _vcdn_json(
            "GET",
            f"/api/v1/videos/{video_id}"
        )

        status = str(
            data.get("status")
            or data.get("state")
            or data.get("data", {}).get(
                "status",
                ""
            )
        ).lower()

        log(
            f"VCDN status: {status or 'unknown'}"
        )

        if status in {
            "ready",
            "completed",
            "complete",
            "published",
            "success"
        }:
            break

        if status in {
            "failed",
            "error",
            "cancelled"
        }:
            raise RuntimeError(
                f"VCDN processing failed: "
                f"{data}"
            )

        time.sleep(10)

    else:
        raise TimeoutError(
            "VCDN processing timed out."
        )

    embed = (
        data.get("embed_url")
        or data.get("embedUrl")
        or data.get("embed")
    )

    if not embed:
        playback = data.get(
            "playback_sources"
        )

        if isinstance(playback, list):
            for source in playback:
                if isinstance(source, dict):
                    embed = (
                        source.get("embed_url")
                        or source.get("embedUrl")
                        or source.get("url")
                    )

                    if embed:
                        break

    if not embed:
        embed = (
            f"https://embed.vcdn.me/"
            f"embed/{video_id}"
        )

    log(
        f"VCDN ready: {embed}"
    )

    return {
        "id": video_id,
        "embed": embed,
        "data": data,
    }


# ============================================================
# FFMPEG
# ============================================================

def parse_fps(value):
    if not value:
        return 30.0

    if "/" in str(value):
        a, b = str(value).split("/", 1)

        try:
            return float(a) / float(b)
        except Exception:
            return 30.0

    try:
        return float(value)

    except Exception:
        return 30.0


def fmt_fps(value):
    return f"{value:.3f}".rstrip("0").rstrip(".")


def probe(path):
    output = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-show_entries",
            "stream=index,codec_type,width,height,r_frame_rate",
            "-of",
            "json",
            str(path),
        ]
    )

    data = json.loads(output)

    duration = float(
        data.get(
            "format",
            {}
        ).get(
            "duration",
            0
        )
    )

    video = None

    for stream in data.get(
        "streams",
        []
    ):
        if stream.get("codec_type") == "video":
            video = stream
            break

    if not video:
        raise RuntimeError(
            "No video stream found."
        )

    width = int(
        video.get("width") or 0
    )

    height = int(
        video.get("height") or 0
    )

    fps = parse_fps(
        video.get("r_frame_rate")
    )

    return {
        "duration": duration,
        "width": width,
        "height": height,
        "fps": fps,
    }


def make_screenshots(
    source,
    out_dir,
    count,
    duration
):
    out_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    if count <= 0 or duration <= 0:
        return []

    results = []

    for i in range(count):
        ratio = (
            (i + 1)
            / (count + 1)
        )

        timestamp = duration * ratio

        output = (
            out_dir
            / f"shot_{i + 1:02d}.jpg"
        )

        run(
            [
                "ffmpeg",
                "-y",
                "-ss",
                str(timestamp),
                "-i",
                str(source),
                "-frames:v",
                "1",
                "-q:v",
                "2",
                str(output),
            ]
        )

        results.append(output)

    return results


def make_thumbnail(
    source,
    out_dir,
    duration
):
    out_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    timestamp = max(
        0,
        duration * 0.15
    )

    output = (
        out_dir
        / "thumbnail.jpg"
    )

    run(
        [
            "ffmpeg",
            "-y",
            "-ss",
            str(timestamp),
            "-i",
            str(source),
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(output),
        ]
    )

    return output


def analysis_inputs(
    source,
    work_dir,
    duration
):
    """
    Keep the existing analysis pipeline.
    """

    audio_dir = (
        work_dir
        / "audio"
    )

    audio_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    audio_file = (
        audio_dir
        / "audio.mp3"
    )

    seconds = min(
        duration,
        AUDIO_MINUTES * 60
    )

    run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(source),
            "-t",
            str(seconds),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-b:a",
            "64k",
            str(audio_file),
        ]
    )

    return audio_file


def transcode(
    source,
    output,
    target_height,
    source_fps
):
    crf = CRF.get(
        target_height,
        23
    )

    output.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(source),

            "-vf",
            (
                f"scale=-2:{target_height}:"
                "force_original_aspect_ratio=decrease"
            ),

            "-c:v",
            "libx264",

            "-preset",
            "veryfast",

            "-crf",
            str(crf),

            "-pix_fmt",
            "yuv420p",

            "-r",
            fmt_fps(source_fps),

            "-c:a",
            "aac",

            "-b:a",
            "128k",

            "-movflags",
            "+faststart",

            str(output),
        ]
    )

    return output


# ============================================================
# GEMINI / LABELS
# ============================================================

FALLBACK_LABELS = [
    "Action",
    "Adventure",
    "Comedy",
    "Crime",
    "Drama",
    "Family",
    "Fantasy",
    "Horror",
    "Mystery",
    "Romance",
    "Sci-Fi",
    "Thriller",
]

BLOCKED_LABEL = "Uncategorized"


def parse_labels(text):
    if not text:
        return []

    parts = re.split(
        r"[,|\n]+",
        text
    )

    cleaned = []

    for part in parts:
        part = part.strip()

        if not part:
            continue

        part = re.sub(
            r"^[\-\*\d\.\)\s]+",
            "",
            part
        ).strip()

        if part and part not in cleaned:
            cleaned.append(part)

    return cleaned


def get_site_labels(blogger):
    try:
        data = blogger.blogs().get(
            blogId=BLOG_ID
        ).execute()

        return data

    except Exception as e:
        log(
            f"Could not read Blogger info: {e}"
        )

        return {}


def pick_labels(text):
    labels = parse_labels(text)

    result = []

    for label in labels:
        if (
            label.lower()
            == BLOCKED_LABEL.lower()
        ):
            continue

        if label not in result:
            result.append(label)

    if not result:
        result = FALLBACK_LABELS[:3]

    return result[:8]


def as_paragraphs(text):
    if not text:
        return ""

    parts = re.split(
        r"\n\s*\n",
        text.strip()
    )

    return "".join(
        f"<p>{html.escape(p.strip())}</p>"
        for p in parts
        if p.strip()
    )


YEAR_RE = re.compile(
    r"\b(19|20)\d{2}\b"
)


def find_year(text):
    if not text:
        return ""

    m = YEAR_RE.search(text)

    return m.group(0) if m else ""


def clean_hint(text):
    return re.sub(
        r"\s+",
        " ",
        text or ""
    ).strip()


def analyze(
    gemini,
    source,
    movie_name,
    duration
):
    prompt = f"""
You are generating metadata for a movie website.

Movie filename:
{movie_name}

Language hint:
{LANGUAGE_HINT}

Director hint:
{DIRECTOR_NAME}

Duration:
{duration:.1f} seconds

Return ONLY valid JSON:

{{
  "title": "",
  "description": "",
  "review": "",
  "themes": "",
  "labels": []
}}

Rules:
- Create a clean movie title.
- Do not invent cast information.
- Do not invent ratings.
- Do not invent facts that are not reasonably supported.
- Description should be useful for a movie website.
- Review should be neutral.
- Themes should be concise.
- Labels should be simple genres/topics.
"""

    response = gemini.models.generate_content(
        model=GEMINI_MODEL,
        contents=[
            types.Content(
                role="user",
                parts=[
                    types.Part.from_text(
                        text=prompt
                    )
                ]
            )
        ]
    )

    text = response.text or ""

    match = re.search(
        r"\{.*\}",
        text,
        re.S
    )

    if not match:
        raise RuntimeError(
            "Gemini did not return valid JSON."
        )

    data = json.loads(
        match.group(0)
    )

    return {
        "title": str(
            data.get(
                "title",
                movie_name
            )
        ).strip(),

        "description": str(
            data.get(
                "description",
                ""
            )
        ).strip(),

        "review": str(
            data.get(
                "review",
                ""
            )
        ).strip(),

        "themes": str(
            data.get(
                "themes",
                ""
            )
        ).strip(),

        "labels": pick_labels(
            ",".join(
                map(
                    str,
                    data.get(
                        "labels",
                        []
                    )
                )
            )
        ),
    }


# ============================================================
# HTML
# ============================================================

def human(size):
    size = float(size)

    units = [
        "B",
        "KB",
        "MB",
        "GB",
        "TB"
    ]

    for unit in units:
        if size < 1024:
            return (
                f"{size:.1f} {unit}"
            )

        size /= 1024

    return f"{size:.1f} PB"


def fmt_runtime(seconds):
    seconds = int(seconds)

    h = seconds // 3600

    m = (
        seconds % 3600
    ) // 60

    s = (
        seconds % 60
    )

    if h:
        return (
            f"{h}:{m:02d}:{s:02d}"
        )

    return (
        f"{m}:{s:02d}"
    )


def TIMER_SCRIPT(wait_seconds):
    return f"""
<script>
(function() {{
    const WAIT = {int(wait_seconds)};

    document.addEventListener(
        "click",
        function(event) {{
            const button =
                event.target.closest(
                   
