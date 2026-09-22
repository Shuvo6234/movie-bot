"""
Movie Bot

Google Drive source
        |
        v
FFmpeg multi-resolution
        |
        +----> Streamtape
        |       480p / 720p / 1080p
        |
        +----> VCDN
                highest quality only
        |
        v
Blogger

IMPORTANT:
- Google Drive is used ONLY as temporary source storage.
- No converted video is uploaded to Google Drive.
- No screenshot is uploaded to Google Drive.
- No thumbnail is uploaded to Google Drive.
- No _output folder is created in Google Drive.
- Original Drive source is deleted ONLY after:
      Streamtape + VCDN + Blogger verification
  all succeed.
- If anything fails, original Drive source remains.
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
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload


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

# Primary Gemini model
GEMINI_MODEL = os.environ.get(
    "GEMINI_MODEL",
    "gemini-3.6-flash"
).strip()

# Fallback Gemini models
GEMINI_FALLBACK_MODELS = [
    x.strip()
    for x in os.environ.get(
        "GEMINI_FALLBACK_MODELS",
        "gemini-2.5-flash"
    ).split(",")
    if x.strip()
]

RESOLUTIONS = [
    int(x.strip())
    for x in os.environ.get(
        "RESOLUTIONS",
        "480,720,1080"
    ).split(",")
    if x.strip()
]

MAX_VIDEOS = int(
    os.environ.get(
        "MAX_VIDEOS",
        "1"
    )
)

PUBLISH = (
    os.environ.get(
        "PUBLISH",
        "false"
    ).lower()
    == "true"
)

LANGUAGE_HINT = os.environ.get(
    "LANGUAGE_HINT",
    ""
).strip()

DIRECTOR_NAME = os.environ.get(
    "DIRECTOR_NAME",
    ""
).strip()

WAIT_SECONDS = int(
    os.environ.get(
        "WAIT_SECONDS",
        "20"
    )
)

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

VCDN_WAIT_SECONDS = int(
    os.environ.get(
        "VCDN_WAIT_SECONDS",
        "1800"
    )
)

# Gemini retry
GEMINI_RETRY_ATTEMPTS = int(
    os.environ.get(
        "GEMINI_RETRY_ATTEMPTS",
        "5"
    )
)

GEMINI_RETRY_BASE_DELAY = int(
    os.environ.get(
        "GEMINI_RETRY_BASE_DELAY",
        "8"
    )
)

GEMINI_RETRY_MAX_DELAY = int(
    os.environ.get(
        "GEMINI_RETRY_MAX_DELAY",
        "120"
    )
)

WORK = Path("work")

CRF = {
    480: 24,
    720: 23,
    1080: 22,
    1440: 22,
    2160: 21,
}

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/blogger",
]


# ============================================================
# LOGGING
# ============================================================

def log(message):
    print(
        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}",
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

        except Exception as error:

            last_error = error

            if attempt >= attempts:
                raise

            log(
                f"Retry {attempt}/{attempts - 1}: "
                f"{error}"
            )

            time.sleep(
                delay * attempt
            )

    raise last_error


# ============================================================
# COMMAND
# ============================================================

def run_command(command, check=True):

    log(
        "$ " + " ".join(
            map(str, command)
        )
    )

    process = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True
    )

    if process.stdout:
        print(
            process.stdout,
            flush=True
        )

    if (
        check
        and process.returncode != 0
    ):
        raise RuntimeError(
            "Command failed with "
            f"exit code {process.returncode}"
        )

    return process.stdout


# ============================================================
# GOOGLE AUTH
# ============================================================

def google_credentials():

    credentials = Credentials(
        token=None,
        refresh_token=REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        scopes=SCOPES
    )

    credentials.refresh(
        Request()
    )

    return credentials


def google_clients():

    credentials = google_credentials()

    drive = build(
        "drive",
        "v3",
        credentials=credentials
    )

    blogger = build(
        "blogger",
        "v3",
        credentials=credentials
    )

    return drive, blogger


# ============================================================
# GOOGLE DRIVE
# ============================================================

def list_videos(drive):

    query = (
        f"'{INPUT_FOLDER}' in parents "
        "and trashed = false "
        "and mimeType contains 'video/'"
    )

    result = (
        drive.files()
        .list(
            q=query,
            fields=(
                "files("
                "id,"
                "name,"
                "mimeType,"
                "size,"
                "createdTime"
                ")"
            ),
            orderBy="createdTime"
        )
        .execute()
    )

    return result.get(
        "files",
        []
    )


def download_drive_file(
    drive,
    file_id,
    destination
):

    log(
        "Downloading original from Google Drive: "
        f"{destination.name}"
    )

    request = (
        drive.files()
        .get_media(
            fileId=file_id
        )
    )

    with open(
        destination,
        "wb"
    ) as file_handle:

        downloader = MediaIoBaseDownload(
            file_handle,
            request,
            chunksize=16 * 1024 * 1024
        )

        done = False

        while not done:

            status, done = (
                downloader.next_chunk()
            )

            if status:

                log(
                    "Drive download: "
                    f"{status.progress() * 100:.1f}%"
                )

    return destination


def delete_drive_file(
    drive,
    file_id
):

    log(
        "Deleting original Google Drive source..."
    )

    retry(
        lambda: (
            drive.files()
            .delete(
                fileId=file_id
            )
            .execute()
        ),
        attempts=5,
        delay=5
    )

    log(
        "Original Google Drive source "
        "deleted successfully."
    )


# ============================================================
# STREAMTAPE
# ============================================================

def streamtape_result(data):

    result = data.get("result")

    if isinstance(
        result,
        dict
    ):
        return result

    return data


def streamtape_api(
    path,
    params=None,
    timeout=120
):

    params = dict(
        params or {}
    )

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

            if not (
                200 <= response.status < 300
            ):

                raise RuntimeError(
                    "Streamtape API HTTP "
                    f"{response.status}"
                )

    except urllib.error.HTTPError as error:

        body = error.read().decode(
            "utf-8",
            errors="replace"
        )

        raise RuntimeError(
            "Streamtape API HTTP "
            f"{error.code}: {body[:500]}"
        )

    except urllib.error.URLError as error:

        raise RuntimeError(
            "Streamtape API connection error: "
            f"{error}"
        )

    try:

        data = json.loads(
            raw.decode("utf-8")
        )

    except Exception as error:

        raise RuntimeError(
            "Streamtape API returned "
            f"invalid JSON: {error}"
        )

    status = data.get(
        "status"
    )

    if status is not None:

        try:

            if int(status) != 200:

                raise RuntimeError(
                    "Streamtape API error: "
                    f"{data}"
                )

        except ValueError:
            pass

    return data


def streamtape_upload_url():

    params = {
        "httponly": 0
    }

    if STREAMTAPE_FOLDER:

        params["folder"] = STREAMTAPE_FOLDER

    data = streamtape_api(
        "/file/ul",
        params=params,
        timeout=120
    )

    result = streamtape_result(
        data
    )

    upload_url = (
        result.get("url")
        or result.get("upload_url")
        or result.get("uploadUrl")
    )

    if not upload_url:

        raise RuntimeError(
            "Streamtape did not return "
            "an upload URL."
        )

    return upload_url


def streamtape_multipart_upload(
    upload_url,
    file_path
):

    file_path = Path(file_path)

    boundary = (
        "----MovieBot"
        + os.urandom(16).hex()
    )

    filename = file_path.name

    header = (
        f"--{boundary}\r\n"
        "Content-Disposition: form-data; "
        f'name="file1"; '
        f'filename="{filename}"\r\n'
        "Content-Type: video/mp4\r\n"
        "\r\n"
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

    connection = http.client.HTTPSConnection(
        host,
        parsed.port or 443,
        timeout=STREAMTAPE_WAIT_SECONDS
    )

    file_size = file_path.stat().st_size

    total_length = (
        len(header)
        + file_size
        + len(footer)
    )

    log(
        "Uploading to Streamtape: "
        f"{filename} "
        f"({file_size / 1024 / 1024:.1f} MB)"
    )

    try:

        connection.putrequest(
            "POST",
            parsed.path
            + (
                "?" + parsed.query
                if parsed.query
                else ""
            )
        )

        connection.putheader(
            "Content-Type",
            "multipart/form-data; "
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
        last_percent = -10

        with open(
            file_path,
            "rb"
        ) as file_handle:

            while True:

                chunk = file_handle.read(
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
                        "Streamtape upload: "
                        f"{percent}%"
                    )

                    last_percent = percent

        connection.send(footer)

        response = connection.getresponse()

        body = response.read()

        if not (
            200 <= response.status < 300
        ):

            raise RuntimeError(
                "Streamtape upload failed "
                f"HTTP {response.status}: "
                f"{body[:500]!r}"
            )

        try:

            return json.loads(
                body.decode(
                    "utf-8",
                    errors="replace"
                )
            )

        except Exception:

            return {}

    finally:

        connection.close()


def streamtape_list_folder():

    params = {}

    if STREAMTAPE_FOLDER:

        params["folder"] = STREAMTAPE_FOLDER

    data = streamtape_api(
        "/file/listfolder",
        params=params,
        timeout=120
    )

    result = streamtape_result(
        data
    )

    if isinstance(
        result,
        dict
    ):

        return result.get(
            "files",
            []
        ) or []

    return []


def streamtape_running_converts():

    data = streamtape_api(
        "/file/runningconverts",
        timeout=120
    )

    result = streamtape_result(
        data
    )

    if isinstance(
        result,
        list
    ):
        return result

    if isinstance(
        result,
        dict
    ):

        return (
            result.get("files")
            or result.get("converts")
            or result.get("items")
            or []
        )

    return []


def streamtape_file_id(item):

    return (
        item.get("linkid")
        or item.get("id")
        or item.get("fileid")
        or item.get("file")
    )


def streamtape_stable_link(item):

    link = (
        item.get("link")
        or item.get("url")
    )

    if link:

        return str(link)

    linkid = item.get(
        "linkid"
    )

    name = item.get(
        "name"
    )

    if linkid and name:

        return (
            "https://streamtape.com/v/"
            f"{urllib.parse.quote(str(linkid), safe='')}/"
            f"{urllib.parse.quote(str(name), safe='')}"
        )

    return None


def streamtape_find_existing(
    name,
    size
):

    try:

        files = streamtape_list_folder()

    except Exception as error:

        log(
            "Could not check existing "
            f"Streamtape files: {error}"
        )

        return None

    for item in files:

        if str(
            item.get("name", "")
        ) != name:

            continue

        item_size = item.get(
            "size"
        )

        if item_size is not None:

            try:

                if int(item_size) != int(size):

                    continue

            except Exception:
                pass

        return item

    return None


def streamtape_is_converting(
    name
):

    try:

        converts = (
            streamtape_running_converts()
        )

    except Exception:

        return False

    for item in converts:

        item_name = str(
            item.get("name", "")
        )

        if item_name != name:

            continue

        status = str(
            item.get("status", "")
        ).lower()

        if status in {
            "done",
            "ready",
            "complete",
            "completed",
            "success"
        }:

            return False

        progress = item.get(
            "progress"
        )

        if progress is not None:

            try:

                if float(progress) >= 100:

                    return False

            except Exception:
                pass

        return True

    return False


def streamtape_wait_ready(
    name,
    size
):

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

            link = streamtape_stable_link(
                item
            )

            if (
                link
                and not streamtape_is_converting(
                    name
                )
            ):

                file_id = streamtape_file_id(
                    item
                )

                log(
                    "Streamtape ready: "
                    f"{name}"
                )

                return {
                    "id": file_id,
                    "name": name,
                    "size": size,
                    "link": link
                }

        now = time.time()

        if now - last_log >= 30:

            log(
                "Waiting for Streamtape: "
                f"{name}"
            )

            last_log = now

        time.sleep(10)

    raise TimeoutError(
        "Streamtape did not become ready "
        f"within {STREAMTAPE_WAIT_SECONDS} "
        f"seconds: {name}"
    )


def streamtape_upload_and_wait(
    file_path
):

    file_path = Path(file_path)

    name = file_path.name
    size = file_path.stat().st_size

    existing = streamtape_find_existing(
        name,
        size
    )

    if existing:

        link = streamtape_stable_link(
            existing
        )

        if link:

            log(
                "Existing Streamtape file found: "
                f"{name}"
            )

            return streamtape_wait_ready(
                name,
                size
            )

    upload_url = retry(
        streamtape_upload_url,
        attempts=4,
        delay=5
    )

    streamtape_multipart_upload(
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

def vcdn_headers():

    return {
        "Authorization": (
            f"Bearer {VCDN_API_KEY}"
        ),
        "User-Agent": "MovieBot/1.0"
    }


def vcdn_json(
    method,
    path,
    body=None,
    timeout=120
):

    url = (
        f"https://{VCDN_API_HOST}"
        f"{path}"
    )

    headers = vcdn_headers()

    headers["Content-Type"] = (
        "application/json"
    )

    data = None

    if body is not None:

        data = json.dumps(
            body
        ).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method=method
    )

    try:

        with urllib.request.urlopen(
            request,
            timeout=timeout
        ) as response:

            raw = response.read()

            if not raw:
                return {}

            if not (
                200 <= response.status < 300
            ):

                raise RuntimeError(
                    "VCDN HTTP "
                    f"{response.status}: "
                    f"{raw[:500]!r}"
                )

            return json.loads(
                raw.decode("utf-8")
            )

    except urllib.error.HTTPError as error:

        body_text = (
            error.read()
            .decode(
                "utf-8",
                errors="replace"
            )
        )

        raise RuntimeError(
            "VCDN HTTP "
            f"{error.code}: "
            f"{body_text[:500]}"
        )


def vcdn_direct_upload(
    file_path
):

    file_path = Path(file_path)

    boundary = (
        "----MovieBotVCDN"
        + os.urandom(16).hex()
    )

    filename = file_path.name

    header = (
        f"--{boundary}\r\n"
        "Content-Disposition: form-data; "
        f'name="file"; '
        f'filename="{filename}"\r\n'
        "Content-Type: video/mp4\r\n"
        "\r\n"
    ).encode("utf-8")

    footer = (
        f"\r\n--{boundary}--\r\n"
    ).encode("utf-8")

    size = file_path.stat().st_size

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

        for key, value in vcdn_headers().items():

            connection.putheader(
                key,
                value
            )

        connection.putheader(
            "Content-Type",
            "multipart/form-data; "
            f"boundary={boundary}"
        )

        connection.putheader(
            "Content-Length",
            str(total)
        )

        connection.endheaders()

        connection.send(header)

        with open(
            file_path,
            "rb"
        ) as file_handle:

            while True:

                chunk = file_handle.read(
                    16 * 1024 * 1024
                )

                if not chunk:
                    break

                connection.send(chunk)

        connection.send(footer)

        response = connection.getresponse()

        body = response.read()

        if not (
            200 <= response.status < 300
        ):

            raise RuntimeError(
                "VCDN direct upload HTTP "
                f"{response.status}: "
                f"{body[:500]!r}"
            )

        return json.loads(
            body.decode("utf-8")
        )

    finally:

        connection.close()


def vcdn_upload_binary(
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
            "Invalid VCDN upload URL."
        )

    path = parsed.path or "/"

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

        for key, value in vcdn_headers().items():

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
        last_percent = -10

        with open(
            file_path,
            "rb"
        ) as file_handle:

            while True:

                chunk = file_handle.read(
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
                        "VCDN upload: "
                        f"{percent}%"
                    )

                    last_percent = percent

        response = connection.getresponse()

        body = response.read()

        if not (
            200 <= response.status < 300
        ):

            raise RuntimeError(
                "VCDN binary upload failed "
                f"HTTP {response.status}: "
                f"{body[:500]!r}"
            )

        return body

    finally:

        connection.close()


def vcdn_value(
    data,
    *keys
):

    if not isinstance(
        data,
        dict
    ):
        return None

    for key in keys:

        value = data.get(key)

        if value:
            return value

    return None


def vcdn_chunked_upload(
    file_path
):

    file_path = Path(file_path)

    init = vcdn_json(
        "POST",
        "/api/v1/upload/init",
        {
            "filename": file_path.name,
            "filesize": file_path.stat().st_size
        }
    )

    data = init.get("data")

    if not isinstance(
        data,
        dict
    ):
        data = {}

    video_id = (
        vcdn_value(
            init,
            "videoId",
            "video_id",
            "id"
        )
        or
        vcdn_value(
            data,
            "videoId",
            "video_id",
            "id"
        )
    )

    upload_id = (
        vcdn_value(
            init,
            "uploadId",
            "upload_id"
        )
        or
        vcdn_value(
            data,
            "uploadId",
            "upload_id"
        )
    )

    upload_url = (
        vcdn_value(
            init,
            "uploadUrl",
            "upload_url"
        )
        or
        vcdn_value(
            data,
            "uploadUrl",
            "upload_url"
        )
    )

    if not video_id:

        raise RuntimeError(
            "VCDN init did not return "
            f"a video ID: {init}"
        )

    if not upload_url:

        upload_url = (
            f"https://{VCDN_API_HOST}"
            f"/api/v1/upload/{video_id}"
        )

    log(
        "VCDN chunked upload initialized: "
        f"{video_id}"
    )

    vcdn_upload_binary(
        upload_url,
        upload_id,
        file_path
    )

    vcdn_json(
        "POST",
        "/api/v1/upload/complete",
        {
            "uploadId": upload_id,
            "upload_id": upload_id,
            "videoId": video_id,
            "video_id": video_id
        }
    )

    log(
        "VCDN chunked upload completed."
    )

    return video_id


def vcdn_extract_embed(
    data,
    video_id
):

    if not isinstance(
        data,
        dict
    ):
        data = {}

    embed = (
        data.get("embed_url")
        or data.get("embedUrl")
        or data.get("embed")
    )

    if embed:
        return str(embed)

    data_section = data.get(
        "data"
    )

    if isinstance(
        data_section,
        dict
    ):

        embed = (
            data_section.get("embed_url")
            or data_section.get("embedUrl")
            or data_section.get("embed")
        )

        if embed:
            return str(embed)

    return (
        "https://embed.vcdn.me/"
        f"embed/{video_id}"
    )


def vcdn_upload(
    file_path
):

    file_path = Path(file_path)

    log(
        "Uploading highest quality to VCDN: "
        f"{file_path.name}"
    )

    video_id = None

    try:

        direct = vcdn_direct_upload(
            file_path
        )

        direct_data = direct

        if isinstance(
            direct.get("result"),
            dict
        ):

            direct_data = direct["result"]

        video_id = (
            vcdn_value(
                direct,
                "id",
                "videoId",
                "video_id"
            )
            or
            vcdn_value(
                direct_data,
                "id",
                "videoId",
                "video_id"
            )
        )

        if not video_id:

            raise RuntimeError(
                "VCDN direct upload returned "
                "no video ID."
            )

        log(
            "VCDN direct upload accepted: "
            f"{video_id}"
        )

    except Exception as direct_error:

        log(
            "VCDN direct upload failed."
        )

        log(
            f"Reason: {direct_error}"
        )

        log(
            "Trying VCDN chunked upload..."
        )

        video_id = vcdn_chunked_upload(
            file_path
        )

    deadline = (
        time.time()
        + VCDN_WAIT_SECONDS
    )

    latest = {}

    while time.time() < deadline:

        latest = vcdn_json(
            "GET",
            f"/api/v1/videos/{video_id}"
        )

        status = ""

        if isinstance(
            latest,
            dict
        ):

            status = str(
                latest.get("status", "")
                or latest.get("state", "")
            ).lower()

            nested = latest.get(
                "data"
            )

            if (
                not status
                and isinstance(
                    nested,
                    dict
                )
            ):

                status = str(
                    nested.get("status", "")
                    or nested.get("state", "")
                ).lower()

        log(
            "VCDN status: "
            f"{status or 'unknown'}"
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
                "VCDN processing failed: "
                f"{latest}"
            )

        time.sleep(10)

    else:

        raise TimeoutError(
            "VCDN processing timed out."
        )

    embed = vcdn_extract_embed(
        latest,
        video_id
    )

    log(
        "VCDN ready: "
        f"{embed}"
    )

    return {
        "id": video_id,
        "embed": embed,
        "data": latest
    }


# ============================================================
# FFMPEG
# ============================================================

def parse_fps(value):

    if not value:
        return 30.0

    value = str(value)

    if "/" in value:

        a, b = value.split("/", 1)

        try:
            return float(a) / float(b)

        except Exception:
            return 30.0

    try:
        return float(value)

    except Exception:
        return 30.0


def fmt_fps(value):

    return (
        f"{value:.3f}"
        .rstrip("0")
        .rstrip(".")
    )


def probe(path):

    output = run_command(
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
            str(path)
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

    video_stream = None

    for stream in data.get(
        "streams",
        []
    ):

        if stream.get(
            "codec_type"
        ) == "video":

            video_stream = stream
            break

    if not video_stream:

        raise RuntimeError(
            "No video stream found."
        )

    width = int(
        video_stream.get(
            "width"
        ) or 0
    )

    height = int(
        video_stream.get(
            "height"
        ) or 0
    )

    fps = parse_fps(
        video_stream.get(
            "r_frame_rate"
        )
    )

    if (
        width <= 0
        or height <= 0
    ):

        raise RuntimeError(
            "Could not determine "
            "video dimensions."
        )

    return {
        "duration": duration,
        "width": width,
        "height": height,
        "fps": fps
    }


def transcode(
    source,
    output,
    target_height,
    source_fps
):

    output = Path(output)

    output.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    crf = CRF.get(
        target_height,
        23
    )

    run_command(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(source),

            "-map",
            "0:v:0",

            "-map",
            "0:a:0?",

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

            str(output)
        ]
    )

    if not output.exists():

        raise RuntimeError(
            f"FFmpeg output missing: "
            f"{output}"
        )

    if output.stat().st_size <= 0:

        raise RuntimeError(
            f"FFmpeg output is empty: "
            f"{output}"
        )

    return output


def target_resolutions(
    width,
    height
):

    short_side = min(
        width,
        height
    )

    targets = sorted(
        {
            target
            for target in RESOLUTIONS
            if target <= short_side * 1.05
        }
    )

    if not targets:

        targets = [
            short_side
        ]

    return targets


# ============================================================
# GEMINI
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
    "Thriller"
]


def parse_labels(text):

    if not text:
        return []

    parts = re.split(
        r"[,|\n]+",
        str(text)
    )

    labels = []

    for part in parts:

        part = part.strip()

        if not part:
            continue

        part = re.sub(
            r"^[\-\*\d\.\)\s]+",
            "",
            part
        ).strip()

        if (
            part
            and part not in labels
        ):

            labels.append(part)

    return labels


def pick_labels(labels):

    if isinstance(
        labels,
        list
    ):

        labels = ",".join(
            map(str, labels)
        )

    labels = parse_labels(
        labels
    )

    result = []

    for label in labels:

        if label.lower() == "uncategorized":
            continue

        if label not in result:
            result.append(label)

    if not result:
        result = FALLBACK_LABELS[:3]

    return result[:8]


def gemini_temporary_error(
    error
):

    text = str(error).upper()

    markers = [
        "429",
        "RESOURCE_EXHAUSTED",
        "500",
        "INTERNAL",
        "502",
        "BAD GATEWAY",
        "503",
        "UNAVAILABLE",
        "504",
        "DEADLINE",
        "TIMEOUT",
        "TIMED OUT",
        "SERVICE UNAVAILABLE"
    ]

    return any(
        marker in text
        for marker in markers
    )


def clean_gemini_json(
    text
):

    if not text:

        raise RuntimeError(
            "Gemini returned an empty response."
        )

    text = str(text).strip()

    text = re.sub(
        r"^```json\s*",
        "",
        text,
        flags=re.I
    )

    text = re.sub(
        r"^```\s*",
        "",
        text
    )

    text = re.sub(
        r"\s*```$",
        "",
        text
    )

    text = text.strip()

    start = text.find("{")
    end = text.rfind("}")

    if (
        start == -1
        or end == -1
        or end <= start
    ):

        raise RuntimeError(
            "Gemini did not return "
            "a valid JSON object."
        )

    json_text = text[
        start:end + 1
    ]

    return json.loads(
        json_text
    )


def fallback_movie_title(
    movie_name
):

    title = Path(
        movie_name
    ).stem

    # Correct regex:
    # Removes things such as:
    # (720P_HD)
    # (1080P)
    # [720P]
    # [1080P_HD]
    title = re.sub(
        r"[^)]*(?:2160P|1080P|720P|480P|HD|FHD|UHD)[^)]*",
        "",
        title,
        flags=re.I
    )

    title = re.sub(
        r"[^]*(?:2160P|1080P|720P|480P|HD|FHD|UHD)[^\]]*\]",
        "",
        title,
        flags=re.I
    )

    title = re.sub(
        r"\b(?:2160P|1080P|720P|480P|HD|FHD|UHD)\b",
        "",
        title,
        flags=re.I
    )

    title = title.replace(
        "_",
        " "
    )

    title = re.sub(
        r"\s+",
        " ",
        title
    ).strip()

    return title or "Movie"


def fmt_runtime(
    seconds
):

    seconds = int(seconds)

    hours = seconds // 3600

    minutes = (
        seconds % 3600
    ) // 60

    secs = seconds % 60

    if hours:

        return (
            f"{hours}:"
            f"{minutes:02d}:"
            f"{secs:02d}"
        )

    return (
        f"{minutes}:"
        f"{secs:02d}"
    )


def fallback_movie_metadata(
    movie_name,
    duration
):

    title = fallback_movie_title(
        movie_name
    )

    runtime = fmt_runtime(
        duration
    )

    return {
        "title": title,

        "description": (
            f"Watch {title} online and "
            "choose from the available "
            "download quality options."
        ),

        "review": (
            f"{title} is available for "
            f"online viewing and download. "
            f"Runtime: {runtime}."
        ),

        "themes": (
            "Movie, Entertainment"
        ),

        "labels": [
            "Movie",
            "Entertainment",
            "HD"
        ]
    }


def gemini_generate_with_retry(
    gemini,
    model,
    prompt
):

    last_error = None

    for attempt in range(
        1,
        GEMINI_RETRY_ATTEMPTS + 1
    ):

        try:

            log(
                "Gemini request: "
                f"model={model}, "
                f"attempt={attempt}/"
                f"{GEMINI_RETRY_ATTEMPTS}"
            )

            # Simple string contents intentionally used.
            # This avoids the previous AFC warning.
            response = (
                gemini.models.generate_content(
                    model=model,
                    contents=prompt
                )
            )

            if response is None:

                raise RuntimeError(
                    "Gemini returned an empty "
                    "response object."
                )

            response_text = getattr(
                response,
                "text",
                None
            )

            if not response_text:

                raise RuntimeError(
                    "Gemini returned a response "
                    "without text."
                )

            log(
                "Gemini request succeeded: "
                f"attempt {attempt}"
            )

            return response_text

        except Exception as error:

            last_error = error

            log(
                "Gemini request failed: "
                f"{error}"
            )

            if not gemini_temporary_error(
                error
            ):

                raise

            if (
                attempt
                >= GEMINI_RETRY_ATTEMPTS
            ):
                break

            delay = min(
                GEMINI_RETRY_BASE_DELAY
                * (2 ** (attempt - 1)),
                GEMINI_RETRY_MAX_DELAY
            )

            log(
                "Temporary Gemini error "
                "detected."
            )

            log(
                f"Waiting {delay}s "
                "before retry..."
            )

            time.sleep(delay)

    raise RuntimeError(
        "Gemini remained unavailable "
        f"after {GEMINI_RETRY_ATTEMPTS} "
        f"attempts. Last error: "
        f"{last_error}"
    )


def analyze(
    gemini,
    movie_name,
    duration
):

    prompt = f"""
You are generating metadata for a movie website.

Movie filename:
{movie_name}

Language hint:
{LANGUAGE_HINT or "English"}

Director hint:
{DIRECTOR_NAME or "Not provided"}

Duration:
{duration:.1f} seconds

Return ONLY valid JSON.

Required format:

{{
  "title": "",
  "description": "",
  "review": "",
  "themes": "",
  "labels": []
}}

Rules:

- Create a clean movie title.
- Do not include resolution such as 480p, 720p or 1080p.
- Do not invent cast information.
- Do not invent ratings.
- Do not invent unsupported facts.
- Description should be useful for a movie website.
- Review should be neutral and concise.
- Review must not contain major spoilers.
- Themes should be concise.
- Labels should be simple movie genres/topics.
- Return JSON only.
"""

    models = []

    if GEMINI_MODEL:

        models.append(
            GEMINI_MODEL
        )

    for model in GEMINI_FALLBACK_MODELS:

        if (
            model
            and model not in models
        ):

            models.append(
                model
            )

    last_error = None

    for index, model in enumerate(
        models,
        start=1
    ):

        log("=" * 70)

        log(
            "Gemini metadata model "
            f"{index}/{len(models)}: "
            f"{model}"
        )

        try:

            raw_text = (
                gemini_generate_with_retry(
                    gemini,
                    model,
                    prompt
                )
            )

            data = clean_gemini_json(
                raw_text
            )

            if not isinstance(
                data,
                dict
            ):

                raise RuntimeError(
                    "Gemini JSON response "
                    "is not an object."
                )

            title = str(
                data.get(
                    "title",
                    ""
                )
            ).strip()

            if not title:

                title = fallback_movie_title(
                    movie_name
                )

            description = str(
                data.get(
                    "description",
                    ""
                )
            ).strip()

            review = str(
                data.get(
                    "review",
                    ""
                )
            ).strip()

            themes = str(
                data.get(
                    "themes",
                    ""
                )
            ).strip()

            labels = pick_labels(
                data.get(
                    "labels",
                    []
                )
            )

            if not description:

                description = (
                    f"Watch {title} online "
                    "and choose from the "
                    "available download "
                    "quality options."
                )

            if not review:

                review = (
                    f"{title} is available "
                    "for online viewing and "
                    "download."
                )

            if not themes:

                themes = (
                    "Movie, Entertainment"
                )

            result = {
                "title": title,
                "description": description,
                "review": review,
                "themes": themes,
                "labels": labels
            }

            log(
                "Gemini metadata parsed "
                "successfully."
            )

            log(
                f"Generated title: {title}"
            )

            log(
                "Labels: "
                + ", ".join(labels)
            )

            return result

        except Exception as error:

            last_error = error

            log(
                f"Gemini model {model} failed: "
                f"{error}"
            )

            if index < len(models):

                log(
                    "Trying next Gemini "
                    f"model: {models[index]}"
                )

    # --------------------------------------------------------
    # LOCAL FALLBACK
    # --------------------------------------------------------

    log("=" * 70)

    log(
        "ALL GEMINI MODELS FAILED."
    )

    if last_error:

        log(
            f"Last Gemini error: "
            f"{last_error}"
        )

    log(
        "Using local fallback metadata."
    )

    fallback = fallback_movie_metadata(
        movie_name,
        duration
    )

    log(
        f"Fallback title: "
        f"{fallback['title']}"
    )

    return fallback


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

    return (
        f"{size:.1f} PB"
    )


def as_paragraphs(
    text
):

    if not text:
        return ""

    paragraphs = re.split(
        r"\n\s*\n",
        str(text).strip()
    )

    return "".join(
        (
            "<p>"
            + html.escape(
                paragraph.strip()
            )
            + "</p>"
        )
        for paragraph in paragraphs
        if paragraph.strip()
    )


# ============================================================
# DOWNLOAD TIMER
# ============================================================

def TIMER_SCRIPT(
    wait_seconds
):

    return f"""
<script>
(function() {{
    const WAIT = {int(wait_seconds)};

    document.addEventListener(
        "click",
        function(event) {{

            const button =
                event.target.closest(
                    "a.mv-dl[data-url]"
                );

            if (!button) {{
                return;
            }}

            event.preventDefault();

            if (
                button.dataset.busy === "1"
            ) {{
                return;
            }}

            button.dataset.busy = "1";

            let remaining = WAIT;

            button.innerHTML =
                "Please wait "
                + remaining
                + "s...";

            const timer =
                setInterval(
                    function() {{

                        remaining--;

                        if (
                            remaining <= 0
                        ) {{

                            clearInterval(timer);

                            const url =
                                button.getAttribute(
                                    "data-url"
                                );

                            if (url) {{
                                window.location.href =
                                    url;
                            }}

                            return;
                        }}

                        button.innerHTML =
                            "Please wait "
                            + remaining
                            + "s...";

                    }},
                    1000
                );

        }},
        true
    );
}})();
</script>
"""


def build_html(
    meta,
    vcdn,
    outputs,
    duration
):

    title = html.escape(
        meta.get(
            "title",
            "Movie"
        )
    )

    description = as_paragraphs(
        meta.get(
            "description",
            ""
        )
    )

    review = as_paragraphs(
        meta.get(
            "review",
            ""
        )
    )

    themes = as_paragraphs(
        meta.get(
            "themes",
            ""
        )
    )

    runtime = fmt_runtime(
        duration
    )

    labels = meta.get(
        "labels",
        []
    )

    parts = []

    parts.append(
        f"""
<div class="movie-box">

<h1>{title}</h1>

<div class="movie-runtime">
Runtime: {runtime}
</div>

<div class="movie-description">
{description}
</div>

</div>
"""
    )

    embed_url = html.escape(
        str(
            vcdn["embed"]
        ),
        quote=True
    )

    parts.append(
        f"""
<div class="watch-box">

<h2>Watch Online</h2>

<div style="
position:relative;
width:100%;
aspect-ratio:16/9;
background:#000;
overflow:hidden;
">

<iframe
src="{embed_url}"
style="
width:100%;
height:100%;
border:0;
"
allowfullscreen
allow="autoplay; fullscreen"
loading="lazy">
</iframe>

</div>

</div>
"""
    )

    if review:

        parts.append(
            f"""
<div class="review-box">

<h2>Review</h2>

{review}

</div>
"""
        )

    if themes:

        parts.append(
            f"""
<div class="themes-box">

<h2>Themes</h2>

{themes}

</div>
"""
        )

    button_style = """
display:block;
width:100%;
box-sizing:border-box;
padding:14px 18px;
margin:10px 0;
border-radius:8px;
background:#111;
color:#fff;
text-decoration:none;
text-align:center;
font-weight:700;
border:1px solid #333;
"""

    buttons = []

    for resolution, streamtape, size in outputs:

        stream_url = html.escape(
            str(
                streamtape["link"]
            ),
            quote=True
        )

        buttons.append(
            f"""
<a
class="mv-dl"
data-url="{stream_url}"
href="{stream_url}"
style="{button_style}"
rel="nofollow noopener"
>
Download {resolution}p
&nbsp;({human(size)})
</a>
"""
        )

    parts.append(
        f"""
<div class="download-box">

<h2>Download</h2>

<div>
{"".join(buttons)}
</div>

</div>
"""
    )

    if labels:

        labels_html = " ".join(
            (
                "<span>"
                + html.escape(
                    str(label)
                )
                + "</span>"
            )
            for label in labels
        )

        parts.append(
            f"""
<div class="movie-meta">

<div>
{labels_html}
</div>

</div>
"""
        )

    parts.append(
        TIMER_SCRIPT(
            WAIT_SECONDS
        )
    )

    return "\n".join(parts)


# ============================================================
# BLOGGER
# ============================================================

def verify_blogger_access(
    blogger
):

    log(
        "Checking Blogger blog access..."
    )

    try:

        blog = (
            blogger.blogs()
            .get(
                blogId=BLOG_ID
            )
            .execute()
        )

    except Exception as error:

        raise RuntimeError(
            "Cannot access Blogger blog. "
            "Check BLOG_ID and Blogger OAuth "
            f"permissions. Error: {error}"
        )

    returned_id = str(
        blog.get(
            "id",
            ""
        )
    )

    if returned_id != str(
        BLOG_ID
    ):

        raise RuntimeError(
            "Blogger returned a different "
            "blog ID. "
            f"Expected={BLOG_ID}, "
            f"Got={returned_id}"
        )

    log(
        "Blogger blog verified:"
    )

    log(
        f"Blog name: {blog.get('name')}"
    )

    log(
        f"Blog URL: {blog.get('url')}"
    )

    return blog


def create_blogger_post(
    blogger,
    title,
    content,
    labels
):

    log(
        "Creating Blogger post..."
    )

    log(
        f"Blogger Blog ID: {BLOG_ID}"
    )

    log(
        f"Publish mode: {PUBLISH}"
    )

    log(
        f"Draft mode: {not PUBLISH}"
    )

    body = {
        "kind": "blogger#post",
        "title": title,
        "content": content,
        "labels": labels
    }

    try:

        result = (
            blogger.posts()
            .insert(
                blogId=BLOG_ID,
                body=body,
                isDraft=not PUBLISH
            )
            .execute()
        )

    except Exception as error:

        log(
            "BLOGGER INSERT FAILED:"
        )

        log(
            str(error)
        )

        raise

    post_id = result.get(
        "id"
    )

    post_url = result.get(
        "url"
    )

    status = result.get(
        "status"
    )

    log(
        "BLOGGER INSERT RESPONSE:"
    )

    log(
        f"Post ID: {post_id}"
    )

    log(
        f"Post URL: {post_url}"
    )

    log(
        f"Post status: {status}"
    )

    if not post_id:

        raise RuntimeError(
            "Blogger API returned success "
            "but no Post ID."
        )

    # --------------------------------------------------------
    # VERIFY POST
    # --------------------------------------------------------

    log(
        "Verifying Blogger post..."
    )

    verified = None
    verify_error = None

    for attempt in range(1, 6):

        try:

            verified = (
                blogger.posts()
                .get(
                    blogId=BLOG_ID,
                    postId=str(post_id),
                    fetchBody=False
                )
                .execute()
            )

            break

        except Exception as error:

            verify_error = error

            log(
                "Blogger verification "
                f"attempt {attempt}/5 failed: "
                f"{error}"
            )

            if attempt < 5:

                time.sleep(3)

    if not verified:

        raise RuntimeError(
            "Blogger post was inserted but "
            "could not be verified. "
            f"Last error: {verify_error}"
        )

    verified_id = verified.get(
        "id"
    )

    if str(
        verified_id
    ) != str(
        post_id
    ):

        raise RuntimeError(
            "Blogger verification returned "
            "a different Post ID."
        )

    verified_status = verified.get(
        "status"
    )

    verified_url = verified.get(
        "url"
    )

    log(
        "BLOGGER POST VERIFIED SUCCESSFULLY."
    )

    log(
        f"Verified Post ID: {verified_id}"
    )

    log(
        f"Verified status: {verified_status}"
    )

    log(
        f"Verified URL: {verified_url}"
    )

    if PUBLISH:

        if verified_status not in {
            None,
            "LIVE"
        }:

            raise RuntimeError(
                "Publish was requested, but "
                "Blogger verification returned "
                f"status={verified_status}"
            )

    return verified


# ============================================================
# PROCESS ONE MOVIE
# ============================================================

def process_movie(
    drive,
    blogger,
    file_info,
    gemini
):

    file_id = file_info["id"]
    original_name = file_info["name"]

    log("=" * 70)

    log(
        "STARTING MOVIE:"
    )

    log(
        original_name
    )

    log(
        f"Drive file ID: {file_id}"
    )

    safe_work_name = re.sub(
        r"[^A-Za-z0-9._-]+",
        "_",
        Path(original_name).stem
    )

    movie_work = (
        WORK
        / safe_work_name
    )

    if movie_work.exists():

        shutil.rmtree(
            movie_work
        )

    movie_work.mkdir(
        parents=True,
        exist_ok=True
    )

    source_path = (
        movie_work
        / original_name
    )

    try:

        # ====================================================
        # 1. DOWNLOAD SOURCE
        # ====================================================

        download_drive_file(
            drive,
            file_id,
            source_path
        )

        # ====================================================
        # 2. PROBE
        # ====================================================

        info = probe(
            source_path
        )

        log(
            "Video information:"
        )

        log(
            f"Resolution: "
            f"{info['width']}x{info['height']}"
        )

        log(
            f"FPS: "
            f"{info['fps']}"
        )

        log(
            f"Duration: "
            f"{info['duration']:.2f}s"
        )

        # ====================================================
        # 3. TARGET RESOLUTIONS
        # ====================================================

        targets = target_resolutions(
            info["width"],
            info["height"]
        )

        log(
            "Target resolutions: "
            + ", ".join(
                f"{x}p"
                for x in targets
            )
        )

        # ====================================================
        # 4. GEMINI
        # ====================================================

        log(
            "Generating movie metadata..."
        )

        meta = analyze(
            gemini,
            original_name,
            info["duration"]
        )

        log(
            f"Generated title: "
            f"{meta['title']}"
        )

        log(
            "Labels: "
            + ", ".join(
                meta["labels"]
            )
        )

        # ====================================================
        # 5. FFMPEG
        # ====================================================

        outputs = []

        for resolution in targets:

            output_path = (
                movie_work
                / f"{Path(original_name).stem}"
                f"_{resolution}p.mp4"
            )

            log(
                f"Creating {resolution}p..."
            )

            transcode(
                source_path,
                output_path,
                resolution,
                info["fps"]
            )

            outputs.append(
                {
                    "resolution": resolution,
                    "path": output_path
                }
            )

        # ====================================================
        # 6. STREAMTAPE
        # ====================================================

        stream_outputs = []

        for item in outputs:

            resolution = item["resolution"]
            output_path = item["path"]

            log(
                f"Uploading {resolution}p "
                "to Streamtape..."
            )

            streamtape = (
                streamtape_upload_and_wait(
                    output_path
                )
            )

            stream_outputs.append(
                (
                    resolution,
                    streamtape,
                    output_path.stat().st_size
                )
            )

            log(
                f"Streamtape {resolution}p ready: "
                f"{streamtape['link']}"
            )

        # ====================================================
        # 7. VCDN - HIGHEST QUALITY ONLY
        # ====================================================

        highest = max(
            outputs,
            key=lambda x: x["resolution"]
        )

        log(
            "Highest generated resolution: "
            f"{highest['resolution']}p"
        )

        vcdn = vcdn_upload(
            highest["path"]
        )

        # ====================================================
        # 8. BLOGGER HTML
        # ====================================================

        content = build_html(
            meta,
            vcdn,
            stream_outputs,
            info["duration"]
        )

        # ====================================================
        # 9. VERIFY BLOGGER
        # ====================================================

        verify_blogger_access(
            blogger
        )

        # ====================================================
        # 10. CREATE BLOGGER POST
        # ====================================================

        blogger_post = (
            create_blogger_post(
                blogger,
                meta["title"],
                content,
                meta["labels"]
            )
        )

        # ====================================================
        # 11. FINAL VERIFICATION
        # ====================================================

        blogger_post_id = blogger_post.get(
            "id"
        )

        blogger_post_url = blogger_post.get(
            "url"
        )

        if not blogger_post_id:

            raise RuntimeError(
                "Blogger post has no ID. "
                "Drive source will NOT be deleted."
            )

        log("=" * 50)

        log(
            "BLOGGER POST VERIFIED"
        )

        log(
            f"Post ID: {blogger_post_id}"
        )

        log(
            f"Post URL: {blogger_post_url}"
        )

        log("=" * 50)

        # ====================================================
        # 12. DELETE DRIVE SOURCE
        # ====================================================

        log(
            "All upload and Blogger checks "
            "passed."
        )

        log(
            "Deleting original Drive source..."
        )

        delete_drive_file(
            drive,
            file_id
        )

        # ====================================================
        # SUCCESS
        # ====================================================

        log(
            "POST DONE"
        )

        log(
            f"Title: {meta['title']}"
        )

        log(
            f"Blogger URL: {blogger_post_url}"
        )

        log(
            f"Drive source deleted: {file_id}"
        )

        return True

    except Exception as error:

        log("=" * 70)

        log(
            "MOVIE PROCESSING FAILED"
        )

        log(
            f"Movie: {original_name}"
        )

        log(
            f"Error: {error}"
        )

        log(
            "Original Google Drive source "
            "will NOT be deleted."
        )

        log("=" * 70)

        traceback.print_exc()

        return False

    finally:

        # Only local GitHub Actions files.
        try:

            if movie_work.exists():

                shutil.rmtree(
                    movie_work,
                    ignore_errors=True
                )

                log(
                    "Local work files cleaned."
                )

        except Exception as cleanup_error:

            log(
                "Local cleanup warning: "
                f"{cleanup_error}"
            )


# ============================================================
# MAIN
# ============================================================

def main():

    log("=" * 70)

    log(
        "MOVIE BOT STARTING"
    )

    log(
        f"Publish mode: {PUBLISH}"
    )

    log(
        "Google Drive is temporary source only."
    )

    log(
        "No output will be uploaded to Drive."
    )

    log("=" * 70)

    WORK.mkdir(
        parents=True,
        exist_ok=True
    )

    try:

        # ----------------------------------------------------
        # GOOGLE
        # ----------------------------------------------------

        log(
            "Connecting to Google..."
        )

        drive, blogger = google_clients()

        # ----------------------------------------------------
        # BLOGGER
        # ----------------------------------------------------

        verify_blogger_access(
            blogger
        )

        # ----------------------------------------------------
        # GEMINI
        # ----------------------------------------------------

        log(
            "Connecting to Gemini..."
        )

        gemini = genai.Client(
            api_key=GEMINI_KEY
        )

        # ----------------------------------------------------
        # DRIVE
        # ----------------------------------------------------

        videos = list_videos(
            drive
        )

        log(
            f"Found {len(videos)} video(s)."
        )

        if not videos:

            log(
                "No videos found."
            )

            return

        videos = videos[:MAX_VIDEOS]

        successful = 0
        failed = 0

        # ----------------------------------------------------
        # PROCESS VIDEOS
        # ----------------------------------------------------

        for index, file_info in enumerate(
            videos,
            start=1
        ):

            log("=" * 70)

            log(
                f"Processing video "
                f"{index}/{len(videos)}"
            )

            success = process_movie(
                drive,
                blogger,
                file_info,
                gemini
            )

            if success:

                successful += 1

            else:

                failed += 1

        # ----------------------------------------------------
        # SUMMARY
        # ----------------------------------------------------

        log("=" * 70)

        log(
            "MOVIE BOT FINISHED"
        )

        log(
            f"Successful: {successful}"
        )

        log(
            f"Failed: {failed}"
        )

        log("=" * 70)

        # GitHub Actions should show failure
        # when a movie could not be processed.
        if failed > 0:

            sys.exit(1)

    except Exception as error:

        log("=" * 70)

        log(
            "FATAL BOT ERROR"
        )

        log(
            f"{error}"
        )

        log("=" * 70)

        traceback.print_exc()

        sys.exit(1)

    finally:

        # ----------------------------------------------------
        # GLOBAL LOCAL CLEANUP
        # ----------------------------------------------------

        try:

            if WORK.exists():

                shutil.rmtree(
                    WORK,
                    ignore_errors=True
                )

                log(
                    "Global local work directory cleaned."
                )

        except Exception as cleanup_error:

            log(
                "Global cleanup warning: "
                f"{cleanup_error}"
            )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    main()
