
"""
Movie Bot: Drive video -> multi-resolution -> VCDN Watch Online
+ Google Drive downloads + screenshots + 9:16 thumbnail
-> Gemini title/description/labels -> Blogger post (draft by default).
Runs on GitHub Actions. All settings come from environment variables.
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

# ---------- settings ----------
CLIENT_ID = os.environ["GOOGLE_CLIENT_ID"]
CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]
REFRESH_TOKEN = os.environ["GOOGLE_REFRESH_TOKEN"]
GEMINI_KEY = os.environ["GEMINI_API_KEY"]
BLOG_ID = os.environ["BLOG_ID"]
INPUT_FOLDER = os.environ["DRIVE_INPUT_FOLDER_ID"]

# VCDN API key must be stored in GitHub Actions Secrets as VCDN_API_KEY.
VCDN_API_KEY = os.environ["VCDN_API_KEY"].strip()
VCDN_API_HOST = "cdn.vcdn.me"

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
RESOLUTIONS = [int(x) for x in os.environ.get("RESOLUTIONS", "480,720,1080").split(",") if x.strip()]
MAX_VIDEOS = int(os.environ.get("MAX_VIDEOS", "1"))
PUBLISH = os.environ.get("PUBLISH", "false").lower() == "true"
LANGUAGE_HINT = os.environ.get("LANGUAGE_HINT", "").strip()
AUDIO_MINUTES = int(os.environ.get("AUDIO_MINUTES", "10"))
SCREENSHOTS = int(os.environ.get("SCREENSHOTS", "6"))
WAIT_SECONDS = int(os.environ.get("WAIT_SECONDS", "20"))
DIRECTOR_NAME = os.environ.get("DIRECTOR_NAME", "").strip()
CRF = {480: 24, 720: 23, 1080: 22, 1440: 22, 2160: 21}

WORK = Path("work")
SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/blogger",
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
creds = Credentials(
    None,
    refresh_token=REFRESH_TOKEN,
    token_uri="https://oauth2.googleapis.com/token",
    client_id=CLIENT_ID,
    client_secret=CLIENT_SECRET,
    scopes=SCOPES,
)
creds.refresh(Request())
drive = build("drive", "v3", credentials=creds, cache_discovery=False)
blogger = build("blogger", "v3", credentials=creds, cache_discovery=False)
gclient = genai.Client(api_key=GEMINI_KEY)


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
    req = drive.files().create(
        body={"name": os.path.basename(path), "parents": [parent]},
        media_body=media, fields="id")
    resp = None
    while resp is None:
        _, resp = retry(req.next_chunk)
    fid = resp["id"]
    retry(lambda: drive.permissions().create(
        fileId=fid, body={"type": "anyone", "role": "reader"}).execute())
    return fid


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
    Upload a video to VCDN.

    VCDN's public homepage currently documents the simple REST upload:
        POST https://api.vcdn.me/videos
        Authorization: Bearer <key>
        multipart fields: file, title

    We use that route first because it avoids the live upload-init validation
    mismatch seen on cdn.vcdn.me. If the direct REST route is unavailable,
    we fall back to the documented chunked route.
    """
    log(f"Uploading {os.path.basename(path)} to VCDN...")

    file_size = os.path.getsize(path)
    if file_size <= 0:
        raise RuntimeError(f"VCDN upload file is empty: {path}")

    # Primary: the REST endpoint shown on VCDN's current homepage.
    try:
        log(f"  VCDN file size: {file_size} bytes")
        return _vcdn_direct_upload(path, title)
    except Exception as direct_error:
        log(f"  VCDN direct REST upload failed: {direct_error}")

        # A 413 from api.vcdn.me means that endpoint's request-size limit was
        # exceeded. Do not retry the same large multipart request; use the
        # chunked upload API instead.
        direct_text = str(direct_error)
        if "HTTP 413" in direct_text or "413 Request Entity Too Large" in direct_text:
            log("  VCDN direct endpoint rejected the file as too large; switching to chunked upload.")

        # Fallback: chunked API. The live endpoint requires a positive size.
        try:
            init = _vcdn_json(
                "POST",
                "/api/v1/upload/init",
                {
                    "filename": os.path.basename(path),
                    "title": title,
                    "size": file_size,
                },
            )
            # The live VCDN endpoint currently returns camelCase fields
            # (uploadId/uploadUrl), while the public docs show snake_case.
            upload_id = init.get("upload_id") or init.get("uploadId")
            upload_url = init.get("upload_url") or init.get("uploadUrl")
            if not upload_id:
                raise RuntimeError(
                    f"VCDN init did not return upload_id/uploadId: {init}"
                )

            log(f"  VCDN chunk upload id: {upload_id}")
            if upload_url:
                log(f"  VCDN chunk upload URL: {upload_url}")
            _vcdn_upload_binary(upload_id, path, upload_url)

            # The live VCDN API returns camelCase `uploadId` from /init
            # and expects the same field name in /complete.
            complete = _vcdn_json(
                "POST",
                "/api/v1/upload/complete",
                {"uploadId": upload_id},
            )

            # The live API currently returns `videoId` (camelCase), while the
            # public docs show `id`. Accept both forms. `status=uploaded` means
            # the file is received but transcoding may still be in progress.
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
            status = complete.get("status")

            if not video_id:
                raise RuntimeError(
                    f"VCDN complete returned no video id: {complete}"
                )

            # Poll the video endpoint until VCDN finishes processing. The
            # documented API exposes GET /api/v1/videos/{id}; this prevents us
            # from publishing a player URL before the video is ready.
            if status not in ("ready", "processed", "complete", "completed") or not embed_url:
                deadline = time.time() + 45
                last_video = complete
                while time.time() < deadline:
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
                    embed_url = (
                        info.get("embed_url")
                        or info.get("embedUrl")
                        or embed_url
                    )
                    playback = info.get("playback") or {}
                    playback_url = (
                        info.get("playback_url")
                        or info.get("playbackUrl")
                        or (playback.get("hls") if isinstance(playback, dict) else None)
                        or playback_url
                    )
                    embed_url = (
                        info.get("embed_url")
                        or info.get("embedUrl")
                        or (playback.get("embed") if isinstance(playback, dict) else None)
                        or embed_url
                    )
                    log(f"  VCDN processing status: {status}")

                    if status in ("ready", "processed", "complete", "completed"):
                        break
                    if status in ("failed", "error"):
                        raise RuntimeError(
                            f"VCDN processing failed for {video_id}: {info}"
                        )

            # Do not block the whole GitHub Actions job waiting for VCDN
            # transcoding. The file can legitimately remain `uploaded` while
            # VCDN processes it in the background. We already have a stable
            # video ID, so publish the post and let the player retry/fallback.
            if status not in ("ready", "processed", "complete", "completed"):
                log("  VCDN is still processing; continuing without waiting for ready status.")

            # The embed URL is deterministic once a video ID exists. If the
            # status endpoint did not return one, construct it as documented.
            if not embed_url:
                embed_url = f"https://embed.vcdn.me/{video_id}"

            # The live API may return the embed URL/status but omit playback_url.
            # VCDN's documented HLS URL is deterministic from the video ID, so
            # use the master playlist as a fallback for the custom HLS player.
            if not playback_url and status in ("ready", "processed", "complete", "completed", "uploaded"):
                playback_url = f"https://stream.vcdn.me/{video_id}/master.m3u8"
                log("  VCDN HLS URL was missing from API response; using documented master playlist:", playback_url)

            if not video_id or not embed_url:
                raise RuntimeError(
                    f"VCDN complete/status returned no usable player data: {last_video}"
                )

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

        except Exception as chunk_error:
            raise RuntimeError(
                "VCDN upload failed using both upload methods.\n"
                f"Direct REST error: {direct_error}\n"
                f"Chunked API error: {chunk_error}"
            ) from chunk_error


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


def make_screenshots(src, dur, outdir):
    files = []
    for i in range(SCREENSHOTS):
        t = dur * (i + 1) / (SCREENSHOTS + 1)
        p = str(outdir / f"shot_{i + 1}.jpg")
        run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", src,
             "-frames:v", "1", "-vf", "scale=1280:-2", "-q:v", "3", p])
        files.append(p)
    return files


def make_thumbnail(src, dur, w, h, outdir):
    """9:16 portrait thumbnail, 720x1280, centre crop from a frame at 35%."""
    if w * 16 >= h * 9:
        ch = h // 2 * 2
        cw = int(h * 9 / 16) // 2 * 2
    else:
        cw = w // 2 * 2
        ch = int(w * 16 / 9) // 2 * 2
    p = str(outdir / "thumb_9x16.jpg")
    run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{dur * 0.35:.2f}", "-i", src,
         "-frames:v", "1", "-vf", f"crop={cw}:{ch},scale=720:1280", "-q:v", "2", p])
    return p


def analysis_inputs(src, dur, outdir):
    frames = []
    n = 12
    for i in range(n):
        t = dur * (i + 1) / (n + 1)
        p = outdir / f"an_{i}.jpg"
        run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", src,
             "-frames:v", "1", "-vf", "scale=512:-2", "-q:v", "5", str(p)])
        frames.append(p.read_bytes())
    audio = outdir / "an_audio.mp3"
    start = dur * 0.10
    run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{start:.2f}", "-t", str(AUDIO_MINUTES * 60),
         "-i", src, "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k", str(audio)])
    return frames, audio.read_bytes()


def transcode(src, target, w, h, out):
    """target = length of the SHORT side (480/720/1080): works for landscape and vertical."""
    crf = CRF.get(target, 23)
    vf = f"scale=-2:{target}" if w >= h else f"scale={target}:-2"
    run(["ffmpeg", "-y", "-loglevel", "error", "-stats", "-i", src,
         "-map", "0:v:0", "-map", "0:a?", "-vf", vf, "-pix_fmt", "yuv420p",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
         "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", out])


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
    t = Path(filename).stem
    t = re.sub(r"[#@]\S+", " ", t)
    t = re.sub(r"[_.\-]+", " ", t)
    t = re.sub(r"[^\w\s]", " ", t, flags=re.UNICODE)
    t = re.sub(r"\s+", " ", t).strip()
    no_year = re.sub(r"\s+", " ", YEAR_RE.sub(" ", t)).strip()
    return no_year or t


def analyze(filename_hint, frames, audio_bytes, site_labels):
    hint = clean_hint(filename_hint)
    year = find_year(filename_hint)
    prompt = f"""You are a film writer. You are publishing an ORIGINAL film, on its own director's film
blog. You get 12 frames spread across the film and an audio sample.
File name hint (may be messy): "{hint}". Language hint (may be empty): "{LANGUAGE_HINT}".
Director name (may be empty): "{DIRECTOR_NAME}".

Rules:
- Write everything in your own words, in natural English. Never copy text from any website, film or review.
- Base it ONLY on what you can actually see and hear in the frames and audio. If you are unsure,
  stay general and talk about mood, visuals, sound and themes instead of specific plot facts.
- Never invent cast, crew, awards, festivals, ratings, box office or plot facts you cannot see.
- No piracy words (leaked, HD print, free download full movie, WEB-DL, dual audio, 300mb).
- The title must be a real film title of 1-6 words. No hashtags, emojis, year or words like "trending reels".
  If the file name hint is messy, invent a fitting title from what the film is about.

Return ONLY JSON with these keys:
  title: the film title,
  tagline: one sentence, max 20 words,
  synopsis: 2 short paragraphs (about 120 words), spoiler-light, separated by a blank line,
  review: 3-4 paragraphs (about 300 words) analysing tone, visual style and camera work, sound and
          music, performances in general terms, themes and who will enjoy the film,
          separated by blank lines,
  themes: list of 3-5 short phrases,
  faq: list of 4 objects {{"q": "...", "a": "..."}} with 1-2 sentence answers about the film
       (genre, language, mood, who it suits, runtime feel),
  genres: list of 1-3 genres,
  language: main spoken language,
  content_rating: one of "General audience", "Teen and above", "Mature audience",
  tags: list of up to 6 short keywords,
  labels: pick 1-4 categories that best fit this film, ONLY from this exact list
          (copy the spelling exactly): {json.dumps(site_labels)}.
          Judge by language spoken, film industry/country, and type (movie, web series,
          trailer, song, etc.). Ignore labels about video encoding or file format."""
    parts = [types.Part.from_bytes(data=b, mime_type="image/jpeg") for b in frames]
    parts.append(types.Part.from_bytes(data=audio_bytes, mime_type="audio/mp3"))
    data = {}
    for model in dict.fromkeys([GEMINI_MODEL, "gemini-flash-latest"]):
        try:
            resp = retry(lambda: gclient.models.generate_content(
                model=model, contents=[prompt, *parts],
                config=types.GenerateContentConfig(response_mime_type="application/json")), tries=2)
            data = json.loads(re.sub(r"^```json|```$", "", resp.text.strip()).strip())
            log("  Gemini model used:", model)
            break
        except Exception as e:  # noqa
            log(f"  Gemini model {model} failed: {e}")
    if not data:
        log("  Using fallback text.")
    faq = [f for f in (data.get("faq") or []) if isinstance(f, dict) and f.get("q") and f.get("a")]
    return {
        "title": data.get("title") or hint or "Untitled Film",
        "tagline": data.get("tagline") or "",
        "synopsis": as_paragraphs(data.get("synopsis")) or ["An original film."],
        "review": as_paragraphs(data.get("review")),
        "themes": [str(t) for t in (data.get("themes") or [])][:5],
        "faq": faq[:4],
        "genres": data.get("genres") or ["Drama"],
        "language": data.get("language") or LANGUAGE_HINT or "Unknown",
        "release_year": year,
        "content_rating": data.get("content_rating") or "General audience",
        "tags": data.get("tags") or [],
        "labels": pick_labels(data.get("labels"), site_labels),
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


TIMER_SCRIPT = """<script>
(function () {
  var WAIT = %d;
  var btns = document.querySelectorAll('a.mv-dl[data-fid]');
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
          window.location.href = 'https://drive.usercontent.google.com/download?id=' +
            b.getAttribute('data-fid') + '&export=download&confirm=t';
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



def build_vcdn_player(playback_url, title, embed_url=None):
    """Render the supplied MV custom player against VCDN HLS."""
    safe_title = html.escape(str(title), quote=True)
    js_url = json.dumps(str(playback_url), ensure_ascii=False).replace('</', '<\\/')
    safe_embed = html.escape(str(embed_url or ""), quote=True)
    template = '<style>\n.mv-player *, .mv-player *::before, .mv-player *::after { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }\n.mv-player { position: relative; width: 100%; height: auto; aspect-ratio: 16 / 9; min-height: 220px; max-height: min(85vh, 640px); background: #000; overflow: hidden; color: #fff; font-family: Roboto, Arial, sans-serif; margin: 20px 0; }\n.mv-player .mv-video { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: contain; background: #000; }\n.mv-center-play { position: absolute; left: 50%; top: 50%; transform: translate(-50%, -50%); width: 72px; height: 72px; border: 0; border-radius: 50%; background: #36a34c; color: #fff; display: flex; align-items: center; justify-content: center; cursor: pointer; z-index: 10; }\n.mv-center-play svg { width: 30px; height: 30px; fill: #000; margin-left: 5px; }\n.mv-controls { position: absolute; left: 0; right: 0; bottom: 0; height: 78px; padding: 0 15px 8px; display: flex; flex-direction: column; background: linear-gradient(to top, rgba(0,0,0,.92), rgba(0,0,0,.55), transparent); z-index: 20; }\n.mv-progress-area { width: 100%; height: 28px; display: flex; align-items: center; cursor: pointer; }\n.mv-progress { position: relative; width: 100%; height: 4px; border-radius: 4px; background: rgba(255,255,255,.30); }\n.mv-buffer { position: absolute; left: 0; top: 0; height: 100%; width: 0%; border-radius: inherit; background: rgba(255,255,255,.40); }\n.mv-progress-fill { position: absolute; left: 0; top: 0; height: 100%; width: 0%; border-radius: inherit; background: #36a34c; }\n.mv-progress-handle { position: absolute; top: 50%; left: 0%; width: 14px; height: 14px; transform: translate(-50%, -50%); border-radius: 50%; background: #fff; box-shadow: 0 0 2px rgba(0,0,0,.5); }\n.mv-control-row { height: 42px; display: flex; align-items: center; gap: 4px; }\n.mv-control-btn { width: 38px; height: 38px; border: 0; padding: 0; background: transparent; color: #fff; display: flex; align-items: center; justify-content: center; cursor: pointer; }\n.mv-control-btn svg { width: 20px; height: 20px; fill: #fff; stroke: #fff; }\n.mv-control-btn:hover { opacity: .85; }\n.mv-time { display: flex; align-items: center; margin-left: 3px; color: #fff; font-size: 14px; letter-spacing: 1px; white-space: nowrap; }\n.mv-time-separator { margin: 0 4px; }\n.mv-volume-wrap { display: flex; align-items: center; margin-left: 2px; }\n.mv-volume { width: 70px; height: 4px; appearance: none; -webkit-appearance: none; background: rgba(255,255,255,.3); border-radius: 4px; outline: none; }\n.mv-volume::-webkit-slider-thumb { appearance: none; -webkit-appearance: none; width: 12px; height: 12px; border-radius: 50%; background: #fff; cursor: pointer; }\n.mv-volume::-moz-range-thumb { width: 12px; height: 12px; border: 0; border-radius: 50%; background: #fff; }\n.mv-spacer { flex: 1; }\n.mv-loading { position: absolute; left: 50%; top: 50%; width: 46px; height: 46px; margin: -23px 0 0 -23px; border: 4px solid rgba(255,255,255,.25); border-top-color: #36a34c; border-radius: 50%; animation: mvspin .8s linear infinite; display: none; z-index: 9; }\n@keyframes mvspin { to { transform: rotate(360deg); } }\n.mv-error { position: absolute; left: 0; top: 0; right: 0; bottom: 0; display: flex; flex-direction: column; align-items: center; justify-content: center; text-align: center; background: rgba(0,0,0,.82); z-index: 40; padding: 20px; }\n.mv-error-msg { font-size: 15px; line-height: 1.5; margin-bottom: 14px; }\n.mv-error-btn { border: 0; border-radius: 4px; padding: 10px 26px; background: #36a34c; color: #fff; font-size: 15px; font-weight: 700; cursor: pointer; }\n.mv-settings { position: absolute; right: 15px; bottom: 65px; min-width: 200px; max-height: calc(100% - 75px); overflow-y: auto; background: rgba(20,20,20,.97); border-radius: 4px; display: none; z-index: 50; box-shadow: 0 4px 20px rgba(0,0,0,.5); }\n.mv-settings.show { display: block; }\n.mv-player:not(.mv-js) .mv-center-play, .mv-player:not(.mv-js) .mv-controls, .mv-player:not(.mv-js) .mv-loading, .mv-player:not(.mv-js) .mv-settings { display: none !important; }\n.mv-settings button { display: flex; justify-content: space-between; align-items: center; width: 100%; border: 0; padding: 12px 14px; background: transparent; color: #fff; text-align: left; cursor: pointer; font-size: 14px; }\n.mv-settings button:hover { background: rgba(255,255,255,.1); }\n.mv-settings button.active { color: #36a34c; }\n.mv-settings button.mv-head { color: #aaa; font-size: 13px; border-bottom: 1px solid rgba(255,255,255,.12); }\n.mv-settings .mv-val { color: #aaa; font-size: 13px; margin-left: 16px; }\n.mv-player:fullscreen { width: 100%; height: 100%; max-height: none; aspect-ratio: auto; margin: 0; }\n.mv-player:-webkit-full-screen { width: 100%; height: 100%; max-height: none; aspect-ratio: auto; margin: 0; }\n@media (max-width:600px) {\n  .mv-player { min-height: 240px; max-height: 78vh; }\n  .mv-controls { padding-left: 15px; padding-right: 15px; }\n  .mv-control-btn { width: 36px; }\n  .mv-time { font-size: 13px; }\n  .mv-volume { width: 60px; }\n  .mv-settings { min-width: 170px; bottom: 60px; max-height: calc(100% - 66px); }\n  .mv-settings button { padding: 8px 12px; font-size: 13px; }\n}\n</style>\n\n<div class="mv-player" id="mvPlayer">\n  <video id="mvVideo" class="mv-video" playsinline preload="metadata" controlslist="nodownload" title="__TITLE__"></video>\n  <iframe id="mvEmbed" title="Video Player" src="__EMBED_URL__" allow="autoplay; encrypted-media; picture-in-picture; fullscreen" allowfullscreen="true" frameborder="0" style="position:absolute;inset:0;width:100%;height:100%;border:0;background:#000;display:none;z-index:35"></iframe>\n  <div class="mv-loading" id="mvLoading"></div>\n  <button class="mv-center-play" id="mvCenterPlay" aria-label="Play"><svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg></button>\n  <div class="mv-settings" id="mvSettings"></div>\n  <div class="mv-controls">\n    <div class="mv-progress-area" id="mvProgressArea"><div class="mv-progress"><div class="mv-buffer" id="mvBuffer"></div><div class="mv-progress-fill" id="mvProgressFill"></div><div class="mv-progress-handle" id="mvProgressHandle"></div></div></div>\n    <div class="mv-control-row">\n      <button class="mv-control-btn" id="mvPlayBtn" aria-label="Play"><svg id="mvPlayIcon" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg></button>\n      <div class="mv-time"><span id="mvCurrentTime">0:00</span><span class="mv-time-separator">-</span><span id="mvDuration">0:00</span></div>\n      <button class="mv-control-btn" id="mvMuteBtn" aria-label="Mute"><svg viewBox="0 0 24 24"><path d="M4 9v6h4l5 4V5L8 9H4z M16 8.5a5 5 0 0 1 0 7 M18.5 6a8.5 8.5 0 0 1 0 12"/></svg></button>\n      <div class="mv-volume-wrap"><input id="mvVolume" class="mv-volume" type="range" min="0" max="1" step="0.01" value="1"></div>\n      <div class="mv-spacer"></div>\n      <button class="mv-control-btn" id="mvSettingsBtn" aria-label="Settings"><svg viewBox="0 0 24 24"><path d="M19.43 12.98 c.04-.32.07-.65.07-.98 s-.02-.66-.07-.98 l2.11-1.65 -.2-.35 -2.49-4.31 -.42.18 -2.49 1 c-.51-.4-1.08-.73-1.69-.98 L13.95 2h-5 l-.3 2.91 c-.61.25-1.18.59-1.69.98 l-2.49-1 -.42-.18 -2.49 4.31 -.2.35 2.11 1.65 c-.04.32-.08.65-.08.98 s.03.66.08.98 l-2.11 1.65 .2.35 2.49 4.31 .42-.18 2.49-1 c.51.4 1.08.73 1.69.98 L8.95 22h5 l.3-2.91 c.61-.25 1.18-.58 1.69-.98 l2.49 1 .42.18 2.49-4.31 .2-.35 -2.11-1.65z M11.45 15.5 A3.5 3.5 0 1 1 11.45 8.5 A3.5 3.5 0 0 1 11.45 15.5z"/></svg></button>\n      <button class="mv-control-btn" id="mvFullscreenBtn" aria-label="Fullscreen"><svg viewBox="0 0 24 24"><path d="M4 4h6v2H6v4H4V4z M14 4h6v6h-2V6h-4V4z M4 14h2v4h4v2H4v-6z M18 14h2v6h-6v-2h4v-4z"/></svg></button>\n    </div>\n  </div>\n</div>\n\n<script src="https://cdn.jsdelivr.net/npm/hls.js@1.5.17/dist/hls.min.js"></script>\n<script>\n/*<![CDATA[*/\n(function () {\n  var SOURCE = __SOURCE_JSON__;\n  var player=document.getElementById("mvPlayer"), video=document.getElementById("mvVideo"), embed=document.getElementById("mvEmbed"), playBtn=document.getElementById("mvPlayBtn"), centerPlay=document.getElementById("mvCenterPlay"), playIcon=document.getElementById("mvPlayIcon"), progressArea=document.getElementById("mvProgressArea"), progressFill=document.getElementById("mvProgressFill"), progressHandle=document.getElementById("mvProgressHandle"), buffer=document.getElementById("mvBuffer"), currentTime=document.getElementById("mvCurrentTime"), duration=document.getElementById("mvDuration"), muteBtn=document.getElementById("mvMuteBtn"), volume=document.getElementById("mvVolume"), settingsBtn=document.getElementById("mvSettingsBtn"), settings=document.getElementById("mvSettings"), fullscreenBtn=document.getElementById("mvFullscreenBtn"), loading=document.getElementById("mvLoading");\n  var hls=null, levels=[], qualityMode="auto", speed=1, scale="contain", view="main", ready=false, retryCount=0;\n  player.className += " mv-js";\n  function formatTime(s){if(!isFinite(s))return "0:00";s=Math.max(0,Math.floor(s));return Math.floor(s/60)+":"+String(s%60).padStart(2,"0");}\n  function showLoading(v){loading.style.display=v?"block":"none";}\n  function clearError(){var x=document.getElementById("mvError");if(x)x.remove();}\n  function showError(msg){clearError();var box=document.createElement("div");box.id="mvError";box.className="mv-error";box.innerHTML=\'<div class="mv-error-msg"></div><button type="button" class="mv-error-btn">Retry</button>\';box.querySelector(".mv-error-msg").textContent=msg;box.querySelector("button").onclick=function(){box.remove();retry();};player.appendChild(box);}\n  function destroyHls(){if(hls){try{hls.destroy();}catch(e){}hls=null;}}\n  function readyOnce(){if(ready)return;ready=true;retryCount=0;showLoading(false);duration.textContent=formatTime(video.duration);renderMenu();}\n  function useEmbedFallback(){\n    if(!embed || !embed.getAttribute("src")) return false;\n    ready=true;\n    showLoading(false);\n    destroyHls();\n    try{video.pause();}catch(e){}\n    video.style.display="none";\n    centerPlay.style.display="none";\n    var controls=player.querySelector(".mv-controls");\n    if(controls)controls.style.display="none";\n    embed.style.display="block";\n    return true;\n  }\n  function loadStream(){clearError();showLoading(true);ready=false;destroyHls();video.removeAttribute("src");video.load();if(video.canPlayType("application/vnd.apple.mpegurl")){video.src=SOURCE;video.addEventListener("loadedmetadata",readyOnce,{once:true});return;}if(window.Hls&&Hls.isSupported()){hls=new Hls({enableWorker:true,capLevelToPlayerSize:true,maxBufferLength:30,backBufferLength:30});hls.loadSource(SOURCE);hls.attachMedia(video);hls.on(Hls.Events.MANIFEST_PARSED,function(){levels=hls.levels||[];renderMenu();readyOnce();});hls.on(Hls.Events.ERROR,function(e,d){if(!d.fatal)return;if(d.type===Hls.ErrorTypes.NETWORK_ERROR&&retryCount<2){retryCount++;setTimeout(function(){if(hls)hls.startLoad();},1200);return;}if(d.type===Hls.ErrorTypes.MEDIA_ERROR&&retryCount<2){retryCount++;try{hls.recoverMediaError();}catch(x){}return;}showLoading(false);useEmbedFallback();if(!embed||!embed.getAttribute("src"))showError("Video stream could not be loaded. Please refresh and try again.");});return;}showLoading(false);showError("This browser does not support HLS playback.");}\n  function retry(){retryCount=0;loadStream();}\n  function togglePlay(){\n    if(video.paused){\n      var p=video.play();\n      if(p&&p.catch)p.catch(function(){useEmbedFallback();});\n    }else video.pause();\n  }\n  function toggleFullscreen(){try{if(!document.fullscreenElement){if(player.requestFullscreen)player.requestFullscreen();else if(video.webkitEnterFullscreen)video.webkitEnterFullscreen();}else document.exitFullscreen();}catch(e){}}\n  function qLabel(){if(qualityMode==="auto")return "Auto";var q=levels[Number(qualityMode)];return q&&q.height?q.height+"p":"Auto";}\n  function renderMenu(){var h="",i;if(view==="main"){h+=\'<button data-act="quality"><span>Quality</span><span class="mv-val">\'+qLabel()+"</span></button>";h+=\'<button data-act="speed"><span>Speed</span><span class="mv-val">\'+speed+\'x</span></button>\';h+=\'<button data-act="scale"><span>Scale</span><span class="mv-val">\'+(scale==="contain"?"Fit":scale==="cover"?"Fill":"Stretch")+"</span></button>";}else if(view==="quality"){h+=\'<button class="mv-head" data-act="back">&#8249; Quality</button>\';h+=\'<button data-q="auto"\'+(qualityMode==="auto"?\' class="active"\':\'\')+\'>Auto</button>\';var seen={};for(i=levels.length-1;i>=0;i--){var q=levels[i];if(!q.height||seen[q.height])continue;seen[q.height]=true;h+=\'<button data-q="\'+i+\'"\'+(String(qualityMode)===String(i)?\' class="active"\':\'\')+\'>\'+q.height+\'p</button>\';}}else if(view==="speed"){h+=\'<button class="mv-head" data-act="back">&#8249; Speed</button>\';[1,1.25,1.5,2].forEach(function(s){h+=\'<button data-s="\'+s+\'"\'+(speed===s?\' class="active"\':\'\')+\'>\'+s+\'x</button>\';});}else{h+=\'<button class="mv-head" data-act="back">&#8249; Scale</button>\';[["contain","Fit"],["cover","Fill"],["fill","Stretch"]].forEach(function(x){h+=\'<button data-c="\'+x[0]+\'"\'+(scale===x[0]?\' class="active"\':\'\')+\'>\'+x[1]+\'</button>\';});}settings.innerHTML=h;}\n  playBtn.onclick=togglePlay;centerPlay.onclick=togglePlay;video.onclick=togglePlay;\n  video.addEventListener("play",function(){playIcon.innerHTML=\'<path d="M7 5h4v14H7zM13 5h4v14h-4z"/>\';centerPlay.style.display="none";});\n  video.addEventListener("pause",function(){playIcon.innerHTML=\'<path d="M8 5v14l11-7z"/>\';centerPlay.style.display="flex";});\n  video.addEventListener("loadedmetadata",function(){duration.textContent=formatTime(video.duration);});\n  video.addEventListener("timeupdate",function(){var p=video.duration?(video.currentTime/video.duration)*100:0;progressFill.style.width=p+"%";progressHandle.style.left=p+"%";currentTime.textContent=formatTime(video.currentTime);duration.textContent=formatTime(video.duration);});\n  video.addEventListener("progress",function(){try{if(!video.duration||!video.buffered.length)return;var end=video.buffered.end(video.buffered.length-1);buffer.style.width=Math.min(100,(end/video.duration)*100)+"%";}catch(e){}});\n  video.addEventListener("waiting",function(){showLoading(true);});video.addEventListener("playing",function(){showLoading(false);});\n  progressArea.onclick=function(e){var r=this.getBoundingClientRect(),p=(e.clientX-r.left)/r.width;if(video.duration)video.currentTime=p*video.duration;};\n  volume.oninput=function(){video.volume=Number(this.value);video.muted=video.volume===0;};\n  muteBtn.onclick=function(){video.muted=!video.muted;volume.value=video.muted?0:(video.volume||1);};\n  settingsBtn.onclick=function(e){e.stopPropagation();view="main";renderMenu();settings.classList.toggle("show");};\n  settings.onclick=function(e){var b=e.target;while(b&&b!==settings&&b.tagName!=="BUTTON")b=b.parentNode;if(!b||b===settings)return;var act=b.getAttribute("data-act"),q=b.getAttribute("data-q"),s=b.getAttribute("data-s"),c=b.getAttribute("data-c");if(act==="quality"||act==="speed"||act==="scale"){view=act;renderMenu();return;}if(act==="back"){view="main";renderMenu();return;}if(q!==null){qualityMode=q;if(hls)hls.currentLevel=q==="auto"?-1:Number(q);settings.classList.remove("show");return;}if(s!==null){speed=Number(s);video.playbackRate=speed;settings.classList.remove("show");return;}if(c!==null){scale=c;video.style.objectFit=c;settings.classList.remove("show");return;}};\n  document.addEventListener("click",function(e){if(!player.contains(e.target))settings.classList.remove("show");});\n  fullscreenBtn.onclick=toggleFullscreen;video.ondblclick=toggleFullscreen;renderMenu();loadStream();\n  setTimeout(function(){if(!ready)useEmbedFallback();},5000);\n})();\n/*]]>*/\n</script>\n'
    return template.replace("__TITLE__", safe_title).replace("__SOURCE_JSON__", js_url).replace("__EMBED_URL__", safe_embed)

def build_html(meta, thumb_id, shot_ids, outputs, fps, dur, vcdn):
    e = html.escape
    title = e(meta["title"])
    year = meta["release_year"]
    ytxt = f" ({year})" if year else ""
    lang_known = meta["language"] and meta["language"].lower() != "unknown"
    lang = e(meta["language"])
    lang_tag = f' <span style="color:#f2f200">{{{lang}}}</span>' if lang_known else ""
    genres = ", ".join(e(g) for g in meta["genres"])
    fps_txt = fmt_fps(fps)
    qualities = " - ".join(f"{h}p" for h, _, _ in outputs)
    sizes = " - ".join(human(s) for _, _, s in outputs)
    syn = [e(p) for p in meta["synopsis"]]
    review = [e(p) for p in meta["review"]]

    btn = ("display:block;width:260px;max-width:90%;margin:0 auto 28px;padding:18px 10px;"
           "text-align:center;color:#fff;font-weight:800;font-size:19px;"
           "text-decoration:none;cursor:pointer;"
           "background:linear-gradient(90deg,#57a51c,#1f4fb4);"
           "box-shadow:0 8px 14px rgba(0,0,0,.45);")
    head = ("text-align:center;color:#fff;font-size:21px;line-height:1.4;"
            "margin:28px 0 18px;font-weight:800")
    hr = '<hr style="border:0;border-top:1px solid rgba(255,255,255,.6);margin:22px 0"/>'
    h3 = '<h3 style="text-align:center">{}</h3>'

    info = [f"<b>Movie Name:</b> {title}"]
    if year:
        info.append(f"<b>Release Year:</b> {year}")
    if DIRECTOR_NAME:
        info.append(f"<b>Directed by:</b> {e(DIRECTOR_NAME)}")
    if lang_known:
        info.append(f"<b>Language:</b> {lang}")
    info += [
        f"<b>Runtime:</b> {fmt_runtime(dur)}",
        f"<b>Genres:</b> {genres}",
        f"<b>Content Advisory:</b> {e(meta['content_rating'])}",
        f"<b>Quality:</b> {qualities}",
        f"<b>Frame Rate:</b> {fps_txt}fps",
        f"<b>Size:</b> {sizes}",
    ]

    parts = [
        f'<div style="text-align:center"><img src="{img_url(thumb_id)}" alt="{title}" '
        f'width="270" style="max-width:60%;height:auto;border-radius:8px"/></div>',
        f'<p style="text-align:center"><b>{title}{ytxt}</b>{" - " + lang + " film" if lang_known else ""}</p>',
    ]
    if meta["tagline"]:
        parts.append(f'<p style="text-align:center"><i>{e(meta["tagline"])}</i></p>')
    parts.append(f"<p>{syn[0]}</p>")
    parts.append(h3.format("Movie Info"))
    parts.append("<p>" + "<br/>".join(info) + "</p>")
    parts.append(h3.format("Movie Synopsis / Plot"))
    parts += [f"<p>{p}</p>" for p in syn]

    parts.append(f"<h3>Watch {title} Online</h3>")
    playback_url = (
        vcdn.get("playback_url")
        or vcdn.get("playbackUrl")
        or ((vcdn.get("playback") or {}).get("hls")
            if isinstance(vcdn.get("playback"), dict) else None)
    )
    if not playback_url and vcdn.get("id"):
        playback_url = f"https://stream.vcdn.me/{vcdn['id']}/master.m3u8"
    if not playback_url:
        raise RuntimeError(
            f"VCDN did not provide a usable HLS playback URL for the custom player: {vcdn}"
        )
    embed_url = (vcdn.get("embed_url") or vcdn.get("embedUrl") or
                 (f"https://embed.vcdn.me/{vcdn['id']}" if vcdn.get("id") else ""))
    parts.append(build_vcdn_player(playback_url, meta["title"], embed_url))

    if review:
        parts.append(h3.format(f"{title} - Film Review and Analysis"))
        parts += [f"<p>{p}</p>" for p in review]
    if meta["themes"]:
        parts.append(h3.format("Themes"))
        parts.append("<ul>" + "".join(f"<li>{e(t)}</li>" for t in meta["themes"]) + "</ul>")
    parts.append(h3.format("Screenshots"))
    for fid in shot_ids:
        parts.append(f'<p style="text-align:center"><img src="{img_url(fid)}" alt="{title} screenshot" '
                     'style="max-width:100%;height:auto"/></p>')
    parts.append(hr)
    parts.append(h3.format("Download Links"))
    for h, fid, size in outputs:
        direct = (f"https://drive.usercontent.google.com/download?id={fid}"
                  "&amp;export=download&amp;confirm=t")
        parts.append(
            f'<h4 style="{head}">{title}{ytxt}{lang_tag} '
            f'{h}p x264 {fps_txt}fps [{human(size)}]</h4>')
        parts.append(
            f'<a class="mv-dl" data-fid="{fid}" href="{direct}" rel="noopener" style="{btn}">'
            '&#11015;&#9889;DOWNLOAD NOW&#9889;&#11015;</a>')
    parts.append(hr)
    if meta["faq"]:
        parts.append(h3.format(f"{title} - FAQ"))
        for f in meta["faq"]:
            parts.append(f"<h4>{e(str(f['q']))}</h4><p>{e(str(f['a']))}</p>")
    parts.append('<h3 style="text-align:center;color:#f0a0ff">Winding Up &#10084;&#65039;</h3>')
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

    log("Making screenshots and thumbnail...")
    shots = make_screenshots(src, dur, job)
    thumb = make_thumbnail(src, dur, w, h, job)

    log("Analysing with Gemini...")
    site_labels = get_site_labels()
    frames, audio = analysis_inputs(src, dur, job)
    meta = analyze(name, frames, audio, site_labels)
    log("  Title:", meta["title"])
    log("  Labels:", meta["labels"])

    targets = sorted({t for t in RESOLUTIONS if t <= short * 1.05}) or [short]
    outputs = []
    vcdn = None

    # Keep the highest generated resolution for VCDN.
    # VCDN then provides adaptive HLS/multi-quality playback.
    vcdn_target = max(targets)

    for t in targets:
        out = str(job / f"{slug}_{t}p.mp4")
        log(f"Converting to {t}p...")
        transcode(src, t, w, h, out)
        size = os.path.getsize(out)
        log(f"Uploading {t}p to Google Drive ({human(size)})...")
        fid = upload_public(out, output_folder, "video/mp4")
        outputs.append((t, fid, size))

        if t == vcdn_target:
            vcdn = vcdn_upload(out, meta["title"])

        os.remove(out)  # free disk

    if not vcdn or not vcdn.get("embed_url"):
        raise RuntimeError("VCDN upload did not return an embeddable player URL.")

    log("Uploading images...")
    thumb_id = upload_public(thumb, output_folder, "image/jpeg")
    shot_ids = [upload_public(p, output_folder, "image/jpeg") for p in shots]

    labels = list(meta["labels"])
    if not labels:
        unc = next((l for l in site_labels if l.lower() == "uncategorized"), None)
        labels = [unc] if unc else [g for g in meta["genres"]][:2] + [meta["language"]]
    labels = [str(l)[:40] for l in labels if l][:8]

    content = build_html(meta, thumb_id, shot_ids, outputs, fps, dur, vcdn)
    ytxt = f' ({meta["release_year"]})' if meta["release_year"] else ""
    ltxt = f' {meta["language"]}' if meta["language"].lower() != "unknown" else ""
    body = {"kind": "blogger#post",
            "title": f'{meta["title"]}{ytxt}{ltxt} Movie - Watch Online & Download',
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
