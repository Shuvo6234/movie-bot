
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
            if status not in ("ready", "processed") or not embed_url:
                deadline = time.time() + 10 * 60
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



def build_vcdn_player(playback_url, title):
    """Return the custom player using VCDN HLS playback instead of the VCDN iframe."""
    src = html.escape(playback_url, quote=True)
    safe_title = html.escape(title, quote=True)
    return f"""
<style>
.vc-player {{ position:relative; width:100%; max-width:1100px; margin:auto; background:#000; overflow:hidden; border-radius:8px; font-family:Arial,sans-serif; user-select:none; }}
.vc-video {{ display:block; width:100%; height:auto; min-height:240px; background:#000; object-fit:contain; transform-origin:center center; transition:transform .18s ease; }}
.vc-center-play {{ position:absolute; top:50%; left:50%; transform:translate(-50%,-50%); width:70px; height:70px; border:0; border-radius:50%; background:#19d66b; color:#000; font-size:28px; cursor:pointer; z-index:5; }}
.vc-controls {{ position:absolute; left:0; right:0; bottom:0; padding:10px 13px 12px; background:linear-gradient(transparent,rgba(0,0,0,.94)); z-index:10; }}
.vc-progress {{ width:100%; height:5px; background:rgba(255,255,255,.25); border-radius:10px; cursor:pointer; margin-bottom:9px; }}
.vc-progress-fill {{ width:0; height:100%; background:#19d66b; border-radius:10px; }}
.vc-control-row {{ display:flex; align-items:center; gap:10px; }}
.vc-btn {{ background:transparent; color:#fff; border:0; font-size:19px; cursor:pointer; padding:4px; }}
.vc-time {{ color:#fff; font-size:13px; white-space:nowrap; }}
.vc-spacer {{ flex:1; }}
.vc-volume {{ width:75px; }}
.vc-settings {{ position:relative; }}
.vc-settings-menu {{ position:absolute; right:0; bottom:42px; width:210px; max-height:430px; overflow-y:auto; background:rgba(18,18,18,.98); border-radius:9px; padding:10px 0; display:none; box-shadow:0 5px 30px rgba(0,0,0,.6); z-index:30; }}
.vc-settings-menu.show {{ display:block; }}
.vc-menu-title {{ color:#999; font-size:12px; padding:7px 15px 8px; text-transform:uppercase; border-top:1px solid rgba(255,255,255,.08); }}
.vc-menu-title:first-child {{ border-top:0; }}
.vc-quality-btn,.vc-speed-btn {{ width:100%; background:transparent; border:0; color:#fff; padding:9px 15px; text-align:left; cursor:pointer; font-size:14px; }}
.vc-quality-btn:hover,.vc-speed-btn:hover {{ background:rgba(255,255,255,.1); }}
.vc-quality-btn.active,.vc-speed-btn.active {{ color:#19d66b; }}
.vc-scale-controls {{ display:flex; align-items:center; justify-content:space-between; padding:7px 12px 10px; }}
.vc-scale-controls button {{ width:35px; height:31px; border:0; border-radius:5px; background:#292929; color:#fff; font-size:21px; cursor:pointer; }}
.vc-scale-controls button:hover {{ background:#3a3a3a; }}
.vc-scale-value {{ color:#fff; font-size:13px; min-width:50px; text-align:center; }}
.vc-error {{ position:absolute; inset:0; display:none; align-items:center; justify-content:center; padding:25px; text-align:center; color:#fff; background:rgba(0,0,0,.86); z-index:20; }}
@media(max-width:600px) {{ .vc-center-play {{ width:56px; height:56px; font-size:23px; }} .vc-volume {{ width:55px; }} .vc-time {{ font-size:11px; }} .vc-settings-menu {{ width:190px; bottom:40px; }} }}
</style>

<div class="vc-player" id="vcPlayer">
  <video id="vcVideo" class="vc-video" playsinline preload="metadata" controlslist="nodownload" title="{safe_title}"></video>
  <button class="vc-center-play" id="vcCenterPlay" aria-label="Play">▶</button>
  <div class="vc-error" id="vcError"></div>
  <div class="vc-controls">
    <div class="vc-progress" id="vcProgress"><div class="vc-progress-fill" id="vcProgressFill"></div></div>
    <div class="vc-control-row">
      <button class="vc-btn" id="vcPlay">▶</button>
      <span class="vc-time"><span id="vcCurrent">00:00</span> / <span id="vcDuration">00:00</span></span>
      <button class="vc-btn" id="vcMute">🔊</button>
      <input class="vc-volume" id="vcVolume" type="range" min="0" max="1" step="0.05" value="1">
      <div class="vc-spacer"></div>
      <div class="vc-settings">
        <button class="vc-btn" id="vcSettings">⚙</button>
        <div class="vc-settings-menu" id="vcSettingsMenu">
          <div class="vc-menu-title">Quality</div>
          <button class="vc-quality-btn active" data-level="auto">✓ Auto</button>
          <div id="vcQualityList"></div>
          <div class="vc-menu-title">Playback Speed</div>
          <button class="vc-speed-btn" data-speed="0.5">0.5x</button>
          <button class="vc-speed-btn" data-speed="0.75">0.75x</button>
          <button class="vc-speed-btn active" data-speed="1">✓ 1x</button>
          <button class="vc-speed-btn" data-speed="1.25">1.25x</button>
          <button class="vc-speed-btn" data-speed="1.5">1.5x</button>
          <button class="vc-speed-btn" data-speed="2">2x</button>
          <div class="vc-menu-title">Viewer Scale</div>
          <div class="vc-scale-controls"><button id="vcZoomOut">−</button><span class="vc-scale-value" id="vcScaleValue">100%</span><button id="vcZoomIn">+</button></div>
        </div>
      </div>
      <button class="vc-btn" id="vcFullscreen">⛶</button>
    </div>
  </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/hls.js@1.5.17/dist/hls.min.js"></script>
<script>
(function() {{
  const video=document.getElementById("vcVideo"), player=document.getElementById("vcPlayer"), playBtn=document.getElementById("vcPlay"), centerPlay=document.getElementById("vcCenterPlay"), progress=document.getElementById("vcProgress"), progressFill=document.getElementById("vcProgressFill"), currentTime=document.getElementById("vcCurrent"), duration=document.getElementById("vcDuration"), muteBtn=document.getElementById("vcMute"), volume=document.getElementById("vcVolume"), settingsBtn=document.getElementById("vcSettings"), settingsMenu=document.getElementById("vcSettingsMenu"), fullscreenBtn=document.getElementById("vcFullscreen"), zoomIn=document.getElementById("vcZoomIn"), zoomOut=document.getElementById("vcZoomOut"), scaleValue=document.getElementById("vcScaleValue"), qualityList=document.getElementById("vcQualityList"), errorBox=document.getElementById("vcError");
  const source="{src}"; let hls=null, scale=1;
  function formatTime(seconds) {{ if(!isFinite(seconds)) return "00:00"; const h=Math.floor(seconds/3600),m=Math.floor((seconds%3600)/60),s=Math.floor(seconds%60); return h>0 ? String(h).padStart(2,"0")+":"+String(m).padStart(2,"0")+":"+String(s).padStart(2,"0") : String(m).padStart(2,"0")+":"+String(s).padStart(2,"0"); }}
  function showError(msg) {{ errorBox.textContent=msg; errorBox.style.display="flex"; }}
  function togglePlay() {{ if(video.paused) video.play().catch(function(){{}}); else video.pause(); }}
  playBtn.addEventListener("click",togglePlay); centerPlay.addEventListener("click",togglePlay);
  video.addEventListener("play",function(){{playBtn.textContent="❚❚";centerPlay.style.display="none";}});
  video.addEventListener("pause",function(){{playBtn.textContent="▶";centerPlay.style.display="block";}});
  video.addEventListener("loadedmetadata",function(){{duration.textContent=formatTime(video.duration);}});
  video.addEventListener("timeupdate",function(){{currentTime.textContent=formatTime(video.currentTime); if(video.duration) progressFill.style.width=((video.currentTime/video.duration)*100)+"%";}});
  progress.addEventListener("click",function(e){{const r=progress.getBoundingClientRect(),pct=(e.clientX-r.left)/r.width;if(video.duration) video.currentTime=pct*video.duration;}});
  volume.addEventListener("input",function(){{video.volume=parseFloat(this.value);video.muted=video.volume===0;muteBtn.textContent=video.muted?"🔇":"🔊";}});
  muteBtn.addEventListener("click",function(){{video.muted=!video.muted;muteBtn.textContent=video.muted?"🔇":"🔊";}});
  settingsBtn.addEventListener("click",function(e){{e.stopPropagation();settingsMenu.classList.toggle("show");}});
  document.addEventListener("click",function(e){{if(!settingsMenu.contains(e.target)&&e.target!==settingsBtn)settingsMenu.classList.remove("show");}});
  function setActiveQuality(btn) {{document.querySelectorAll(".vc-quality-btn").forEach(function(b){{b.classList.remove("active");b.textContent=b.textContent.replace("✓ ","");}});btn.classList.add("active");btn.textContent="✓ "+btn.textContent;}}
  function makeQualityButtons() {{
    if(!hls || !hls.levels) return;
    const seen={{}};
    hls.levels.forEach(function(level,index) {{ const height=level.height; if(!height || seen[height]) return; seen[height]=true; const b=document.createElement("button"); b.className="vc-quality-btn"; b.dataset.level=String(index); b.textContent=height+"p"; b.addEventListener("click",function(){{hls.currentLevel=index;setActiveQuality(b);settingsMenu.classList.remove("show");}}); qualityList.appendChild(b); }});
  }}
  function initHls() {{
    if(video.canPlayType("application/vnd.apple.mpegurl")) {{ video.src=source; video.addEventListener("loadedmetadata",function(){{}}); return; }}
    if(window.Hls && Hls.isSupported()) {{ hls=new Hls({{enableWorker:true,capLevelToPlayerSize:true}}); hls.loadSource(source); hls.attachMedia(video); hls.on(Hls.Events.MANIFEST_PARSED,function(){{makeQualityButtons();}}); hls.on(Hls.Events.ERROR,function(event,data){{if(data.fatal) showError("Video stream could not be loaded. Please refresh the page and try again.");}}); return; }}
    showError("This browser does not support HLS playback.");
  }}
  document.querySelectorAll(".vc-speed-btn").forEach(function(button){{button.addEventListener("click",function(){{const speed=parseFloat(this.dataset.speed);video.playbackRate=speed;document.querySelectorAll(".vc-speed-btn").forEach(function(btn){{btn.classList.remove("active");btn.textContent=btn.textContent.replace("✓ ","");}});this.classList.add("active");this.textContent="✓ "+speed+"x";settingsMenu.classList.remove("show");}});}});
  function updateScale() {{scaleValue.textContent=Math.round(scale*100)+"%";video.style.transform="scale("+scale+")";}}
  zoomIn.addEventListener("click",function(){{if(scale<2){{scale+=0.25;updateScale();}}}}); zoomOut.addEventListener("click",function(){{if(scale>0.75){{scale-=0.25;updateScale();}}}});
  fullscreenBtn.addEventListener("click",function(){{if(!document.fullscreenElement){{if(player.requestFullscreen)player.requestFullscreen();}}else document.exitFullscreen();}});
  video.addEventListener("dblclick",function(){{if(!document.fullscreenElement){{if(player.requestFullscreen)player.requestFullscreen();}}else document.exitFullscreen();}});
  video.addEventListener("error",function(){{if(video.error) showError("Video playback error. Please refresh and try again.");}});
  initHls();
}})();
</script>
"""

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
    parts.append(build_vcdn_player(playback_url, meta["title"]))

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
