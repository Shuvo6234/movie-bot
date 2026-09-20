"""
Movie Bot: Drive video -> multi-resolution -> screenshots + 9:16 thumbnail
-> Gemini title/description/labels -> Blogger post (draft by default).
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
    body = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [INPUT_FOLDER]
    }
    return drive.files().create(body=body, fields="id").execute()["id"]


def list_videos():
    q = f"'{INPUT_FOLDER}' in parents and mimeType contains 'video/' and trashed=false"
    res = drive.files().list(
        q=q,
        fields="files(id,name,size)",
        orderBy="createdTime"
    ).execute()
    return res["files"]


def download(file_id, dest):
    req = drive.files().get_media(fileId=file_id)
    with open(dest, "wb") as fh:
        dl = MediaIoBaseDownload(
            fh,
            req,
            chunksize=64 * 1024 * 1024
        )
        done = False
        while not done:
            status, done = retry(dl.next_chunk)
            if status:
                log(f"  download {int(status.progress() * 100)}%")


def upload_public(path, parent, mime):
    media = MediaFileUpload(
        path,
        mimetype=mime,
        resumable=True,
        chunksize=64 * 1024 * 1024
    )
    req = drive.files().create(
        body={
            "name": os.path.basename(path),
            "parents": [parent]
        },
        media_body=media,
        fields="id"
    )

    resp = None
    while resp is None:
        _, resp = retry(req.next_chunk)

    fid = resp["id"]

    retry(lambda: drive.permissions().create(
        fileId=fid,
        body={"type": "anyone", "role": "reader"}
    ).execute())

    return fid


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
        "ffprobe",
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries",
        "stream=width,height,avg_frame_rate,r_frame_rate:format=duration",
        "-of", "json",
        path
    ])

    d = json.loads(out)
    st = d["streams"][0]
    fps = parse_fps(
        st.get("avg_frame_rate"),
        st.get("r_frame_rate")
    )

    return (
        float(d["format"]["duration"]),
        int(st["width"]),
        int(st["height"]),
        fps
    )


def make_screenshots(src, dur, outdir):
    files = []

    for i in range(SCREENSHOTS):
        t = dur * (i + 1) / (SCREENSHOTS + 1)
        p = str(outdir / f"shot_{i + 1}.jpg")

        run([
            "ffmpeg",
            "-y",
            "-loglevel", "error",
            "-ss", f"{t:.2f}",
            "-i", src,
            "-frames:v", "1",
            "-vf", "scale=1280:-2",
            "-q:v", "3",
            p
        ])

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

    run([
        "ffmpeg",
        "-y",
        "-loglevel", "error",
        "-ss", f"{dur * 0.35:.2f}",
        "-i", src,
        "-frames:v", "1",
        "-vf", f"crop={cw}:{ch},scale=720:1280",
        "-q:v", "2",
        p
    ])

    return p


def analysis_inputs(src, dur, outdir):
    frames = []
    n = 12

    for i in range(n):
        t = dur * (i + 1) / (n + 1)
        p = outdir / f"an_{i}.jpg"

        run([
            "ffmpeg",
            "-y",
            "-loglevel", "error",
            "-ss", f"{t:.2f}",
            "-i", src,
            "-frames:v", "1",
            "-vf", "scale=512:-2",
            "-q:v", "5",
            str(p)
        ])

        frames.append(p.read_bytes())

    audio = outdir / "an_audio.mp3"
    start = dur * 0.10

    run([
        "ffmpeg",
        "-y",
        "-loglevel", "error",
        "-ss", f"{start:.2f}",
        "-t", str(AUDIO_MINUTES * 60),
        "-i", src,
        "-vn",
        "-ac", "1",
        "-ar", "16000",
        "-b:a", "32k",
        str(audio)
    ])

    return frames, audio.read_bytes()


def transcode(src, target, w, h, out):
    """target = length of the SHORT side (480/720/1080): works for landscape and vertical."""
    crf = CRF.get(target, 23)

    vf = f"scale=-2:{target}" if w >= h else f"scale={target}:-2"

    run([
        "ffmpeg",
        "-y",
        "-loglevel", "error",
        "-stats",
        "-i", src,
        "-map", "0:v:0",
        "-map", "0:a?",
        "-vf", vf,
        "-pix_fmt", "yuv420p",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", str(crf),
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        out
    ])


# ---------- menu labels ----------
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

BLOCKED_LABEL = re.compile(r"18\+|adult|xxx|hevc|x265", re.I)


def parse_labels(page):
    found = re.findall(
        r"/search/label/([^\"'?&#<>\s/]+)",
        page
    )

    labels = []
    seen = set()

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

        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0"}
        )

        page = urllib.request.urlopen(
            req,
            timeout=30
        ).read().decode("utf-8", "ignore")

        labels = parse_labels(page)

        log(f"  Found {len(labels)} labels on the blog")

    except Exception as e:  # noqa
        log("  Could not read blog labels:", e)

    if len(labels) < 3:
        have = {l.lower() for l in labels}

        labels += [
            l for l in FALLBACK_LABELS
            if l.lower() not in have
        ]

    labels = [
        l for l in labels
        if not BLOCKED_LABEL.search(l)
    ]

    return labels[:60]


def pick_labels(raw, site_labels):
    canon = {
        l.lower(): l
        for l in site_labels
    }

    out = []

    for r in raw or []:
        c = canon.get(
            str(r).strip().lower()
        )

        if c and c not in out:
            out.append(c)

    return out[:4]


# ---------- Gemini ----------
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
    r"(?<!\d)(19[5-9]\d|20[0-4]\d)(?!\d)"
)


def find_year(filename):
    """Release year only if the file name contains one."""
    m = YEAR_RE.search(
        Path(filename).stem
    )

    return int(m.group(1)) if m else None


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
        YEAR_RE.sub(" ", t)
    ).strip()

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

    for model in dict.fromkeys([
        GEMINI_MODEL,
        "gemini-flash-latest"
    ]):
        try:
            resp = retry(
                lambda: gclient.models.generate_content(
                    model=model,
                    contents=[prompt, *parts],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json"
                    )
                ),
                tries=2
            )

            data = json.loads(
                re.sub(
                    r"^```json|```$",
                    "",
                    resp.text.strip()
                ).strip()
            )

            log(
                "  Gemini model used:",
                model
            )

            break

        except Exception as e:  # noqa
            log(
                f"  Gemini model {model} failed: {e}"
            )

    if not data:
        log("  Using fallback text.")

    faq = [
        f for f in (data.get("faq") or [])
        if isinstance(f, dict)
        and f.get("q")
        and f.get("a")
    ]

    return {
        "title": data.get("title")
        or hint
        or "Untitled Film",

        "tagline": data.get("tagline")
        or "",

        "synopsis": as_paragraphs(
            data.get("synopsis")
        ) or ["An original film."],

        "review": as_paragraphs(
            data.get("review")
        ),

        "themes": [
            str(t)
            for t in (data.get("themes") or [])
        ][:5],

        "faq": faq[:4],

        "genres": data.get("genres")
        or ["Drama"],

        "language": data.get("language")
        or LANGUAGE_HINT
        or "Unknown",

        "release_year": year,

        "content_rating": data.get("content_rating")
        or "General audience",

        "tags": data.get("tags")
        or [],

        "labels": pick_labels(
            data.get("labels"),
            site_labels
        ),
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


# ============================================================
# CUSTOM HDVB-STYLE VIDEO PLAYER
# ONLY THIS SECTION REPLACES THE OLD IFRAME PLAYER
# ============================================================
def build_html(meta, thumb_id, shot_ids, outputs, fps, dur):
    e = html.escape

    title = e(meta["title"])

    year = meta["release_year"]
    ytxt = f" ({year})" if year else ""

    lang_known = (
        meta["language"]
        and meta["language"].lower() != "unknown"
    )

    lang = e(meta["language"])

    lang_tag = (
        f' <span style="color:#f2f200">{{{lang}}}</span>'
        if lang_known else ""
    )

    genres = ", ".join(
        e(g)
        for g in meta["genres"]
    )

    fps_txt = fmt_fps(fps)

    qualities = " - ".join(
        f"{h}p"
        for h, _, _ in outputs
    )

    sizes = " - ".join(
        human(s)
        for _, _, s in outputs
    )

    # Same player-file selection as the old player:
    # use 720p if available, otherwise use the last output.
    player_id = next(
        (
            fid
            for h, fid, _
            in outputs
            if h == 720
        ),
        outputs[-1][1]
    )

    # Direct public Google Drive video URL
    player_src = (
        "https://drive.usercontent.google.com/download?"
        f"id={player_id}&export=download&confirm=t"
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
        "background:linear-gradient(90deg,#57a51c,#1f4fb4);"
        "box-shadow:0 8px 14px rgba(0,0,0,.45);"
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
        'border-top:1px solid rgba(255,255,255,.6);'
        'margin:22px 0"/>'
    )

    h3 = '<h3 style="text-align:center">{}</h3>'

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
        f"<b>Runtime:</b> {fmt_runtime(dur)}",
        f"<b>Genres:</b> {genres}",
        f"<b>Content Advisory:</b> "
        f"{e(meta['content_rating'])}",
        f"<b>Quality:</b> {qualities}",
        f"<b>Frame Rate:</b> {fps_txt}fps",
        f"<b>Size:</b> {sizes}",
    ]

    parts = []

    # Thumbnail
    parts.append(
        f'<div style="text-align:center">'
        f'<img src="{img_url(thumb_id)}" '
        f'alt="{title}" '
        f'width="270" '
        f'style="max-width:60%;height:auto;'
        f'border-radius:8px"/>'
        f'</div>'
    )

    parts.append(
        f'<p style="text-align:center">'
        f'<b>{title}{ytxt}</b>'
        f'{" - " + lang + " film" if lang_known else ""}'
        f'</p>'
    )

    if meta["tagline"]:
        parts.append(
            f'<p style="text-align:center">'
            f'<i>{e(meta["tagline"])}</i>'
            f'</p>'
        )

    parts.append(
        f"<p>{syn[0]}</p>"
    )

    parts.append(
        h3.format("Movie Info")
    )

    parts.append(
        "<p>" +
        "<br/>".join(info) +
        "</p>"
    )

    parts.append(
        h3.format("Movie Synopsis / Plot")
    )

    parts += [
        f"<p>{p}</p>"
        for p in syn
    ]

    # ========================================================
    # NEW CUSTOM HDVB-STYLE PLAYER
    # ========================================================
    player_html = f"""
<style>
.mv-player{{
    position:relative;
    width:100%;
    max-width:100%;
    background:#000;
    overflow:hidden;
    border-radius:4px;
    box-shadow:0 8px 30px rgba(0,0,0,.55);
    font-family:Arial,Helvetica,sans-serif;
    user-select:none;
    -webkit-user-select:none;
}}

.mv-player *{{
    box-sizing:border-box;
}}

.mv-video{{
    display:block;
    width:100%;
    height:auto;
    min-height:240px;
    max-height:80vh;
    background:#000;
    object-fit:contain;
    cursor:pointer;
}}

.mv-center-play{{
    position:absolute;
    left:50%;
    top:50%;
    transform:translate(-50%,-50%);
    width:76px;
    height:76px;
    border-radius:50%;
    border:0;
    background:#58a51c;
    color:#fff;
    cursor:pointer;
    display:flex;
    align-items:center;
    justify-content:center;
    font-size:31px;
    padding-left:5px;
    box-shadow:0 4px 18px rgba(0,0,0,.6);
    transition:transform .15s ease,opacity .2s ease;
    z-index:5;
}}

.mv-center-play:hover{{
    transform:translate(-50%,-50%) scale(1.08);
}}

.mv-player.mv-playing .mv-center-play{{
    opacity:0;
    pointer-events:none;
}}

.mv-controls{{
    position:absolute;
    left:0;
    right:0;
    bottom:0;
    padding:42px 12px 10px;
    background:linear-gradient(
        to bottom,
        transparent,
        rgba(0,0,0,.78) 45%,
        rgba(0,0,0,.96)
    );
    z-index:10;
    opacity:1;
    transition:opacity .25s ease;
}}

.mv-player.mv-playing:not(:hover) .mv-controls{{
    opacity:.35;
}}

.mv-player:hover .mv-controls,
.mv-player.mv-paused .mv-controls{{
    opacity:1;
}}

.mv-progress-area{{
    position:relative;
    width:100%;
    height:16px;
    cursor:pointer;
    padding:6px 0;
}}

.mv-progress-track{{
    position:absolute;
    left:0;
    right:0;
    top:6px;
    height:4px;
    border-radius:4px;
    background:rgba(255,255,255,.3);
}}

.mv-buffer{{
    position:absolute;
    left:0;
    top:0;
    height:100%;
    width:0;
    border-radius:4px;
    background:rgba(255,255,255,.42);
}}

.mv-progress-fill{{
    position:absolute;
    left:0;
    top:0;
    height:100%;
    width:0;
    border-radius:4px;
    background:#58a51c;
}}

.mv-progress-handle{{
    position:absolute;
    top:50%;
    left:0;
    width:12px;
    height:12px;
    margin-left:-6px;
    margin-top:-6px;
    border-radius:50%;
    background:#fff;
    box-shadow:0 1px 5px rgba(0,0,0,.7);
    opacity:0;
}}

.mv-progress-area:hover .mv-progress-handle{{
    opacity:1;
}}

.mv-control-row{{
    display:flex;
    align-items:center;
    gap:9px;
    width:100%;
}}

.mv-control-btn{{
    border:0;
    background:transparent;
    color:#fff;
    width:34px;
    height:34px;
    padding:0;
    display:flex;
    align-items:center;
    justify-content:center;
    cursor:pointer;
    font-size:20px;
    flex-shrink:0;
}}

.mv-control-btn:hover{{
    color:#58a51c;
}}

.mv-time{{
    color:#fff;
    font-size:13px;
    white-space:nowrap;
    line-height:34px;
}}

.mv-spacer{{
    flex:1;
}}

.mv-volume-wrap{{
    display:flex;
    align-items:center;
    gap:4px;
}}

.mv-volume{{
    width:76px;
    height:4px;
    accent-color:#58a51c;
    cursor:pointer;
}}

.mv-settings-wrap{{
    position:relative;
}}

.mv-settings-menu{{
    position:absolute;
    right:0;
    bottom:42px;
    width:145px;
    background:rgba(20,20,20,.97);
    border:1px solid rgba(255,255,255,.15);
    border-radius:5px;
    padding:6px 0;
    box-shadow:0 6px 20px rgba(0,0,0,.6);
    display:none;
    z-index:20;
}}

.mv-settings-menu.mv-open{{
    display:block;
}}

.mv-settings-title{{
    padding:7px 13px;
    color:#aaa;
    font-size:11px;
    text-transform:uppercase;
}}

.mv-speed{{
    display:block;
    width:100%;
    border:0;
    background:transparent;
    color:#fff;
    text-align:left;
    padding:8px 13px;
    cursor:pointer;
    font-size:13px;
}}

.mv-speed:hover,
.mv-speed.mv-active{{
    background:#58a51c;
    color:#fff;
}}

.mv-fullscreen{{
    font-size:19px;
}}

@media(max-width:600px){{
    .mv-controls{{
        padding:34px 7px 6px;
    }}

    .mv-control-row{{
        gap:3px;
    }}

    .mv-control-btn{{
        width:31px;
        height:31px;
        font-size:18px;
    }}

    .mv-time{{
        font-size:11px;
    }}

    .mv-volume{{
        width:52px;
    }}

    .mv-volume-wrap{{
        display:none;
    }}

    .mv-center-play{{
        width:64px;
        height:64px;
        font-size:26px;
    }}

    .mv-video{{
        min-height:200px;
    }}
}}
</style>

<div class="mv-player mv-paused" id="mvPlayer">
    <video
        class="mv-video"
        id="mvVideo"
        preload="metadata"
        playsinline
        webkit-playsinline
        controlslist="nodownload"
    >
        <source src="{player_src}" type="video/mp4">
        Your browser does not support HTML5 video.
    </video>

    <button
        class="mv-center-play"
        id="mvCenterPlay"
        type="button"
        aria-label="Play"
    >▶</button>

    <div class="mv-controls">

        <div
            class="mv-progress-area"
            id="mvProgressArea"
            title="Seek"
        >
            <div class="mv-progress-track">
                <div
                    class="mv-buffer"
                    id="mvBuffer"
                ></div>

                <div
                    class="mv-progress-fill"
                    id="mvProgressFill"
                ></div>

                <div
                    class="mv-progress-handle"
                    id="mvProgressHandle"
                ></div>
            </div>
        </div>

        <div class="mv-control-row">

            <button
                class="mv-control-btn"
                id="mvPlayBtn"
                type="button"
                aria-label="Play/Pause"
            >
                <span id="mvPlayIcon">▶</span>
            </button>

            <span
                class="mv-time"
                id="mvCurrentTime"
            >00:00</span>

            <span class="mv-time">/</span>

            <span
                class="mv-time"
                id="mvDuration"
            >00:00</span>

            <div class="mv-volume-wrap">

                <button
                    class="mv-control-btn"
                    id="mvMuteBtn"
                    type="button"
                    aria-label="Mute"
                >🔊</button>

                <input
                    class="mv-volume"
                    id="mvVolume"
                    type="range"
                    min="0"
                    max="1"
                    step="0.05"
                    value="1"
                    aria-label="Volume"
                >

            </div>

            <div class="mv-spacer"></div>

            <div class="mv-settings-wrap">

                <button
                    class="mv-control-btn"
                    id="mvSettingsBtn"
                    type="button"
                    aria-label="Settings"
                >⚙</button>

                <div
                    class="mv-settings-menu"
                    id="mvSettings"
                >
                    <div class="mv-settings-title">
                        Playback Speed
                    </div>

                    <button
                        class="mv-speed mv-active"
                        data-speed="1"
                        type="button"
                    >1x</button>

                    <button
                        class="mv-speed"
                        data-speed="1.25"
                        type="button"
                    >1.25x</button>

                    <button
                        class="mv-speed"
                        data-speed="1.5"
                        type="button"
                    >1.5x</button>

                    <button
                        class="mv-speed"
                        data-speed="2"
                        type="button"
                    >2x</button>
                </div>

            </div>

            <button
                class="mv-control-btn mv-fullscreen"
                id="mvFullscreenBtn"
                type="button"
                aria-label="Fullscreen"
            >⛶</button>

        </div>
    </div>
</div>

<script>
(function () {{
    var player = document.getElementById("mvPlayer");
    var video = document.getElementById("mvVideo");
    var centerPlay = document.getElementById("mvCenterPlay");
    var playBtn = document.getElementById("mvPlayBtn");
    var playIcon = document.getElementById("mvPlayIcon");

    var currentTime = document.getElementById("mvCurrentTime");
    var duration = document.getElementById("mvDuration");

    var muteBtn = document.getElementById("mvMuteBtn");
    var volume = document.getElementById("mvVolume");

    var progressArea = document.getElementById("mvProgressArea");
    var progressFill = document.getElementById("mvProgressFill");
    var progressHandle = document.getElementById("mvProgressHandle");
    var buffer = document.getElementById("mvBuffer");

    var settingsBtn = document.getElementById("mvSettingsBtn");
    var settings = document.getElementById("mvSettings");

    var fullscreenBtn = document.getElementById("mvFullscreenBtn");

    function formatTime(sec) {{
        if (!isFinite(sec) || sec < 0) {{
            return "00:00";
        }}

        sec = Math.floor(sec);

        var h = Math.floor(sec / 3600);
        var m = Math.floor((sec % 3600) / 60);
        var s = sec % 60;

        var mm = m < 10 ? "0" + m : String(m);
        var ss = s < 10 ? "0" + s : String(s);

        if (h > 0) {{
            var hh = h < 10 ? "0" + h : String(h);
            return hh + ":" + mm + ":" + ss;
        }}

        return mm + ":" + ss;
    }}

    function updatePlayState() {{
        if (video.paused) {{
            player.classList.remove("mv-playing");
            player.classList.add("mv-paused");
            playIcon.textContent = "▶";
        }} else {{
            player.classList.add("mv-playing");
            player.classList.remove("mv-paused");
            playIcon.textContent = "❚❚";
        }}
    }}

    function togglePlay() {{
        if (video.paused) {{
            var p = video.play();

            if (p && p.catch) {{
                p.catch(function () {{}});
            }}
        }} else {{
            video.pause();
        }}
    }}

    function updateProgress() {{
        if (!isFinite(video.duration) || video.duration <= 0) {{
            return;
        }}

        var percent =
            (video.currentTime / video.duration) * 100;

        progressFill.style.width = percent + "%";
        progressHandle.style.left = percent + "%";

        currentTime.textContent =
            formatTime(video.currentTime);

        duration.textContent =
            formatTime(video.duration);
    }}

    function updateBuffer() {{
        if (!video.buffered.length || !isFinite(video.duration)) {{
            return;
        }}

        try {{
            var end =
                video.buffered.end(
                    video.buffered.length - 1
                );

            var percent =
                (end / video.duration) * 100;

            buffer.style.width =
                Math.min(100, percent) + "%";
        }} catch (e) {{}}
    }}

    function seek(event) {{
        if (!isFinite(video.duration) || video.duration <= 0) {{
            return;
        }}

        var rect =
            progressArea.getBoundingClientRect();

        var x =
            event.clientX - rect.left;

        var percent =
            Math.max(
                0,
                Math.min(1, x / rect.width)
            );

        video.currentTime =
            percent * video.duration;
    }}

    playBtn.addEventListener(
        "click",
        togglePlay
    );

    centerPlay.addEventListener(
        "click",
        togglePlay
    );

    video.addEventListener(
        "click",
        togglePlay
    );

    video.addEventListener(
        "play",
        updatePlayState
    );

    video.addEventListener(
        "pause",
        updatePlayState
    );

    video.addEventListener(
        "ended",
        updatePlayState
    );

    video.addEventListener(
        "loadedmetadata",
        function () {{
            duration.textContent =
                formatTime(video.duration);

            updateProgress();
        }}
    );

    video.addEventListener(
        "timeupdate",
        updateProgress
    );

    video.addEventListener(
        "progress",
        updateBuffer
    );

    video.addEventListener(
        "durationchange",
        function () {{
            duration.textContent =
                formatTime(video.duration);
        }}
    );

    progressArea.addEventListener(
        "click",
        seek
    );

    muteBtn.addEventListener(
        "click",
        function () {{
            video.muted = !video.muted;

            if (video.muted) {{
                muteBtn.textContent = "🔇";
            }} else {{
                muteBtn.textContent = "🔊";
            }}
        }}
    );

    volume.addEventListener(
        "input",
        function () {{
            video.volume =
                parseFloat(volume.value);

            if (video.volume === 0) {{
                video.muted = true;
                muteBtn.textContent = "🔇";
            }} else {{
                video.muted = false;
                muteBtn.textContent = "🔊";
            }}
        }}
    );

    settingsBtn.addEventListener(
        "click",
        function (event) {{
            event.stopPropagation();
            settings.classList.toggle("mv-open");
        }}
    );

    var speedButtons =
        settings.querySelectorAll(".mv-speed");

    for (
        var i = 0;
        i < speedButtons.length;
        i++
    ) {{
        speedButtons[i].addEventListener(
            "click",
            function () {{
                var speed =
                    parseFloat(
                        this.getAttribute("data-speed")
                    );

                video.playbackRate = speed;

                for (
                    var j = 0;
                    j < speedButtons.length;
                    j++
                ) {{
                    speedButtons[j].classList.remove(
                        "mv-active"
                    );
                }}

                this.classList.add("mv-active");

                settings.classList.remove(
                    "mv-open"
                );
            }}
        );
    }}

    document.addEventListener(
        "click",
        function (event) {{
            if (
                !settings.contains(event.target) &&
                event.target !== settingsBtn
            ) {{
                settings.classList.remove(
                    "mv-open"
                );
            }}
        }}
    );

    function enterFullscreen() {{
        if (player.requestFullscreen) {{
            player.requestFullscreen();
        }} else if (player.webkitRequestFullscreen) {{
            player.webkitRequestFullscreen();
        }} else if (video.webkitEnterFullscreen) {{
            video.webkitEnterFullscreen();
        }}
    }}

    function exitFullscreen() {{
        if (document.exitFullscreen) {{
            document.exitFullscreen();
        }} else if (document.webkitExitFullscreen) {{
            document.webkitExitFullscreen();
        }}
    }}

    fullscreenBtn.addEventListener(
        "click",
        function () {{
            if (
                document.fullscreenElement ||
                document.webkitFullscreenElement
            ) {{
                exitFullscreen();
            }} else {{
                enterFullscreen();
            }}
        }}
    );

    player.addEventListener(
        "dblclick",
        function (event) {{
            if (
                event.target === video ||
                event.target === centerPlay
            ) {{
                if (
                    document.fullscreenElement ||
                    document.webkitFullscreenElement
                ) {{
                    exitFullscreen();
                }} else {{
                    enterFullscreen();
                }}
            }}
        }}
    );

    document.addEventListener(
        "keydown",
        function (event) {{
            if (
                event.target.tagName === "INPUT" ||
                event.target.tagName === "TEXTAREA"
            ) {{
                return;
            }}

            if (
                event.code === "Space" &&
                document.activeElement === document.body
            ) {{
                event.preventDefault();
                togglePlay();
            }}

            if (
                event.key === "ArrowRight" &&
                !video.paused
            ) {{
                video.currentTime =
                    Math.min(
                        video.duration,
                        video.currentTime + 5
                    );
            }}

            if (
                event.key === "ArrowLeft" &&
                !video.paused
            ) {{
                video.currentTime =
                    Math.max(
                        0,
                        video.currentTime - 5
                    );
            }}
        }}
    );

    video.volume = 1;
    video.playbackRate = 1;

    updatePlayState();
}})();
</script>
"""

    parts.append(
        h3.format(
            f"Watch {title} Online"
        )
    )

    parts.append(player_html)

    # ---------- review ----------
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

    # ---------- themes ----------
    if meta["themes"]:
        parts.append(
            h3.format("Themes")
        )

        parts.append(
            "<ul>" +
            "".join(
                f"<li>{e(t)}</li>"
                for t in meta["themes"]
            ) +
            "</ul>"
        )

    # ---------- screenshots ----------
    parts.append(
        h3.format("Screenshots")
    )

    for fid in shot_ids:
        parts.append(
            f'<p style="text-align:center">'
            f'<img src="{img_url(fid)}" '
            f'alt="{title} screenshot" '
            f'style="max-width:100%;height:auto"/>'
            f'</p>'
        )

    parts.append(hr)

    # ---------- download links ----------
    parts.append(
        h3.format("Download Links")
    )

    for h, fid, size in outputs:

        direct = (
            f"https://drive.usercontent.google.com/download?"
            f"id={fid}"
            "&amp;export=download"
            "&amp;confirm=t"
        )

        parts.append(
            f'<h4 style="{head}">'
            f'{title}{ytxt}{lang_tag} '
            f'{h}p x264 {fps_txt}fps '
            f'[{human(size)}]'
            f'</h4>'
        )

        parts.append(
            f'<a class="mv-dl" '
            f'data-fid="{fid}" '
            f'href="{direct}" '
            f'rel="noopener" '
            f'style="{btn}">'
            '&#11015;&#9889;DOWNLOAD NOW&#9889;&#11015;'
            f'</a>'
        )

    parts.append(hr)

    # ---------- FAQ ----------
    if meta["faq"]:
        parts.append(
            h3.format(
                f"{title} - FAQ"
            )
        )

        for f in meta["faq"]:
            parts.append(
                f"<h4>{e(str(f['q']))}</h4>"
                f"<p>{e(str(f['a']))}</p>"
            )

    # ---------- winding up ----------
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


# ---------- main pipeline ----------
def process(video, processed_folder, output_folder):
    name = video["name"]

    log(
        f"\n=== Processing: {name} ==="
    )

    job = WORK / video["id"]
    job.mkdir(
        parents=True,
        exist_ok=True
    )

    src = str(
        job / "source.mp4"
    )

    slug = (
        re.sub(
            r"[^a-zA-Z0-9]+",
            "-",
            Path(name).stem
        )
        .strip("-")
        .lower()
        or "movie"
    )

    log("Downloading original...")

    download(
        video["id"],
        src
    )

    dur, w, h, fps = probe(src)

    short = min(w, h)

    log(
        f"Duration {dur / 60:.1f} min, "
        f"{w}x{h}, "
        f"{fps:.2f} fps"
    )

    log(
        "Making screenshots and thumbnail..."
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

    log(
        "Analysing with Gemini..."
    )

    site_labels = get_site_labels()

    frames, audio = analysis_inputs(
        src,
        dur,
        job
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

    targets = sorted({
        t for t in RESOLUTIONS
        if t <= short * 1.05
    }) or [short]

    outputs = []

    for t in targets:

        out = str(
            job / f"{slug}_{t}p.mp4"
        )

        log(
            f"Converting to {t}p..."
        )

        transcode(
            src,
            t,
            w,
            h,
            out
        )

        size = os.path.getsize(out)

        log(
            f"Uploading {t}p "
            f"({human(size)})..."
        )

        fid = upload_public(
            out,
            output_folder,
            "video/mp4"
        )

        outputs.append(
            (t, fid, size)
        )

        os.remove(out)

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

    labels = list(
        meta["labels"]
    )

    if not labels:
        unc = next(
            (
                l
                for l in site_labels
                if l.lower() == "uncategorized"
            ),
            None
        )

        labels = (
            [unc]
            if unc
            else
            [g for g in meta["genres"]][:2]
            + [meta["language"]]
        )

    labels = [
        str(l)[:40]
        for l in labels
        if l
    ][:8]

    content = build_html(
        meta,
        thumb_id,
        shot_ids,
        outputs,
        fps,
        dur
    )

    ytxt = (
        f' ({meta["release_year"]})'
        if meta["release_year"]
        else ""
    )

    ltxt = (
        f' {meta["language"]}'
        if meta["language"].lower() != "unknown"
        else ""
    )

    body = {
        "kind": "blogger#post",
        "title": (
            f'{meta["title"]}'
            f'{ytxt}'
            f'{ltxt} '
            f'Movie - Watch Online & Download'
        ),
        "content": content,
        "labels": labels
    }

    post = retry(
        lambda: blogger.posts().insert(
            blogId=BLOG_ID,
            body=body,
            isDraft=not PUBLISH
        ).execute()
    )

    log(
        "Blogger post created:",
        post.get("url") or post.get("id"),
        "(DRAFT)" if not PUBLISH else "(PUBLISHED)"
    )

    drive.files().update(
        fileId=video["id"],
        addParents=processed_folder,
        removeParents=INPUT_FOLDER,
        fields="id"
    ).execute()

    shutil.rmtree(
        job,
        ignore
