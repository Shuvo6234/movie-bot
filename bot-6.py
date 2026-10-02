"""
Movie Bot: Drive video -> multi-resolution -> VCDN Watch Online
+ Internet Archive downloads (unlisted) + screenshots + 2:3 poster thumbnail
-> Gemini title/description/labels -> Blogger post (draft by default).
Runs on GitHub Actions. All settings come from environment variables.

Screenshots and the poster thumbnail are exported as high-quality WebP.
Converted movie files are uploaded to Internet Archive (noindex) and the
Download buttons in the post link directly to archive.org/download/...
"""
import hashlib
import difflib
import base64
import html
import http.client
import json
import os
import re
import secrets
import shlex
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

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from internetarchive import upload as ia_upload

# ---------- settings ----------
# Drive now authenticates via a service account (never expires).
# Blogger still needs a normal user OAuth refresh token, since Blogger
# does not support service-account access to a personal blog.
SERVICE_ACCOUNT_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
CLIENT_ID = os.environ["GOOGLE_CLIENT_ID"]
CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]
REFRESH_TOKEN = os.environ["GOOGLE_REFRESH_TOKEN"]
GEMINI_KEY = os.environ["GEMINI_API_KEY"]
BLOG_ID = os.environ["BLOG_ID"]
INPUT_FOLDER = os.environ["DRIVE_INPUT_FOLDER_ID"]

# VCDN API key must be stored in GitHub Actions Secrets as VCDN_API_KEY.
VCDN_API_KEY = os.environ["VCDN_API_KEY"].strip()
VCDN_API_HOST = "cdn.vcdn.me"

# Internet Archive S3-style keys: https://archive.org/account/s3.php
# Store them in GitHub Actions Secrets as IA_ACCESS and IA_SECRET.
IA_ACCESS = os.environ["IA_ACCESS"].strip()
IA_SECRET = os.environ["IA_SECRET"].strip()
# File extension of the converted download files. Use "mkv" if browsers
# start playing the mp4 instead of downloading it.
IA_FILE_EXT = os.environ.get("IA_FILE_EXT", "mp4").strip(". ").lower() or "mp4"

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
RESOLUTIONS = [int(x) for x in os.environ.get("RESOLUTIONS", "480,720,1080").split(",") if x.strip()]
MAX_VIDEOS = int(os.environ.get("MAX_VIDEOS", "1"))
PUBLISH = os.environ.get("PUBLISH", "false").lower() == "true"
LANGUAGE_HINT = os.environ.get("LANGUAGE_HINT", "").strip()
AUDIO_MINUTES = int(os.environ.get("AUDIO_MINUTES", "10"))
SCREENSHOTS = int(os.environ.get("SCREENSHOTS", "6"))
WAIT_SECONDS = int(os.environ.get("WAIT_SECONDS", "20"))
DIRECTOR_NAME = os.environ.get("DIRECTOR_NAME", "").strip()
# Optional manual title. When set, the same title is used for the Blogger post,
# Movie Info -> Movie Name, thumbnail search, and synopsis context.
MANUAL_TITLE = next((
    os.environ.get(k, "").strip()
    for k in ("MANUAL_TITLE", "MOVIE_TITLE", "CUSTOM_TITLE")
    if os.environ.get(k, "").strip()
), "")
CRF = {480: 24, 720: 23, 1080: 22, 1440: 22, 2160: 21}

# ---------- image quality (WebP) ----------
# Screenshots and the poster thumbnail are exported as WebP.
# Quality 0-100 (higher = sharper, bigger file). compression_level 0-6
# (6 = slowest, best compression for the same quality).
SHOT_WEBP_QUALITY = int(os.environ.get("SHOT_WEBP_QUALITY", "95"))
THUMB_WEBP_QUALITY = int(os.environ.get("THUMB_WEBP_QUALITY", "97"))
WEBP_COMPRESSION = int(os.environ.get("WEBP_COMPRESSION", "6"))
# Poster thumbnail size (exact 2:3).
THUMB_W = int(os.environ.get("THUMB_W", "800"))
THUMB_H = THUMB_W * 3 // 2

# ---------- TMDB (optional) ----------
# If neither value is set, the bot skips TMDB and works exactly as before.
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "").strip()          # v3 key
TMDB_READ_TOKEN = os.environ.get("TMDB_READ_TOKEN", "").strip()    # v4 read token

# ---------- OMDb (optional) ----------
# Gives the real IMDb rating + basic facts. Free key: omdbapi.com/apikey.aspx
OMDB_API_KEY = os.environ.get("OMDB_API_KEY", "").strip()
# Optional manual IMDb id (e.g. tt0111161). If valid, it is used to match the
# exact movie on TMDB and OMDb instead of searching by name.
IMDB_ID = os.environ.get("IMDB_ID", "").strip()

# ---------- local AI fallback ----------
# Gemini remains the primary AI. Qwen3-VL is started ONLY when a Gemini
# task has failed completely. The local model runs through llama.cpp so no
# external AI API/file upload is needed for the fallback.
LOCAL_AI_ENABLED = os.environ.get("LOCAL_AI_ENABLED", "true").lower() == "true"
LOCAL_QWEN_MODEL = os.environ.get(
    "LOCAL_QWEN_MODEL", "Qwen/Qwen3-VL-4B-Instruct-GGUF:Q4_K_M"
)
LOCAL_AI_TIMEOUT = int(os.environ.get("LOCAL_AI_TIMEOUT", "900"))
LOCAL_AI_CONTEXT = int(os.environ.get("LOCAL_AI_CONTEXT", "8192"))
LOCAL_AI_THREADS = int(os.environ.get("LOCAL_AI_THREADS", "4"))
LOCAL_AI_MAX_IMAGES = int(os.environ.get("LOCAL_AI_MAX_IMAGES", "12"))


WORK = Path("work")
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]
USER_SCOPES = [
    "https://www.googleapis.com/auth/blogger",
    "https://www.googleapis.com/auth/drive.file",
]


def log(*a):
    print(*a, flush=True)


def retry(fn, tries=4):
    for i in range(tries):
        try:
            return fn()
        except Exception as e:  # noqa
            if i == tries - 1:
                raise
            log(f"  retry {i + 1} after error: {e}")
            time.sleep(5 * (i + 1))


def run(cmd):
    subprocess.run(cmd, check=True)


# ---------- Google clients ----------
# Drive (read side): service account credentials. These never expire on
# their own. Used for listing/downloading input videos, creating small
# folders, and moving files between folders -- none of this consumes
# storage quota, so the service account's lack of Drive storage is fine.
sa_info = json.loads(SERVICE_ACCOUNT_JSON)
drive_creds = service_account.Credentials.from_service_account_info(
    sa_info, scopes=DRIVE_SCOPES
)
drive = build("drive", "v3", credentials=drive_creds, cache_discovery=False)

# User OAuth (write side + Blogger): normal user refresh token, scoped to
# Blogger and drive.file. Service accounts have zero Drive storage quota,
# so NEW files (thumbnails, screenshots) must be created under the real
# Google account, which owns actual storage.
# drive.file is a "sensitive" (not "restricted") scope, so it doesn't
# block publishing the OAuth consent screen the way full "drive" does.
user_creds = Credentials(
    None,
    refresh_token=REFRESH_TOKEN,
    token_uri="https://oauth2.googleapis.com/token",
    client_id=CLIENT_ID,
    client_secret=CLIENT_SECRET,
    scopes=USER_SCOPES,
)
user_creds.refresh(Request())
blogger = build("blogger", "v3", credentials=user_creds, cache_discovery=False)
drive_write = build("drive", "v3", credentials=user_creds, cache_discovery=False)


# ---------- Drive helpers ----------
def ensure_folder(name):
    q = (f"'{INPUT_FOLDER}' in parents and name='{name}' and "
         "mimeType='application/vnd.google-apps.folder' and trashed=false")
    res = drive.files().list(q=q, fields="files(id)").execute()
    if res["files"]:
        return res["files"][0]["id"]
    body = {"name": name, "mimeType": "application/vnd.google-apps.folder",
            "parents": [INPUT_FOLDER]}
    return drive.files().create(body=body, fields="id").execute()["id"]


def list_videos():
    q = f"'{INPUT_FOLDER}' in parents and mimeType contains 'video/' and trashed=false"
    res = drive.files().list(q=q, fields="files(id,name,size)", orderBy="createdTime").execute()
    return res["files"]


def download(file_id, dest):
    req = drive.files().get_media(fileId=file_id)
    with open(dest, "wb") as fh:
        dl = MediaIoBaseDownload(fh, req, chunksize=64 * 1024 * 1024)
        done = False
        while not done:
            status, done = retry(dl.next_chunk)
            if status:
                log(f"  download {int(status.progress() * 100)}%")


def upload_public(path, parent, mime):
    media = MediaFileUpload(path, mimetype=mime, resumable=True, chunksize=64 * 1024 * 1024)
    req = drive_write.files().create(
        body={"name": os.path.basename(path), "parents": [parent]},
        media_body=media, fields="id")
    resp = None
    while resp is None:
        _, resp = retry(req.next_chunk)
    fid = resp["id"]
    retry(lambda: drive_write.permissions().create(
        fileId=fid, body={"type": "anyone", "role": "reader"}).execute())
    return fid


# ---------- Internet Archive helpers ----------
def make_ia_item_id(slug):
    """Unguessable item id, e.g. my-movie-3fa91c27b0d4 (one item per movie)."""
    base = re.sub(r"[^a-z0-9-]+", "-", str(slug).lower()).strip("-") or "movie"
    return f"{base[:60].strip('-') or 'movie'}-{secrets.token_hex(6)}"


def ia_upload_file(path, item_id, title):
    """
    Upload one file to an Internet Archive item and return its permanent
    direct download URL. The item is marked noindex so it does not show up
    in archive.org search/browse (only people with the link can find it).
    """
    def do_upload():
        return ia_upload(
            item_id,
            files=[path],
            metadata={
                "mediatype": "data",   # no video player / derivatives
                "title": str(title)[:200],
                "noindex": "true",
            },
            access_key=IA_ACCESS,
            secret_key=IA_SECRET,
            queue_derive=False,
            retries=8,
            retries_sleep=30,
            verbose=True,
        )

    res = retry(do_upload, tries=2)
    if not res or not all(r.status_code == 200 for r in res):
        raise RuntimeError(f"Internet Archive upload failed: {res}")

    url = ("https://archive.org/download/" + item_id + "/"
           + urllib.parse.quote(os.path.basename(path)))
    log("  Internet Archive link:", url)
    return url


# ---------- VCDN helpers ----------
VCDN_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
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


def _vcdn_json(method, path, payload=None):
    """Call VCDN's documented JSON API with both supported auth headers."""
    body = None
    headers = _vcdn_auth_headers()
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    def request():
        req = urllib.request.Request(
            f"https://{VCDN_API_HOST}{path}",
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                raw = resp.read().decode("utf-8", "replace")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            raise RuntimeError(
                f"VCDN {method} {path} failed: HTTP {e.code}: {detail}"
            ) from e
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"VCDN connection failed for {method} {path}: {e}"
            ) from e

    return retry(request, tries=4)


def _vcdn_upload_binary(upload_id, path, upload_url=None):
    """
    Upload the video bytes to VCDN without loading the entire movie into RAM.
    If the init response provides an uploadUrl, use that exact URL.
    """
    file_size = os.path.getsize(path)
    target = upload_url or f"https://{VCDN_API_HOST}/api/v1/upload/{upload_id}/chunk"

    if target.startswith("https://"):
        from urllib.parse import urlsplit
        parsed = urlsplit(target)
        target_host = parsed.netloc
        target_path = parsed.path or "/"
        if parsed.query:
            target_path += "?" + parsed.query
    else:
        target_host = VCDN_API_HOST
        target_path = target if target.startswith("/") else "/" + target

    def upload():
        conn = http.client.HTTPSConnection(target_host, timeout=1800)
        try:
            conn.putrequest("POST", target_path)
            headers = _vcdn_auth_headers("application/octet-stream")
            headers["Content-Length"] = str(file_size)
            for key, value in headers.items():
                conn.putheader(key, value)
            conn.endheaders()

            sent = 0
            last_log = -1
            with open(path, "rb") as fh:
                while True:
                    chunk = fh.read(16 * 1024 * 1024)
                    if not chunk:
                        break
                    conn.send(chunk)
                    sent += len(chunk)
                    pct = int(sent * 100 / file_size) if file_size else 100
                    if pct >= last_log + 10 or pct == 100:
                        log(f"  VCDN upload {pct}%")
                        last_log = pct

            resp = conn.getresponse()
            raw = resp.read().decode("utf-8", "replace")
            if resp.status < 200 or resp.status >= 300:
                raise RuntimeError(
                    f"VCDN binary upload failed: HTTP {resp.status}: {raw}"
                )
            try:
                return json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                return {"raw": raw}
        finally:
            conn.close()

    return retry(upload, tries=3)


def _multipart_header(boundary, title, filename, file_size):
    """Build a deterministic multipart/form-data prefix and suffix."""
    safe_name = os.path.basename(filename).replace('"', "'")
    safe_title = str(title).replace('"', "'")
    prefix = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="title"\r\n\r\n'
        f"{safe_title}\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{safe_name}"\r\n'
        f"Content-Type: video/mp4\r\n\r\n"
    ).encode("utf-8")
    suffix = f"\r\n--{boundary}--\r\n".encode("utf-8")
    return prefix, suffix


def _vcdn_direct_upload(path, title):
    """
    Fallback upload route exposed on VCDN's main site:
      POST https://api.vcdn.me/videos
    Uses multipart/form-data and streams the file so the whole video is
    never loaded into memory.
    """
    host = "api.vcdn.me"
    boundary = "----MovieBotVCDNBoundary7MA4YWxkTrZu0gW"
    file_size = os.path.getsize(path)
    prefix, suffix = _multipart_header(boundary, title, path, file_size)
    total_length = len(prefix) + file_size + len(suffix)

    def upload():
        conn = http.client.HTTPSConnection(host, timeout=1800)
        try:
            conn.putrequest("POST", "/videos")
            headers = _vcdn_auth_headers(
                f"multipart/form-data; boundary={boundary}"
            )
            headers["Content-Length"] = str(total_length)
            for key, value in headers.items():
                conn.putheader(key, value)
            conn.endheaders()

            conn.send(prefix)
            sent = 0
            last_log = -1
            with open(path, "rb") as fh:
                while True:
                    chunk = fh.read(16 * 1024 * 1024)
                    if not chunk:
                        break
                    conn.send(chunk)
                    sent += len(chunk)
                    pct = int(sent * 100 / file_size) if file_size else 100
                    if pct >= last_log + 10 or pct == 100:
                        log(f"  VCDN direct upload {pct}%")
                        last_log = pct
            conn.send(suffix)

            resp = conn.getresponse()
            raw = resp.read().decode("utf-8", "replace")
            if resp.status < 200 or resp.status >= 300:
                raise RuntimeError(
                    f"VCDN direct upload failed: HTTP {resp.status}: {raw}"
                )
            try:
                data = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                raise RuntimeError(
                    f"VCDN direct upload returned non-JSON response: {raw[:1000]}"
                )

            video_id = data.get("id") or data.get("video_id")
            embed_url = data.get("embed_url") or data.get("embedUrl")
            playback_url = data.get("playback_url") or data.get("playbackUrl")
            if not embed_url and video_id:
                embed_url = f"https://embed.vcdn.me/{video_id}"
            if not video_id and not embed_url:
                raise RuntimeError(
                    f"VCDN direct upload returned no video id/embed_url: {data}"
                )

            log("  VCDN video:", video_id or "unknown")
            log("  VCDN embed:", embed_url or "unknown")
            if playback_url:
                log("  VCDN HLS:", playback_url)

            return {
                "id": video_id,
                "embed_url": embed_url,
                "playback_url": playback_url,
                "status": data.get("status"),
            }
        finally:
            conn.close()

    return retry(upload, tries=3)


def vcdn_upload(path, title):
    """
    Upload a video to VCDN using the documented API:
      POST /api/v1/upload/init      (with ladderProfile for multi-resolution)
      POST /api/v1/upload/{id}/chunk
      POST /api/v1/upload/complete
      GET  /api/v1/videos/{id}      (poll until ready)
    """
    log(f"Uploading {os.path.basename(path)} to VCDN...")

    file_size = os.path.getsize(path)
    if file_size <= 0:
        raise RuntimeError(f"VCDN upload file is empty: {path}")

    log(f"  VCDN file size: {file_size} bytes")

    init = _vcdn_json(
        "POST",
        "/api/v1/upload/init",
        {
            "filename": os.path.basename(path),
            "title": title,
            "size": file_size,
            "contentType": "video/mp4",

        },
    )

    upload_id = init.get("upload_id") or init.get("uploadId")
    upload_url = init.get("upload_url") or init.get("uploadUrl")
    if not upload_id:
        raise RuntimeError(f"VCDN init did not return upload_id/uploadId: {init}")

    log(f"  VCDN upload id: {upload_id}")
    if upload_url:
        log(f"  VCDN upload URL: {upload_url}")
    _vcdn_upload_binary(upload_id, path, upload_url)

    complete = _vcdn_json("POST", "/api/v1/upload/complete", {"uploadId": upload_id})

    video_id = (
        complete.get("id")
        or complete.get("video_id")
        or complete.get("videoId")
        or upload_id
    )
    status = complete.get("status")
    embed_url = complete.get("embed_url") or complete.get("embedUrl")
    playback_url = complete.get("playback_url") or complete.get("playbackUrl")

    deadline = time.time() + 15 * 60
    last_video = complete
    while time.time() < deadline:
        if status == "ready" and embed_url:
            break
        if status in ("failed", "error"):
            raise RuntimeError(f"VCDN processing failed for {video_id}: {last_video}")
        time.sleep(5)
        try:
            info = _vcdn_json(
                "GET",
                f"/api/v1/videos/{urllib.parse.quote(str(video_id), safe='')}"
            )
        except Exception as poll_error:
            log(f"  VCDN status check failed: {poll_error}")
            continue
        last_video = info or last_video
        status = info.get("status") or status
        embed_url = info.get("embed_url") or info.get("embedUrl") or embed_url
        playback_url = info.get("playback_url") or info.get("playbackUrl") or playback_url
        progress = info.get("transcode_progress")
        log(f"  VCDN status: {status} progress: {progress}")

    if not embed_url:
        embed_url = f"https://embed.vcdn.me/embed/{video_id}"

    if not video_id or not embed_url:
        raise RuntimeError(f"VCDN returned no usable player data: {last_video}")

    log("  VCDN video:", video_id)
    log("  VCDN embed:", embed_url)
    if playback_url:
        log("  VCDN HLS:", playback_url)
    log("  VCDN final status:", status or "unknown")

    return {
        "id": video_id,
        "embed_url": embed_url,
        "playback_url": playback_url,
        "status": status,
    }


# ---------- ffmpeg helpers ----------
def parse_fps(*vals):
    for v in vals:
        try:
            a, b = str(v).split("/")
            a, b = float(a), float(b)
            if b and a / b > 0:
                return a / b
        except Exception:  # noqa
            pass
    return 30.0


def fmt_fps(x):
    r = round(x)
    return str(r) if abs(x - r) < 0.05 else f"{x:.2f}"


def probe(path):
    out = subprocess.check_output([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,avg_frame_rate,r_frame_rate:format=duration",
        "-of", "json", path])
    d = json.loads(out)
    st = d["streams"][0]
    fps = parse_fps(st.get("avg_frame_rate"), st.get("r_frame_rate"))
    return float(d["format"]["duration"]), int(st["width"]), int(st["height"]), fps


def webp_args(quality, lossless=False):
    """Shared high-quality libwebp encoder arguments for ffmpeg."""
    args = [
        "-c:v", "libwebp",
        "-quality", str(quality),
        "-compression_level", str(WEBP_COMPRESSION),
        "-preset", "picture",
        "-pix_fmt", "yuv420p",
    ]
    if lossless:
        args += ["-lossless", "1"]
    return args


# ---------- AI Scene Intelligence screenshots ----------
def _scene_timestamps(src, dur):
    candidates = []
    vf = "fps=2,select='gt(scene,0.28)',showinfo"
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "info", "-i", src,
             "-vf", vf, "-an", "-f", "null", "-"],
            capture_output=True, text=True, check=True
        )
        raw = (proc.stderr or "") + (proc.stdout or "")
        for m in re.finditer(r"pts_time:([0-9]+(?:\.[0-9]+)?)", raw):
            t = float(m.group(1))
            if 2.0 < t < max(2.0, dur - 2.0):
                candidates.append(t)
    except Exception as ex:
        log("  Scene detection failed:", ex)

    candidates = sorted(set(round(x, 2) for x in candidates))
    spaced = []
    min_gap = max(4.0, dur / 80.0)
    for t in candidates:
        if not spaced or t - spaced[-1] >= min_gap:
            spaced.append(t)

    fallback = [
        dur * i / 16.0
        for i in range(1, 16)
        if 2.0 < dur * i / 16.0 < dur - 2.0
    ]
    merged = sorted(set(spaced + [round(x, 2) for x in fallback]))

    if len(merged) > 36:
        selected = []
        step = (len(merged) - 1) / 35
        for i in range(36):
            selected.append(merged[round(i * step)])
        merged = sorted(set(selected))

    log(f"  Scene Intelligence: {len(merged)} candidate timestamps")
    return merged


def _detect_crop(src, dur, w, h):
    crops = []
    for t in [dur * 0.20, dur * 0.40, dur * 0.60, dur * 0.80]:
        try:
            proc = subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "info",
                 "-ss", f"{t:.3f}", "-i", src,
                 "-frames:v", "1",
                 "-vf", "cropdetect=limit=24:round=2:reset=0",
                 "-f", "null", "-"],
                capture_output=True, text=True, check=True
            )
            raw = (proc.stderr or "") + (proc.stdout or "")
            matches = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", raw)
            if matches:
                cw, ch, cx, cy = map(int, matches[-1])
                if cw >= int(w * 0.70) and ch >= int(h * 0.70):
                    crops.append((cw, ch, cx, cy))
        except Exception as ex:
            log("  cropdetect sample skipped:", ex)

    if not crops:
        return w, h, 0, 0

    from collections import Counter
    best, count = Counter(crops).most_common(1)[0]

    if count >= 2:
        log(f"  Black-bar detection: crop={best[0]}:{best[1]}:{best[2]}:{best[3]} "
            f"({count}/{len(crops)} samples)")
        return best

    cw, ch, cx, cy = crops[0]
    if cw < w * 0.98 or ch < h * 0.98:
        log(f"  Black-bar detection: crop={cw}:{ch}:{cx}:{cy}")
        return crops[0]

    return w, h, 0, 0


def _make_scene_candidates(src, timestamps, base_crop, outdir):
    candidate_dir = outdir / "scene_candidates"
    candidate_dir.mkdir(parents=True, exist_ok=True)

    bw, bh, bx, by = base_crop
    ratio = bw / bh if bh else 16 / 9

    if ratio > 16 / 9:
        pw = int(bh * 16 / 9)
        ph = bh
        px = bx + (bw - pw) // 2
        py = by
    elif ratio < 16 / 9:
        pw = bw
        ph = int(bw * 9 / 16)
        px = bx
        py = by + (bh - ph) // 2
    else:
        pw, ph, px, py = bw, bh, bx, by

    # Keep crop strictly inside the source frame.
    pw = min(pw, w := max(2, bw))
    ph = min(ph, h := max(2, bh))
    px = max(bx, min(int(px), bx + bw - pw))
    py = max(by, min(int(py), by + bh - ph))

    pw = max(2, (pw // 2) * 2)
    ph = max(2, (ph // 2) * 2)

    files = []
    for i, t in enumerate(timestamps):
        p = candidate_dir / f"candidate_{i:02d}.jpg"
        vf = f"crop={pw}:{ph}:{px}:{py},scale=768:432:flags=lanczos,setsar=1"
        try:
            run([
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", src,
                "-ss", f"{t:.3f}",
                "-frames:v", "1",
                "-vf", vf,
                "-q:v", "3",
                "-pix_fmt", "yuvj420p",
                str(p),
            ])
            files.append((t, p))
        except Exception as ex:
            log(f"  Candidate frame {i} failed:", ex)

    return files, (pw, ph, px, py)


def _gemini_scene_select_rest(model, prompt, candidates):
    """Call Gemini Scene Intelligence through REST, avoiding SDK AFC warnings."""
    parts = [{"text": prompt}]
    for idx, (_, p) in enumerate(candidates):
        parts.append({"text": f"Candidate index: {idx}"})
        parts.append({
            "inline_data": {
                "mime_type": "image/jpeg",
                "data": base64.b64encode(p.read_bytes()).decode("ascii"),
            }
        })

    payload = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.1,
        },
    }

    endpoint = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        + urllib.parse.quote(model, safe="")
        + ":generateContent?key="
        + urllib.parse.quote(GEMINI_KEY, safe="")
    )
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "MovieBot/1.0",
        },
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _ensure_local_qwen():
    """Return the local llama command, installing llama.cpp only if needed."""
    if not LOCAL_AI_ENABLED:
        raise RuntimeError("Local AI fallback is disabled (LOCAL_AI_ENABLED=false).")

    for cmd in ("llama", "llama-cli"):
        if shutil.which(cmd):
            return cmd

    log("  Local AI: llama.cpp not found; installing the official llama binary...")
    install_script = "https://llama.app/install.sh"
    proc = subprocess.run(
        ["bash", "-lc", f"curl -LsSf {shlex.quote(install_script)} | sh"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=300,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"llama.cpp installation failed: {proc.stdout[-2000:]}")

    local_bin = str(Path.home() / ".local" / "bin")
    os.environ["PATH"] = local_bin + os.pathsep + os.environ.get("PATH", "")
    for cmd in ("llama", "llama-cli"):
        if shutil.which(cmd):
            return cmd

    raise RuntimeError("llama.cpp installed but no llama executable was found.")


def _local_qwen_json(prompt, image_paths=None, max_tokens=700):
    """Run Qwen3-VL locally and parse its JSON response."""
    cmd = _ensure_local_qwen()
    image_paths = [str(x) for x in (image_paths or [])][:LOCAL_AI_MAX_IMAGES]
    if not image_paths:
        raise RuntimeError("Qwen fallback requires at least one image.")

    image_arg = ",".join(image_paths)
    if cmd == "llama":
        executable = [cmd, "cli"]
    else:
        executable = [cmd]

    args = executable + [
        "-hf", LOCAL_QWEN_MODEL,
        "--image", image_arg,
        "-p", prompt,
        "-c", str(LOCAL_AI_CONTEXT),
        "-n", str(max_tokens),
        "-t", str(LOCAL_AI_THREADS),
        "--temperature", "0.1",
        "--reasoning", "off",
        "--simple-io",
        "--single-turn",
        "--no-warmup",
    ]

    log("  Local AI: running Qwen3-VL fallback...")
    proc = subprocess.run(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=LOCAL_AI_TIMEOUT,
    )
    raw = proc.stdout or ""
    if proc.returncode != 0:
        raise RuntimeError(f"Qwen3-VL exited with code {proc.returncode}: {raw[-2500:]}")

    # llama.cpp may print status/timing lines around the model response.
    candidates = re.findall(r"\{.*\}", raw, flags=re.S)
    for candidate in reversed(candidates):
        cleaned = candidate.strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            continue

    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.I).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as ex:
        raise RuntimeError(f"Qwen3-VL returned invalid JSON: {raw[-3000:]}") from ex


def _local_qwen_scene_select(candidates, count):
    """Select screenshot indexes with Qwen3-VL after Gemini scene selection fails."""
    if not candidates:
        return []
    wanted = min(count, len(candidates))
    prompt = f"""You are selecting screenshots for an original film website.
Choose exactly {wanted} candidate frame indexes.
Prefer sharp, cinematic, well-lit frames with people, emotion, action, scenery, or strong composition.
Avoid black frames, blur, credits, logos, title cards, empty frames, and near-duplicates.
Spread choices across the movie when possible.
Return ONLY JSON: {{\"selected\":[0,1,2]}}"""
    paths = [p for _, p in candidates]
    data = _local_qwen_json(prompt, paths, max_tokens=200)
    selected = []
    for value in data.get("selected") or []:
        try:
            idx = int(value)
            if 0 <= idx < len(candidates):
                selected.append(idx)
        except (TypeError, ValueError):
            pass
    selected = list(dict.fromkeys(selected))
    if not selected:
        raise RuntimeError("Qwen3-VL returned no valid screenshot indexes.")
    return selected[:wanted]


def _local_qwen_analyze(filename_hint, frame_paths, site_labels):
    """Metadata fallback using local Qwen3-VL."""
    hint = clean_hint(filename_hint)
    year = find_year(filename_hint)
    prompt = f"""You are the backup metadata editor for an ORIGINAL movie blog.
Gemini failed, so you must complete the metadata task locally.
Filename hint: {hint}
Language hint: {LANGUAGE_HINT}
Director: {DIRECTOR_NAME}
Release year from filename: {year or 'Unknown'}
Manual title supplied by the site owner: {MANUAL_TITLE or 'None'}

Use the supplied movie frames, audio context, filename hint, and the manual title when present.
When a manual title is supplied, treat that title as the authoritative movie name and do NOT try to replace it.
Identify the release year when it can reasonably be established from the supplied video/context; otherwise return null.
Do not invent cast, crew, awards, ratings, box office, exact plot facts, or IMDb information.
For the title, first read any visible movie title/title card and use it; if the filename clearly contains
the real title, clean it and preserve it. Never use an actor name, character name, genre or generic phrase
as the title when a real title is visible. Invent a title only when no real title can reasonably be identified.
Return ONLY valid JSON with:
title: clean 1-8 word film title, preferably the actual visible/filename title
tagline: one short sentence, max 18 words
release_year: 4-digit release year when reasonably identifiable, otherwise null
description: 2 compact paragraphs, about 120-180 words total, written as a proper full-movie synopsis even when the supplied video is only a short clip; do not describe only the clip scene
language: display language(s) if reasonably identifiable, otherwise the language hint or Unknown
original_language: main/original language if reasonably identifiable, otherwise Unknown
genres: 1-3 genres
content_rating: General audience, Teen and above, or Mature audience
tags: up to 6 short keywords
labels: pick 1-4 values ONLY from this exact list: {json.dumps(site_labels)}
Do not use piracy terms such as leaked, HD print, free download full movie, WEB-DL, dual audio, or 300mb."""
    return _local_qwen_json(prompt, frame_paths, max_tokens=700)


def _ai_choose_scene_frames(candidates, count):
    if not candidates:
        return []

    candidates = candidates[:36]
    wanted = min(count, len(candidates))

    prompt = f"""You are selecting screenshots for an ORIGINAL film website.

Choose exactly {wanted} of the supplied candidate frames.

Selection rules:
- Prefer sharp, cinematic, visually interesting and well-lit frames.
- Prefer strong characters, environments, action, emotion, or composition.
- Avoid black frames, blurry frames, transitional frames, credits, logos,
  title cards, empty shots, and frames dominated by darkness.
- Avoid near-duplicate frames.
- Spread selections across different parts of the movie when possible.
- Do not infer or invent plot facts.

Return ONLY valid JSON:
{{"selected":[0,1,2]}}
"""

    # Scene selection does not need function calling or tools. Use the REST
    # generateContent endpoint directly so the SDK's automatic-function-calling
    # path is not involved. A temporary 503 should not kill the workflow.
    scene_models = []
    for model in [
        os.environ.get("SCENE_GEMINI_MODEL", "gemini-3.5-flash"),
        GEMINI_MODEL,
        "gemini-3.5-flash",
    ]:
        if model and model not in scene_models:
            scene_models.append(model)

    for model in scene_models:
        for attempt in range(1, 4):
            try:
                response = _gemini_scene_select_rest(model, prompt, candidates)
                texts = []
                for candidate in response.get("candidates") or []:
                    for part in (candidate.get("content") or {}).get("parts") or []:
                        if part.get("text"):
                            texts.append(part["text"])
                raw_text = "\n".join(texts).strip()
                if not raw_text:
                    raise RuntimeError("Gemini returned no scene-selection text.")

                cleaned = re.sub(
                    r"^```(?:json)?\s*|\s*```$",
                    "",
                    raw_text,
                    flags=re.I,
                ).strip()
                data = json.loads(cleaned)

                selected = []
                for value in data.get("selected") or []:
                    try:
                        idx = int(value)
                        if 0 <= idx < len(candidates):
                            selected.append(idx)
                    except (TypeError, ValueError):
                        pass

                selected = list(dict.fromkeys(selected))
                if selected:
                    log(f"  AI selected scene frames ({model}): {selected[:wanted]}")
                    return selected[:wanted]

                raise RuntimeError("Gemini returned an empty/invalid selected list.")

            except urllib.error.HTTPError as ex:
                detail = ""
                try:
                    detail = ex.read().decode("utf-8", "replace")[:500]
                except Exception:
                    pass
                if ex.code in (408, 429, 500, 502, 503, 504):
                    if attempt < 3:
                        wait = 2 ** attempt
                        log(
                            f"  Scene selection {model}: HTTP {ex.code}; "
                            f"retrying in {wait}s ({attempt}/3)"
                        )
                        time.sleep(wait)
                        continue
                    log(f"  Scene selection {model}: HTTP {ex.code} after 3 attempts: {detail}")
                else:
                    log(f"  Scene selection {model}: HTTP {ex.code}: {detail}")
                break

            except (urllib.error.URLError, TimeoutError) as ex:
                if attempt < 3:
                    wait = 2 ** attempt
                    log(
                        f"  Scene selection {model}: temporary network error; "
                        f"retrying in {wait}s ({attempt}/3): {ex}"
                    )
                    time.sleep(wait)
                    continue
                log(f"  Scene selection {model}: network error after 3 attempts: {ex}")
                break

            except Exception as ex:
                log(f"  Gemini scene selection failed ({model}): {ex}")
                break

    # Gemini failed completely for this task: use local Qwen3-VL before the
    # deterministic selector. This path is never reached when Gemini succeeds.
    try:
        selected = _local_qwen_scene_select(candidates, wanted)
        log(f"  AI selected scene frames (local Qwen3-VL): {selected}")
        return selected
    except Exception as ex:
        log(f"  Local Qwen3-VL scene selection failed: {ex}")

    # Deterministic fallback if both Gemini and local AI are unavailable.
    # The screenshot pipeline therefore continues even during a Gemini outage.
    log("  Scene Intelligence: Gemini unavailable; using deterministic frame selection.")
    if len(candidates) <= wanted:
        return list(range(len(candidates)))

    step = (len(candidates) - 1) / max(1, wanted - 1)
    return [round(i * step) for i in range(wanted)]


def make_screenshots(src, dur, w, h, outdir):
    """
    AI Scene Intelligence screenshot pipeline.

    Final screenshots:
      - exact 16:9
      - encoded black letterbox bars removed where detectable
      - no artificial borders
      - up to 1920x1080
      - high-quality WebP (shot_N.webp) for upload
      - a lossless-quality JPG twin (shot_N.jpg) kept only for AI analysis
      - selected by Gemini from scene candidates
      - extracted from the original source
    """
    base_crop = _detect_crop(src, dur, w, h)
    timestamps = _scene_timestamps(src, dur)

    minimum_candidates = max(8, SCREENSHOTS * 2)
    if len(timestamps) < minimum_candidates:
        extra = [
            dur * i / (minimum_candidates + 1)
            for i in range(1, minimum_candidates + 1)
            if 2.0 < dur * i / (minimum_candidates + 1) < dur - 2.0
        ]
        timestamps = sorted(set(
            timestamps + [round(x, 2) for x in extra]
        ))

    candidates, crop16 = _make_scene_candidates(
        src, timestamps, base_crop, outdir
    )

    chosen_indexes = _ai_choose_scene_frames(candidates, SCREENSHOTS)

    if not chosen_indexes:
        chosen_indexes = list(range(min(SCREENSHOTS, len(candidates))))

    # Fill missing slots without duplicating a selected candidate.
    used = set(chosen_indexes)
    for i in range(len(candidates)):
        if len(chosen_indexes) >= SCREENSHOTS:
            break
        if i not in used:
            chosen_indexes.append(i)
            used.add(i)

    bw, bh, bx, by = crop16

    # crop16 is already 16:9. Never add padding or black bars.
    if bw >= 1920:
        out_w, out_h = 1920, 1080
    elif bw >= 1280:
        out_w, out_h = 1280, 720
    else:
        out_w = max(2, (bw // 2) * 2)
        out_h = max(2, (int(out_w * 9 / 16) // 2) * 2)

    files = []
    for out_index, candidate_index in enumerate(
        chosen_indexes[:SCREENSHOTS], 1
    ):
        t = candidates[candidate_index][0]
        jpg_p = str(outdir / f"shot_{out_index}.jpg")
        webp_p = str(outdir / f"shot_{out_index}.webp")

        vf = (
            f"crop={bw}:{bh}:{bx}:{by},"
            f"scale={out_w}:{out_h}:flags=lanczos+accurate_rnd+full_chroma_int,"
            "setsar=1"
        )

        # High-quality JPG: used ONLY as input for Gemini/Qwen analysis.
        run([
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", src,
            "-ss", f"{t:.3f}",
            "-frames:v", "1",
            "-vf", vf,
            "-q:v", "1",
            "-pix_fmt", "yuvj420p",
            jpg_p,
        ])

        # Final WebP: this is the file that gets uploaded and shown on the post.
        run([
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", src,
            "-ss", f"{t:.3f}",
            "-frames:v", "1",
            "-vf", vf,
        ] + webp_args(SHOT_WEBP_QUALITY) + [webp_p])

        files.append(webp_p)
        log(
            f"  Screenshot {out_index}: {t:.2f}s -> "
            f"{out_w}x{out_h}, AI-selected, WebP q{SHOT_WEBP_QUALITY}"
        )

    return files


def _gemini_google_image_search(query, outdir):
    """
    Use Gemini's Google Search grounding with image_search enabled to find
    a real web image for the movie poster/thumbnail.
    Returns a list of candidate image URLs.
    """
    prompt = f"""Search Google Images for the movie/film poster for:
"{query}"

Find the most relevant official or professionally published poster/cover.
Prefer a clean portrait movie poster, ideally close to 2:3 ratio.
Avoid fan edits, screenshots, social-media collages, unrelated films,
logos-only images, and images with large watermarks.

Use image search. Return only a short JSON object:
{{"query":"{query}"}}.
Do not invent image URLs; the application will read the image-search results.
"""

    candidates = []

    for model in dict.fromkeys([GEMINI_MODEL, "gemini-flash-latest"]):
        try:
            payload = {
                "contents": [
                    {"parts": [{"text": prompt}]}
                ],
                "tools": [
                    {
                        "google_search": {
                            "search_types": {
                                "image_search": {}
                            }
                        }
                    }
                ],
                "generationConfig": {
                    "responseMimeType": "application/json",
                    "temperature": 0.1,
                },
            }

            endpoint = (
                "https://generativelanguage.googleapis.com/v1beta/models/"
                + urllib.parse.quote(model, safe="")
                + ":generateContent?key="
                + urllib.parse.quote(GEMINI_KEY, safe="")
            )

            req = urllib.request.Request(
                endpoint,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "MovieBot/1.0",
                },
                method="POST",
            )

            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))

            # REST response uses camelCase. Accept snake_case too so the
            # function remains tolerant of SDK/proxy transformations.
            candidates_json = data.get("candidates") or []
            for candidate in candidates_json:
                gm = (
                    candidate.get("groundingMetadata")
                    or candidate.get("grounding_metadata")
                    or {}
                )
                chunks = (
                    gm.get("groundingChunks")
                    or gm.get("grounding_chunks")
                    or []
                )

                for chunk in chunks:
                    image = chunk.get("image") or {}
                    image_uri = (
                        image.get("imageUri")
                        or image.get("image_uri")
                    )
                    if image_uri:
                        candidates.append(image_uri)

                # If the model happened to return a URL in text, keep it as
                # a secondary candidate; grounding image URLs remain preferred.
                for part in (candidate.get("content") or {}).get("parts") or []:
                    value = part.get("text") or ""
                    for url in re.findall(r"https?://[^\s\"'<>]+", value):
                        candidates.append(url.rstrip(".,)"))

            candidates = list(dict.fromkeys(candidates))
            if candidates:
                log(f"  Google Image Search found {len(candidates)} image candidate(s)")
                return candidates[:8]

            log(f"  Gemini Google Image Search returned no image candidates ({model})")

        except Exception as ex:
            log(f"  Gemini Google Image Search failed ({model}): {ex}")

    return []


def _download_web_image(url, path):
    """Download one image-search result without loading an entire movie."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/131.0 Safari/537.36"
            ),
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        },
    )

    with urllib.request.urlopen(req, timeout=45) as resp:
        data = resp.read()

    if len(data) < 2048:
        raise RuntimeError("Downloaded image is too small.")

    Path(path).write_bytes(data)


def _make_2x3_thumbnail_from_image(src_image, out_path):
    """
    Convert a web poster to a clean 2:3 portrait high-quality WebP thumbnail.
    No 9:16 crop and no artificial black borders.
    """
    probe_json = subprocess.check_output([
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "json", str(src_image)
    ])
    info = json.loads(probe_json)
    stream = info["streams"][0]
    iw = int(stream["width"])
    ih = int(stream["height"])

    if iw <= 0 or ih <= 0:
        raise RuntimeError("Invalid downloaded image dimensions.")

    # Target poster ratio = 2:3, matching the reference-style movie cards.
    if iw / ih > 2 / 3:
        ch = ih
        cw = int(ih * 2 / 3)
        cx = (iw - cw) // 2
        cy = 0
    else:
        cw = iw
        ch = int(iw * 3 / 2)
        cx = 0
        cy = (ih - ch) // 2

    cw = max(2, (cw // 2) * 2)
    ch = max(2, (ch // 2) * 2)
    cx = max(0, min(cx, iw - cw))
    cy = max(0, min(cy, ih - ch))

    # THUMB_W x THUMB_H = exact 2:3 (default 800x1200). NOT 9:16.
    run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(src_image),
        "-vf",
        f"crop={cw}:{ch}:{cx}:{cy},"
        f"scale={THUMB_W}:{THUMB_H}:flags=lanczos+accurate_rnd+full_chroma_int,setsar=1",
        "-frames:v", "1",
    ] + webp_args(THUMB_WEBP_QUALITY) + [str(out_path)])

    return str(out_path)


def make_thumbnail(src, dur, w, h, outdir, movie_title="", filename_hint=""):
    """
    Google-search poster thumbnail (high-quality WebP).

    Priority:
      1. Gemini + Google Image Search result
      2. If search/download fails, create a 2:3 thumbnail from the source

    The final image is always 2:3 (default 800x1200), never 9:16.
    """
    title = str(movie_title or "").strip()
    hint = clean_hint(filename_hint) if filename_hint else ""
    query = title or hint or "movie poster"
    if hint and title and hint.lower() not in title.lower():
        query = f"{title} {hint}"

    raw_dir = outdir / "web_thumbnail_candidates"
    raw_dir.mkdir(parents=True, exist_ok=True)

    image_urls = _gemini_google_image_search(
        f"{query} official movie poster",
        raw_dir
    )

    for i, image_url in enumerate(image_urls, 1):
        raw_path = raw_dir / f"poster_{i}.source"
        final_path = outdir / "thumb_2x3.webp"

        try:
            log(f"  Trying Google image poster {i}/{len(image_urls)}")
            _download_web_image(image_url, raw_path)
            _make_2x3_thumbnail_from_image(raw_path, final_path)

            if final_path.exists() and final_path.stat().st_size > 10_000:
                log("  Thumbnail source: Google Image Search")
                log(f"  Thumbnail size: {THUMB_W}x{THUMB_H} (2:3) WebP q{THUMB_WEBP_QUALITY}")
                return str(final_path)

        except Exception as ex:
            log(f"  Google poster candidate {i} failed: {ex}")
            try:
                raw_path.unlink(missing_ok=True)
            except Exception:
                pass

    # Safe fallback: still use the requested reference-style 2:3 ratio,
    # but never revert to the old 9:16 thumbnail.
    log("  Google poster unavailable; using source-frame 2:3 fallback.")

    if w / h > 2 / 3:
        ch = h
        cw = int(h * 2 / 3)
    else:
        cw = w
        ch = int(w * 3 / 2)

    cw = max(2, (cw // 2) * 2)
    ch = max(2, (ch // 2) * 2)

    p = str(outdir / "thumb_2x3.webp")
    run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", f"{dur * 0.35:.2f}",
        "-i", src,
        "-frames:v", "1",
        "-vf",
        f"crop={cw}:{ch},scale={THUMB_W}:{THUMB_H}:flags=lanczos+accurate_rnd+full_chroma_int,setsar=1",
    ] + webp_args(THUMB_WEBP_QUALITY) + [p])
    return p


def analysis_inputs(src, dur, outdir, screenshot_paths):
    """Reuse the final screenshots for Gemini; only extract the audio sample."""
    frames = []
    for path in screenshot_paths[:SCREENSHOTS]:
        try:
            # Final screenshots are WebP; the AI analysis uses the JPG twin
            # (same frame) because the requests are sent as image/jpeg.
            jpg = Path(path).with_suffix(".jpg")
            frames.append((jpg if jpg.exists() else Path(path)).read_bytes())
        except OSError as ex:
            log(f"  Analysis screenshot skipped: {ex}")

    if not frames:
        raise RuntimeError("No screenshots available for AI analysis.")

    audio = outdir / "an_audio.mp3"
    start = dur * 0.10
    run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{start:.2f}", "-t", str(AUDIO_MINUTES * 60),
         "-i", src, "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k", str(audio)])
    return frames, audio.read_bytes()


def transcode(src, target, w, h, out):
    """target = length of the SHORT side (480/720/1080): works for landscape and vertical."""
    crf = CRF.get(target, 23)
    vf = f"scale=-2:{target}" if w >= h else f"scale={target}:-2"
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-stats", "-i", src,
           "-map", "0:v:0", "-map", "0:a?", "-vf", vf, "-pix_fmt", "yuv420p",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
           "-c:a", "aac", "-b:a", "128k"]
    # +faststart only exists for the mp4/mov muxers (not mkv).
    if out.lower().endswith((".mp4", ".mov", ".m4v")):
        cmd += ["-movflags", "+faststart"]
    run(cmd + [out])


# ---------- menu labels (read from the live blog) ----------
FALLBACK_LABELS = [
    "Bollywood Content", "Desi Junction", "Dual Audio", "Hindi Dubbed", "Hindi TV Shows",
    "Web Series", "WWE", "Hollywood Movies", "Malayalam Movies", "Marathi Movies",
    "Mobile Movies", "Multi Audio", "Pakistani Movies", "PC Games", "Pre Release",
    "Punjabi Movies", "Single Video Songs", "Tamil Movies", "Telugu Movies", "Trailers",
    "Uncategorized",
]
BLOCKED_LABEL = re.compile(r"18\+|adult|xxx|hevc|x265", re.I)


def parse_labels(page):
    found = re.findall(r"/search/label/([^\"'?&#<>\s/]+)", page)
    labels, seen = [], set()
    for f in found:
        name = urllib.parse.unquote_plus(f).strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            labels.append(name)
    return labels


def get_site_labels():
    """Read the real label names from the blog's menu, fall back to a built-in list."""
    labels = []
    try:
        url = blogger.blogs().get(blogId=BLOG_ID).execute()["url"]
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        page = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "ignore")
        labels = parse_labels(page)
        log(f"  Found {len(labels)} labels on the blog")
    except Exception as e:  # noqa
        log("  Could not read blog labels:", e)
    if len(labels) < 3:
        have = {l.lower() for l in labels}
        labels += [l for l in FALLBACK_LABELS if l.lower() not in have]
    labels = [l for l in labels if not BLOCKED_LABEL.search(l)]
    return labels[:60]


def pick_labels(raw, site_labels):
    canon = {l.lower(): l for l in site_labels}
    out = []
    for r in raw or []:
        c = canon.get(str(r).strip().lower())
        if c and c not in out:
            out.append(c)
    return out[:4]


# ---------- Gemini ----------
def as_paragraphs(v):
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    return [p.strip() for p in re.split(r"\n\s*\n", str(v or "")) if p.strip()]


YEAR_RE = re.compile(r"(?<!\d)(19[5-9]\d|20[0-4]\d)(?!\d)")


def find_year(filename):
    """Release year only if the file name contains one (e.g. 'My Film 2019.mp4'), else None."""
    m = YEAR_RE.search(Path(filename).stem)
    return int(m.group(1)) if m else None


def clean_hint(filename):
    """Extract a useful movie-title hint from a messy video filename.

    Keep real title words, but remove the common release/encoding/social
    metadata that frequently gets appended to uploaded movie files.
    """
    t = Path(filename).stem
    t = re.sub(r"[#@]\S+", " ", t)
    t = re.sub(r"[_.]+", " ", t)
    t = re.sub(r"[\[\]{}()]+", " ", t)

    # Remove common technical/release metadata without touching normal title words.
    metadata = r"""\b(?:480p|576p|720p|1080p|1440p|2160p|4k|8k|x264|x265|h264|h265|hevc|av1|aac|ac3|ddp|dd|5\.1|2\.0|10bit|8bit|hdr|sdr|bluray|blu[- ]?ray|web[- ]?dl|web[- ]?rip|webrip|brrip|hdrip|dvdrip|camrip|proper|repack|remux|yts|rarbg|hindi|english|tamil|telugu|malayalam|kannada|bengali|dual[ -]?audio|multi[ -]?audio|dubbed|subbed|subs|eng[ -]?sub|movie|full[ -]?movie|watch[ -]?online|download)\b"""
    t = re.sub(metadata, " ", t, flags=re.I)
    t = YEAR_RE.sub(" ", t)
    t = re.sub(r"[^\w\s'&:-]", " ", t, flags=re.UNICODE)
    t = re.sub(r"\s+", " ", t).strip(" -_:|")
    return t


def filename_title_candidate(filename):
    """Return a strong title candidate only when the filename contains one."""
    raw = Path(filename).stem
    cleaned = clean_hint(filename)
    if not cleaned:
        return ""

    # Social/reel filenames are not reliable movie-title evidence.
    low = cleaned.lower()
    bad = {"wait for it", "instagram reels", "reels", "explore page",
           "viral reels", "content creator", "trending reels", "parrot skit"}
    if low in bad or len(cleaned.split()) > 10:
        return ""

    # A usable title is normally 1-8 words after metadata removal.
    words = cleaned.split()
    if 1 <= len(words) <= 8:
        return cleaned
    return ""


def _gemini_multimodal_json_rest(model, prompt, frames, audio_bytes=None):
    """Gemini multimodal JSON call through REST; avoids SDK AFC warnings."""
    parts = [{"text": prompt}]
    for b in frames:
        parts.append({
            "inline_data": {
                "mime_type": "image/jpeg",
                "data": base64.b64encode(b).decode("ascii"),
            }
        })
    if audio_bytes:
        parts.append({
            "inline_data": {
                "mime_type": "audio/mp3",
                "data": base64.b64encode(audio_bytes).decode("ascii"),
            }
        })

    payload = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.2,
        },
    }
    endpoint = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        + urllib.parse.quote(model, safe="")
        + ":generateContent?key="
        + urllib.parse.quote(GEMINI_KEY, safe="")
    )
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "MovieBot/1.0"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        response = json.loads(resp.read().decode("utf-8", "replace"))

    texts = []
    for candidate in response.get("candidates") or []:
        for part in (candidate.get("content") or {}).get("parts") or []:
            if part.get("text"):
                texts.append(part["text"])
    text = "\n".join(texts).strip()
    if not text:
        raise RuntimeError("Gemini returned no text.")
    return json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I).strip())


def _gemini_imdb_lookup(title, year=None, language=""):
    """Look up an IMDb rating through Gemini Google Search grounding.

    We only accept a rating when Gemini returns an IMDb source URL and the
    matched title/year are reasonably consistent. Otherwise return N/A.
    """
    title = str(title or "").strip()
    if not title:
        return "N/A"

    year_text = str(year) if year else ""
    prompt = f"""Search the web for the exact movie/title below and verify its IMDb rating.
Movie title: {title}
Release year: {year_text or 'unknown'}
Language: {language or 'unknown'}

IMPORTANT:
- Use ONLY the official IMDb website (imdb.com) as the source for the rating.
- Do NOT guess or invent a rating.
- Match the title and release year carefully. If the title/year cannot be confidently matched,
  return rating as N/A.
- If IMDb has no displayed rating, return N/A.
- Return ONLY JSON with exactly these keys:
  matched_title: exact IMDb title if found, otherwise "",
  matched_year: year if found, otherwise null,
  rating: IMDb aggregate rating such as "7.2/10", otherwise "N/A",
  imdb_url: official IMDb title URL if found, otherwise ""
"""

    models = []
    for m in [
        os.environ.get("IMDB_GEMINI_MODEL", "gemini-2.5-flash"),
        GEMINI_MODEL,
        "gemini-2.5-flash",
    ]:
        if m and m not in models:
            models.append(m)

    for model in models:
        for attempt in range(1, 3):
            try:
                payload = {
                    "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                    "tools": [{"google_search": {}}],
                    "generationConfig": {
                        "responseMimeType": "application/json",
                        "temperature": 0.0,
                    },
                }
                endpoint = (
                    "https://generativelanguage.googleapis.com/v1beta/models/"
                    + urllib.parse.quote(model, safe="")
                    + ":generateContent?key="
                    + urllib.parse.quote(GEMINI_KEY, safe="")
                )
                req = urllib.request.Request(
                    endpoint,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json", "User-Agent": "MovieBot/1.0"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=90) as resp:
                    response = json.loads(resp.read().decode("utf-8", "replace"))

                texts = []
                imdb_sources = []
                for candidate in response.get("candidates") or []:
                    for part in (candidate.get("content") or {}).get("parts") or []:
                        if part.get("text"):
                            texts.append(part["text"])
                    gm = candidate.get("groundingMetadata") or candidate.get("grounding_metadata") or {}
                    for chunk in gm.get("groundingChunks") or gm.get("grounding_chunks") or []:
                        web = chunk.get("web") or {}
                        uri = web.get("uri") or web.get("url") or ""
                        if "imdb.com" in uri.lower():
                            imdb_sources.append(uri)

                text = "\n".join(texts).strip()
                if not text:
                    raise RuntimeError("IMDb lookup returned no text")
                data = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I).strip())

                matched_title = str(data.get("matched_title") or "").strip()
                matched_year = data.get("matched_year")
                rating = str(data.get("rating") or "N/A").strip()
                imdb_url = str(data.get("imdb_url") or "").strip()
                if not imdb_url and imdb_sources:
                    imdb_url = imdb_sources[0]

                # Strict validation: the URL must actually come from Gemini's
                # IMDb grounding result, and the rating must be on IMDb's 1-10 scale.
                normalized_returned = imdb_url.split("?")[0].rstrip("/").lower()
                grounded_match = any(
                    normalized_returned == src.split("?")[0].rstrip("/").lower()
                    for src in imdb_sources
                ) if imdb_sources else False
                m = re.fullmatch(r"(?:10(?:\.0)?|[1-9](?:\.[0-9])?)/10", rating)
                if not grounded_match or "imdb.com" not in imdb_url.lower() or not m:
                    log(f"  IMDb lookup: no verified rating for '{title}'")
                    return "N/A"

                # Guard against an unrelated same-name result.
                ratio = difflib.SequenceMatcher(
                    None, re.sub(r"\W+", "", title.lower()),
                    re.sub(r"\W+", "", matched_title.lower())
                ).ratio() if matched_title else 0.0
                year_ok = True
                if year and matched_year:
                    try:
                        year_ok = int(matched_year) == int(year)
                    except Exception:
                        year_ok = False

                if ratio < 0.72 or not year_ok:
                    log(f"  IMDb lookup: title/year mismatch for '{title}' -> '{matched_title}' ({matched_year})")
                    return "N/A"

                log(f"  IMDb verified: {matched_title} ({matched_year or year_text}) = {rating}")
                return rating

            except urllib.error.HTTPError as ex:
                if ex.code in (408, 429, 500, 502, 503, 504) and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                log(f"  IMDb lookup via Gemini failed on {model}: HTTP {ex.code}")
                break
            except Exception as ex:
                log(f"  IMDb lookup via Gemini failed on {model}: {ex}")
                break

    return "N/A"


def normalize_movie_title(value, fallback=""):
    """Keep the Blogger title short, clean and poster-like.

    The AI may occasionally add marketing words even when asked not to.
    Strip only obvious metadata/marketing suffixes; do not rewrite genuine
    movie names.
    """
    text = html.unescape(str(value or "")).strip()
    text = text.strip('\"\'`“”‘’')
    text = YEAR_RE.sub(" ", text)
    text = re.sub(
        r"\s*(?:[-|:–—]\s*)?(?:full\s+movie|movie\s+full|watch\s+online|online\s+watch|download|official\s+trailer|trailer)\s*$",
        "",
        text,
        flags=re.I,
    )
    text = re.sub(r"\s+", " ", text).strip(" -|:–—")

    if not text or text.lower() in {"untitled", "untitled film", "movie", "film"}:
        text = clean_hint(fallback) if fallback else "Untitled Film"

    return text[:120].strip()


def _gemini_title_facts_lookup(title, current_year=None, current_language=""):
    """Fallback web verification for missing year/audio-language metadata."""
    title = str(title or "").strip()
    if not title:
        return {}
    prompt = f"""Find factual metadata for this exact film: {title}

Use Google Search and return ONLY JSON:
{{
  "release_year": 4-digit original release year or null,
  "languages": ["language1", "language2"],
  "original_language": "primary original language or Unknown"
}}
Rules: match the exact film; include only actual spoken/audio languages, not subtitle languages; do not guess.
"""
    for model in [GEMINI_MODEL, "gemini-flash-latest"]:
        if not model:
            continue
        try:
            payload = {
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "tools": [{"google_search": {}}],
                "generationConfig": {"responseMimeType": "application/json", "temperature": 0.0},
            }
            endpoint = (
                "https://generativelanguage.googleapis.com/v1beta/models/"
                + urllib.parse.quote(model, safe="")
                + ":generateContent?key=" + urllib.parse.quote(GEMINI_KEY, safe="")
            )
            req = urllib.request.Request(endpoint, data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json", "User-Agent": "MovieBot/1.0"}, method="POST")
            with urllib.request.urlopen(req, timeout=90) as resp:
                response = json.loads(resp.read().decode("utf-8", "replace"))
            texts = []
            for candidate in response.get("candidates") or []:
                for part in (candidate.get("content") or {}).get("parts") or []:
                    if part.get("text"):
                        texts.append(part["text"])
            if not texts:
                continue
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", "\n".join(texts).strip(), flags=re.I).strip()
            data = json.loads(raw)
            out = {}
            if not current_year:
                try:
                    y = int(data.get("release_year"))
                    if 1888 <= y <= 2100:
                        out["release_year"] = y
                except (TypeError, ValueError):
                    pass
            if not current_language:
                langs = _title_languages(" | ".join(str(x) for x in (data.get("languages") or [])))
                if langs:
                    out["language"] = " - ".join(langs)
            original = str(data.get("original_language") or "").strip()
            if original and original.lower() not in {"unknown", "n/a", "none", "null"}:
                out["original_language"] = original
            if out:
                log("  Title facts verified by Gemini search:", out)
                return out
        except Exception as ex:
            log(f"  Title-facts lookup failed on {model}: {ex}")
    return {}


def _gemini_audio_language_fallback(audio_bytes):
    """Identify spoken language from the extracted audio without relying on frames."""
    if not audio_bytes:
        return {}
    prompt = """Listen to the supplied movie audio carefully and identify the language(s) actually spoken.
Return ONLY JSON in exactly this form:
{
  "languages": ["Hindi", "English"],
  "primary_language": "Hindi"
}
Rules:
- Identify spoken/dialogue languages from the audio itself.
- Do NOT count subtitles, captions, background music, or metadata.
- If the audio is dubbed, report the language actually spoken in this audio.
- Use common English language names only (Hindi, English, Kannada, Telugu, Tamil, Malayalam, Bengali, Marathi, Punjabi, Urdu, etc.).
- If uncertain, return an empty languages list and primary_language "Unknown".
"""
    models = []
    for model in [GEMINI_MODEL, os.environ.get("ANALYSIS_GEMINI_FALLBACK", "gemini-3.5-flash")]:
        if model and model not in models:
            models.append(model)
    for model in models:
        try:
            payload = {
                "contents": [{"role": "user", "parts": [
                    {"text": prompt},
                    {"inline_data": {
                        "mime_type": "audio/mp3",
                        "data": base64.b64encode(audio_bytes).decode("ascii"),
                    }},
                ]}],
                "generationConfig": {
                    "responseMimeType": "application/json",
                    "temperature": 0.0,
                },
            }
            endpoint = (
                "https://generativelanguage.googleapis.com/v1beta/models/"
                + urllib.parse.quote(model, safe="")
                + ":generateContent?key=" + urllib.parse.quote(GEMINI_KEY, safe="")
            )
            req = urllib.request.Request(
                endpoint, data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json", "User-Agent": "MovieBot/1.0"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=180) as resp:
                response = json.loads(resp.read().decode("utf-8", "replace"))
            texts = []
            for candidate in response.get("candidates") or []:
                for part in (candidate.get("content") or {}).get("parts") or []:
                    if part.get("text"):
                        texts.append(part["text"])
            if not texts:
                continue
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", "\n".join(texts).strip(), flags=re.I).strip()
            data = json.loads(raw)
            langs = _title_languages(" | ".join(str(x) for x in (data.get("languages") or [])))
            primary = str(data.get("primary_language") or "").strip()
            if primary.lower() in {"unknown", "n/a", "none", "null"}:
                primary = ""
            if langs or primary:
                if primary and not langs:
                    langs = [primary]
                log("  Spoken-language check from audio:", langs, "primary=", primary or "Unknown")
                return {"language": " - ".join(langs), "original_language": primary}
        except Exception as ex:
            log(f"  Gemini audio-language fallback failed on {model}: {ex}")
    return {}


def _gemini_video_language_fallback(filename_hint, frames, audio_bytes):
    """Dedicated audio/visual language check when the main analysis says Unknown."""
    prompt = f"""Listen carefully to the supplied audio sample and inspect the movie frames.
File hint: {clean_hint(filename_hint)}
Return ONLY JSON: {{"languages":["Hindi","English"],"original_language":"Hindi"}}
Include only languages actually spoken in the supplied audio. Do not count subtitles or captions.
If uncertain, return an empty languages list and original_language "Unknown".
"""
    models = []
    for model in [GEMINI_MODEL, os.environ.get("ANALYSIS_GEMINI_FALLBACK", "gemini-3.5-flash")]:
        if model and model not in models:
            models.append(model)
    for model in models:
        try:
            data = _gemini_multimodal_json_rest(model, prompt, frames, audio_bytes)
            langs = _title_languages(" | ".join(str(x) for x in (data.get("languages") or [])))
            original = str(data.get("original_language") or "").strip()
            if original.lower() in {"unknown", "n/a", "none", "null"}:
                original = ""
            if langs or original:
                return {"language": " - ".join(langs), "original_language": original}
        except Exception as ex:
            log(f"  Gemini video-language fallback failed on {model}: {ex}")
    return {}


# ---------- TMDB + Gemini synopsis rewrite ----------
def _tmdb_get(path, params=None):
    params = dict(params or {})
    headers = {"Accept": "application/json", "User-Agent": "MovieBot/1.0"}
    if TMDB_READ_TOKEN:
        headers["Authorization"] = f"Bearer {TMDB_READ_TOKEN}"
    else:
        params["api_key"] = TMDB_API_KEY
    url = "https://api.themoviedb.org/3" + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def tmdb_lookup(title, year=None, imdb_id=None):
    """Return TMDB facts for the movie, or {} on any problem (never raises)."""
    if not (TMDB_API_KEY or TMDB_READ_TOKEN):
        return {}
    try:
        best = None
        if imdb_id:
            found = retry(lambda: _tmdb_get(
                f"/find/{imdb_id}", {"external_source": "imdb_id"}), tries=2)
            hits = found.get("movie_results") or []
            best = hits[0] if hits else None
        if not best and title:
            params = {"query": title, "include_adult": "false"}
            if year:
                params["year"] = year
            results = retry(lambda: _tmdb_get("/search/movie", params), tries=2).get("results") or []

            def norm(s):
                return re.sub(r"\W+", "", str(s or "").lower())

            best_ratio = 0.0
            for r in results[:5]:
                for cand in (r.get("title"), r.get("original_title")):
                    ratio = difflib.SequenceMatcher(None, norm(title), norm(cand)).ratio()
                    if ratio > best_ratio:
                        best, best_ratio = r, ratio
            if not best or best_ratio < 0.72:
                log(f"  TMDB: no confident match for '{title}'")
                return {}
        if not best:
            return {}

        d = retry(lambda: _tmdb_get(
            f"/movie/{best['id']}", {"append_to_response": "external_ids"}), tries=2)
        y = None
        try:
            y = int(str(d.get("release_date") or "")[:4])
        except ValueError:
            pass
        out = {
            "id": d.get("id"),
            "title": d.get("title"),
            "overview": str(d.get("overview") or "").strip(),
            "year": y,
            "rating": d.get("vote_average"),
            "genres": [g["name"] for g in d.get("genres") or [] if g.get("name")],
            "imdb_id": (d.get("external_ids") or {}).get("imdb_id"),
            "runtime": d.get("runtime"),
            "original_language": d.get("original_language"),
        }
        log(f"  TMDB matched: {out['title']} ({out['year']}) id={out['id']}")
        return out
    except Exception as ex:
        log(f"  TMDB lookup failed (limit/network/key?): {ex}")
        return {}


def _omdb_get(params):
    params = dict(params)
    params["apikey"] = OMDB_API_KEY
    url = "https://www.omdbapi.com/?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "MovieBot/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def omdb_lookup(title, year=None, imdb_id=None):
    """Return OMDb facts (incl. real IMDb rating), or {} on any problem (never raises)."""
    if not OMDB_API_KEY:
        return {}
    try:
        variants = []
        if imdb_id:
            variants.append({"i": imdb_id, "plot": "full"})
        elif title:
            if year:
                variants.append({"t": title, "y": year, "type": "movie", "plot": "full"})
            variants.append({"t": title, "type": "movie", "plot": "full"})
        d = None
        for params in variants:
            d = retry(lambda: _omdb_get(params), tries=2)
            if str(d.get("Response")).lower() == "true":
                break
            log(f"  OMDb: {d.get('Error') or 'no result'}")
            d = None
        if not d:
            return {}

        def clean(v):
            v = str(v or "").strip()
            return "" if v.upper() == "N/A" else v

        if not imdb_id:
            ratio = difflib.SequenceMatcher(
                None, re.sub(r"\W+", "", str(title).lower()),
                re.sub(r"\W+", "", clean(d.get("Title")).lower())).ratio()
            if ratio < 0.72:
                log(f"  OMDb: title mismatch '{title}' -> '{d.get('Title')}'")
                return {}

        rating = clean(d.get("imdbRating"))
        if not re.fullmatch(r"(?:10(?:\.0)?|[1-9](?:\.[0-9])?)", rating):
            rating = ""
        y = None
        m = re.search(r"\d{4}", clean(d.get("Year")))
        if m:
            y = int(m.group(0))
        out = {
            "title": clean(d.get("Title")),
            "year": y,
            "imdb_id": clean(d.get("imdbID")),
            "imdb_rating": f"{rating}/10" if rating else "",
            "plot": clean(d.get("Plot")),
            "genres": [g.strip() for g in clean(d.get("Genre")).split(",") if g.strip()],
        }
        log(f"  OMDb matched: {out['title']} ({out['year']}) rating={out['imdb_rating'] or 'N/A'}")
        return out
    except Exception as ex:
        log(f"  OMDb lookup failed (limit/network/key?): {ex}")
        return {}


def gemini_rewrite_synopsis(title, year, overview):
    """Rewrite a TMDB overview with Gemini. Returns [] if every model fails."""
    prompt = f"""Rewrite this movie overview as an original synopsis for a film blog.
Movie: {title} {f'({year})' if year else ''}
Source overview: {overview}

Rules:
- Your own words, natural English, 2 compact paragraphs, about 120-180 words total.
- Use ONLY facts in the source overview. Do not invent characters, events or endings.
- Spoiler-light. No piracy words, no mention of TMDB, AI or this website.
Return ONLY JSON: {{"synopsis": ["paragraph 1", "paragraph 2"]}}"""
    models = []
    for m in [GEMINI_MODEL, os.environ.get("ANALYSIS_GEMINI_FALLBACK", "gemini-3.5-flash")]:
        if m and m not in models:
            models.append(m)
    for model in models:
        try:
            data = _gemini_multimodal_json_rest(model, prompt, [], None)
            paras = as_paragraphs(data.get("synopsis") or data.get("description"))
            if paras:
                log(f"  Synopsis rewritten by Gemini ({model})")
                return paras[:2]
        except Exception as ex:
            log(f"  Gemini synopsis rewrite failed on {model}: {ex}")
    return []


def analyze(filename_hint, frames, audio_bytes, site_labels):
    hint = clean_hint(filename_hint)
    year = find_year(filename_hint)
    prompt = f"""You are a film writer and metadata editor for an ORIGINAL movie blog.
You get 12 frames spread across the film and an audio sample.
File name hint (may be messy): "{hint}". Language hint (may be empty): "{LANGUAGE_HINT}".
Director name (may be empty): "{DIRECTOR_NAME}".
Manual title (if supplied): "{MANUAL_TITLE}".

Rules:
- Write everything in your own words, in natural English. Never copy text from any website, film or review.
- Base movie facts ONLY on what you can actually see/hear and the filename hint. If uncertain, use a safe general value.
- Do not invent cast, crew, awards, festivals, box office, IMDb pages, or exact plot facts.
- Identify the actual film title whenever it is visible in a frame/title card, opening/closing title, or clearly present in the filename.
- Treat a recognizable filename title as a strong candidate, but verify it against the supplied frames before changing it.
- If a genuine title is visible, preserve its wording; only normalize capitalization/spacing. Do NOT invent a different title just to make it sound more attractive.
- Never use a character name, actor name, tagline, scene description, genre, or generic phrase as the movie title when a real title can be identified.
- Title must be 1-8 words, with no hashtags, emojis, year, language, quality, file size, "trending reels", "watch online", "download", "full movie", or "official trailer" text.
- Write the description as a proper FULL-MOVIE synopsis, even when the uploaded source is only a short clip or a short excerpt. Do NOT describe only the selected scene (for example, do not start with "A powerful 30-second clip...").
- When the movie title is reliably identified from the filename or supplied visual/audio evidence, write the synopsis for the whole movie: introduce the protagonist(s), central premise, setting, major conflict and overall story progression. You may use well-known factual plot knowledge associated with the identified movie, but do not invent characters, events, relationships or endings.
- Keep the synopsis spoiler-light: explain the movie's main journey and conflict without revealing the final twist, ending or major resolution.
- Do not mention that AI analyzed the movie, do not mention the filename, the uploaded clip length, screenshots, or the website.
- The website description should be 2 compact paragraphs, about 120-180 words total, natural, informative and engaging.
- Language should list the languages actually evident from the audio/filename when possible, for example "Hindi - English".
- Original language should be the primary/original spoken language when reasonably identifiable; otherwise "Unknown".
- Genres should be 1-3 suitable genres based on the film.
- Content rating should be one of "General audience", "Teen and above", "Mature audience".
- No piracy words in title/description/metadata (leaked, HD print, free download full movie, WEB-DL, dual audio, 300mb).

Return ONLY JSON with these keys:
  title: clean film title,
  release_year: 4-digit release year when it can be identified from the supplied video/filename/context, otherwise null,
  tagline: one short sentence, max 18 words,
  description: 2 compact paragraphs, about 120-180 words total, a full-movie spoiler-light synopsis even if the source video is only a short clip,
  imdb_rating: always return "N/A" here; IMDb will be verified separately through official IMDb search,
  language: display language(s), e.g. "Hindi - English",
  original_language: original/main language, e.g. "English",
  genres: list of 1-3 genres,
  content_rating: one of the allowed values,
  tags: list of up to 6 short keywords,
  labels: pick 1-4 categories that best fit this film, ONLY from this exact list
          (copy spelling exactly): {json.dumps(site_labels)}.
          Judge by language spoken, film industry/country, and type. Ignore encoding/file-format labels."""
    data = {}
    analysis_models = []
    for model in [GEMINI_MODEL, os.environ.get("ANALYSIS_GEMINI_FALLBACK", "gemini-3.5-flash")]:
        if model and model not in analysis_models:
            analysis_models.append(model)

    for model in analysis_models:
        for attempt in range(1, 4):
            try:
                data = _gemini_multimodal_json_rest(model, prompt, frames, audio_bytes)
                log("  Gemini model used:", model)
                break
            except urllib.error.HTTPError as ex:
                detail = ""
                try:
                    detail = ex.read().decode("utf-8", "replace")[:500]
                except Exception:
                    pass
                if ex.code in (408, 429, 500, 502, 503, 504) and attempt < 3:
                    wait = 2 ** attempt
                    log(f"  Gemini analysis {model}: HTTP {ex.code}; retrying in {wait}s ({attempt}/3)")
                    time.sleep(wait)
                    continue
                log(f"  Gemini analysis {model}: HTTP {ex.code}: {detail}")
                break
            except (urllib.error.URLError, TimeoutError) as ex:
                if attempt < 3:
                    wait = 2 ** attempt
                    log(f"  Gemini analysis {model}: temporary network error; retrying in {wait}s ({attempt}/3)")
                    time.sleep(wait)
                    continue
                log(f"  Gemini analysis {model}: network error after 3 attempts: {ex}")
                break
            except Exception as e:  # noqa
                log(f"  Gemini model {model} failed: {e}")
                break
        if data:
            break

    if not data:
        # Gemini failed: use the local Qwen3-VL fallback directly.
        try:
            local_dir = WORK / "local_ai_frames"
            local_dir.mkdir(parents=True, exist_ok=True)
            local_paths = []
            for i, frame_bytes in enumerate(frames[:LOCAL_AI_MAX_IMAGES]):
                fp = local_dir / f"frame_{i}.jpg"
                fp.write_bytes(frame_bytes)
                local_paths.append(fp)
            data = _local_qwen_analyze(filename_hint, local_paths, site_labels)
            log("  Local Qwen3-VL metadata fallback succeeded.")
        except Exception as ex:
            log(f"  Local Qwen3-VL metadata fallback failed: {ex}")

    if not data:
        log("  Using deterministic fallback text.")

    desc = as_paragraphs(data.get("description"))
    if not desc:
        desc = as_paragraphs(data.get("synopsis"))
    if not desc:
        fallback_title = filename_title_candidate(filename_hint) or clean_hint(filename_hint) or "This film"
        desc = [
            f"{fallback_title} follows a central character whose life is shaped by the people, circumstances and conflict established in the story. The film develops its premise through the characters' goals, challenges and decisions, building toward a larger confrontation while maintaining its overall dramatic and cinematic tone.",
            "This synopsis is kept spoiler-light and avoids claiming specific events that could not be reliably established. It focuses on the film's central premise and story direction rather than describing only the short portion of the movie contained in the uploaded video.",
        ]

    faq = [f for f in (data.get("faq") or []) if isinstance(f, dict) and f.get("q") and f.get("a")]
    ai_title = normalize_movie_title(data.get("title"), "")
    if data.get("release_year") and not year:
        try:
            year = int(data.get("release_year"))
        except (TypeError, ValueError):
            pass
    filename_candidate = filename_title_candidate(filename_hint)
    if MANUAL_TITLE:
        final_title = normalize_movie_title(MANUAL_TITLE, filename_hint)
        log("  Title source: MANUAL_TITLE ->", final_title)
    elif filename_candidate:
        final_title = normalize_movie_title(filename_candidate, filename_hint)
        # If Gemini/Qwen produced an exact-looking title matching the filename,
        # keep the filename spelling; this prevents hallucinated replacement titles.
        log("  Title source: filename candidate ->", final_title)
    else:
        final_title = normalize_movie_title(ai_title, filename_hint)
        log("  Title source: AI/frame analysis ->", final_title)
    # Language must come from the actual video/audio when possible.
    # If the main multimodal analysis says Unknown, first ask Gemini using
    # the audio alone, then use the visual+audio check, and only then use
    # title-based web verification as a last factual fallback.
    current_lang = str(data.get("language") or "").strip()
    if not current_lang or current_lang.lower() in {"unknown", "n/a", "none", "null"}:
        try:
            lang_fallback = _gemini_audio_language_fallback(audio_bytes)
            if lang_fallback.get("language"):
                data["language"] = lang_fallback["language"]
            if lang_fallback.get("original_language"):
                data["original_language"] = lang_fallback["original_language"]
        except Exception as ex:
            log("  Audio-language fallback error:", ex)

    current_lang = str(data.get("language") or "").strip()
    if not current_lang or current_lang.lower() in {"unknown", "n/a", "none", "null"}:
        try:
            lang_fallback = _gemini_video_language_fallback(filename_hint, frames, audio_bytes)
            if lang_fallback.get("language"):
                data["language"] = lang_fallback["language"]
            if lang_fallback.get("original_language"):
                data["original_language"] = lang_fallback["original_language"]
        except Exception as ex:
            log("  Video-language fallback error:", ex)

    if MANUAL_TITLE and (not year or not str(data.get("language") or "").strip() or str(data.get("language")).strip().lower() in {"unknown", "n/a", "none", "null"}):
        try:
            facts = _gemini_title_facts_lookup(final_title, year, data.get("language") or "")
            if not year and facts.get("release_year"):
                year = facts["release_year"]
            if (not str(data.get("language") or "").strip() or str(data.get("language")).strip().lower() in {"unknown", "n/a", "none", "null"}) and facts.get("language"):
                data["language"] = facts["language"]
            if facts.get("original_language") and (not data.get("original_language") or str(data.get("original_language")).lower() == "unknown"):
                data["original_language"] = facts["original_language"]
        except Exception as ex:
            log("  Title-facts fallback error:", ex)

    # Final lightweight language fallback from the filename only when it
    # explicitly contains a language marker. This never guesses from the
    # movie title itself.
    final_lang = str(data.get("language") or LANGUAGE_HINT or "").strip()
    if not final_lang or final_lang.lower() in {"unknown", "n/a", "none", "null"}:
        lower_name = str(filename_hint or "").lower()
        found = []
        language_markers = [
            ("hindi", "Hindi"), ("english", "English"), ("kannada", "Kannada"),
            ("telugu", "Telugu"), ("tamil", "Tamil"), ("malayalam", "Malayalam"),
            ("bengali", "Bengali"), ("marathi", "Marathi"), ("punjabi", "Punjabi"),
            ("urdu", "Urdu"),
        ]
        for marker, label in language_markers:
            if re.search(r"(?<![a-z])" + re.escape(marker) + r"(?![a-z])", lower_name) and label not in found:
                found.append(label)
        if found:
            final_lang = " - ".join(found[:4])

    final_original = str(data.get("original_language") or "").strip()
    if not final_original or final_original.lower() in {"unknown", "n/a", "none", "null"}:
        # Keep a known spoken language rather than showing Unknown when that
        # is all we can establish reliably.
        final_original = _title_languages(final_lang)[0] if _title_languages(final_lang) else "Unknown"

    # ----- TMDB + OMDb: facts, real IMDb rating, Gemini synopsis rewrite -----
    # Each source is optional and can fail (no key / daily limit / no match)
    # without stopping the other one:
    #   Synopsis: longest of TMDB overview / OMDb plot -> Gemini rewrite
    #             -> Gemini failed: raw overview -> no source: Gemini/Qwen text above
    #   Rating:   OMDb IMDb rating -> Gemini IMDb search -> N/A
    # Title is never changed by these sources.
    start_id = IMDB_ID if re.fullmatch(r"tt\d{6,10}", IMDB_ID) else ""
    tmdb = tmdb_lookup(final_title, year, start_id or None)
    omdb = omdb_lookup(final_title, year, start_id or None)
    imdb_id = start_id or tmdb.get("imdb_id") or omdb.get("imdb_id") or ""
    if imdb_id and imdb_id != start_id:
        # One source found the IMDb id: let the other one use it for an exact match.
        if not tmdb:
            tmdb = tmdb_lookup(final_title, year, imdb_id)
        if not omdb:
            omdb = omdb_lookup(final_title, year, imdb_id)

    if tmdb or omdb:
        year = year or tmdb.get("year") or omdb.get("year")
        genres = tmdb.get("genres") or omdb.get("genres")
        if genres:
            data["genres"] = genres[:3]
        sources = [t for t in (tmdb.get("overview"), omdb.get("plot")) if t]
        if sources:
            source_text = max(sources, key=len)
            rewritten = gemini_rewrite_synopsis(final_title, year, source_text)
            if rewritten:
                desc = rewritten
            else:
                log("  Gemini failed -> using original TMDB/OMDb text as synopsis")
                desc = as_paragraphs(source_text)[:2]

    imdb_rating = omdb.get("imdb_rating") or "N/A"
    if imdb_rating == "N/A":
        log("  No OMDb IMDb rating; trying Gemini IMDb search...")
        imdb_rating = _gemini_imdb_lookup(final_title, year, final_lang)
    return {
        "title": final_title or "Untitled Film",
        "source_filename": filename_hint,
        "tagline": str(data.get("tagline") or "").strip(),
        "description": desc[:2],
        "imdb_rating": imdb_rating,
        "imdb_id": imdb_id,
        "language": final_lang or "Unknown",
        "original_language": final_original,
        "genres": [str(g).strip() for g in (data.get("genres") or ["Drama"]) if str(g).strip()][:3],
        "content_rating": str(data.get("content_rating") or "General audience").strip(),
        "tags": [str(t).strip() for t in (data.get("tags") or []) if str(t).strip()][:6],
        "release_year": year,
        "labels": pick_labels(data.get("labels"), site_labels),
        # Kept for compatibility with any other code that may read these fields.
        "synopsis": desc[:2],
        "review": [],
        "themes": [],
        "faq": faq[:4],
    }


# ---------- post HTML ----------
def human(n):
    n = float(n)
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.1f}GB"
    return f"{n / 1024 ** 2:.0f}MB"


def fmt_runtime(sec):
    sec = int(sec)
    if sec < 60:
        return f"{sec} sec"
    m = sec // 60
    if m < 60:
        return f"{m} min"
    return f"{m // 60} h {m % 60} min"


def img_url(fid):
    return f"https://lh3.googleusercontent.com/d/{fid}"


# The Download button now carries the Internet Archive URL in data-url.
# After the countdown the browser is sent straight to that URL.
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
        if (busy) { return; }
        busy = true;
        var left = WAIT;
        b.style.opacity = '0.85';
        b.innerHTML = 'Please wait ' + left + ' seconds...';
        var t = setInterval(function () {
          left--;
          if (left > 0) {
            b.innerHTML = 'Please wait ' + left + ' seconds...';
            return;
          }
          clearInterval(t);
          b.innerHTML = 'Download starting...';
          window.location.href = b.getAttribute('data-url');
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


def _post_colors(title):
    """Pick a stable-but-different color theme per movie post."""
    palettes = [
        {"heading": "#ffbf00", "rating": "#18e000", "label": "#e8e8e8", "value": "#f5f5f5", "lang": "#ff3030", "quality": "#ff3030"},
        {"heading": "#00d9ff", "rating": "#7dff2a", "label": "#ededed", "value": "#ffffff", "lang": "#ff4f81", "quality": "#ff4f81"},
        {"heading": "#ff6b35", "rating": "#65ff4d", "label": "#eeeeee", "value": "#ffffff", "lang": "#ff2f92", "quality": "#ff2f92"},
        {"heading": "#b56cff", "rating": "#48ff9b", "label": "#ededed", "value": "#ffffff", "lang": "#ff4d4d", "quality": "#ff4d4d"},
        {"heading": "#ffd166", "rating": "#39ff14", "label": "#f0f0f0", "value": "#ffffff", "lang": "#00d9ff", "quality": "#00d9ff"},
        {"heading": "#00e5a8", "rating": "#a8ff00", "label": "#ededed", "value": "#ffffff", "lang": "#ff3b30", "quality": "#ff3b30"},
        {"heading": "#ff8c42", "rating": "#00ff7f", "label": "#eeeeee", "value": "#ffffff", "lang": "#ff4d6d", "quality": "#ff4d6d"},
        {"heading": "#4dabf7", "rating": "#7cff00", "label": "#eeeeee", "value": "#ffffff", "lang": "#ff5c8a", "quality": "#ff5c8a"},
    ]
    digest = hashlib.sha256(str(title).encode("utf-8")).hexdigest()
    return palettes[int(digest[:8], 16) % len(palettes)]


def _title_languages(value):
    """Turn AI language text into clean title segments such as Hindi | English."""
    text = str(value or "").strip()
    if not text or text.lower() in {"unknown", "n/a", "none", "null"}:
        return []
    # Normalize common separators used by Gemini/Qwen.
    text = re.sub(r"\s*(?:-|–|—|/|,|&|\+)\s*", "|", text)
    parts = []
    for part in text.split("|"):
        part = re.sub(r"\s+", " ", part).strip(" .")
        if not part:
            continue
        if part.lower() in {"unknown", "n/a", "none", "null"}:
            continue
        if part.lower() not in {x.lower() for x in parts}:
            parts.append(part)
    return parts[:4]


def make_final_post_title(meta, outputs):
    """Build the complete Blogger title from the owner title + video metadata."""
    base = str(MANUAL_TITLE or meta.get("title") or "Untitled Film").strip()
    base = normalize_movie_title(base, meta.get("source_filename", ""))

    year = meta.get("release_year")
    year_text = ""
    if year:
        try:
            year_int = int(year)
            if 1888 <= year_int <= 2100:
                year_text = f" ({year_int})"
        except (TypeError, ValueError):
            pass

    languages = _title_languages(meta.get("language") or meta.get("original_language") or LANGUAGE_HINT)
    language_text = " | ".join(languages)

    quality_text = " | ".join(f"{int(h)}p" for h, _, _ in outputs)

    lead = f"{base}{year_text}"
    if language_text:
        lead += f" – {language_text}"
    parts = [lead]
    if quality_text:
        parts.append(quality_text)
    parts.append("Watch Online & Download")
    return " | ".join(parts)


def build_html(meta, thumb_id, shot_ids, outputs, fps, dur, vcdn):
    # outputs = list of (height, internet_archive_download_url, size_in_bytes)
    e = html.escape
    raw_title = str(MANUAL_TITLE or meta.get("title") or "").strip()
    if not raw_title:
        raw_title = str(filename_title_candidate(meta.get("source_filename", "")) or "Untitled Film").strip()
    title = e(raw_title)
    year = meta["release_year"]
    ytxt = f" ({year})" if year else ""
    lang_known = meta.get("language") and str(meta["language"]).lower() != "unknown"
    lang = e(str(meta.get("language") or "Unknown"))
    original_lang = e(str(meta.get("original_language") or "Unknown"))
    genres = ", ".join(e(g) for g in meta.get("genres", [])) or "Drama"
    fps_txt = fmt_fps(fps)
    qualities = " - ".join(f"{h}p" for h, _, _ in outputs)
    sizes = " - ".join(human(s) for _, _, s in outputs)
    colors = _post_colors(raw_title)

    # Premium compact Movie Info card.
    # Keep the reference-style fields, but remove the excessive vertical gaps.
    rating = str(meta.get("imdb_rating") or "N/A")

    def chip(text, accent):
        return (
            f'<span style="display:inline-block;padding:3px 8px;margin:2px 4px 2px 0;'
            f'border-radius:999px;border:1px solid {accent}66;background:{accent}18;'
            f'color:{accent};font-weight:800;font-size:12px;line-height:1.2;">{e(text)}</span>'
        )

    quality_chips = "".join(chip(f"{h}p", colors["quality"]) for h, _, _ in outputs) or chip("N/A", colors["quality"])
    size_chips = "".join(chip(human(s), colors["value"]) for _, _, s in outputs) or chip("N/A", colors["value"])

    def info_item(label, value, accent=None, full=False):
        value_color = accent or colors["value"]
        width = "100%" if full else "50%"
        return (
            f'<div style="box-sizing:border-box;width:{width};padding:5px 8px;min-width:0;">'
            f'<div style="font-size:11px;line-height:1.15;text-transform:uppercase;letter-spacing:.45px;'
            f'color:{colors["label"]};opacity:.72;margin-bottom:3px;">{e(label)}</div>'
            f'<div style="font-size:14px;line-height:1.35;color:{value_color};font-weight:700;overflow-wrap:anywhere;">{value}</div>'
            f'</div>'
        )

    # Movie Name is synced from the actual Blogger post title in the browser.
    # This is important because the owner may manually edit the Blogger post
    # title after the bot creates the draft. The initial value is only a
    # fallback for users who do not edit the title manually.
    movie_name_value = (
        f'<span class="mv-movie-name-value" data-mv-fallback="{e(raw_title, quote=True)}">'
        f'{title}</span>'
    )

    info_cells = [
        info_item("IMDb Rating", f'<span style="color:{colors["rating"]};">★ {e(rating)}</span>'),
        info_item("Movie Name", movie_name_value),
    ]
    if year:
        info_cells.append(info_item("Release Year", str(year)))
    if DIRECTOR_NAME:
        info_cells.append(info_item("Directed by", e(DIRECTOR_NAME)))
    if lang_known:
        info_cells.append(info_item("Language", lang, colors["lang"]))
    info_cells.append(info_item("Original Language", original_lang))
    info_cells.append(info_item("Runtime", fmt_runtime(dur)))
    info_cells.append(info_item("Genres", genres))
    info_cells.append(info_item("Content Advisory", e(meta["content_rating"])))
    info_cells.append(info_item("Frame Rate", f"{e(fps_txt)} fps"))
    info_cells.append(info_item("Quality", quality_chips, colors["quality"], full=True))
    info_cells.append(info_item("Size", size_chips, colors["value"], full=True))

    info_title = (
        f'<div style="text-align:center;color:{colors["heading"]};font-size:20px;line-height:1.2;'
        f'margin:18px 0 10px;font-weight:900;letter-spacing:.2px;">Movie Info</div>'
    )
    info_card = (
        f'<div style="width:100%;box-sizing:border-box;margin:0 auto 22px;padding:7px 4px;'
        f'border:1px solid rgba(255,255,255,.13);border-radius:12px;'
        f'background:linear-gradient(145deg,rgba(255,255,255,.075),rgba(255,255,255,.025));'
        f'box-shadow:0 7px 22px rgba(0,0,0,.24);">'
        f'<div style="display:flex;flex-wrap:wrap;align-items:stretch;">'
        + "".join(info_cells) +
        f'</div></div>'
    )

    btn = (
        "display:block;width:200px;max-width:82%;margin:0 auto 18px;padding:11px 8px;"
        "text-align:center;color:#fff;font-weight:800;font-size:14px;"
        "text-decoration:none;cursor:pointer;border-radius:6px;"
        "background:linear-gradient(90deg,#57a51c,#1f4fb4);"
        "box-shadow:0 8px 14px rgba(0,0,0,.45);"
    )
    head = (
        "text-align:center;color:#fff;font-size:15px;line-height:1.3;"
        "margin:16px 0 10px;font-weight:800"
    )
    hr = '<hr style="border:0;border-top:1px solid rgba(255,255,255,.35);margin:24px 0"/>'

    parts = []

    # 1) 2:3 Google Image Search poster (WebP)
    parts.append(
        f'<div style="text-align:center;margin:0 auto 18px;">'
        f'<img src="{img_url(thumb_id)}" alt="{title}" '
        f'width="360" style="display:block;width:360px;max-width:72%;height:auto;'
        f'margin:0 auto;border-radius:8px;box-shadow:0 8px 22px rgba(0,0,0,.35);"/>'
        f'</div>'
    )

    # 2) Title
    parts.append(
        f'<h2 style="text-align:center;color:{colors["heading"]};font-size:24px;'
        f'line-height:1.3;margin:10px 0 28px;font-weight:800;">{title}</h2>'
    )

    # 3) Movie Info — same field structure/style as bot-9.py, with the
    # current verified IMDb rating added at the top.
    parts.append(info_title)
    parts.append(info_card)

    # 4) VCDN player — immediately after Movie Info.
    parts.append(
        f'<h3 style="text-align:center;color:{colors["heading"]};font-size:22px;'
        f'margin:28px 0 18px;">Watch {title} Online</h3>'
    )
    embed_url = vcdn["embed_url"]
    parts.append(
        f'<div style="position:relative;width:100%;padding-top:56.25%;'
        f'background:#000;border-radius:8px;overflow:hidden;margin:0 auto 28px">'
        f'<iframe src="{e(embed_url, quote=True)}" '
        'style="position:absolute;top:0;left:0;width:100%;height:100%;border:0" '
        'frameborder="0" allow="autoplay; encrypted-media; picture-in-picture" '
        'allowfullscreen="true"></iframe>'
        f'</div>'
    )

    # 5) Screenshots — no description/review/info between player and screenshots.
    parts.append(
        f'<h3 style="text-align:center;color:{colors["heading"]};font-size:22px;'
        f'margin:28px 0 18px;">Screenshots</h3>'
    )
    for fid in shot_ids:
        parts.append(
            f'<div style="width:100%;max-width:1920px;margin:0 auto 18px;line-height:0;'
            f'padding:0;background:none;">'
            f'<img src="{img_url(fid)}" alt="{title} screenshot" '
            'style="display:block;width:100%;height:auto;max-width:1920px;margin:0;padding:0;'
            'border:0;outline:0;box-shadow:none"/>'
            f'</div>'
        )

    # 6) Download buttons — directly after screenshots. Nothing else in between.
    # Each button links to the file on Internet Archive.
    parts.append(hr)
    parts.append(
        f'<h3 style="text-align:center;color:{colors["heading"]};font-size:22px;'
        f'margin:28px 0 18px;">Download Links</h3>'
    )
    for h, dl_url, size in outputs:
        parts.append(
            f'<h4 style="{head}">{h}p x264 {fps_txt}fps '
            f'[{human(size)}]</h4>'
        )
        parts.append(
            f'<a class="mv-dl" data-url="{e(dl_url, quote=True)}" '
            f'href="{e(dl_url, quote=True)}" rel="noopener" style="{btn}">'
            '&#11015;&#9889; DOWNLOAD NOW &#9889;&#11015;</a>'
        )

    # 7) Full-movie synopsis/plot — intentionally AFTER all download buttons.
    # The AI description is the site's full-movie, spoiler-light synopsis.
    # Keep the heading visible even if a future metadata provider returns no text.
    synopsis = [e(p) for p in (meta.get("synopsis") or meta.get("description") or []) if str(p).strip()]
    if not synopsis:
        synopsis = [
            f"{title} is presented as a complete film story, following its central characters as they face the circumstances and conflict that shape the narrative.",
            "The story develops through the characters' choices, relationships and challenges, building toward the film's larger dramatic direction without revealing its ending."
        ]

    if meta.get("tagline"):
        parts.append(hr)
        parts.append(
            f'<p style="text-align:center;color:{colors["heading"]};font-size:20px;'
            f'font-weight:700;margin:22px 0 14px;"><i>{e(meta["tagline"])}</i></p>'
        )

    parts.append(
        f'<h3 style="text-align:center;color:{colors["heading"]};font-size:22px;'
        f'margin:28px 0 18px;">Movie Synopsis / Plot</h3>'
    )
    for p in synopsis[:2]:
        parts.append(f'<p style="line-height:1.75;font-size:18px;">{p}</p>')

    parts.append(hr)

    # Sync Movie Info -> Movie Name with the title that is actually visible
    # at the top of the Blogger post. This means the user can manually edit
    # the Blogger title and Movie Info will follow it automatically.
    # We strip the technical suffix after the first | so a title such as
    # "KGF Chapter 2 | 720p | Watch Online & Download" becomes "KGF Chapter 2".
    parts.append(
        '<script>(function(){'
        'function syncMovieName(){'
        'var value=document.querySelector(\'.mv-movie-name-value\');'
        'if(!value)return;'
        'var selectors=[\'h1.post-title\',\'h1.entry-title\',\'.post-title h1\',\'.entry-title\',\'h1[itemprop="name"]\',\'.post h1\',\'h1\'];'
        'var postTitle="";'
        'for(var i=0;i<selectors.length;i++){'
        'var els=document.querySelectorAll(selectors[i]);'
        'for(var j=0;j<els.length;j++){'
        'var t=(els[j].textContent||"").replace(/\\s+/g," ").trim();'
        'if(t && !/^(movie info|watch online|screenshots|download links|movie synopsis \\/ plot)$/i.test(t)){postTitle=t;break;}'
        '}'
        'if(postTitle)break;'
        '}'
        'if(!postTitle){return;}'
        'var name=postTitle.split("|")[0].trim();'
        'name=name.replace(/\\s*\\(\\d{4}\\)\\s*$/," ").trim();'
        'if(name){value.textContent=name;}'
        '}'
        'if(document.readyState===\'loading\'){document.addEventListener(\'DOMContentLoaded\',syncMovieName);}else{syncMovieName();}'
        'setTimeout(syncMovieName,500);'
        'setTimeout(syncMovieName,1500);'
        '})();</script>'
    )
    parts.append(TIMER_SCRIPT)
    return "\n".join(parts)

# ---------- main pipeline ----------
def process(video, processed_folder, output_folder):
    name = video["name"]
    log(f"\n=== Processing: {name} ===")
    job = WORK / video["id"]
    job.mkdir(parents=True, exist_ok=True)
    src = str(job / "source.mp4")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", Path(name).stem).strip("-").lower() or "movie"

    log("Downloading original...")
    download(video["id"], src)
    dur, w, h, fps = probe(src)
    short = min(w, h)
    log(f"Duration {dur / 60:.1f} min, {w}x{h}, {fps:.2f} fps")

    log("Making AI-selected screenshots...")
    shots = make_screenshots(src, dur, w, h, job)

    log("Analysing with Gemini...")
    site_labels = get_site_labels()
    frames, audio = analysis_inputs(src, dur, job, shots)
    meta = analyze(name, frames, audio, site_labels)
    log("  Title:", meta["title"])
    log("  Labels:", meta["labels"])

    log("Searching Google Images for movie poster thumbnail...")
    thumb = make_thumbnail(
        src, dur, w, h, job,
        movie_title=meta["title"],
        filename_hint=name,
    )

    targets = sorted({t for t in RESOLUTIONS if t <= short * 1.05}) or [short]
    outputs = []
    vcdn = None

    # One unlisted (noindex) Internet Archive item per movie; every
    # resolution is added to it as a separate file.
    ia_item = make_ia_item_id(slug)
    log("Internet Archive item:", ia_item)

    for t in targets:
        out = str(job / f"{slug}_{t}p.{IA_FILE_EXT}")
        log(f"Converting to {t}p...")
        transcode(src, t, w, h, out)
        size = os.path.getsize(out)
        log(f"Uploading {t}p to Internet Archive ({human(size)})...")
        dl_url = ia_upload_file(out, ia_item, meta["title"])
        outputs.append((t, dl_url, size))

        os.remove(out)  # free disk

    # Upload the ORIGINAL source to VCDN so its adaptive HLS pipeline gets
    # the highest-quality source available, instead of only the generated
    # 720p/1080p download file. This is what gives VCDN the best chance to
    # create lower adaptive renditions such as 480p.
    log("Uploading original source to VCDN for adaptive HLS...")
    vcdn = vcdn_upload(src, meta["title"])

    if not vcdn or not vcdn.get("embed_url"):
        raise RuntimeError("VCDN upload did not return an embeddable player URL.")

    log("Uploading images (WebP)...")
    thumb_id = upload_public(thumb, output_folder, "image/webp")
    shot_ids = [upload_public(p, output_folder, "image/webp") for p in shots]

    labels = list(meta["labels"])
    if not labels:
        unc = next((l for l in site_labels if l.lower() == "uncategorized"), None)
        labels = [unc] if unc else [g for g in meta["genres"]][:2] + [meta["language"]]
    labels = [str(l)[:40] for l in labels if l][:8]

    content = build_html(meta, thumb_id, shot_ids, outputs, fps, dur, vcdn)
    final_post_title = make_final_post_title(meta, outputs)
    log("  Final Blogger title:", final_post_title)
    body = {"kind": "blogger#post",
            "title": final_post_title,
            "content": content, "labels": labels}
    post = retry(lambda: blogger.posts().insert(
        blogId=BLOG_ID, body=body, isDraft=not PUBLISH).execute())
    log("Blogger post created:", post.get("url") or post.get("id"),
        "(DRAFT)" if not PUBLISH else "(PUBLISHED)")

    drive.files().update(fileId=video["id"], addParents=processed_folder,
                         removeParents=INPUT_FOLDER, fields="id").execute()
    shutil.rmtree(job, ignore_errors=True)


def main():
    WORK.mkdir(exist_ok=True)
    videos = list_videos()
    if not videos:
        log("No new videos in the input folder. Nothing to do.")
        return 0
    processed = ensure_folder("_processed")
    output = ensure_folder("_output")
    failed = 0
    for v in videos[:MAX_VIDEOS]:
        try:
            process(v, processed, output)
        except Exception:  # noqa
            failed += 1
            traceback.print_exc()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
