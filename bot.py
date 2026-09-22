"""
Movie Bot

Google Drive
    |
    v
FFmpeg
    |
    +--> Streamtape
    |      480p / 720p / 1080p
    |
    +--> VCDN
           highest quality only
    |
    v
Blogger

Google Drive is temporary source storage only.
Converted files, screenshots and thumbnails are NEVER uploaded
back to Google Drive.

Drive source is deleted only after:
    Streamtape upload
    VCDN upload
    Blogger post
    Blogger verification

all succeed.
"""

import html
import http.client
import json
import os
import re
import shutil
import subprocess
import time
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

GEMINI_MODEL = os.environ.get(
    "GEMINI_MODEL",
    "gemini-3.6-flash"
).strip()

GEMINI_FALLBACK_MODELS = [
    x.strip()
    for x in os.environ.get(
        "GEMINI_FALLBACK_MODELS",
        "gemini-2.5-flash"
    ).split(",")
    if x.strip()
]

RESOLUTIONS = sorted({
    int(x.strip())
    for x in os.environ.get(
        "RESOLUTIONS",
        "480,720,1080"
    ).split(",")
    if x.strip()
})

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
# RETRY
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

            time.sleep(delay * attempt)

    raise last_error


# ============================================================
# COMMAND
# ============================================================

def run_command(command, check=True):

    command = [
        str(x)
        for x in command
    ]

    log(
        "$ " + " ".join(command)
    )

    try:
        process = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace"
        )

    except FileNotFoundError as error:
        raise RuntimeError(
            f"Command not found: {command[0]}\n"
            f"{error}"
        )

    except Exception as error:
        raise RuntimeError(
            f"Could not execute command:\n{error}"
        )

    output = process.stdout or ""

    if output:
        print(
            output,
            flush=True
        )

    if check and process.returncode != 0:

        lines = output.strip().splitlines()

        tail = "\n".join(
            lines[-100:]
        )

        raise RuntimeError(
            "Command failed.\n"
            f"Exit code: {process.returncode}\n\n"
            "Command:\n"
            + " ".join(command)
            + "\n\n"
            "Last command output:\n"
            + tail
        )

    return output


# ============================================================
# GOOGLE
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
# DRIVE
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
        f"Downloading: {destination.name}"
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
    ) as output:

        downloader = MediaIoBaseDownload(
            output,
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

    if not destination.exists():
        raise RuntimeError(
            "Drive download did not create "
            "the local file."
        )

    if destination.stat().st_size <= 0:
        raise RuntimeError(
            "Downloaded file is empty."
        )

    log(
        f"Downloaded: "
        f"{destination.stat().st_size / 1024 / 1024:.2f} MB"
    )

    return destination


def delete_drive_file(
    drive,
    file_id
):

    log(
        "Deleting original Drive source..."
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
        "Original Drive source deleted."
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
        }
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
                    f"Streamtape HTTP {response.status}"
                )

    except urllib.error.HTTPError as error:

        body = error.read().decode(
            "utf-8",
            errors="replace"
        )

        raise RuntimeError(
            f"Streamtape HTTP {error.code}: "
            f"{body[:500]}"
        )

    except urllib.error.URLError as error:

        raise RuntimeError(
            f"Streamtape connection error: {error}"
        )

    try:

        data = json.loads(
            raw.decode("utf-8")
        )

    except Exception as error:

        raise RuntimeError(
            f"Invalid Streamtape JSON: {error}"
        )

    status = data.get("status")

    if status is not None:

        try:

            if int(status) != 200:

                raise RuntimeError(
                    f"Streamtape API error: {data}"
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
        params=params
    )

    result = streamtape_result(data)

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
    ).encode()

    footer = (
        f"\r\n--{boundary}--\r\n"
    ).encode()

    size = file_path.stat().st_size

    connection = None

    try:

        parsed = urllib.parse.urlsplit(
            upload_url
        )

        if not parsed.hostname:
            raise RuntimeError(
                "Invalid Streamtape upload URL."
            )

        connection = http.client.HTTPSConnection(
            parsed.hostname,
            parsed.port or 443,
            timeout=STREAMTAPE_WAIT_SECONDS
        )

        path = parsed.path or "/"

        if parsed.query:
            path += "?" + parsed.query

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
            str(
                len(header)
                + size
                + len(footer)
            )
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
        ) as source:

            while True:

                chunk = source.read(
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
                        f"Streamtape upload: "
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
                f"Streamtape upload HTTP "
                f"{response.status}: "
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

        if connection:
            connection.close()


def streamtape_list_folder():

    params = {}

    if STREAMTAPE_FOLDER:
        params["folder"] = STREAMTAPE_FOLDER

    data = streamtape_api(
        "/file/listfolder",
        params=params
    )

    result = streamtape_result(data)

    if isinstance(result, dict):

        return result.get(
            "files",
            []
        ) or []

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

    linkid = item.get("linkid")
    name = item.get("name")

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
            f"Streamtape list warning: {error}"
        )

        return None

    for item in files:

        if str(
            item.get("name", "")
        ) != name:
            continue

        item_size = item.get("size")

        if item_size is not None:

            try:

                if int(item_size) != int(size):
                    continue

            except Exception:
                pass

        return item

    return None


def streamtape_wait_ready(
    name,
    size
):

    deadline = (
        time.time()
        + STREAMTAPE_WAIT_SECONDS
    )

    while time.time() < deadline:

        item = streamtape_find_existing(
            name,
            size
        )

        if item:

            link = streamtape_stable_link(
                item
            )

            if link:

                log(
                    f"Streamtape ready: {name}"
                )

                return {
                    "id": streamtape_file_id(item),
                    "name": name,
                    "size": size,
                    "link": link
                }

        log(
            f"Waiting for Streamtape: {name}"
        )

        time.sleep(15)

    raise TimeoutError(
        f"Streamtape timeout: {name}"
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

        log(
            f"Existing Streamtape file: {name}"
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
        ).encode()

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
                    f"VCDN HTTP {response.status}"
                )

            return json.loads(
                raw.decode()
            )

    except urllib.error.HTTPError as error:

        body_text = error.read().decode(
            "utf-8",
            errors="replace"
        )

        raise RuntimeError(
            f"VCDN HTTP {error.code}: "
            f"{body_text[:500]}"
        )


def vcdn_value(
    data,
    *keys
):

    if not isinstance(data, dict):
        return None

    for key in keys:

        value = data.get(key)

        if value:
            return value

    return None


def vcdn_upload_binary(
    upload_url,
    upload_id,
    file_path
):

    file_path = Path(file_path)

    parsed = urllib.parse.urlsplit(
        upload_url
    )

    if not parsed.hostname:

        raise RuntimeError(
            "Invalid VCDN upload URL."
        )

    path = parsed.path or "/"

    if parsed.query:
        path += "?" + parsed.query

    size = file_path.stat().st_size

    connection = http.client.HTTPSConnection(
        parsed.hostname,
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
        ) as source:

            while True:

                chunk = source.read(
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

        if not (
            200 <= response.status < 300
        ):

            raise RuntimeError(
                f"VCDN upload HTTP "
                f"{response.status}: "
                f"{body[:500]!r}"
            )

        return body

    finally:

        connection.close()


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

    if not isinstance(data, dict):
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

    nested = data.get("data")

    if isinstance(
        nested,
        dict
    ):

        embed = (
            nested.get("embed_url")
            or nested.get("embedUrl")
            or nested.get("embed")
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
        f"Uploading highest quality to VCDN: "
        f"{file_path.name}"
    )

    # The chunked API is used directly because
    # the previous direct multipart /videos endpoint
    # returned HTTP 413 for large files.

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
                latest.get("status")
                or latest.get("state")
                or ""
            ).lower()

            nested = latest.get("data")

            if (
                not status
                and isinstance(
                    nested,
                    dict
                )
            ):

                status = str(
                    nested.get("status")
                    or nested.get("state")
                    or ""
                ).lower()

        log(
            f"VCDN status: "
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
                f"VCDN processing failed: {latest}"
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
        f"VCDN ready: {embed}"
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

            a = float(a)
            b = float(b)

            if b == 0:
                return 30.0

            return a / b

        except Exception:
            return 30.0

    try:
        return float(value)

    except Exception:
        return 30.0


def fmt_fps(value):

    if not value or value <= 0:
        return "30"

    return (
        f"{value:.3f}"
        .rstrip("0")
        .rstrip(".")
    )


def probe(path):

    output = run_command([
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-show_entries",
        "stream=codec_type,width,height,r_frame_rate",
        "-of",
        "json",
        str(path)
    ])

    data = json.loads(output)

    duration = float(
        data.get(
            "format",
            {}
        ).get(
            "duration",
            0
        ) or 0
    )

    video = None

    for stream in data.get(
        "streams",
        []
    ):

        if stream.get(
            "codec_type"
        ) == "video":

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

    if width <= 0 or height <= 0:
        raise RuntimeError(
            "Invalid video dimensions."
        )

    return {
        "duration": duration,
        "width": width,
        "height": height,
        "fps": fps
    }


def validate_mp4(path):

    path = Path(path)

    if not path.exists():
        raise RuntimeError(
            f"Output does not exist: {path}"
        )

    if path.stat().st_size <= 0:
        raise RuntimeError(
            f"Output is empty: {path}"
        )

    output = run_command([
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-show_entries",
        "stream=codec_type,width,height",
        "-of",
        "json",
        str(path)
    ])

    data = json.loads(output)

    video_found = False

    for stream in data.get(
        "streams",
        []
    ):

        if stream.get(
            "codec_type"
        ) == "video":

            video_found = True

            if not stream.get("width"):
                raise RuntimeError(
                    "Invalid output video width."
                )

            if not stream.get("height"):
                raise RuntimeError(
                    "Invalid output video height."
                )

            break

    if not video_found:
        raise RuntimeError(
            "Output contains no video stream."
        )

    duration = float(
        data.get(
            "format",
            {}
        ).get(
            "duration",
            0
        ) or 0
    )

    if duration <= 0:
        raise RuntimeError(
            "Output has invalid duration."
        )

    log(
        f"Validated MP4: {path.name} | "
        f"{path.stat().st_size / 1024 / 1024:.2f} MB | "
        f"{duration:.2f}s"
    )


def transcode(
    source,
    output,
    target_height,
    source_fps
):

    source = Path(source)
    output = Path(output)

    output.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    if not source.exists():
        raise RuntimeError(
            f"Source does not exist: {source}"
        )

    if source.stat().st_size <= 0:
        raise RuntimeError(
            "Source file is empty."
        )

    crf = CRF.get(
        target_height,
        23
    )

    fps = fmt_fps(
        source_fps
    )

    if output.exists():

        try:
            output.unlink()
        except Exception:
            pass

    normal = [
        "ffmpeg",
        "-hide_banner",
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
        fps,

        "-c:a",
        "aac",

        "-b:a",
        "128k",

        "-sn",
        "-dn",

        "-movflags",
        "+faststart",

        str(output)
    ]

    try:

        run_command(
            normal,
            check=True
        )

    except Exception as first_error:

        log(
            "Normal FFmpeg encode failed."
        )

        log(
            str(first_error)
        )

        if output.exists():

            try:
                output.unlink()
            except Exception:
                pass

        fallback = [
            "ffmpeg",
            "-hide_banner",
            "-y",
            "-i",
            str(source),

            "-map",
            "0:v:0",

            "-map",
            "0:a:0?",

            "-vf",
            f"scale=-2:{target_height}",

            "-c:v",
            "libx264",

            "-preset",
            "ultrafast",

            "-crf",
            str(crf + 1),

            "-pix_fmt",
            "yuv420p",

            "-c:a",
            "aac",

            "-b:a",
            "128k",

            "-sn",
            "-dn",

            "-movflags",
            "+faststart",

            str(output)
        ]

        try:

            run_command(
                fallback,
                check=True
            )

        except Exception as second_error:

            raise RuntimeError(
                "Both FFmpeg encodes failed.\n\n"
                f"Normal:\n{first_error}\n\n"
                f"Fallback:\n{second_error}"
            )

    validate_mp4(output)

    return output


def target_resolutions(
    width,
    height
):

    short_side = min(
        width,
        height
    )

    targets = sorted({
        target
        for target in RESOLUTIONS
        if target <= short_side * 1.05
    })

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

    result = []

    for part in parts:

        part = re.sub(
            r"^[\-\*\d\.\)\s]+",
            "",
            part.strip()
        )

        if (
            part
            and part not in result
        ):
            result.append(part)

    return result


def pick_labels(labels):

    if isinstance(
        labels,
        list
    ):
        labels = ",".join(
            map(str, labels)
        )

    labels = parse_labels(labels)

    labels = [
        x for x in labels
        if x.lower() != "uncategorized"
    ]

    if not labels:
        labels = FALLBACK_LABELS[:3]

    return labels[:8]


def gemini_temporary_error(
    error
):

    text = str(error).upper()

    return any(
        marker in text
        for marker in [
            "429",
            "RESOURCE_EXHAUSTED",
            "500",
            "502",
            "503",
            "504",
            "INTERNAL",
            "UNAVAILABLE",
            "TIMEOUT",
            "TIMED OUT",
            "SERVICE UNAVAILABLE"
        ]
    )


def clean_gemini_json(
    text
):

    if not text:
        raise RuntimeError(
            "Gemini returned empty text."
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

    start = text.find("{")
    end = text.rfind("}")

    if start < 0 or end <= start:
        raise RuntimeError(
            "Gemini did not return valid JSON."
        )

    return json.loads(
        text[start:end + 1]
    )


def fallback_movie_title(
    movie_name
):

    title = Path(
        movie_name
    ).stem

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


def fmt_runtime(seconds):

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
            "quality options."
        ),

        "review": (
            f"{title} is available for "
            f"online viewing and download. "
            f"Runtime: {runtime}."
        ),

        "themes": "Movie, Entertainment",

        "labels": [
            "Movie",
            "Entertainment",
            "HD"
        ]
    }


def gemini_generate_with_retry(
    client,
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
                f"Gemini: {model} "
                f"attempt {attempt}/"
                f"{GEMINI_RETRY_ATTEMPTS}"
            )

            response = (
                client.models.generate_content(
                    model=model,
                    contents=prompt
                )
            )

            text = getattr(
                response,
                "text",
                None
            )

            if not text:
                raise RuntimeError(
                    "Gemini returned no text."
                )

            return text

        except Exception as error:

            last_error = error

            log(
                f"Gemini error: {error}"
            )

            if not gemini_temporary_error(
                error
            ):
                raise

            if attempt >= GEMINI_RETRY_ATTEMPTS:
                break

            delay = min(
                GEMINI_RETRY_BASE_DELAY
                * (2 ** (attempt - 1)),
                GEMINI_RETRY_MAX_DELAY
            )

            log(
                f"Waiting {delay}s..."
            )

            time.sleep(delay)

    raise RuntimeError(
        "Gemini unavailable after retries: "
        f"{last_error}"
    )


def analyze(
    client,
    movie_name,
    duration
):

    prompt = f"""
You are generating metadata for a movie website.

Movie filename:
{movie_name}

Language hint:
{LANGUAGE_HINT or "English"}

Director:
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
- Remove filename junk.
- Do not include 480p, 720p, 1080p,
  2160p, HD, FHD or UHD in the title.
- Do not invent cast members.
- Do not invent a director.
- Write a natural description.
- Write a short useful review.
- Themes should be comma-separated.
- Labels should be an array.
- Do not use markdown.
- Do not put JSON inside a code block.
"""

    models = [
        GEMINI_MODEL,
        *GEMINI_FALLBACK_MODELS
    ]

    seen = set()

    for model in models:

        if not model or model in seen:
            continue

        seen.add(model)

        try:

            raw = gemini_generate_with_retry(
                client,
                model,
                prompt
            )

            data = clean_gemini_json(
                raw
            )

            title = str(
                data.get("title")
                or fallback_movie_title(movie_name)
            ).strip()

            description = str(
                data.get("description")
                or ""
            ).strip()

            review = str(
                data.get("review")
                or ""
            ).strip()

            themes = str(
                data.get("themes")
                or "Movie, Entertainment"
            ).strip()

            labels = pick_labels(
                data.get("labels", [])
            )

            return {
                "title": title,
                "description": description,
                "review": review,
                "themes": themes,
                "labels": labels
            }

        except Exception as error:

            log(
                f"Gemini model failed: "
                f"{model}: {error}"
            )

    log(
        "All Gemini models failed. "
        "Using local metadata fallback."
    )

    return fallback_movie_metadata(
        movie_name,
        duration
    )


# ============================================================
# BLOGGER HTML
# ============================================================

def quality_label(
    height
):

    return f"{height}p"


def build_download_button(
    height,
    link
):

    return f"""
<a href="{html.escape(link, quote=True)}"
   target="_blank"
   rel="nofollow noopener"
   class="movie-download-btn">
    Download {quality_label(height)}
</a>
"""


def build_post_html(
    title,
    description,
    review,
    themes,
    vcdn_embed,
    downloads
):

    download_html = ""

    for item in downloads:

        download_html += build_download_button(
            item["height"],
            item["link"]
        )

    safe_embed = html.escape(
        vcdn_embed,
        quote=True
    )

    safe_description = html.escape(
        description
    )

    safe_review = html.escape(
        review
    )

    safe_themes = html.escape(
        themes
    )

    return f"""
<div class="movie-page">

<style>
.movie-page {{
    max-width: 900px;
    margin: auto;
    font-family: Arial, sans-serif;
}}

.movie-player {{
    position: relative;
    width: 100%;
    background: #000;
    border-radius: 10px;
    overflow: hidden;
}}

.movie-player iframe {{
    display: block;
    width: 100%;
    aspect-ratio: 16 / 9;
    border: 0;
}}

.movie-info {{
    padding: 18px 0;
    line-height: 1.7;
}}

.movie-downloads {{
    display: grid;
    grid-template-columns:
        repeat(auto-fit, minmax(140px, 1fr));
    gap: 10px;
    margin-top: 20px;
}}

.movie-download-btn {{
    display: block;
    text-align: center;
    padding: 13px 10px;
    background: #111;
    color: #fff !important;
    text-decoration: none;
    border-radius: 7px;
    font-weight: 700;
}}

.movie-download-btn:hover {{
    opacity: .85;
}}

.movie-section {{
    margin-top: 20px;
}}
</style>

<div class="movie-player">
    <iframe
        src="{safe_embed}"
        allowfullscreen
        scrolling="no"
        frameborder="0">
    </iframe>
</div>

<div class="movie-info">

    <div class="movie-section">
        {safe_description}
    </div>

    <div class="movie-section">
        <strong>Review</strong>
        <p>{safe_review}</p>
    </div>

    <div class="movie-section">
        <strong>Themes:</strong>
        {safe_themes}
    </div>

    <div class="movie-section">
        <h3>Download</h3>

        <div class="movie-downloads">
            {download_html}
        </div>
    </div>

</div>

</div>
"""


# ============================================================
# BLOGGER
# ============================================================

def blogger_create_post(
    blogger,
    title,
    content,
    labels
):

    log(
        f"Creating Blogger post: {title}"
    )

    body = {
        "kind": "blogger#post",
        "title": title,
        "content": content,
        "labels": labels
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

    return result


def blogger_verify_post(
    blogger,
    post_id
):

    result = (
        blogger.posts()
        .get(
            blogId=BLOG_ID,
            postId=post_id
        )
        .execute()
    )

    if not result:
        raise RuntimeError(
            "Blogger verification returned "
            "an empty response."
        )

    if str(
        result.get("id")
    ) != str(post_id):

        raise RuntimeError(
            "Blogger post verification "
            "failed."
        )

    return result


# ============================================================
# PROCESS ONE MOVIE
# ============================================================

def process_movie(
    drive,
    blogger,
    movie
):

    file_id = movie["id"]
    original_name = movie["name"]

    log("=" * 80)
    log(
        f"PROCESSING: {original_name}"
    )
    log("=" * 80)

    WORK.mkdir(
        parents=True,
        exist_ok=True
    )

    source = WORK / original_name

    generated = []

    drive_deleted = False

    try:

        # ----------------------------------------------------
        # DOWNLOAD ORIGINAL
        # ----------------------------------------------------

        download_drive_file(
            drive,
            file_id,
            source
        )

        # ----------------------------------------------------
        # PROBE
        # ----------------------------------------------------

        info = probe(
            source
        )

        log(
            f"Source resolution: "
            f"{info['width']}x{info['height']}"
        )

        log(
            f"Source FPS: "
            f"{info['fps']:.3f}"
        )

        log(
            f"Source duration: "
            f"{info['duration']:.2f}s"
        )

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

        # ----------------------------------------------------
        # GEMINI
        # ----------------------------------------------------

        gemini = genai.Client(
            api_key=GEMINI_KEY
        )

        metadata = analyze(
            gemini,
            original_name,
            info["duration"]
        )

        title = metadata["title"]

        log(
            f"Movie title: {title}"
        )

        # ----------------------------------------------------
        # TRANSCODE
        # ----------------------------------------------------

        encoded = []

        for height in targets:

            output = (
                WORK
                / f"{Path(original_name).stem}"
                f".{height}p.mp4"
            )

            transcode(
                source,
                output,
                height,
                info["fps"]
            )

            encoded.append({
                "height": height,
                "path": output
            })

        if not encoded:

            raise RuntimeError(
                "No encoded files were generated."
            )

        # ----------------------------------------------------
        # STREAMTAPE ALL QUALITIES
        # ----------------------------------------------------

        downloads = []

        for item in encoded:

            height = item["height"]
            path = item["path"]

            log(
                f"Uploading {height}p to Streamtape..."
            )

            result = streamtape_upload_and_wait(
                path
            )

            link = result.get(
                "link"
            )

            if not link:

                raise RuntimeError(
                    f"Streamtape returned no stable "
                    f"link for {height}p."
                )

            downloads.append({
                "height": height,
                "link": link
            })

            log(
                f"Streamtape {height}p: "
                f"{link}"
            )

        # ----------------------------------------------------
        # VCDN HIGHEST QUALITY ONLY
        # ----------------------------------------------------

        highest = max(
            encoded,
            key=lambda x: x["height"]
        )

        log(
            "Highest quality selected for VCDN: "
            f"{highest['height']}p"
        )

        vcdn = vcdn_upload(
            highest["path"]
        )

        vcdn_embed = vcdn["embed"]

        if not vcdn_embed:

            raise RuntimeError(
                "VCDN did not return an embed URL."
            )

        # ----------------------------------------------------
        # BLOGGER
        # ----------------------------------------------------

        content = build_post_html(
            title=title,
            description=metadata["description"],
            review=metadata["review"],
            themes=metadata["themes"],
            vcdn_embed=vcdn_embed,
            downloads=downloads
        )

        post = blogger_create_post(
            blogger,
            title,
            content,
            metadata["labels"]
        )

        post_id = post.get("id")

        if not post_id:

            raise RuntimeError(
                "Blogger did not return post ID."
            )

        log(
            f"Blogger post created: {post_id}"
        )

        # ----------------------------------------------------
        # VERIFY BLOGGER
        # ----------------------------------------------------

        blogger_verify_post(
            blogger,
            post_id
        )

        log(
            "Blogger verification successful."
        )

        # ----------------------------------------------------
        # WAIT
        # ----------------------------------------------------

        if WAIT_SECONDS > 0:

            log(
                f"Waiting {WAIT_SECONDS}s..."
            )

            time.sleep(
                WAIT_SECONDS
            )

        # ----------------------------------------------------
        # DELETE DRIVE ORIGINAL
        # ----------------------------------------------------

        delete_drive_file(
            drive,
            file_id
        )

        drive_deleted = True

        log(
            "MOVIE COMPLETED SUCCESSFULLY."
        )

        return {
            "success": True,
            "title": title,
            "post_id": post_id,
            "drive_deleted": drive_deleted,
            "downloads": downloads,
            "vcdn": vcdn
        }

    except Exception as error:

        log("=" * 80)

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
            "Google Drive original will "
            "NOT be deleted."
        )

        raise

    finally:

        # ----------------------------------------------------
        # LOCAL CLEANUP ONLY
        # ----------------------------------------------------

        try:

            if source.exists():
                source.unlink()

        except Exception as error:

            log(
                f"Local source cleanup warning: "
                f"{error}"
            )

        for item in encoded:

            try:

                path = item["path"]

                if path.exists():
                    path.unlink()

            except Exception as error:

                log(
                    f"Local output cleanup warning: "
                    f"{error}"
                )


# ============================================================
# MAIN
# ============================================================

def main():

    log("=" * 80)

    log(
        "Movie Bot starting..."
    )

    log(
        f"Publish mode: "
        f"{'PUBLISH' if PUBLISH else 'DRAFT'}"
    )

    log(
        "Resolutions: "
        + ", ".join(
            f"{x}p"
            for x in RESOLUTIONS
        )
    )

    log("=" * 80)

    # --------------------------------------------------------
    # CLEAN LOCAL WORK DIRECTORY
    # --------------------------------------------------------

    if WORK.exists():

        try:
            shutil.rmtree(WORK)
        except Exception as error:

            log(
                f"Could not clean work directory: "
                f"{error}"
            )

    WORK.mkdir(
        parents=True,
        exist_ok=True
    )

    drive, blogger = google_clients()

    videos = list_videos(
        drive
    )

    if not videos:

        log(
            "No new video found in Drive."
        )

        return

    log(
        f"Found {len(videos)} video(s)."
    )

    processed = 0

    for movie in videos:

        if processed >= MAX_VIDEOS:
            break

        try:

            process_movie(
                drive,
                blogger,
                movie
            )

            processed += 1

        except Exception as error:

            log(
                f"Failed: {movie.get('name')}"
            )

            log(
                str(error)
            )

            # Continue to the next video if
            # MAX_VIDEOS allows it.

    log("=" * 80)

    log(
        f"Finished. Successfully processed: "
        f"{processed}"
    )

    log("=" * 80)

    # Final local cleanup.
    try:

        if WORK.exists():
            shutil.rmtree(WORK)

        log(
            "Global local work directory cleaned."
        )

    except Exception as error:

        log(
            f"Local cleanup warning: "
            f"{error}"
        )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()
