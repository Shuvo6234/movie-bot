"""
Movie Bot: Drive video -> multi-resolution -> screenshots + 9:16 thumbnail
-> Gemini title/description -> Blogger post (draft by default).
Runs on GitHub Actions. All settings come from environment variables.
"""
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
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

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
RESOLUTIONS = [int(x) for x in os.environ.get("RESOLUTIONS", "480,720,1080").split(",") if x.strip()]
MAX_VIDEOS = int(os.environ.get("MAX_VIDEOS", "1"))
PUBLISH = os.environ.get("PUBLISH", "false").lower() == "true"
LANGUAGE_HINT = os.environ.get("LANGUAGE_HINT", "").strip()
AUDIO_MINUTES = int(os.environ.get("AUDIO_MINUTES", "10"))
SCREENSHOTS = int(os.environ.get("SCREENSHOTS", "6"))
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


# ---------- ffmpeg helpers ----------
def probe(path):
    out = subprocess.check_output([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=height:format=duration",
        "-of", "json", path])
    d = json.loads(out)
    return float(d["format"]["duration"]), int(d["streams"][0]["height"])


def make_screenshots(src, dur, outdir):
    files = []
    for i in range(SCREENSHOTS):
        t = dur * (i + 1) / (SCREENSHOTS + 1)
        p = str(outdir / f"shot_{i + 1}.jpg")
        run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", src,
             "-frames:v", "1", "-vf", "scale=1280:-2", "-q:v", "3", p])
        files.append(p)
    return files


def make_thumbnail(src, dur, outdir):
    """9:16 portrait thumbnail, 720x1280, centre crop from a frame at 35%."""
    p = str(outdir / "thumb_9x16.jpg")
    run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{dur * 0.35:.2f}", "-i", src,
         "-frames:v", "1", "-vf", "crop=ih*9/16:ih,scale=720:1280", "-q:v", "2", p])
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


def transcode(src, height, out):
    crf = CRF.get(height, 23)
    run(["ffmpeg", "-y", "-loglevel", "error", "-stats", "-i", src,
         "-map", "0:v:0", "-map", "0:a?",
         "-vf", f"scale=-2:{height}",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
         "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", out])


# ---------- Gemini ----------
def analyze(filename_hint, frames, audio_bytes):
    hint = Path(filename_hint).stem.replace("_", " ").replace(".", " ").strip()
    prompt = f"""You are helping publish an ORIGINAL film by its director on a movie blog.
You get 12 frames spread across the film and an audio sample. The file name hint is: "{hint}".
Language hint (may be empty): "{LANGUAGE_HINT}".
Return ONLY JSON with keys:
  title: catchy movie title (use the file name hint if it looks like a real title),
  synopsis: 3-4 sentence spoiler-light plot description in English, based on what you see/hear,
  genres: list of 1-3 genres (e.g. Drama, Thriller, Romance),
  language: main spoken language,
  release_year: integer (use {datetime.now().year} if unknown),
  tags: list of up to 6 short keywords.
Do not invent famous actor names or claim awards."""
    parts = [types.Part.from_bytes(data=b, mime_type="image/jpeg") for b in frames]
    parts.append(types.Part.from_bytes(data=audio_bytes, mime_type="audio/mp3"))
    try:
        resp = retry(lambda: gclient.models.generate_content(
            model=GEMINI_MODEL, contents=[prompt, *parts],
            config=types.GenerateContentConfig(response_mime_type="application/json")), tries=3)
        data = json.loads(re.sub(r"^```json|```$", "", resp.text.strip()).strip())
    except Exception as e:  # noqa
        log("  Gemini failed, using fallback text:", e)
        data = {}
    return {
        "title": data.get("title") or hint or "Untitled Film",
        "synopsis": data.get("synopsis") or "An original film.",
        "genres": data.get("genres") or ["Drama"],
        "language": data.get("language") or LANGUAGE_HINT or "Unknown",
        "release_year": data.get("release_year") or datetime.now().year,
        "tags": data.get("tags") or [],
    }


# ---------- post HTML ----------
def human(n):
    n = float(n)
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.1f}GB"
    return f"{n / 1024 ** 2:.0f}MB"


def img_url(fid):
    return f"https://lh3.googleusercontent.com/d/{fid}"


def build_html(meta, thumb_id, shot_ids, outputs):
    e = html.escape
    title = e(meta["title"])
    genres = ", ".join(e(g) for g in meta["genres"])
    qualities = " - ".join(f"{h}p" for h, _, _ in outputs)
    sizes = " - ".join(human(s) for _, _, s in outputs)
    player_id = next((fid for h, fid, _ in outputs if h == 720), outputs[-1][1])

    btn = ("display:block;max-width:520px;margin:6px auto 18px;padding:16px 10px;"
           "text-align:center;color:#fff;font-weight:700;text-decoration:none;"
           "background:linear-gradient(90deg,#0a4d0a,#001a80);")
    parts = [
        f'<div style="text-align:center"><img src="{img_url(thumb_id)}" alt="{title}" '
        f'width="270" style="max-width:60%;height:auto;border-radius:8px"/></div>',
        f'<p style="text-align:center"><b>Download {title} ({meta["release_year"]}) - Full Movie</b></p>',
        '<h3 style="text-align:center">Movie Info</h3>',
        f'<p><b>Movie Name:</b> {title}<br/>'
        f'<b>Release Year:</b> {meta["release_year"]}<br/>'
        f'<b>Language:</b> {e(meta["language"])}<br/>'
        f'<b>Quality:</b> {qualities}<br/>'
        f'<b>Size:</b> {sizes}<br/>'
        f'<b>Genres:</b> {genres}</p>',
        '<h3 style="text-align:center">Movie-SYNOPSIS/PLOT:</h3>',
        f'<p>{e(meta["synopsis"])}</p>',
        f'<h3>Watch {title} Online</h3>',
        f'<iframe src="https://drive.google.com/file/d/{player_id}/preview" '
        'width="100%" height="420" allow="autoplay" allowfullscreen="true" '
        'style="border:0;max-width:100%"></iframe>',
        '<h3 style="text-align:center">Screenshots: (Must See Before Downloading)...</h3>',
    ]
    for fid in shot_ids:
        parts.append(f'<p style="text-align:center"><img src="{img_url(fid)}" alt="{title} screenshot" '
                     'style="max-width:100%;height:auto"/></p>')
    parts.append('<h3 style="text-align:center">Download Links</h3>')
    for h, fid, size in outputs:
        link = f"https://drive.google.com/uc?export=download&id={fid}"
        parts.append(f'<h4 style="text-align:center;color:#ff7b7b">{h}p x264</h4>')
        parts.append(f'<a href="{link}" target="_blank" rel="noopener" style="{btn}">'
                     f'&#11015;&#9889; CLICK HERE TO DOWNLOAD ({human(size)}) &#9889;&#11015;</a>')
    parts.append('<h3 style="text-align:center;color:#f0a0ff">Winding Up &#10084;&#65039;</h3>')
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
    dur, src_h = probe(src)
    log(f"Duration {dur / 60:.1f} min, height {src_h}p")

    log("Making screenshots and thumbnail...")
    shots = make_screenshots(src, dur, job)
    thumb = make_thumbnail(src, dur, job)

    log("Analysing with Gemini...")
    frames, audio = analysis_inputs(src, dur, job)
    meta = analyze(name, frames, audio)
    log("  Title:", meta["title"])

    heights = sorted({h for h in RESOLUTIONS if h <= src_h * 1.05})
    if not heights:
        heights = [src_h]
    outputs = []
    for h in heights:
        out = str(job / f"{slug}_{h}p.mp4")
        log(f"Converting to {h}p...")
        transcode(src, h, out)
        size = os.path.getsize(out)
        log(f"Uploading {h}p ({human(size)})...")
        fid = upload_public(out, output_folder, "video/mp4")
        outputs.append((h, fid, size))
        os.remove(out)  # free disk

    log("Uploading images...")
    thumb_id = upload_public(thumb, output_folder, "image/jpeg")
    shot_ids = [upload_public(p, output_folder, "image/jpeg") for p in shots]

    labels = [g for g in meta["genres"]][:3] + [meta["language"], str(meta["release_year"])]
    labels = [str(l)[:40] for l in labels if l][:8]
    content = build_html(meta, thumb_id, shot_ids, outputs)
    body = {"kind": "blogger#post", "title": f'{meta["title"]} ({meta["release_year"]}) - Full Movie',
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
