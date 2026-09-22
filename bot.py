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
- Converted videos are NOT uploaded back to Google Drive.
- Screenshots/thumbnail are kept in Drive because Blogger's existing
  image layout uses Google-hosted image URLs.
- Original Drive source is moved to _processed ONLY after:
    1) all Streamtape uploads succeed
    2) VCDN upload succeeds
    3) Blogger post succeeds
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

# ---------------- VCDN ----------------

VCDN_API_KEY = os.environ["VCDN_API_KEY"].strip()
VCDN_API_HOST = "cdn.vcdn.me"

# ---------------- Streamtape ----------------

STREAMTAPE_LOGIN = os.environ.get("STREAMTAPE_LOGIN", "").strip()
STREAMTAPE_KEY = os.environ.get("STREAMTAPE_KEY", "").strip()
STREAMTAPE_API_HOST = "api.streamtape.com"

STREAMTAPE_FOLDER = os.environ.get(
    "STREAMTAPE_FOLDER",
    ""
).strip()

STREAMTAPE_WAIT_SECONDS = int(
    os.environ.get("STREAMTAPE_WAIT_SECONDS", "20")
)

# ---------------- Gemini ----------------

GEMINI_MODEL = os.environ.get(
    "GEMINI_MODEL",
    "gemini-3.6-flash"
).strip()

GEMINI_FALLBACK_MODELS = [
    x.strip()
    for x in os.environ.get(
        "GEMINI_FALLBACK_MODELS",
        "gemini-2.5-flash,gemini-flash-latest"
    ).split(",")
    if x.strip()
]

# ---------------- Pipeline ----------------

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

def log(*args):
    print(*args, flush=True)


def retry(fn, tries=4, base_delay=5):
    last_error = None

    for attempt in range(tries):
        try:
            return fn()

        except Exception as error:
            last_error = error

            if attempt >= tries - 1:
                raise

            delay = base_delay * (attempt + 1)

            log(
                f"  retry {attempt + 1}/{tries - 1} "
                f"after error: {error}"
            )

            time.sleep(delay)

    raise last_error


def run(cmd, capture_output=False):
    """
    Run command and show useful FFmpeg error output.
    """

    log(
        "  CMD:",
        " ".join(str(x) for x in cmd)
    )

    result = subprocess.run(
        cmd,
        text=True,
        stdout=subprocess.PIPE if capture_output else None,
        stderr=subprocess.STDOUT if capture_output else None,
    )

    if result.returncode != 0:
        output = result.stdout or ""

        lines = output.splitlines()

        tail = "\n".join(lines[-100:])

        raise RuntimeError(
            f"Command failed with exit code {result.returncode}\n"
            f"Command: {' '.join(str(x) for x in cmd)}\n"
            f"Last output:\n{tail}"
        )

    return result.stdout if capture_output else ""


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
    cache_discovery=False,
)

blogger = build(
    "blogger",
    "v3",
    credentials=creds,
    cache_discovery=False,
)

gclient = genai.Client(api_key=GEMINI_KEY)


# ============================================================
# GOOGLE DRIVE
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
        fields="files(id,name)"
    ).execute()

    if res.get("files"):
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
        fields="files(id,name,size,createdTime)",
        orderBy="createdTime",
    ).execute()

    return res.get("files", [])


def download(file_id, dest):
    req = drive.files().get_media(
        fileId=file_id
    )

    with open(dest, "wb") as fh:

        dl = MediaIoBaseDownload(
            fh,
            req,
            chunksize=64 * 1024 * 1024,
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
    """
    Used ONLY for thumbnail/screenshots.
    Converted videos are NEVER uploaded here.
    """

    media = MediaFileUpload(
        path,
        mimetype=mime,
        resumable=True,
        chunksize=64 * 1024 * 1024,
    )

    req = drive.files().create(
        body={
            "name": os.path.basename(path),
            "parents": [parent],
        },
        media_body=media,
        fields="id",
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
                "role": "reader",
            },
        ).execute()
    )

    return fid


def move_to_processed(file_id, processed_folder):
    """
    Only called after the complete pipeline succeeds.
    """

    return retry(
        lambda: drive.files().update(
            fileId=file_id,
            addParents=processed_folder,
            removeParents=INPUT_FOLDER,
            fields="id,parents",
        ).execute()
    )


# ============================================================
# STREAMTAPE
# ============================================================

def streamtape_require_config():
    if not STREAMTAPE_LOGIN:
        raise RuntimeError(
            "STREAMTAPE_LOGIN is missing."
        )

    if not STREAMTAPE_KEY:
        raise RuntimeError(
            "STREAMTAPE_KEY is missing."
        )


def streamtape_api(path, params=None, method="GET"):
    streamtape_require_config()

    params = dict(params or {})

    params["login"] = STREAMTAPE_LOGIN
    params["key"] = STREAMTAPE_KEY

    query = urllib.parse.urlencode(params)

    url = (
        f"https://{STREAMTAPE_API_HOST}"
        f"{path}?{query}"
    )

    def request():
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json",
            },
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

                data = json.loads(raw)

                if data.get("status") not in (
                    None,
                    200,
                ):
                    raise RuntimeError(
                        f"Streamtape API error: {data}"
                    )

                return data

        except urllib.error.HTTPError as e:
            detail = e.read().decode(
                "utf-8",
                "replace"
            )

            raise RuntimeError(
                f"Streamtape HTTP {e.code}: {detail}"
            ) from e

        except urllib.error.URLError as e:
            raise RuntimeError(
                f"Streamtape connection error: {e}"
            ) from e

    return retry(
        request,
        tries=4,
        base_delay=5,
    )


def streamtape_upload_url():
    params = {}

    if STREAMTAPE_FOLDER:
        params["folder"] = STREAMTAPE_FOLDER

    data = streamtape_api(
        "/file/ul",
        params=params,
    )

    result = data.get("result")

    if isinstance(result, str):
        return result

    if isinstance(result, dict):
        for key in (
            "url",
            "upload_url",
            "uploadUrl",
        ):
            value = result.get(key)

            if value:
                return value

    raise RuntimeError(
        f"Streamtape upload URL missing: {data}"
    )


def streamtape_multipart_upload(
    upload_url,
    path,
):
    """
    Stream file to Streamtape without loading
    the whole movie into RAM.
    """

    filename = os.path.basename(path)

    boundary = (
        "----MovieBotStreamtape"
        + str(int(time.time()))
    )

    prefix = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; '
        f'name="file1"; filename="{filename}"\r\n'
        f"Content-Type: video/mp4\r\n\r\n"
    ).encode("utf-8")

    suffix = (
        f"\r\n--{boundary}--\r\n"
    ).encode("utf-8")

    file_size = os.path.getsize(path)

    parsed = urllib.parse.urlsplit(
        upload_url
    )

    host = parsed.netloc

    request_path = (
        parsed.path or "/"
    )

    if parsed.query:
        request_path += "?" + parsed.query

    total = (
        len(prefix)
        + file_size
        + len(suffix)
    )

    def upload():

        conn = http.client.HTTPSConnection(
            host,
            timeout=1800,
        )

        try:
            conn.putrequest(
                "POST",
                request_path,
            )

            conn.putheader(
                "Content-Type",
                f"multipart/form-data; boundary={boundary}",
            )

            conn.putheader(
                "Content-Length",
                str(total),
            )

            conn.putheader(
                "User-Agent",
                "Mozilla/5.0",
            )

            conn.endheaders()

            conn.send(prefix)

            sent = 0
            last_pct = -10

            with open(path, "rb") as fh:

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
                        pct >= last_pct + 10
                        or pct == 100
                    ):
                        log(
                            f"  Streamtape upload "
                            f"{pct}%"
                        )
                        last_pct = pct

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
                    f"Streamtape upload failed: "
                    f"HTTP {response.status}: {raw}"
                )

            try:
                return json.loads(raw)

            except json.JSONDecodeError:
                raise RuntimeError(
                    "Streamtape returned "
                    f"non-JSON response: {raw[:1000]}"
                )

        finally:
            conn.close()

    return retry(
        upload,
        tries=3,
        base_delay=10,
    )


def streamtape_extract_linkid(data):
    """
    Extract Streamtape link ID from multiple
    response formats.
    """

    def search(value):

        if isinstance(value, dict):

            for key in (
                "linkid",
                "linkId",
                "link_id",
                "id",
            ):
                if value.get(key):
                    return str(
                        value[key]
                    )

            for key in (
                "link",
                "url",
                "download",
            ):
                v = value.get(key)

                if isinstance(v, str):

                    m = re.search(
                        r"/v/([^/?#]+)",
                        v,
                        re.I,
                    )

                    if m:
                        return m.group(1)

            for v in value.values():

                found = search(v)

                if found:
                    return found

        elif isinstance(value, list):

            for item in value:

                found = search(item)

                if found:
                    return found

        elif isinstance(value, str):

            m = re.search(
                r"/v/([^/?#]+)",
                value,
                re.I,
            )

            if m:
                return m.group(1)

        return None

    return search(data)


def streamtape_stable_link(
    linkid,
    filename,
):
    clean = re.sub(
        r"[^a-zA-Z0-9._-]+",
        "-",
        Path(filename).name,
    ).strip("-")

    if not clean:
        clean = "video.mp4"

    return (
        "https://streamtape.com/v/"
        f"{urllib.parse.quote(str(linkid), safe='')}/"
        f"{urllib.parse.quote(clean, safe='._-')}"
    )


def streamtape_upload(path):
    """
    Upload one generated MP4 to Streamtape
    and return a stable /v/... URL.

    Temporary tapecontent URLs are NEVER stored.
    """

    log(
        f"Uploading {os.path.basename(path)} "
        "to Streamtape..."
    )

    if not os.path.isfile(path):
        raise RuntimeError(
            f"Streamtape source missing: {path}"
        )

    size = os.path.getsize(path)

    if size <= 0:
        raise RuntimeError(
            f"Streamtape source is empty: {path}"
        )

    upload_url = streamtape_upload_url()

    log(
        "  Streamtape upload endpoint obtained."
    )

    result = streamtape_multipart_upload(
        upload_url,
        path,
    )

    linkid = streamtape_extract_linkid(
        result
    )

    if not linkid:
        raise RuntimeError(
            "Could not extract Streamtape "
            f"linkid from upload response: {result}"
        )

    stable = streamtape_stable_link(
        linkid,
        os.path.basename(path),
    )

    log(
        "  Streamtape link:",
        stable,
    )

    return {
        "linkid": linkid,
        "url": stable,
        "size": size,
        "filename": os.path.basename(path),
    }


# ============================================================
# VCDN
# ============================================================

VCDN_USER_AGENT = (
    "Mozilla/5.0 "
    "(Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 "
    "(KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


def _vcdn_auth_headers(
    content_type=None
):
    headers = {
        "Authorization": (
            f"Bearer {VCDN_API_KEY}"
        ),
        "X-API-Key": VCDN_API_KEY,
        "Accept": "application/json",
        "User-Agent": VCDN_USER_AGENT,
    }

    if content_type:
        headers["Content-Type"] = (
            content_type
        )

    return headers


def _vcdn_json(
    method,
    path,
    payload=None,
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
                timeout=120,
            ) as resp:

                raw = resp.read().decode(
                    "utf-8",
                    "replace",
                )

                return (
                    json.loads(raw)
                    if raw
                    else {}
                )

        except urllib.error.HTTPError as e:

            detail = e.read().decode(
                "utf-8",
                "replace",
            )

            raise RuntimeError(
                f"VCDN {method} {path} "
                f"failed: HTTP {e.code}: {detail}"
            ) from e

        except urllib.error.URLError as e:

            raise RuntimeError(
                f"VCDN connection failed "
                f"for {method} {path}: {e}"
            ) from e

    return retry(
        request,
        tries=4,
        base_delay=5,
    )


def _vcdn_upload_binary(
    upload_id,
    path,
    upload_url=None,
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

    if target.startswith(
        "https://"
    ):

        parsed = urllib.parse.urlsplit(
            target
        )

        target_host = parsed.netloc

        target_path = (
            parsed.path or "/"
        )

        if parsed.query:
            target_path += (
                "?" + parsed.query
            )

    else:

        target_host = (
            VCDN_API_HOST
        )

        target_path = (
            target
            if target.startswith("/")
            else "/" + target
        )

    def upload():

        conn = http.client.HTTPSConnection(
            target_host,
            timeout=1800,
        )

        try:

            conn.putrequest(
                "POST",
                target_path,
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
                    value,
                )

            conn.endheaders()

            sent = 0
            last_log = -1

            with open(path, "rb") as fh:

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
                            sent * 100
                            / file_size
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

            response = conn.getresponse()

            raw = response.read().decode(
                "utf-8",
                "replace",
            )

            if (
                response.status < 200
                or response.status >= 300
            ):
                raise RuntimeError(
                    "VCDN binary upload failed: "
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
        tries=3,
        base_delay=10,
    )


def _multipart_header(
    boundary,
    title,
    filename,
):
    safe_name = (
        os.path.basename(filename)
        .replace('"', "'")
    )

    safe_title = (
        str(title)
        .replace('"', "'")
    )

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
    title,
):
    host = "api.vcdn.me"

    boundary = (
        "----MovieBotVCDNBoundary"
        + str(int(time.time()))
    )

    file_size = os.path.getsize(
        path
    )

    prefix, suffix = _multipart_header(
        boundary,
        title,
        path,
    )

    total_length = (
        len(prefix)
        + file_size
        + len(suffix)
    )

    def upload():

        conn = http.client.HTTPSConnection(
            host,
            timeout=1800,
        )

        try:

            conn.putrequest(
                "POST",
                "/videos",
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
                    value,
                )

            conn.endheaders()

            conn.send(prefix)

            sent = 0
            last_log = -1

            with open(path, "rb") as fh:

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
                            sent * 100
                            / file_size
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

            response = conn.getresponse()

            raw = response.read().decode(
                "utf-8",
                "replace",
            )

            if (
                response.status < 200
                or response.status >= 300
            ):
                raise RuntimeError(
                    "VCDN direct upload failed: "
                    f"HTTP {response.status}: {raw}"
                )

            try:

                data = (
                    json.loads(raw)
                    if raw
                    else {}
                )

            except json.JSONDecodeError:

                raise RuntimeError(
                    "VCDN direct upload returned "
                    f"non-JSON response: {raw[:1000]}"
                )

            video_id = (
                data.get("id")
                or data.get("video_id")
                or data.get("videoId")
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
                    f"https://embed.vcdn.me/"
                    f"{video_id}"
                )

            if not video_id and not embed_url:
                raise RuntimeError(
                    "VCDN direct upload returned "
                    f"no video id/embed_url: {data}"
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
        tries=3,
        base_delay=10,
    )


def vcdn_upload(
    path,
    title,
):
    """
    Upload highest generated resolution
    to VCDN only.
    """

    log(
        f"Uploading {os.path.basename(path)} "
        "to VCDN..."
    )

    file_size = os.path.getsize(path)

    if file_size <= 0:
        raise RuntimeError(
            f"VCDN upload file is empty: {path}"
        )

    # --------------------------------------------------------
    # First try direct REST.
    # --------------------------------------------------------

    try:

        result = _vcdn_direct_upload(
            path,
            title,
        )

        log(
            "  VCDN direct upload succeeded."
        )

        return result

    except Exception as direct_error:

        log(
            "  VCDN direct REST upload failed:",
            direct_error,
        )

    # --------------------------------------------------------
    # Chunked fallback.
    # --------------------------------------------------------

    init = _vcdn_json(
        "POST",
        "/api/v1/upload/init",
        {
            "filename": os.path.basename(
                path
            ),
            "title": title,
            "size": file_size,
        },
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
            f"upload_id/uploadId: {init}"
        )

    log(
        "  VCDN chunk upload id:",
        upload_id,
    )

    _vcdn_upload_binary(
        upload_id,
        path,
        upload_url,
    )

    complete = _vcdn_json(
        "POST",
        "/api/v1/upload/complete",
        {
            "uploadId": upload_id
        },
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
            f"no video id: {complete}"
        )

    last_video = complete

    # --------------------------------------------------------
    # Poll VCDN processing.
    # --------------------------------------------------------

    deadline = time.time() + 15 * 60

    while time.time() < deadline:

        if (
            status in (
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
                    safe="",
                ),
            )

        except Exception as poll_error:

            log(
                "  VCDN status check failed:",
                poll_error,
            )

            continue

        if info:
            last_video = info

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
            status,
        )

        if status in (
            "failed",
            "error",
        ):
            raise RuntimeError(
                "VCDN processing failed "
                f"for {video_id}: {info}"
            )

        if (
            status in (
                "ready",
                "processed",
                "complete",
                "completed",
            )
            and embed_url
        ):
            break

    if not embed_url:
        embed_url = (
            f"https://embed.vcdn.me/"
            f"{video_id}"
        )

    if not embed_url:
        raise RuntimeError(
            "VCDN did not return an embeddable "
            f"player URL: {last_video}"
        )

    log(
        "  VCDN video:",
        video_id,
    )

    log(
        "  VCDN embed:",
        embed_url,
    )

    return {
        "id": video_id,
        "embed_url": embed_url,
        "playback_url": playback_url,
        "status": status,
    }


# ============================================================
# FFMPEG
# ============================================================

def parse_fps(*vals):
    for value in vals:

        try:

            a, b = str(
                value
            ).split("/")

            a = float(a)
            b = float(b)

            if b and a / b > 0:
                return a / b

        except Exception:
            pass

    return 30.0


def fmt_fps(x):
    r = round(x)

    if abs(x - r) < 0.05:
        return str(r)

    return f"{x:.2f}"


def probe(path):
    out = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,avg_frame_rate,r_frame_rate",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            path,
        ]
    )

    data = json.loads(out)

    if not data.get("streams"):
        raise RuntimeError(
            f"No video stream found: {path}"
        )

    stream = data["streams"][0]

    fps = parse_fps(
        stream.get("avg_frame_rate"),
        stream.get("r_frame_rate"),
    )

    duration = float(
        data["format"]["duration"]
    )

    return (
        duration,
        int(stream["width"]),
        int(stream["height"]),
        fps,
    )


def validate_mp4(path):
    if not os.path.isfile(path):
        raise RuntimeError(
            f"Output file missing: {path}"
        )

    size = os.path.getsize(path)

    if size < 1024:
        raise RuntimeError(
            f"Output file is too small: "
            f"{path} ({size} bytes)"
        )

    try:

        duration, width, height, fps = probe(
            path
        )

    except Exception as error:

        raise RuntimeError(
            f"ffprobe validation failed for "
            f"{path}: {error}"
        ) from error

    if duration <= 0:
        raise RuntimeError(
            f"Invalid output duration: {path}"
        )

    if width <= 0 or height <= 0:
        raise RuntimeError(
            f"Invalid output dimensions: {path}"
        )

    log(
        f"  Validated MP4: "
        f"{width}x{height}, "
        f"{duration:.1f}s, "
        f"{fps:.2f}fps, "
        f"{human(size)}"
    )


def transcode(
    src,
    target,
    w,
    h,
    out,
    fps,
):
    """
    target = short-side resolution.

    Landscape:
        1280x720 -> 854x480 / 1280x720

    Portrait:
        720x1280 -> 480x854 / 720x1280

    Never upscale above the source.
    """

    crf = CRF.get(
        target,
        23,
    )

    if w >= h:
        vf = (
            f"scale=-2:{target}:"
            "force_original_aspect_ratio=decrease"
        )
    else:
        vf = (
            f"scale={target}:-2:"
            "force_original_aspect_ratio=decrease"
        )

    normal = [
        "ffmpeg",
        "-hide_banner",
        "-y",
        "-i",
        str(src),

        "-map",
        "0:v:0",

        "-map",
        "0:a:0?",

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

        "-r",
        fmt_fps(fps),

        "-c:a",
        "aac",

        "-b:a",
        "128k",

        "-sn",
        "-dn",

        "-movflags",
        "+faststart",

        str(out),
    ]

    fallback = [
        "ffmpeg",
        "-hide_banner",
        "-y",
        "-i",
        str(src),

        "-map",
        "0:v:0",

        "-map",
        "0:a:0?",

        "-vf",
        vf,

        "-pix_fmt",
        "yuv420p",

        "-c:v",
        "libx264",

        "-preset",
        "ultrafast",

        "-crf",
        str(crf + 1),

        "-r",
        fmt_fps(fps),

        "-c:a",
        "aac",

        "-b:a",
        "128k",

        "-sn",
        "-dn",

        "-movflags",
        "+faststart",

        str(out),
    ]

    try:

        run(
            normal,
            capture_output=True,
        )

    except Exception as first_error:

        log(
            "  Normal FFmpeg encode failed."
        )

        log(
            "  Trying fallback ultrafast encoder..."
        )

        try:

            if os.path.exists(out):
                os.remove(out)

            run(
                fallback,
                capture_output=True,
            )

        except Exception as fallback_error:

            raise RuntimeError(
                "FFmpeg failed using both "
                "normal and fallback encoders.\n\n"
                f"Normal error:\n{first_error}\n\n"
                f"Fallback error:\n{fallback_error}"
            ) from fallback_error

    validate_mp4(out)


def target_resolutions(
    width,
    height,
):
    short_side = min(
        width,
        height,
    )

    targets = sorted(
        {
            target
            for target in RESOLUTIONS
            if target <= short_side * 1.01
        }
    )

    if not targets:
        targets = [
            int(short_side)
        ]

    return targets


# ============================================================
# SCREENSHOTS / THUMBNAIL
# ============================================================

def make_screenshots(
    src,
    dur,
    outdir,
):
    files = []

    for i in range(
        SCREENSHOTS
    ):

        t = (
            dur
            * (i + 1)
            / (SCREENSHOTS + 1)
        )

        path = (
            outdir
            / f"shot_{i + 1}.jpg"
        )

        run(
            [
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
                str(path),
            ],
            capture_output=True,
        )

        files.append(path)

    return files


def make_thumbnail(
    src,
    dur,
    w,
    h,
    outdir,
):
    """
    9:16 portrait thumbnail.
    """

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

    path = (
        outdir
        / "thumb_9x16.jpg"
    )

    run(
        [
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
            str(path),
        ],
        capture_output=True,
    )

    return path


def analysis_inputs(
    src,
    dur,
    outdir,
):
    frames = []

    n = 12

    for i in range(n):

        t = (
            dur
            * (i + 1)
            / (n + 1)
        )

        path = (
            outdir
            / f"an_{i}.jpg"
        )

        run(
            [
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
                str(path),
            ],
            capture_output=True,
        )

        frames.append(
            path.read_bytes()
        )

    audio = (
        outdir
        / "an_audio.mp3"
    )

    start = dur * 0.10

    audio_duration = min(
        AUDIO_MINUTES * 60,
        max(
            1,
            dur - start,
        ),
    )

    run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-ss",
            f"{start:.2f}",
            "-t",
            str(audio_duration),
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
        ],
        capture_output=True,
    )

    return (
        frames,
        audio.read_bytes(),
    )


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
    re.I,
)


def parse_labels(page):

    found = re.findall(
        r"/search/label/([^\"'?&#<>\s/]+)",
        page,
    )

    labels = []
    seen = set()

    for item in found:

        name = (
            urllib.parse.unquote_plus(
                item
            ).strip()
        )

        if (
            name
            and name.lower() not in seen
        ):

            seen.add(
                name.lower()
            )

            labels.append(name)

    return labels


def get_site_labels():

    labels = []

    try:

        url = (
            blogger.blogs()
            .get(blogId=BLOG_ID)
            .execute()["url"]
        )

        req = urllib.request.Request(
            url,
            headers={
                "User-Agent":
                    "Mozilla/5.0"
            },
        )

        page = (
            urllib.request.urlopen(
                req,
                timeout=30,
            )
            .read()
            .decode(
                "utf-8",
                "ignore",
            )
        )

        labels = parse_labels(
            page
        )

        log(
            f"  Found {len(labels)} "
            "labels on the blog"
        )

    except Exception as error:

        log(
            "  Could not read blog labels:",
            error,
        )

    if len(labels) < 3:

        have = {
            x.lower()
            for x in labels
        }

        labels += [
            x
            for x in FALLBACK_LABELS
            if x.lower() not in have
        ]

    labels = [
        x
        for x in labels
        if not BLOCKED_LABEL.search(x)
    ]

    return labels[:60]


def pick_labels(
    raw,
    site_labels,
):
    canon = {
        x.lower(): x
        for x in site_labels
    }

    out = []

    for item in raw or []:

        value = canon.get(
            str(item)
            .strip()
            .lower()
        )

        if (
            value
            and value not in out
        ):
            out.append(value)

    return out[:4]


# ============================================================
# GEMINI
# ============================================================

YEAR_RE = re.compile(
    r"(?<!\d)(19[5-9]\d|20[0-4]\d)(?!\d)"
)


def as_paragraphs(value):

    if isinstance(
        value,
        list,
    ):
        return [
            str(x).strip()
            for x in value
            if str(x).strip()
        ]

    return [
        p.strip()
        for p in re.split(
            r"\n\s*\n",
            str(value or ""),
        )
        if p.strip()
    ]


def find_year(filename):

    match = YEAR_RE.search(
        Path(filename).stem
    )

    return (
        int(match.group(1))
        if match
        else None
    )


def clean_hint(filename):

    text = Path(
        filename
    ).stem

    text = re.sub(
        r"[#@]\S+",
        " ",
        text,
    )

    text = re.sub(
        r"[_.\-]+",
        " ",
        text,
    )

    text = re.sub(
        r"[^\w\s]",
        " ",
        text,
        flags=re.UNICODE,
    )

    text = re.sub(
        r"\s+",
        " ",
        text,
    ).strip()

    no_year = re.sub(
        r"\s+",
        " ",
        YEAR_RE.sub(
            " ",
            text,
        ),
    ).strip()

    return no_year or text


def fallback_metadata(
    filename_hint,
    site_labels,
):
    hint = clean_hint(
        filename_hint
    )

    year = find_year(
        filename_hint
    )

    title = hint or "Untitled Film"

    title = re.sub(
        r"\b(?:480p|720p|1080p|2160p)\b",
        "",
        title,
        flags=re.I,
    )

    title = re.sub(
        r"\b(?:WEB[- ]?DL|WEB[- ]?RIP|HDTS|CAM|HDRIP|X264|X265|HEVC)\b",
        "",
        title,
        flags=re.I,
    )

    title = re.sub(
        r"\s+",
        " ",
        title,
    ).strip()

    labels = []

    if LANGUAGE_HINT:

        for label in site_labels:

            if (
                LANGUAGE_HINT.lower()
                in label.lower()
            ):
                labels.append(
                    label
                )
                break

    if not labels:

        unc = next(
            (
                x
                for x in site_labels
                if x.lower()
                == "uncategorized"
            ),
            None,
        )

        if unc:
            labels = [unc]

    return {
        "title": title,
        "tagline": "",
        "synopsis": [
            "This film presents its story through "
            "its characters, atmosphere and visual style."
        ],
        "review": [
            "The available material suggests a film "
            "built around its visual atmosphere and "
            "storytelling.",
        ],
        "themes": [],
        "faq": [],
        "genres": ["Drama"],
        "language": (
            LANGUAGE_HINT
            or "Unknown"
        ),
        "release_year": year,
        "content_rating": "General audience",
        "tags": [],
        "labels": labels,
    }


def clean_gemini_json(text):

    text = (
        text
        or ""
    ).strip()

    if text.startswith(
        "```"
    ):

        text = re.sub(
            r"^```(?:json)?\s*",
            "",
            text,
            flags=re.I,
        )

        text = re.sub(
            r"\s*```$",
            "",
            text,
        )

    return text.strip()


def analyze(
    filename_hint,
    frames,
    audio_bytes,
    site_labels,
):

    hint = clean_hint(
        filename_hint
    )

    year = find_year(
        filename_hint
    )

    prompt = f"""
You are a film writer.

You are preparing an ORIGINAL film article for a movie blog.

File name hint:
"{hint}"

Possible release year from filename:
"{year or ""}"

Language hint:
"{LANGUAGE_HINT}"

Director name:
"{DIRECTOR_NAME}"

Rules:

- Write everything in natural English.
- Use your own words.
- Do not copy text from websites, reviews or other sources.
- Base the article only on what can reasonably be seen/heard.
- Do not invent actors, awards, ratings, box office or specific plot facts.
- Do not use piracy terminology.
- Do not use "free download", "leaked", "WEB-DL", "dual audio",
  "300mb", "HD print", "CAM" or similar terms.
- The title should be 1-6 words.
- Do not put a year, hashtag or emoji in the title.

Return ONLY valid JSON.

Keys:

title:
  film title

tagline:
  one sentence, maximum 20 words

synopsis:
  2 short paragraphs

review:
  3-4 paragraphs discussing visual style,
  camera work, sound, atmosphere, performances,
  themes and audience suitability

themes:
  list of 3-5 short phrases

faq:
  list of 4 objects with q and a

genres:
  list of 1-3 genres

language:
  main spoken language

content_rating:
  one of:
  "General audience"
  "Teen and above"
  "Mature audience"

tags:
  up to 6 short keywords

labels:
  choose 1-4 labels ONLY from this exact list:

{json.dumps(site_labels)}
"""

    parts = [
        types.Part.from_bytes(
            data=frame,
            mime_type="image/jpeg",
        )
        for frame in frames
    ]

    parts.append(
        types.Part.from_bytes(
            data=audio_bytes,
            mime_type="audio/mp3",
        )
    )

    models = list(
        dict.fromkeys(
            [
                GEMINI_MODEL,
                *GEMINI_FALLBACK_MODELS,
            ]
        )
    )

    data = {}

    for model in models:

        try:

            log(
                f"  Trying Gemini model: {model}"
            )

            response = retry(
                lambda: gclient.models.generate_content(
                    model=model,
                    contents=[
                        prompt,
                        *parts,
                    ],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json"
                    ),
                ),
                tries=2,
                base_delay=5,
            )

            text = clean_gemini_json(
                response.text
            )

            data = json.loads(
                text
            )

            log(
                "  Gemini model used:",
                model,
            )

            break

        except Exception as error:

            log(
                f"  Gemini model {model} failed:",
                error,
            )

    if not data:

        log(
            "  Gemini unavailable. "
            "Using local fallback metadata."
        )

        return fallback_metadata(
            filename_hint,
            site_labels,
        )

    faq = [
        item
        for item in (
            data.get("faq")
            or []
        )
        if (
            isinstance(item, dict)
            and item.get("q")
            and item.get("a")
        )
    ]

    fallback = fallback_metadata(
        filename_hint,
        site_labels,
    )

    return {
        "title": (
            data.get("title")
            or fallback["title"]
        ),

        "tagline": (
            data.get("tagline")
            or ""
        ),

        "synopsis": (
            as_paragraphs(
                data.get("synopsis")
            )
            or fallback["synopsis"]
        ),

        "review": as_paragraphs(
            data.get("review")
        ),

        "themes": [
            str(x)
            for x in (
                data.get("themes")
                or []
            )
        ][:5],

        "faq": faq[:4],

        "genres": (
            data.get("genres")
            or fallback["genres"]
        ),

        "language": (
            data.get("language")
            or fallback["language"]
        ),

        "release_year": year,

        "content_rating": (
            data.get("content_rating")
            or fallback["content_rating"]
        ),

        "tags": (
            data.get("tags")
            or []
        )[:6],

        "labels": pick_labels(
            data.get("labels"),
            site_labels,
        ),
    }


# ============================================================
# HTML
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

    minutes = sec // 60

    if minutes < 60:
        return f"{minutes} min"

    return (
        f"{minutes // 60} h "
        f"{minutes % 60} min"
    )


def img_url(fid):
    return (
        f"https://lh3.googleusercontent.com/d/{fid}"
    )


TIMER_SCRIPT = """<script>
(function () {
  var WAIT = %d;
  var btns = document.querySelectorAll('a.mv-dl[data-url]');

  for (var i = 0; i < btns.length; i++) {
    (function (b) {

      var label = b.innerHTML;
      var busy = false;

      b.addEventListener('click', function (e) {

        e.preventDefault();

        if (busy) {
          return;
        }

        busy = true;

        var left = WAIT;

        b.style.opacity = '0.85';

        b.innerHTML =
          'Please wait ' +
          left +
          ' seconds...';

        var t = setInterval(function () {

          left--;

          if (left > 0) {

            b.innerHTML =
              'Please wait ' +
              left +
              ' seconds...';

            return;
          }

          clearInterval(t);

          b.innerHTML =
            'Opening download...';

          var url =
            b.getAttribute('data-url');

          window.location.href = url;

          setTimeout(function () {

            b.innerHTML = label;
            b.style.opacity = '1';
            busy = false;

          }, 6000);

        }, 1000);

      });

    })(btns[i]);
  }
})();
</script>""" % WAIT_SECONDS


def build_html(
    meta,
    thumb_id,
    shot_ids,
    outputs,
    fps,
    dur,
    vcdn,
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

    language = meta[
        "language"
    ]

    lang_known = (
        language
        and language.lower()
        != "unknown"
    )

    lang = e(
        language
    )

    lang_tag = (
        f' <span style="color:#f2f200">'
        f'{{{lang}}}</span>'
        if lang_known
        else ""
    )

    genres = ", ".join(
        e(str(g))
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
        for _, _, size, _ in outputs
    )

    synopsis = [
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
        "background:linear-gradient("
        "90deg,#57a51c,#1f4fb4);"
        "box-shadow:0 8px 14px "
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

    if synopsis:

        parts.append(
            f"<p>{synopsis[0]}</p>"
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
        for p in synopsis
    ]

    # --------------------------------------------------------
    # VCDN PLAYER
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
        f'<iframe src="'
        f'{e(embed_url, quote=True)}" '
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
        'Adaptive streaming player powered by VCDN.'
        '</p>'
    )

    # --------------------------------------------------------
    # REVIEW
    # --------------------------------------------------------

    if review:

        parts.append(
            h3.format(
                f"{title} - Film Review and Analysis"
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

    # --------------------------------------------------------
    # DOWNLOAD LINKS
    # --------------------------------------------------------

    parts.append(hr)

    parts.append(
        h3.format(
            "Download Links"
        )
    )

    for (
        resolution,
        streamtape_url,
        size,
        _,
    ) in outputs:

        parts.append(
            f'<h4 style="{head}">'
            f'{title}{ytxt}{lang_tag} '
            f'{resolution}p x264 '
            f'{fps_txt}fps '
            f'[{human(size)}]'
            f'</h4>'
        )

        parts.append(
            f'<a class="mv-dl" '
            f'data-url="{e(streamtape_url, quote=True)}" '
            f'href="{e(streamtape_url, quote=True)}" '
            f'rel="noopener" '
            f'style="{btn}">'
            '&#11015;&#9889;'
            'DOWNLOAD NOW'
            '&#9889;&#11015;'
            '</a>'
        )

    parts.append(hr)

    # --------------------------------------------------------
    # FAQ
    # --------------------------------------------------------

    if meta["faq"]:

        parts.append(
            h3.format(
                f"{title} - FAQ"
            )
        )

        for faq in meta["faq"]:

            parts.append(
                f"<h4>{e(str(faq['q']))}</h4>"
                f"<p>{e(str(faq['a']))}</p>"
            )

    parts.append(
        '<h3 style="text-align:center;'
        'color:#f0a0ff">'
        'Winding Up &#10084;&#65039;'
        '</h3>'
    )

    parts.append(
        TIMER_SCRIPT
    )

    return "\n".join(parts)


# ============================================================
# BLOGGER
# ============================================================

def blogger_create_post(
    title,
    content,
    labels,
):
    body = {
        "kind": "blogger#post",
        "title": title,
        "content": content,
        "labels": labels,
    }

    return retry(
        lambda: blogger.posts().insert(
            blogId=BLOG_ID,
            body=body,
            isDraft=not PUBLISH,
        ).execute()
    )


def blogger_verify_post(
    post_id,
):
    return retry(
        lambda: blogger.posts().get(
            blogId=BLOG_ID,
            postId=post_id,
        ).execute()
    )


# ============================================================
# PROCESS ONE MOVIE
# ============================================================

def process(
    video,
    processed_folder,
    image_output_folder,
):
    name = video["name"]

    log(
        f"\n{'=' * 70}"
    )

    log(
        f"Processing: {name}"
    )

    log(
        f"{'=' * 70}"
    )

    job = (
        WORK
        / str(video["id"])
    )

    job.mkdir(
        parents=True,
        exist_ok=True,
    )

    src = (
        job
        / "source.mp4"
    )

    slug = re.sub(
        r"[^a-zA-Z0-9]+",
        "-",
        Path(name).stem,
    ).strip(
        "-"
    ).lower()

    if not slug:
        slug = "movie"

    # --------------------------------------------------------
    # Download original
    # --------------------------------------------------------

    log(
        "Downloading original..."
    )

    download(
        video["id"],
        src,
    )

    # --------------------------------------------------------
    # Probe
    # --------------------------------------------------------

    dur, w, h, fps = probe(
        str(src)
    )

    short = min(
        w,
        h,
    )

    log(
        f"Duration: {dur / 60:.1f} min"
    )

    log(
        f"Source: {w}x{h}"
    )

    log(
        f"FPS: {fps:.2f}"
    )

    # --------------------------------------------------------
    # Screenshots / thumbnail
    # --------------------------------------------------------

    log(
        "Making screenshots and thumbnail..."
    )

    shots = make_screenshots(
        str(src),
        dur,
        job,
    )

    thumb = make_thumbnail(
        str(src),
        dur,
        w,
        h,
        job,
    )

    # --------------------------------------------------------
    # Gemini
    # --------------------------------------------------------

    log(
        "Analysing with Gemini..."
    )

    site_labels = get_site_labels()

    frames, audio = analysis_inputs(
        str(src),
        dur,
        job,
    )

    meta = analyze(
        name,
        frames,
        audio,
        site_labels,
    )

    log(
        "  Title:",
        meta["title"],
    )

    log(
        "  Language:",
        meta["language"],
    )

    log(
        "  Labels:",
        meta["labels"],
    )

    # --------------------------------------------------------
    # Determine resolutions
    # --------------------------------------------------------

    targets = target_resolutions(
        w,
        h,
    )

    log(
        "Target resolutions:",
        ", ".join(
            f"{x}p"
            for x in targets
        ),
    )

    vcdn_target = max(
        targets
    )

    outputs = []

    vcdn = None

    # --------------------------------------------------------
    # Generate + Streamtape + VCDN
    # --------------------------------------------------------

    for resolution in targets:

        out = (
            job
            / f"{slug}_{resolution}p.mp4"
        )

        log(
            f"\nConverting to "
            f"{resolution}p..."
        )

        transcode(
            str(src),
            resolution,
            w,
            h,
            str(out),
            fps,
        )

        size = os.path.getsize(
            out
        )

        # ----------------------------------------------------
        # Streamtape
        # ----------------------------------------------------

        stream = streamtape_upload(
            str(out)
        )

        stable_url = stream[
            "url"
        ]

        outputs.append(
            (
                resolution,
                stable_url,
                size,
                stream["linkid"],
            )
        )

        # ----------------------------------------------------
        # Highest quality -> VCDN
        # ----------------------------------------------------

        if resolution == vcdn_target:

            vcdn = vcdn_upload(
                str(out),
                meta["title"],
            )

        # ----------------------------------------------------
        # Free local disk immediately
        # ----------------------------------------------------

        try:
            out.unlink()
        except FileNotFoundError:
            pass

    # --------------------------------------------------------
    # Verify VCDN
    # --------------------------------------------------------

    if (
        not vcdn
        or not vcdn.get("embed_url")
    ):
        raise RuntimeError(
            "VCDN upload did not return "
            "an embeddable player URL."
        )

    # --------------------------------------------------------
    # Verify Streamtape
    # --------------------------------------------------------

    if len(outputs) != len(targets):

        raise RuntimeError(
            "Not all generated resolutions "
            "were uploaded to Streamtape."
        )

    for item in outputs:

        if not item[1].startswith(
            "https://streamtape.com/v/"
        ):

            raise RuntimeError(
                "Invalid Streamtape stable "
                f"URL: {item[1]}"
            )

    log(
        "\nAll video uploads completed."
    )

    # --------------------------------------------------------
    # Upload thumbnail/screenshots
    #
    # These are NOT converted video files.
    # They are needed because the existing Blogger
    # HTML uses Google-hosted images.
    # --------------------------------------------------------

    log(
        "Uploading thumbnail..."
    )

    thumb_id = upload_public(
        thumb,
        image_output_folder,
        "image/jpeg",
    )

    log(
        "Uploading screenshots..."
    )

    shot_ids = [
        upload_public(
            path,
            image_output_folder,
            "image/jpeg",
        )
        for path in shots
    ]

    # --------------------------------------------------------
    # Labels
    # --------------------------------------------------------

    labels = list(
        meta["labels"]
    )

    if not labels:

        unc = next(
            (
                label
                for label in site_labels
                if label.lower()
                == "uncategorized"
            ),
            None,
        )

        if unc:
            labels = [unc]

        else:

            labels = [
                str(x)
                for x in meta["genres"][:2]
            ]

            if (
                meta["language"]
                and meta["language"].lower()
                != "unknown"
            ):
                labels.append(
                    meta["language"]
                )

    labels = [
        str(x)[:40]
        for x in labels
        if x
    ][:8]

    # --------------------------------------------------------
    # Build Blogger HTML
    # --------------------------------------------------------

    content = build_html(
        meta,
        thumb_id,
        shot_ids,
        outputs,
        fps,
        dur,
        vcdn,
    )

    year_text = (
        f" ({meta['release_year']})"
        if meta["release_year"]
        else ""
    )

    language_text = (
        f" {meta['language']}"
        if meta["language"]
        and meta["language"].lower()
        != "unknown"
        else ""
    )

    post_title = (
        f"{meta['title']}"
        f"{year_text}"
        f"{language_text}"
        " Movie - Watch Online & Download"
    )

    # --------------------------------------------------------
    # Blogger insert
    # --------------------------------------------------------

    log(
        "\nCreating Blogger post..."
    )

    post = blogger_create_post(
        post_title,
        content,
        labels,
    )

    post_id = post.get(
        "id"
    )

    if not post_id:
        raise RuntimeError(
            f"Blogger returned no post ID: {post}"
        )

    # --------------------------------------------------------
    # Verify Blogger post
    # --------------------------------------------------------

    log(
        "Verifying Blogger post..."
    )

    verified = blogger_verify_post(
        post_id
    )

    verified_content = (
        verified.get("content")
        or ""
    )

    if not verified_content:
        raise RuntimeError(
            "Blogger post verification failed: "
            "post has no content."
        )

    if (
        "streamtape.com/v/"
        not in verified_content
    ):
        raise RuntimeError(
            "Blogger verification failed: "
            "Streamtape download links "
            "were not found in the saved post."
        )

    if (
        vcdn["embed_url"]
        not in verified_content
    ):
        raise RuntimeError(
            "Blogger verification failed: "
            "VCDN embed URL was not found "
            "in the saved post."
        )

    log(
        "Blogger post verified successfully."
    )

    log(
        "Post URL:",
        verified.get("url")
        or post.get("url")
        or post_id,
    )

    if PUBLISH:
        log(
            "Status: PUBLISHED"
        )
    else:
        log(
            "Status: DRAFT"
        )

    # --------------------------------------------------------
    # ONLY NOW move original Drive source
    # --------------------------------------------------------

    log(
        "\nAll required steps succeeded."
    )

    log(
        "Moving original source to _processed..."
    )

    move_to_processed(
        video["id"],
        processed_folder,
    )

    log(
        "Original Drive source moved successfully."
    )

    # --------------------------------------------------------
    # Local cleanup
    # --------------------------------------------------------

    shutil.rmtree(
        job,
        ignore_errors=True,
    )

    log(
        f"\nSUCCESS: {name}"
    )


# ============================================================
# MAIN
# ============================================================

def main():

    WORK.mkdir(
        exist_ok=True
    )

    log(
        "Checking Google Drive input folder..."
    )

    videos = list_videos()

    if not videos:

        log(
            "No new videos in the input folder."
        )

        return 0

    log(
        f"Found {len(videos)} video(s)."
    )

    # --------------------------------------------------------
    # IMPORTANT:
    # _output is no longer used for videos.
    #
    # We still use it for the existing Blogger
    # thumbnail/screenshot system.
    # --------------------------------------------------------

    processed_folder = ensure_folder(
        "_processed"
    )

    image_output_folder = ensure_folder(
        "_output"
    )

    failed = 0

    for video in videos[:MAX_VIDEOS]:

        try:

            process(
                video,
                processed_folder,
                image_output_folder,
            )

        except Exception as error:

            failed += 1

            log(
                "\n"
                + "=" * 70
            )

            log(
                "FAILED:",
                video.get("name"),
            )

            log(
                str(error)
            )

            log(
                "=" * 70
                + "\n"
            )

            traceback.print_exc()

            # ------------------------------------------------
            # IMPORTANT:
            # Original Drive source stays untouched.
            # It remains in INPUT_FOLDER so the next run
            # can retry it.
            # ------------------------------------------------

            try:

                job = (
                    WORK
                    / str(video["id"])
                )

                if job.exists():

                    shutil.rmtree(
                        job,
                        ignore_errors=True,
                    )

            except Exception as cleanup_error:

                log(
                    "Local cleanup warning:",
                    cleanup_error,
                )

    return (
        1
        if failed
        else 0
    )


if __name__ == "__main__":

    try:

        sys.exit(
            main()
        )

    finally:

        try:

            if WORK.exists():

                shutil.rmtree(
                    WORK,
                    ignore_errors=True,
                )

            log(
                "Global local work directory cleaned."
            )

        except Exception as cleanup_error:

            log(
                "Global cleanup warning:",
                cleanup_error,
            )
