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
      Streamtape + VCDN + Blogger
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
from google.genai import types
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

GEMINI_MODEL = os.environ.get(
    "GEMINI_MODEL",
    "gemini-3.6-flash"
)

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

WORK = Path("work")

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/blogger",
]

CRF = {
    480: 24,
    720: 23,
    1080: 22,
    1440: 22,
    2160: 21,
}


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
# RETRY
# ============================================================

def retry(fn, attempts=5, delay=5):
    last_error = None

    for attempt in range(
        1,
        attempts + 1
    ):
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
# COMMAND RUNNER
# ============================================================

def run_command(
    command,
    check=True
):
    log(
        "$ "
        + " ".join(
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
    credentials = (
        google_credentials()
    )

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
        "Downloading original from "
        f"Google Drive: {destination.name}"
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

        downloader = (
            MediaIoBaseDownload(
                file_handle,
                request,
                chunksize=16 * 1024 * 1024
            )
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
        "Deleting original Google Drive "
        "source..."
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
    result = data.get(
        "result"
    )

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

    params["login"] = (
        STREAMTAPE_LOGIN
    )

    params["key"] = (
        STREAMTAPE_KEY
    )

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
            "User-Agent":
                "MovieBot/1.0"
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
                200
                <= response.status
                < 300
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
            f"{error.code}: "
            f"{body[:500]}"
        )

    except urllib.error.URLError as error:

        raise RuntimeError(
            "Streamtape API connection "
            f"error: {error}"
        )

    try:
        data = json.loads(
            raw.decode(
                "utf-8"
            )
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
        params["folder"] = (
            STREAMTAPE_FOLDER
        )

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
    file_path = Path(
        file_path
    )

    boundary = (
        "----MovieBot"
        + os.urandom(16).hex()
    )

    filename = file_path.name

    header = (
        f"--{boundary}\r\n"
        "Content-Disposition: "
        "form-data; "
        f'name="file1"; '
        f'filename="{filename}"\r\n'
        "Content-Type: video/mp4\r\n"
        "\r\n"
    ).encode(
        "utf-8"
    )

    footer = (
        f"\r\n--{boundary}--\r\n"
    ).encode(
        "utf-8"
    )

    parsed = urllib.parse.urlsplit(
        upload_url
    )

    host = parsed.hostname

    if not host:
        raise RuntimeError(
            "Invalid Streamtape "
            "upload URL."
        )

    port = (
        parsed.port
        or 443
    )

    path = (
        parsed.path
        or "/"
    )

    if parsed.query:
        path += (
            "?"
            + parsed.query
        )

    file_size = (
        file_path.stat()
        .st_size
    )

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

    connection = (
        http.client.HTTPSConnection(
            host,
            port,
            timeout=STREAMTAPE_WAIT_SECONDS
        )
    )

    try:

        connection.putrequest(
            "POST",
            path
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

        connection.send(
            header
        )

        sent = 0
        last_percent = -10

        with open(
            file_path,
            "rb"
        ) as file_handle:

            while True:

                chunk = (
                    file_handle.read(
                        16 * 1024 * 1024
                    )
                )

                if not chunk:
                    break

                connection.send(
                    chunk
                )

                sent += len(
                    chunk
                )

                percent = int(
                    sent
                    * 100
                    / file_size
                )

                if (
                    percent
                    >= last_percent + 10
                    or percent == 100
                ):
                    log(
                        "Streamtape upload: "
                        f"{percent}%"
                    )

                    last_percent = (
                        percent
                    )

        connection.send(
            footer
        )

        response = (
            connection.getresponse()
        )

        body = response.read()

        if not (
            200
            <= response.status
            < 300
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
        params["folder"] = (
            STREAMTAPE_FOLDER
        )

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

    if (
        linkid
        and name
    ):
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
        files = (
            streamtape_list_folder()
        )

    except Exception as error:

        log(
            "Could not check existing "
            f"Streamtape files: {error}"
        )

        return None

    for item in files:

        if str(
            item.get(
                "name",
                ""
            )
        ) != name:
            continue

        item_size = item.get(
            "size"
        )

        if item_size is not None:

            try:
                if int(
                    item_size
                ) != int(
                    size
                ):
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
            item.get(
                "name",
                ""
            )
        )

        if item_name != name:
            continue

        status = str(
            item.get(
                "status",
                ""
            )
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
                if float(
                    progress
                ) >= 100:
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

        item = (
            streamtape_find_existing(
                name,
                size
            )
        )

        if item:

            link = (
                streamtape_stable_link(
                    item
                )
            )

            if link:

                if not streamtape_is_converting(
                    name
                ):

                    file_id = (
                        streamtape_file_id(
                            item
                        )
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

        if (
            now - last_log
            >= 30
        ):
            log(
                "Waiting for Streamtape: "
                f"{name}"
            )

            last_log = now

        time.sleep(
            10
        )

    raise TimeoutError(
        "Streamtape did not become "
        "ready within "
        f"{STREAMTAPE_WAIT_SECONDS} "
        f"seconds: {name}"
    )


def streamtape_upload_and_wait(
    file_path
):
    file_path = Path(
        file_path
    )

    name = file_path.name

    size = (
        file_path.stat()
        .st_size
    )

    # --------------------------------------------------------
    # Check duplicate
    # --------------------------------------------------------

    existing = (
        streamtape_find_existing(
            name,
            size
        )
    )

    if existing:

        link = (
            streamtape_stable_link(
                existing
            )
        )

        if link:

            log(
                "Existing Streamtape "
                f"file found: {name}"
            )

            return (
                streamtape_wait_ready(
                    name,
                    size
                )
            )

    # --------------------------------------------------------
    # Get upload URL
    # --------------------------------------------------------

    upload_url = retry(
        streamtape_upload_url,
        attempts=4,
        delay=5
    )

    # --------------------------------------------------------
    # Upload
    #
    # Do NOT automatically retry this upload.
    # Otherwise duplicate files may be created.
    # --------------------------------------------------------

    streamtape_multipart_upload(
        upload_url,
        file_path
    )

    # --------------------------------------------------------
    # Wait until stable /v/ link exists
    # --------------------------------------------------------

    return (
        streamtape_wait_ready(
            name,
            size
        )
    )


# ============================================================
# VCDN
# ============================================================

def vcdn_headers():
    return {
        "Authorization":
            f"Bearer {VCDN_API_KEY}",
        "User-Agent":
            "MovieBot/1.0"
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
        ).encode(
            "utf-8"
        )

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
                200
                <= response.status
                < 300
            ):
                raise RuntimeError(
                    "VCDN HTTP "
                    f"{response.status}: "
                    f"{raw[:500]!r}"
                )

            return json.loads(
                raw.decode(
                    "utf-8"
                )
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
    file_path = Path(
        file_path
    )

    boundary = (
        "----MovieBotVCDN"
        + os.urandom(16).hex()
    )

    filename = file_path.name

    header = (
        f"--{boundary}\r\n"
        "Content-Disposition: "
        "form-data; "
        f'name="file"; '
        f'filename="{filename}"\r\n'
        "Content-Type: video/mp4\r\n"
        "\r\n"
    ).encode(
        "utf-8"
    )

    footer = (
        f"\r\n--{boundary}--\r\n"
    ).encode(
        "utf-8"
    )

    size = (
        file_path.stat()
        .st_size
    )

    total = (
        len(header)
        + size
        + len(footer)
    )

    connection = (
        http.client.HTTPSConnection(
            VCDN_API_HOST,
            timeout=3600
        )
    )

    try:

        connection.putrequest(
            "POST",
            "/videos"
        )

        for key, value in (
            vcdn_headers().items()
        ):
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

        connection.send(
            header
        )

        with open(
            file_path,
            "rb"
        ) as file_handle:

            while True:

                chunk = (
                    file_handle.read(
                        16 * 1024 * 1024
                    )
                )

                if not chunk:
                    break

                connection.send(
                    chunk
                )

        connection.send(
            footer
        )

        response = (
            connection.getresponse()
        )

        body = response.read()

        if not (
            200
            <= response.status
            < 300
        ):
            raise RuntimeError(
                "VCDN direct upload HTTP "
                f"{response.status}: "
                f"{body[:500]!r}"
            )

        return json.loads(
            body.decode(
                "utf-8"
            )
        )

    finally:
        connection.close()


def vcdn_upload_binary(
    upload_url,
    upload_id,
    file_path
):
    file_path = Path(
        file_path
    )

    parsed = urllib.parse.urlsplit(
        upload_url
    )

    host = parsed.hostname

    if not host:
        raise RuntimeError(
            "Invalid VCDN upload URL."
        )

    path = (
        parsed.path
        or "/"
    )

    if parsed.query:
        path += (
            "?"
            + parsed.query
        )

    size = (
        file_path.stat()
        .st_size
    )

    connection = (
        http.client.HTTPSConnection(
            host,
            parsed.port or 443,
            timeout=3600
        )
    )

    try:

        connection.putrequest(
            "PUT",
            path
        )

        for key, value in (
            vcdn_headers().items()
        ):
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

                chunk = (
                    file_handle.read(
                        16 * 1024 * 1024
                    )
                )

                if not chunk:
                    break

                connection.send(
                    chunk
                )

                sent += len(
                    chunk
                )

                percent = int(
                    sent
                    * 100
                    / size
                )

                if (
                    percent
                    >= last_percent + 10
                    or percent == 100
                ):
                    log(
                        "VCDN upload: "
                        f"{percent}%"
                    )

                    last_percent = (
                        percent
                    )

        response = (
            connection.getresponse()
        )

        body = response.read()

        if not (
            200
            <= response.status
            < 300
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
    for key in keys:

        if isinstance(
            data,
            dict
        ):
            value = data.get(
                key
            )

            if value:
                return value

    return None


def vcdn_chunked_upload(
    file_path
):
    file_path = Path(
        file_path
    )

    init = vcdn_json(
        "POST",
        "/api/v1/upload/init",
        {
            "filename":
                file_path.name,
            "filesize":
                file_path.stat().st_size
        }
    )

    data = init.get(
        "data"
    )

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

    complete_body = {
        "uploadId":
            upload_id,
        "upload_id":
            upload_id,
        "videoId":
            video_id,
        "video_id":
            video_id
    }

    vcdn_json(
        "POST",
        "/api/v1/upload/complete",
        complete_body
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
            data_section.get(
                "embed_url"
            )
            or data_section.get(
                "embedUrl"
            )
            or data_section.get(
                "embed"
            )
        )

        if embed:
            return str(embed)

    playback = data.get(
        "playback_sources"
    )

    if isinstance(
        playback,
        list
    ):

        for source in playback:

            if not isinstance(
                source,
                dict
            ):
                continue

            embed = (
                source.get(
                    "embed_url"
                )
                or source.get(
                    "embedUrl"
                )
                or source.get(
                    "url"
                )
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
    file_path = Path(
        file_path
    )

    log(
        "Uploading highest quality "
        "to VCDN: "
        f"{file_path.name}"
    )

    video_id = None

    # --------------------------------------------------------
    # First try direct upload.
    # --------------------------------------------------------

    try:

        direct = vcdn_direct_upload(
            file_path
        )

        direct_data = direct

        if isinstance(
            direct.get("result"),
            dict
        ):
            direct_data = (
                direct.get(
                    "result"
                )
            )

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
            "Reason: "
            f"{direct_error}"
        )

        log(
            "Trying VCDN chunked upload..."
        )

        video_id = (
            vcdn_chunked_upload(
                file_path
            )
        )

    # --------------------------------------------------------
    # Poll VCDN
    # --------------------------------------------------------

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
                latest.get(
                    "status",
                    ""
                )
                or latest.get(
                    "state",
                    ""
                )
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
                    nested.get(
                        "status",
                        ""
                    )
                    or nested.get(
                        "state",
                        ""
                    )
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

        time.sleep(
            10
        )

    else:
        raise TimeoutError(
            "VCDN processing timed out."
        )

    embed = (
        vcdn_extract_embed(
            latest,
            video_id
        )
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
# FFMPEG / FFPROBE
# ============================================================

def parse_fps(value):
    if not value:
        return 30.0

    value = str(
        value
    )

    if "/" in value:

        a, b = value.split(
            "/",
            1
        )

        try:
            return (
                float(a)
                / float(b)
            )

        except Exception:
            return 30.0

    try:
        return float(
            value
        )

    except Exception:
        return 30.0


def fmt_fps(value):
    return (
        f"{value:.3f}"
        .rstrip("0")
        .rstrip(".")
    )


def probe(
    path
):
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

    data = json.loads(
        output
    )

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

        if (
            stream.get(
                "codec_type"
            )
            == "video"
        ):
            video_stream = (
                stream
            )
            break

    if not video_stream:
        raise RuntimeError(
            "No video stream found."
        )

    width = int(
        video_stream.get(
            "width"
        )
        or 0
    )

    height = int(
        video_stream.get(
            "height"
        )
        or 0
    )

    fps = parse_fps(
        video_stream.get(
            "r_frame_rate"
        )
    )

    if width <= 0 or height <= 0:
        raise RuntimeError(
            "Could not determine "
            "video dimensions."
        )

    return {
        "duration":
            duration,
        "width":
            width,
        "height":
            height,
        "fps":
            fps
    }


def transcode(
    source,
    output,
    target_height,
    source_fps
):
    output = Path(
        output
    )

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
            fmt_fps(
                source_fps
            ),

            "-c:a",
            "aac",

            "-b:a",
            "128k",

            "-movflags",
            "+faststart",

            str(output)
        ]
    )

    return output


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


def parse_labels(
    text
):
    if not text:
        return []

    parts = re.split(
        r"[,|\n]+",
        text
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
            labels.append(
                part
            )

    return labels


def pick_labels(
    labels
):
    if isinstance(
        labels,
        list
    ):
        labels = ",".join(
            map(
                str,
                labels
            )
        )

    labels = parse_labels(
        labels
    )

    result = []

    for label in labels:

        if label.lower() == (
            "uncategorized"
        ):
            continue

        if label not in result:
            result.append(
                label
            )

    if not result:
        result = (
            FALLBACK_LABELS[:3]
        )

    return result[:8]


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
{LANGUAGE_HINT}

Director hint:
{DIRECTOR_NAME}

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
- Do not invent cast information.
- Do not invent ratings.
- Do not invent unsupported facts.
- Description should be useful for a movie website.
- Review should be neutral and concise.
- Themes should be concise.
- Labels should be simple genres/topics.
"""

    response = (
        gemini.models.generate_content(
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
    )

    text = (
        response.text
        or ""
    ).strip()

    # Remove markdown JSON fences if Gemini adds them.
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
    ).strip()

    match = re.search(
        r"\{.*\}",
        text,
        re.S
    )

    if not match:
        raise RuntimeError(
            "Gemini did not return "
            "valid JSON."
        )

    try:
        data = json.loads(
            match.group(0)
        )

    except json.JSONDecodeError as error:
        raise RuntimeError(
            "Gemini JSON parsing failed: "
            f"{error}"
        )

    title = str(
        data.get(
            "title",
            movie_name
        )
    ).strip()

    if not title:
        title = movie_name

    return {
        "title":
            title,

        "description":
            str(
                data.get(
                    "description",
                    ""
                )
            ).strip(),

        "review":
            str(
                data.get(
                    "review",
                    ""
                )
            ).strip(),

        "themes":
            str(
                data.get(
                    "themes",
                    ""
                )
            ).strip(),

        "labels":
            pick_labels(
                data.get(
                    "labels",
                    []
                )
            )
    }


# ============================================================
# HTML HELPERS
# ============================================================

def human(
    size
):
    size = float(
        size
    )

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


def fmt_runtime(
    seconds
):
    seconds = int(
        seconds
    )

    hours = (
        seconds
        // 3600
    )

    minutes = (
        seconds % 3600
    ) // 60

    secs = (
        seconds % 60
    )

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


def as_paragraphs(
    text
):
    if not text:
        return ""

    paragraphs = re.split(
        r"\n\s*\n",
        text.strip()
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

            const originalText =
                button.innerHTML;

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

                            clearInterval(
                                timer
                            );

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


# ============================================================
# BLOGGER HTML
# ============================================================

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

    # --------------------------------------------------------
    # MOVIE INFORMATION
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # VCDN WATCH ONLINE
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # REVIEW
    # --------------------------------------------------------

    if review:

        parts.append(
            f"""
<div class="review-box">

<h2>Review</h2>

{review}

</div>
"""
        )

    # --------------------------------------------------------
    # THEMES
    # --------------------------------------------------------

    if themes:

        parts.append(
            f"""
<div class="themes-box">

<h2>Themes</h2>

{themes}

</div>
"""
        )

    # --------------------------------------------------------
    # DOWNLOAD BUTTONS
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # LABELS
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # TIMER
    # --------------------------------------------------------

    parts.append(
        TIMER_SCRIPT(
            WAIT_SECONDS
        )
    )

    return "\n".join(
        parts
    )


# ============================================================
# BLOGGER
# ============================================================

def create_blogger_post(
    blogger,
    title,
    content,
    labels
):
    body = {
        "kind":
            "blogger#post",

        "title":
            title,

        "content":
            content,

        "labels":
            labels
    }

    result = (
        blogger.posts()
        .insert(
            blogId=BLOG_ID,
            body=body,
            isDraft=not PUBLISH
        )
        .execute()
    )

    log(
        "Blogger post created: "
        f"{result.get('id')}"
    )

    return result


# ============================================================
# RESOLUTION SELECTION
# ============================================================

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
            if target
            <= short_side * 1.05
        }
    )

    if not targets:

        targets = [
            short_side
        ]

    return targets


# ============================================================
# PROCESS ONE
