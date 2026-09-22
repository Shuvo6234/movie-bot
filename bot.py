"""
Movie Bot:
Google Drive -> screenshots + 9:16 thumbnail
-> FFmpeg multi-resolution
-> VCDN Watch Online
-> Google Drive download files
-> Gemini title/description/labels
-> Blogger post

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

GEMINI_MODEL = os.environ.get(
    "GEMINI_MODEL",
    "gemini-3.6-flash"
).strip()

RESOLUTIONS = sorted({
    int(x.strip())
    for x in os.environ.get(
        "RESOLUTIONS",
        "480,720,1080"
    ).split(",")
    if x.strip().isdigit()
})

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
    os.environ.get(
        "AUDIO_MINUTES",
        "10"
    )
)

SCREENSHOTS = int(
    os.environ.get(
        "SCREENSHOTS",
        "6"
    )
)

WAIT_SECONDS = int(
    os.environ.get(
        "WAIT_SECONDS",
        "20"
    )
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


def retry(fn, tries=4, delay=5):
    last_error = None

    for i in range(tries):
        try:
            return fn()

        except Exception as e:
            last_error = e

            if i == tries - 1:
                raise

            wait = delay * (i + 1)

            log(
                f"  retry {i + 1}/{tries - 1} "
                f"after error: {e}"
            )

            time.sleep(wait)

    raise last_error


def run(cmd, capture_output=False):
    """
    Run command.

    When FFmpeg fails, print the last part of stderr so the
    actual FFmpeg error is visible in GitHub Actions.
    """

    log(
        "  Running:",
        " ".join(str(x) for x in cmd)
    )

    result = subprocess.run(
        cmd,
        text=True,
        stdout=subprocess.PIPE if capture_output else None,
        stderr=subprocess.PIPE if capture_output else None,
    )

    if result.returncode != 0:
        if capture_output:
            stderr = result.stderr or ""
            stdout = result.stdout or ""

            if stderr:
                log("----- COMMAND STDERR -----")
                log(stderr[-12000:])

            if stdout:
                log("----- COMMAND STDOUT -----")
                log(stdout[-4000:])

        raise RuntimeError(
            f"Command failed with exit code "
            f"{result.returncode}: "
            f"{' '.join(str(x) for x in cmd)}"
        )

    return result


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
        fields="files(id,name,size)",
        orderBy="createdTime"
    ).execute()

    return res.get("files", [])


def download(file_id, dest):

    log("  Downloading to:", dest)

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

    log(
        "  Uploading to Google Drive:",
        os.path.basename(path)
    )

    media = MediaFileUpload(
        path,
        mimetype=mime,
        resumable=True,
        chunksize=64 * 1024 * 1024
    )

    req = drive.files().create(
        body={
            "name": os.path.basename(path),
            "parents": [parent],
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


# ============================================================
# VCDN
# ============================================================

VCDN_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
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
                f"VCDN {method} {path} "
                f"failed: HTTP {e.code}: "
                f"{detail}"
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

    file_size = os.path.getsize(path)

    target = (
        upload_url
        or
        f"https://{VCDN_API_HOST}"
        f"/api/v1/upload/{upload_id}/chunk"
    )

    if target.startswith("https://"):

        from urllib.parse import urlsplit

        parsed = urlsplit(target)

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

            resp = conn.getresponse()

            raw = resp.read().decode(
                "utf-8",
                "replace"
            )

            if not (
                200 <= resp.status < 300
            ):
                raise RuntimeError(
                    "VCDN binary upload "
                    f"failed: HTTP "
                    f"{resp.status}: {raw}"
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
    filename
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
        f'Content-Disposition: '
        f'form-data; name="title"\r\n\r\n'
        f"{safe_title}\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: '
        f'form-data; name="file"; '
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

    file_size = os.path.getsize(path)

    prefix, suffix = _multipart_header(
        boundary,
        title,
        path
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
                "multipart/form-data; "
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
                            f"  VCDN direct "
                            f"upload {pct}%"
                        )

                        last_log = pct

            conn.send(suffix)

            resp = conn.getresponse()

            raw = resp.read().decode(
                "utf-8",
                "replace"
            )

            if not (
                200 <= resp.status < 300
            ):

                raise RuntimeError(
                    "VCDN direct upload "
                    f"failed: HTTP "
                    f"{resp.status}: {raw}"
                )

            try:

                data = (
                    json.loads(raw)
                    if raw
                    else {}
                )

            except json.JSONDecodeError:

                raise RuntimeError(
                    "VCDN direct upload "
                    "returned non-JSON response: "
                    f"{raw[:1000]}"
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
                    "VCDN direct upload "
                    "returned no video id/"
                    f"embed_url: {data}"
                )

            return {
                "id": video_id,
                "embed_url": embed_url,
                "playback_url": playback_url,
                "status": data.get("status"),
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
        f"Uploading "
        f"{os.path.basename(path)} "
        "to VCDN..."
    )

    file_size = os.path.getsize(path)

    if file_size <= 0:

        raise RuntimeError(
            f"VCDN upload file is empty: "
            f"{path}"
        )

    try:

        log(
            f"  VCDN file size: "
            f"{file_size} bytes"
        )

        return _vcdn_direct_upload(
            path,
            title
        )

    except Exception as direct_error:

        log(
            "  VCDN direct REST "
            f"upload failed: {direct_error}"
        )

        try:

            init = _vcdn_json(
                "POST",
                "/api/v1/upload/init",
                {
                    "filename": os.path.basename(path),
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
                    f"upload_id/uploadId: {init}"
                )

            log(
                f"  VCDN chunk upload id: "
                f"{upload_id}"
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
                    f"no video id: {complete}"
                )

            last_video = complete

            if (
                status
                not in (
                    "ready",
                    "processed"
                )
                or not embed_url
            ):

                deadline = (
                    time.time()
                    + 10 * 60
                )

                while time.time() < deadline:

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
                            "  VCDN status "
                            f"check failed: "
                            f"{poll_error}"
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
                        "  VCDN processing "
                        f"status: {status}"
                    )

                    if status in (
                        "ready",
                        "processed",
                        "complete",
                        "completed"
                    ):
                        break

                    if status in (
                        "failed",
                        "error"
                    ):

                        raise RuntimeError(
                            "VCDN processing "
                            f"failed for "
                            f"{video_id}: {info}"
                        )

            if not embed_url:

                embed_url = (
                    "https://embed.vcdn.me/"
                    f"{video_id}"
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
# FFMPEG
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

    if abs(x - r) < 0.05:
        return str(r)

    return f"{x:.2f}"


def probe(path):

    out = subprocess.check_output([
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
    ])

    data = json.loads(out)

    if not data.get("streams"):
        raise RuntimeError(
            f"No video stream found: {path}"
        )

    stream = data["streams"][0]

    fps = parse_fps(
        stream.get("avg_frame_rate"),
        stream.get("r_frame_rate")
    )

    return (
        float(data["format"]["duration"]),
        int(stream["width"]),
        int(stream["height"]),
        fps,
    )


def validate_mp4(path):

    if not os.path.exists(path):
        raise RuntimeError(
            f"FFmpeg output does not exist: "
            f"{path}"
        )

    size = os.path.getsize(path)

    if size <= 0:
        raise RuntimeError(
            f"FFmpeg created empty file: "
            f"{path}"
        )

    try:

        duration, width, height, fps = probe(
            path
        )

    except Exception as e:

        raise RuntimeError(
            f"Invalid MP4 output "
            f"{path}: {e}"
        ) from e

    log(
        f"  Output validated: "
        f"{width}x{height}, "
        f"{fps:.2f}fps, "
        f"{duration:.1f}s, "
        f"{size} bytes"
    )


def make_even(n):

    n = int(n)

    if n % 2:
        n -= 1

    return max(2, n)


def calculate_target_size(
    target,
    w,
    h
):
    """
    target = SHORT side.

    Example:
    1280x720 -> 852x480
    1920x1080 -> 852x480
    720x1280 -> 480x852

    Both dimensions are forced to even numbers.
    """

    if w >= h:

        target_h = make_even(target)

        target_w = make_even(
            round(
                w
                * target_h
                / h
            )
        )

    else:

        target_w = make_even(target)

        target_h = make_even(
            round(
                h
                * target_w
                / w
            )
        )

    return target_w, target_h


def transcode(
    src,
    target,
    w,
    h,
    out
):
    """
    Convert to target short-side resolution.

    Critical fix:
    H.264 requires even dimensions.
    """

    target_w, target_h = calculate_target_size(
        target,
        w,
        h
    )

    crf = CRF.get(
        target,
        23
    )

    log(
        f"  Target resolution: "
        f"{target_w}x{target_h}"
    )

    vf = (
        f"scale={target_w}:"
        f"{target_h}:"
        "flags=lanczos"
    )

    normal_command = [
        "ffmpeg",
        "-hide_banner",
        "-y",
        "-i",
        src,

        "-map",
        "0:v:0",

        "-map",
        "0:a:0?",

        "-vf",
        vf,

        "-c:v",
        "libx264",

        "-preset",
        "veryfast",

        "-crf",
        str(crf),

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

        out,
    ]

    try:

        run(
            normal_command,
            capture_output=True
        )

        validate_mp4(out)

        return

    except Exception as first_error:

        log(
            "  Normal FFmpeg encode "
            f"failed: {first_error}"
        )

        if os.path.exists(out):
            try:
                os.remove(out)
            except Exception:
                pass

    # Fallback encode
    log(
        "  Trying fallback "
        "ultrafast encode..."
    )

    fallback_command = [
        "ffmpeg",
        "-hide_banner",
        "-y",
        "-i",
        src,

        "-map",
        "0:v:0",

        "-map",
        "0:a:0?",

        "-vf",
        vf,

        "-c:v",
        "libx264",

        "-preset",
        "ultrafast",

        "-crf",
        str(
            min(
                crf + 2,
                30
            )
        ),

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

        out,
    ]

    try:

        run(
            fallback_command,
            capture_output=True
        )

        validate_mp4(out)

    except Exception as fallback_error:

        if os.path.exists(out):
            try:
                os.remove(out)
            except Exception:
                pass

        raise RuntimeError(
            "FFmpeg conversion failed.\n"
            f"Normal encode: {first_error}\n"
            f"Fallback encode: {fallback_error}"
        ) from fallback_error


def make_screenshots(
    src,
    dur,
    outdir
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

    """
    9:16 portrait thumbnail.
    """

    if w * 16 >= h * 9:

        ch = make_even(
            h
        )

        cw = make_even(
            int(h * 9 / 16)
        )

    else:

        cw = make_even(
            w
        )

        ch = make_even(
            int(w * 16 / 9)
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

    audio_duration = min(
        AUDIO_MINUTES * 60,
        max(
            1,
            dur - start
        )
    )

    run([
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
    ])

    return (
        frames,
        audio.read_bytes()
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
    re.I
)


def parse_labels(page):

    found = re.findall(
        r"/search/label/"
        r"([^\"'?&#<>\s/]+)",
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
            and name.lower()
            not in seen
        ):

            seen.add(
                name.lower()
            )

            labels.append(name)

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
            "  Could not read "
            f"blog labels: {e}"
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
    site_labels
):

    canon = {
        x.lower(): x
        for x in site_labels
    }

    out = []

    for r in raw or []:

        c = canon.get(
            str(r).strip().lower()
        )

        if c and c not in out:
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
    r"(?<!\d)"
    r"(19[5-9]\d|20[0-4]\d)"
    r"(?!\d)"
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

    return (
        no_year
        or t
    )


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
You are a film writer.

You are publishing an ORIGINAL film on its own film blog.

You receive:
- 12 frames spread across the film
- an audio sample
- a messy filename hint

Filename hint:
"{hint}"

Language hint:
"{LANGUAGE_HINT}"

Director name:
"{DIRECTOR_NAME}"

Rules:

- Write everything in natural English.
- Never copy text from websites, reviews, films or other sources.
- Base your description only on what you can actually see and hear.
- If uncertain, stay general.
- Never invent cast, crew, awards, festivals, ratings, box office or
  specific plot facts.
- Do not use piracy-related wording.
- Do not mention leaked, HD print, free download full movie, WEB-DL,
  dual audio, 300mb, etc.
- The title must be 1-6 words.
- No hashtags.
- No emojis.
- No year in title.
- Do not use words like trending reels.

Return ONLY valid JSON.

Keys:

title:
realistic film title, 1-6 words

tagline:
one sentence, maximum 20 words

synopsis:
2 short paragraphs, about 120 words total,
spoiler-light, separated by a blank line

review:
3-4 paragraphs, about 300 words total,
analysing tone, visual style, camera work,
sound, music, performances in general terms,
themes and who may enjoy the film

themes:
list of 3-5 short phrases

faq:
list of 4 objects:
{{"q":"...","a":"..."}}

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
list of up to 6 short keywords

labels:
pick 1-4 categories ONLY from this exact list:

{json.dumps(site_labels)}

Judge labels by:
- language
- film industry/country
- type
- movie/web series/trailer/song

Ignore encoding and file-format labels.
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

    models = list(dict.fromkeys([
        GEMINI_MODEL,
        "gemini-flash-latest"
    ]))

    for model in models:

        try:

            resp = retry(
                lambda: (
                    gclient
                    .models
                    .generate_content(
                        model=model,
                        contents=[
                            prompt,
                            *parts
                        ],
                        config=(
                            types
                            .GenerateContentConfig(
                                response_mime_type=
                                "application/json"
                            )
                        )
                    )
                ),
                tries=2
            )

            raw = (
                resp.text
                .strip()
            )

            raw = re.sub(
                r"^```json",
                "",
                raw,
                flags=re.I
            )

            raw = re.sub(
                r"```$",
                "",
                raw
            ).strip()

            data = json.loads(
                raw
            )

            log(
                "  Gemini model used:",
                model
            )

            break

        except Exception as e:

            log(
                f"  Gemini model "
                f"{model} failed: {e}"
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

    title = (
        data.get("title")
        or hint
        or "Untitled Film"
    )

    return {
        "title": str(title).strip(),

        "tagline": str(
            data.get("tagline")
            or ""
        ).strip(),

        "synopsis": (
            as_paragraphs(
                data.get("synopsis")
            )
            or [
                "An original film."
            ]
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

        "genres": [
            str(x)
            for x in (
                data.get("genres")
                or ["Drama"]
            )
        ][:3],

        "language": (
            str(
                data.get("language")
                or LANGUAGE_HINT
                or "Unknown"
            )
        ),

        "release_year": year,

        "content_rating": (
            data.get(
                "content_rating"
            )
            or "General audience"
        ),

        "tags": [
            str(x)
            for x in (
                data.get("tags")
                or []
            )
        ][:6],

        "labels": pick_labels(
            data.get("labels"),
            site_labels
        ),
    }


# ============================================================
# POST HTML
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
        "https://lh3.googleusercontent.com/d/"
        f"{fid}"
    )


TIMER_SCRIPT = """
<script>
(function () {
  var WAIT = %d;

  var btns =
    document.querySelectorAll(
      'a.mv-dl[data-fid]'
    );

  for (
    var i = 0;
    i < btns.length;
    i++
  ) {

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
                'Download starting...';

              window.location.href =
                'https://drive.usercontent.google.com/download?id='
                + b.getAttribute('data-fid')
                + '&export=download&confirm=t';

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
        and meta["language"]
        .lower()
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
        human(s)
        for _, _, s in outputs
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
        "90deg,#57a51c,#1f4fb4"
        ");"
        "box-shadow:"
        "0 8px 14px rgba(0,0,0,.45);"
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
            f"<b>Release Year:</b> "
            f"{year}"
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
                f' - {lang} film'
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

    if syn:

        parts.append(
            f"<p>{syn[0]}</p>"
        )

    parts.append(
        h3.format("Movie Info")
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

    # ---------------- WATCH ----------------

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

        f'<iframe '
        f'src="{e(embed_url, quote=True)}" '
        'width="100%" '
        'height="420" '
        'frameborder="0" '
        'allow="autoplay; '
        'encrypted-media; '
        'picture-in-picture" '
        'allowfullscreen="true" '
        'style="border:0;'
        'display:block">'
        '</iframe>'

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

    # ---------------- REVIEW ----------------

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

    # ---------------- THEMES ----------------

    if meta["themes"]:

        parts.append(
            h3.format("Themes")
        )

        parts.append(
            "<ul>"
            + "".join(
                f"<li>{e(t)}</li>"
                for t in meta["themes"]
            )
            + "</ul>"
        )

    # ---------------- SCREENSHOTS ----------------

    parts.append(
        h3.format("Screenshots")
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

    parts.append(hr)

    # ---------------- DOWNLOAD ----------------

    parts.append(
        h3.format("Download Links")
    )

    for h, fid, size in outputs:

        direct = (
            "https://drive.usercontent.google.com/"
            f"download?id={fid}"
            "&amp;export=download"
            "&amp;confirm=t"
        )

        parts.append(
            f'<h4 style="{head}">'
            f'{title}{ytxt}'
            f'{lang_tag} '
            f'{h}p x264 '
            f'{fps_txt}fps '
            f'[{human(size)}]'
            '</h4>'
        )

        parts.append(
            f'<a class="mv-dl" '
            f'data-fid="{fid}" '
            f'href="{direct}" '
            'rel="noopener" '
            f'style="{btn}">'
            '&#11015;'
            '&#9889;'
            'DOWNLOAD NOW'
            '&#9889;'
            '&#11015;'
            '</a>'
        )

    parts.append(hr)

    # ---------------- FAQ ----------------

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
# TARGET RESOLUTIONS
# ============================================================

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
        if target <= short_side
    })

    if not targets:

        # Never upscale.
        # If source is below 480, use the original
        # short side rounded down to an even number.
        original = make_even(
            short_side
        )

        targets = [
            original
        ]

    return targets


# ============================================================
# PROCESS ONE MOVIE
# ============================================================

def process(
    video,
    processed_folder,
    output_folder
):

    name = video["name"]

    log(
        f"\n=== Processing: {name} ==="
    )

    job = (
        WORK
        / video["id"]
    )

    job.mkdir(
        parents=True,
        exist_ok=True
    )

    src = str(
        job
        / "source.mp4"
    )

    slug = re.sub(
        r"[^a-zA-Z0-9]+",
        "-",
        Path(name).stem
    ).strip("-").lower()

    if not slug:
        slug = "movie"

    # ---------------- DOWNLOAD ----------------

    log(
        "Downloading original..."
    )

    download(
        video["id"],
        src
    )

    # ---------------- PROBE ----------------

    dur, w, h, fps = probe(
        src
    )

    short = min(
        w,
        h
    )

    log(
        f"Duration "
        f"{dur / 60:.1f} min, "
        f"{w}x{h}, "
        f"{fps:.2f} fps"
    )

    # ---------------- IMAGES ----------------

    log(
        "Making screenshots "
        "and thumbnail..."
    )

    shots = make_screenshots(
        src,
        dur,
        job
    )

    thumb = make_thumbnail(
        src,
        dur,
        w,
        h,
        job
    )

    # ---------------- GEMINI ----------------

    log(
        "Analysing with Gemini..."
    )

    site_labels = (
        get_site_labels()
    )

    frames, audio = (
        analysis_inputs(
            src,
            dur,
            job
        )
    )

    meta = analyze(
        name,
        frames,
        audio,
        site_labels
    )

    log(
        "  Title:",
        meta["title"]
    )

    log(
        "  Labels:",
        meta["labels"]
    )

    # ---------------- RESOLUTIONS ----------------

    targets = target_resolutions(
        w,
        h
    )

    log(
        "Generated resolutions:",
        ", ".join(
            f"{x}p"
            for x in targets
        )
    )

    outputs = []

    vcdn = None

    # Highest generated resolution
    # goes to VCDN.
    vcdn_target = max(
        targets
    )

    # ---------------- TRANSCODE ----------------

    for target in targets:

        out = str(
            job
            / f"{slug}_{target}p.mp4"
        )

        log(
            f"\nConverting to "
            f"{target}p..."
        )

        transcode(
            src,
            target,
            w,
            h,
            out
        )

        size = os.path.getsize(
            out
        )

        log(
            f"  Created "
            f"{target}p: "
            f"{human(size)}"
        )

        # ---------------- DRIVE UPLOAD ----------------

        log(
            f"Uploading {target}p "
            "to Google Drive..."
        )

        fid = upload_public(
            out,
            output_folder,
            "video/mp4"
        )

        outputs.append(
            (
                target,
                fid,
                size
            )
        )

        # ---------------- VCDN ----------------

        if target == vcdn_target:

            log(
                f"Uploading highest "
                f"resolution ({target}p) "
                "to VCDN..."
            )

            vcdn = vcdn_upload(
                out,
                meta["title"]
            )

        # Free local disk
        try:
            os.remove(out)
        except Exception:
            pass

    # ---------------- VCDN VALIDATION ----------------

    if (
        not vcdn
        or not vcdn.get("embed_url")
    ):

        raise RuntimeError(
            "VCDN upload did not return "
            "an embeddable player URL."
        )

    # ---------------- IMAGES TO DRIVE ----------------

    log(
        "Uploading images..."
    )

    thumb_id = upload_public(
        thumb,
        output_folder,
        "image/jpeg"
    )

    shot_ids = [
        upload_public(
            p,
            output_folder,
            "image/jpeg"
        )
        for p in shots
    ]

    # ---------------- LABELS ----------------

    labels = list(
        meta["labels"]
    )

    if not labels:

        unc = next(
            (
                l
                for l in site_labels
                if l.lower()
                == "uncategorized"
            ),
            None
        )

        if unc:

            labels = [
                unc
            ]

        else:

            labels = (
                [
                    g
                    for g in meta["genres"]
                ][:2]
                +
                [
                    meta["language"]
                ]
            )

    labels = [
        str(l)[:40]
        for l in labels
        if l
    ][:8]

    # ---------------- BLOGGER HTML ----------------

    content = build_html(
        meta,
        thumb_id,
        shot_ids,
        outputs,
        fps,
        dur,
        vcdn
    )

    ytxt = (
        f' ({meta["release_year"]})'
        if meta["release_year"]
        else ""
    )

    ltxt = (
        f' {meta["language"]}'
        if meta["language"].lower()
        != "unknown"
        else ""
    )

    body = {
        "kind":
            "blogger#post",

        "title":
            f'{meta["title"]}'
            f'{ytxt}'
            f'{ltxt} '
            "Movie - Watch Online "
            "& Download",

        "content":
            content,

        "labels":
            labels,
    }

    # ---------------- BLOGGER CREATE ----------------

    log(
        "\nCreating Blogger post..."
    )

    post = retry(
        lambda:
            blogger.posts().insert(
                blogId=BLOG_ID,
                body=body,
                isDraft=not PUBLISH
            ).execute()
    )

    post_id = post.get(
        "id"
    )

    post_url = post.get(
        "url"
    )

    log(
        "Blogger post created:"
    )

    log(
        "  ID:",
        post_id
    )

    log(
        "  URL:",
        post_url
        or "not returned"
    )

    log(
        "  Mode:",
        "PUBLISHED"
        if PUBLISH
        else "DRAFT"
    )

    # ---------------- VERIFY BLOGGER ----------------

    if post_id:

        try:

            verified = retry(
                lambda:
                    blogger.posts().get(
                        blogId=BLOG_ID,
                        postId=post_id
                    ).execute()
            )

            if not verified.get("id"):

                raise RuntimeError(
                    "Blogger post verification "
                    "failed."
                )

            log(
                "Blogger post verified."
            )

        except Exception as verify_error:

            raise RuntimeError(
                "Blogger post was created "
                "but verification failed: "
                f"{verify_error}"
            ) from verify_error

    # ========================================================
    # ONLY NOW MOVE ORIGINAL DRIVE VIDEO TO PROCESSED
    # ========================================================

    log(
        "All processing completed."
    )

    log(
        "Moving original Drive video "
        "to _processed..."
    )

    drive.files().update(
        fileId=video["id"],
        addParents=processed_folder,
        removeParents=INPUT_FOLDER,
        fields="id,parents"
    ).execute()

    log(
        "Original Drive video moved "
        "to _processed."
    )

    # ---------------- LOCAL CLEANUP ----------------

    shutil.rmtree(
        job,
        ignore_errors=True
    )

    log(
        "Local job cleaned."
    )


# ============================================================
# MAIN
# ============================================================

def main():

    WORK.mkdir(
        exist_ok=True
    )

    videos = list_videos()

    if not videos:

        log(
            "No new videos in "
            "the input folder. "
            "Nothing to do."
        )

        return 0

    processed = ensure_folder(
        "_processed"
    )

    output = ensure_folder(
        "_output"
    )

    failed = 0

    for video in videos[:MAX_VIDEOS]:

        try:

            process(
                video,
                processed,
                output
            )

        except Exception as error:

            failed += 1

            log(
                "\n================================"
            )

            log(
                "MOVIE PROCESSING FAILED"
            )

            log(
                "================================"
            )

            log(
                f"Error: {error}"
            )

            traceback.print_exc()

            log(
                "Original Drive source "
                "was NOT moved to _processed."
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
                    WORK
                )

            log(
                "Global local work "
                "directory cleaned."
            )

        except Exception as cleanup_error:

            log(
                "Local cleanup warning: "
                f"{cleanup_error}"
            )
