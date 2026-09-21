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
RESOLUTIONS = [
    int(x)
    for x in os.environ.get("RESOLUTIONS", "480,720,1080").split(",")
    if x.strip()
]
MAX_VIDEOS = int(os.environ.get("MAX_VIDEOS", "1"))
PUBLISH = os.environ.get("PUBLISH", "false").lower() == "true"
LANGUAGE_HINT = os.environ.get("LANGUAGE_HINT", "").strip()
AUDIO_MINUTES = int(os.environ.get("AUDIO_MINUTES", "10"))
SCREENSHOTS = int(os.environ.get("SCREENSHOTS", "6"))
WAIT_SECONDS = int(os.environ.get("WAIT_SECONDS", "20"))
DIRECTOR_NAME = os.environ.get("DIRECTOR_NAME", "").strip()

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


# ---------- Drive helpers ----------
def ensure_folder(name):
    q = (
        f"'{INPUT_FOLDER}' in parents and "
        f"name='{name}' and "
        "mimeType='application/vnd.google-apps.folder' and "
        "trashed=false"
    )

    res = drive.files().list(
        q=q,
        fields="files(id)"
    ).execute()

    if res["files"]:
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
        f"'{INPUT_FOLDER}' in parents and "
        "mimeType contains 'video/' and "
        "trashed=false"
    )

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
                log(
                    f"  download "
                    f"{int(status.progress() * 100)}%"
                )


def upload_public(path, parent, mime):
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
        _, resp = retry(req.next_chunk)

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
        "stream=width,height,avg_frame_rate,r_frame_rate:"
        "format=duration",
        "-of",
        "json",
        path,
    ])

    d = json.loads(out)

    st = d["streams"][0]

    fps = parse_fps(
        st.get("avg_frame_rate"),
        st.get("r_frame_rate"),
    )

    return (
        float(d["format"]["duration"]),
        int(st["width"]),
        int(st["height"]),
        fps,
    )


def make_screenshots(src, dur, outdir):
    files = []

    for i in range(SCREENSHOTS):
        t = dur * (i + 1) / (SCREENSHOTS + 1)

        p = str(
            outdir / f"shot_{i + 1}.jpg"
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


def make_thumbnail(src, dur, w, h, outdir):
    """
    9:16 portrait thumbnail, 720x1280,
    centre crop from a frame at 35%.
    """

    if w * 16 >= h * 9:
        ch = h // 2 * 2
        cw = int(h * 9 / 16) // 2 * 2
    else:
        cw = w // 2 * 2
        ch = int(w * 16 / 9) // 2 * 2

    p = str(
        outdir / "thumb_9x16.jpg"
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


def analysis_inputs(src, dur, outdir):
    frames = []

    n = 12

    for i in range(n):
        t = dur * (i + 1) / (n + 1)

        p = outdir / f"an_{i}.jpg"

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

        frames.append(p.read_bytes())

    audio = outdir / "an_audio.mp3"

    start = dur * 0.10

    run([
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-ss",
        f"{start:.2f}",
        "-t",
        str(AUDIO_MINUTES * 60),
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

    return frames, audio.read_bytes()


def transcode(src, target, w, h, out):
    """
    target = length of the SHORT side (480/720/1080):
    works for landscape and vertical.
    """

    crf = CRF.get(target, 23)

    if w >= h:
        vf = f"scale=-2:{target}"
    else:
        vf = f"scale={target}:-2"

    run([
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-stats",
        "-i",
        src,
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
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
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        out,
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

BLOCKED_LABEL = re.compile(
    r"18\+|adult|xxx|hevc|x265",
    re.I
)


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
    """
    Read the real label names from the blog's menu,
    fall back to a built-in list.
    """

    labels = []

    try:
        url = blogger.blogs().get(
            blogId=BLOG_ID
        ).execute()["url"]

        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0"
            },
        )

        page = urllib.request.urlopen(
            req,
            timeout=30
        ).read().decode(
            "utf-8",
            "ignore"
        )

        labels = parse_labels(page)

        log(
            f"  Found {len(labels)} labels on the blog"
        )

    except Exception as e:  # noqa
        log(
            "  Could not read blog labels:",
            e
        )

    if len(labels) < 3:
        have = {
            l.lower()
            for l in labels
        }

        labels += [
            l
            for l in FALLBACK_LABELS
            if l.lower() not in have
        ]

    labels = [
        l
        for l in labels
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
    """
    Release year only if the file name contains one.
    """

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


def analyze(
    filename_hint,
    frames,
    audio_bytes,
    site_labels
):
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
                    contents=[
                        prompt,
                        *parts
                    ],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json"
                    ),
                ),
                tries=2,
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
        f
        for f in (data.get("faq") or [])
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

        "synopsis": (
            as_paragraphs(
                data.get("synopsis")
            )
            or ["An original film."]
        ),

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

        "content_rating": (
            data.get("content_rating")
            or "General audience"
        ),

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
    return (
        f"https://lh3.googleusercontent.com/d/{fid}"
    )


# ============================================================
# NEW GOOGLE DRIVE VIDEO PLAYER
# ============================================================

def drive_video_url(file_id):
    """
    Creates the Google Drive direct-download/playback URL
    used by the HTML5 video element.
    """

    return (
        "https://drive.usercontent.google.com/download?"
        f"id={file_id}&export=download&confirm=t"
    )


def build_drive_player(outputs):

    # Find resolutions automatically
    quality_ids = {}

    for height, file_id, size in outputs:
        if height in (480, 720, 1080):
            quality_ids[height] = file_id

    # Default quality:
    # 720p -> 1080p -> 480p -> first available
    default_id = (
        quality_ids.get(720)
        or quality_ids.get(1080)
        or quality_ids.get(480)
        or outputs[-1][1]
    )

    default_url = drive_video_url(default_id)

    url_480 = (
        drive_video_url(quality_ids[480])
        if 480 in quality_ids
        else ""
    )

    url_720 = (
        drive_video_url(quality_ids[720])
        if 720 in quality_ids
        else ""
    )

    url_1080 = (
        drive_video_url(quality_ids[1080])
        if 1080 in quality_ids
        else ""
    )

    # --------------------------------------------------------
    # Quality button HTML
    # --------------------------------------------------------

    quality_buttons = []

    quality_buttons.append(
        """
        <button class="gd-quality-btn active"
                data-quality="auto">
            ✓ Auto
        </button>
        """
    )

    if url_480:
        quality_buttons.append(
            """
            <button class="gd-quality-btn"
                    data-quality="480">
                480p
            </button>
            """
        )

    if url_720:
        quality_buttons.append(
            """
            <button class="gd-quality-btn"
                    data-quality="720">
                720p
            </button>
            """
        )

    if url_1080:
        quality_buttons.append(
            """
            <button class="gd-quality-btn"
                    data-quality="1080">
                1080p
            </button>
            """
        )

    quality_html = "\n".join(
        quality_buttons
    )

    # --------------------------------------------------------
    # Full player
    # --------------------------------------------------------

    return f"""
<style>

.gd-player {{
    position: relative;
    width: 100%;
    max-width: 1100px;
    margin: 20px auto;
    background: #000;
    overflow: hidden;
    border-radius: 8px;
    font-family: Arial, Helvetica, sans-serif;
    user-select: none;
}}

.gd-video {{
    display: block;
    width: 100%;
    height: auto;
    min-height: 250px;
    background: #000;
    object-fit: contain;
    transform-origin: center center;
    transition: transform .18s ease;
}}

.gd-center-play {{
    position: absolute;
    top: 50%;
    left: 50%;
    transform: translate(-50%, -50%);

    width: 72px;
    height: 72px;

    border: 0;
    border-radius: 50%;

    background: #19d66b;
    color: #000;

    font-size: 29px;
    font-weight: bold;

    cursor: pointer;

    z-index: 20;

    box-shadow:
        0 4px 25px rgba(0,0,0,.45);
}}

.gd-controls {{
    position: absolute;

    left: 0;
    right: 0;
    bottom: 0;

    padding: 10px 13px 12px;

    background:
        linear-gradient(
            transparent,
            rgba(0,0,0,.94)
        );

    z-index: 15;
}}

.gd-progress {{
    width: 100%;
    height: 5px;

    background:
        rgba(255,255,255,.25);

    border-radius: 10px;

    cursor: pointer;

    margin-bottom: 9px;
}}

.gd-progress-fill {{
    width: 0%;
    height: 100%;

    background: #19d66b;

    border-radius: 10px;
}}

.gd-control-row {{
    display: flex;
    align-items: center;
    gap: 9px;
}}

.gd-btn {{
    background: transparent;
    border: 0;

    color: #fff;

    font-size: 19px;

    cursor: pointer;

    padding: 4px;

    line-height: 1;
}}

.gd-btn:hover {{
    color: #19d66b;
}}

.gd-time {{
    color: #fff;
    font-size: 13px;
    white-space: nowrap;
}}

.gd-spacer {{
    flex: 1;
}}

.gd-volume {{
    width: 75px;
    cursor: pointer;
}}

.gd-settings {{
    position: relative;
}}

.gd-settings-menu {{
    position: absolute;

    right: 0;
    bottom: 43px;

    width: 205px;

    background:
        rgba(18,18,18,.98);

    border-radius: 8px;

    padding: 9px 0;

    display: none;

    box-shadow:
        0 5px 30px rgba(0,0,0,.65);

    z-index: 50;
}}

.gd-settings-menu.show {{
    display: block;
}}

.gd-menu-title {{
    color: #999;

    font-size: 11px;

    padding:
        6px 15px 8px;

    text-transform: uppercase;

    letter-spacing: .5px;
}}

.gd-quality-btn {{
    display: block;

    width: 100%;

    background: transparent;
    border: 0;

    color: #fff;

    padding: 9px 15px;

    text-align: left;

    cursor: pointer;

    font-size: 14px;
}}

.gd-quality-btn:hover {{
    background:
        rgba(255,255,255,.08);
}}

.gd-quality-btn.active {{
    color: #19d66b;
}}

.gd-scale-controls {{
    display: flex;

    align-items: center;
    justify-content: space-between;

    padding: 6px 12px 10px;
}}

.gd-scale-controls button {{
    width: 35px;
    height: 31px;

    border: 0;
    border-radius: 5px;

    background: #292929;
    color: #fff;

    font-size: 21px;

    cursor: pointer;
}}

.gd-scale-controls button:hover {{
    background: #19d66b;
    color: #000;
}}

.gd-scale-value {{
    color: #fff;

    font-size: 13px;

    min-width: 55px;

    text-align: center;
}}

.gd-player:fullscreen {{
    width: 100vw;
    height: 100vh;

    border-radius: 0;

    display: flex;
    align-items: center;
    justify-content: center;
}}

.gd-player:fullscreen .gd-video {{
    max-height: 100vh;
    width: 100%;
}}

@media (max-width: 600px) {{

    .gd-center-play {{
        width: 58px;
        height: 58px;
        font-size: 24px;
    }}

    .gd-volume {{
        width: 55px;
    }}

    .gd-time {{
        font-size: 11px;
    }}

    .gd-control-row {{
        gap: 5px;
    }}

    .gd-settings-menu {{
        width: 190px;
    }}
}}

</style>


<div class="gd-player" id="gdPlayer">

    <video
        id="gdVideo"
        class="gd-video"
        playsinline
        preload="metadata"
        controlslist="nodownload"
        src="{html.escape(default_url, quote=True)}">
    </video>

    <button
        type="button"
        class="gd-center-play"
        id="gdCenterPlay"
        aria-label="Play">
        ▶
    </button>

    <div class="gd-controls">

        <div
            class="gd-progress"
            id="gdProgress"
            title="Seek">

            <div
                class="gd-progress-fill"
                id="gdProgressFill">
            </div>

        </div>

        <div class="gd-control-row">

            <button
                type="button"
                class="gd-btn"
                id="gdPlay"
                aria-label="Play/Pause">
                ▶
            </button>

            <span class="gd-time">

                <span id="gdCurrent">
                    00:00
                </span>

                /

                <span id="gdDuration">
                    00:00
                </span>

            </span>

            <button
                type="button"
                class="gd-btn"
                id="gdMute"
                aria-label="Mute">
                🔊
            </button>

            <input
                class="gd-volume"
                id="gdVolume"
                type="range"
                min="0"
                max="1"
                step="0.05"
                value="1"
                aria-label="Volume">

            <div class="gd-spacer"></div>

            <div class="gd-settings">

                <button
                    type="button"
                    class="gd-btn"
                    id="gdSettings"
                    aria-label="Settings">
                    ⚙
                </button>

                <div
                    class="gd-settings-menu"
                    id="gdSettingsMenu">

                    <div class="gd-menu-title">
                        Quality
                    </div>

                    {quality_html}

                    <div
                        class="gd-menu-title"
                        style="margin-top:8px;">
                        Viewer Scale
                    </div>

                    <div class="gd-scale-controls">

                        <button
                            type="button"
                            id="gdZoomOut"
                            aria-label="Zoom out">
                            −
                        </button>

                        <span
                            class="gd-scale-value"
                            id="gdScaleValue">
                            100%
                        </span>

                        <button
                            type="button"
                            id="gdZoomIn"
                            aria-label="Zoom in">
                            +
                        </button>

                    </div>

                </div>

            </div>

            <button
                type="button"
                class="gd-btn"
                id="gdFullscreen"
                aria-label="Fullscreen">
                ⛶
            </button>

        </div>

    </div>

</div>


<script>
(function () {{

    "use strict";

    var video =
        document.getElementById("gdVideo");

    var player =
        document.getElementById("gdPlayer");

    var playBtn =
        document.getElementById("gdPlay");

    var centerPlay =
        document.getElementById("gdCenterPlay");

    var progress =
        document.getElementById("gdProgress");

    var progressFill =
        document.getElementById("gdProgressFill");

    var currentTime =
        document.getElementById("gdCurrent");

    var duration =
        document.getElementById("gdDuration");

    var muteBtn =
        document.getElementById("gdMute");

    var volume =
        document.getElementById("gdVolume");

    var settingsBtn =
        document.getElementById("gdSettings");

    var settingsMenu =
        document.getElementById("gdSettingsMenu");

    var fullscreenBtn =
        document.getElementById("gdFullscreen");

    var zoomIn =
        document.getElementById("gdZoomIn");

    var zoomOut =
        document.getElementById("gdZoomOut");

    var scaleValue =
        document.getElementById("gdScaleValue");


    /* -----------------------------------------
       Quality sources
    ----------------------------------------- */

    var sources = {{
        auto: "{html.escape(default_url, quote=True)}",
        480: "{html.escape(url_480, quote=True)}",
        720: "{html.escape(url_720, quote=True)}",
        1080: "{html.escape(url_1080, quote=True)}"
    }};


    /* -----------------------------------------
       Play / Pause
    ----------------------------------------- */

    function togglePlay() {{

        if (video.paused) {{

            var promise = video.play();

            if (promise &&
                typeof promise.catch === "function") {{

                promise.catch(function () {{}});
            }}

        }} else {{

            video.pause();

        }}

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
        "play",
        function () {{

            playBtn.textContent = "❚❚";

            centerPlay.style.display = "none";

        }}
    );


    video.addEventListener(
        "pause",
        function () {{

            playBtn.textContent = "▶";

            centerPlay.style.display = "block";

        }}
    );


    /* -----------------------------------------
       Time formatting
    ----------------------------------------- */

    function formatTime(seconds) {{

        if (!isFinite(seconds)) {{
            return "00:00";
        }}

        var h =
            Math.floor(seconds / 3600);

        var m =
            Math.floor(
                (seconds % 3600) / 60
            );

        var s =
            Math.floor(seconds % 60);

        function pad(n) {{
            return String(n).padStart(2, "0");
        }}

        if (h > 0) {{

            return (
                pad(h) +
                ":" +
                pad(m) +
                ":" +
                pad(s)
            );

        }}

        return (
            pad(m) +
            ":" +
            pad(s)
        );
    }}


    /* -----------------------------------------
       Metadata
    ----------------------------------------- */

    video.addEventListener(
        "loadedmetadata",
        function () {{

            duration.textContent =
                formatTime(video.duration);

        }}
    );


    /* -----------------------------------------
       Progress
    ----------------------------------------- */

    video.addEventListener(
        "timeupdate",
        function () {{

            currentTime.textContent =
                formatTime(video.currentTime);

            if (video.duration) {{

                var percent =
                    (video.currentTime /
                     video.duration) * 100;

                progressFill.style.width =
                    percent + "%";
            }}

        }}
    );


    /* -----------------------------------------
       Seek
    ----------------------------------------- */

    progress.addEventListener(
        "click",
        function (e) {{

            var rect =
                progress.getBoundingClientRect();

            var percent =
                (e.clientX - rect.left) /
                rect.width;

            percent =
                Math.max(
                    0,
                    Math.min(1, percent)
                );

            if (video.duration) {{

                video.currentTime =
                    percent * video.duration;
            }}

        }}
    );


    /* -----------------------------------------
       Volume
    ----------------------------------------- */

    volume.addEventListener(
        "input",
        function () {{

            video.volume =
                parseFloat(this.value);

            if (video.volume <= 0) {{

                video.muted = true;

                muteBtn.textContent =
                    "🔇";

            }} else {{

                video.muted = false;

                muteBtn.textContent =
                    "🔊";
            }}

        }}
    );


    muteBtn.addEventListener(
        "click",
        function () {{

            video.muted =
                !video.muted;

            muteBtn.textContent =
                video.muted
                    ? "🔇"
                    : "🔊";

        }}
    );


    /* -----------------------------------------
       Settings menu
    ----------------------------------------- */

    settingsBtn.addEventListener(
        "click",
        function (e) {{

            e.stopPropagation();

            settingsMenu.classList.toggle(
                "show"
            );

        }}
    );


    settingsMenu.addEventListener(
        "click",
        function (e) {{

            e.stopPropagation();

        }}
    );


    document.addEventListener(
        "click",
        function () {{

            settingsMenu.classList.remove(
                "show"
            );

        }}
    );


    /* -----------------------------------------
       Quality switching
    ----------------------------------------- */

    var qualityButtons =
        document.querySelectorAll(
            ".gd-quality-btn"
        );


    for (
        var qi = 0;
        qi < qualityButtons.length;
        qi++
    ) {{

        qualityButtons[qi].addEventListener(
            "click",
            function () {{

                var quality =
                    this.getAttribute(
                        "data-quality"
                    );

                var newSource =
                    sources[quality];

                if (!newSource) {{
                    return;
                }}

                var oldTime =
                    video.currentTime || 0;

                var wasPlaying =
                    !video.paused;

                var oldVolume =
                    video.volume;

                var oldMuted =
                    video.muted;


                video.src = newSource;

                video.load();


                video.addEventListener(
                    "loadedmetadata",
                    function restorePosition() {{

                        try {{

                            if (
                                isFinite(oldTime) &&
                                oldTime > 0 &&
                                oldTime < video.duration
                            ) {{

                                video.currentTime =
                                    oldTime;
                            }}

                        }} catch (err) {{}}


                        video.volume =
                            oldVolume;

                        video.muted =
                            oldMuted;


                        if (wasPlaying) {{

                            var p =
                                video.play();

                            if (
                                p &&
                                typeof p.catch ===
                                "function"
                            ) {{

                                p.catch(
                                    function () {{}}
                                );
                            }}
                        }}


                        video.removeEventListener(
                            "loadedmetadata",
                            restorePosition
                        );

                    }}
                );


                for (
                    var j = 0;
                    j < qualityButtons.length;
                    j++
                ) {{

                    qualityButtons[j]
                        .classList
                        .remove("active");

                    qualityButtons[j].textContent =
                        qualityButtons[j]
                            .textContent
                            .replace(/^✓\s*/, "");
                }}


                this.classList.add(
                    "active"
                );

                this.textContent =
                    "✓ " +
                    this.textContent;


                settingsMenu.classList.remove(
                    "show"
                );

            }}
        );

    }


    /* -----------------------------------------
       Viewer Zoom
       75% -> 100% -> ... -> 200%
    ----------------------------------------- */

    var scale = 1;


    function updateScale() {{

        scaleValue.textContent =
            Math.round(scale * 100) +
            "%";

        video.style.transform =
            "scale(" +
            scale +
            ")";

    }}


    zoomIn.addEventListener(
        "click",
        function () {{

            if (scale < 2) {{

                scale =
                    Math.min(
                        2,
                        scale + 0.25
                    );

                updateScale();

            }}

        }}
    );


    zoomOut.addEventListener(
        "click",
        function () {{

            if (scale > 0.75) {{

                scale =
                    Math.max(
                        0.75,
                        scale - 0.25
                    );

                updateScale();

            }}

        }}
    );


    /* -----------------------------------------
       Fullscreen
    ----------------------------------------- */

    fullscreenBtn.addEventListener(
        "click",
        function () {{

            if (!document.fullscreenElement) {{

                if (
                    player.requestFullscreen
                ) {{

                    player.requestFullscreen()
                        .catch(function () {{}});
                }}

            }} else {{

                if (
                    document.exitFullscreen
                ) {{

                    document.exitFullscreen()
                        .catch(function () {{}});
                }}

            }}

        }}
    );


    /* -----------------------------------------
       Double click fullscreen
    ----------------------------------------- */

    video.addEventListener(
        "dblclick",
        function () {{

            if (!document.fullscreenElement) {{

                if (
                    player.requestFullscreen
                ) {{

                    player.requestFullscreen()
                        .catch(function () {{}});
                }}

            }} else {{

                if (
                    document.exitFullscreen
                ) {{

                    document.exitFullscreen()
                        .catch(function () {{}});
                }}

            }}

        }}
    );


    /* -----------------------------------------
       Keyboard shortcuts
    ----------------------------------------- */

    document.addEventListener(
        "keydown",
        function (e) {{

            if (
                e.target.tagName === "INPUT" ||
                e.target.tagName === "TEXTAREA"
            ) {{
                return;
            }}

            if (e.code === "Space") {{

                e.preventDefault();

                togglePlay();

            }}

        }}
    );


    /* -----------------------------------------
       Initial state
    ----------------------------------------- */

    updateScale();

}})();
</script>
"""


# ---------- Download timer ----------
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
            'Download starting...';

          window.location.href =
            'https://drive.usercontent.google.com/download?id=' +
            b.getAttribute('data-fid') +
            '&export=download&confirm=t';

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
# BUILD BLOGGER HTML
# ============================================================

def build_html(
    meta,
    thumb_id,
    shot_ids,
    outputs,
    fps,
    dur
):
    e = html.escape

    title = e(meta["title"])

    year = meta["release_year"]

    ytxt = (
        f" ({year})"
        if year
        else ""
    )

    lang_known = (
        meta["language"]
        and meta["language"].lower()
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

    fps_txt = fmt_fps(fps)

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


    # -----------------------------------------
    # Download button style
    # -----------------------------------------

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
        "linear-gradient(90deg,#57a51c,#1f4fb4);"
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


    # -----------------------------------------
    # Movie info
    # -----------------------------------------

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


    # -----------------------------------------
    # Page parts
    # -----------------------------------------

    parts = [

        (
            f'<div style="text-align:center">'
            f'<img src="{img_url(thumb_id)}" '
            f'alt="{title}" '
            f'width="270" '
            f'style="max-width:60%;'
            f'height:auto;'
            f'border-radius:8px"/>'
            f'</div>'
        ),

        (
            f'<p style="text-align:center">'
            f'<b>{title}{ytxt}</b>'
            f'{" - " + lang + " film" if lang_known else ""}'
            f'</p>'
        ),
    ]


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
        h3.format(
            "Movie Synopsis / Plot"
        )
    )


    parts += [
        f"<p>{p}</p>"
        for p in syn
    ]


    # ========================================================
    # NEW PLAYER
    # ========================================================

    parts.append(
        f"<h3>Watch {title} Online</h3>"
    )

    parts.append(
        build_drive_player(outputs)
    )


    # -----------------------------------------
    # Review
    # -----------------------------------------

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


    # -----------------------------------------
    # Themes
    # -----------------------------------------

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


    # -----------------------------------------
    # Screenshots
    # -----------------------------------------

    parts.append(
        h3.format("Screenshots")
    )

    for fid in shot_ids:

        parts.append(
            f'<p style="text-align:center">'
            f'<img src="{img_url(fid)}" '
            f'alt="{title} screenshot" '
            f'style="max-width:100%;'
            f'height:auto"/>'
            f'</p>'
        )


    parts.append(hr)


    # -----------------------------------------
    # Download links
    # -----------------------------------------

    parts.append(
        h3.format("Download Links")
    )

    for h, fid, size in outputs:

        direct = (
            f"https://drive.usercontent.google.com/"
            f"download?id={fid}"
            "&amp;export=download"
            "&amp;confirm=t"
        )

        parts.append(
            f'<h4 style="{head}">'
            f'{title}{ytxt}{lang_tag} '
            f'{h}p x264 '
            f'{fps_txt}fps '
            f'[{human(size)}]'
            f'</h4>'
        )

        parts.append(
            f'<a class="mv-dl" '
            f'data-fid="{fid}" '
            f'href="{direct}" '
            f'rel="noopener" '
            f'style="{btn}">'
            '&#11015;&#9889;'
            'DOWNLOAD NOW'
            '&#9889;&#11015;'
            '</a>'
        )


    parts.append(hr)


    # -----------------------------------------
    # FAQ
    # -----------------------------------------

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
# MAIN PIPELINE
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

    job = WORK / video["id"]

    job.mkdir(
        parents=True,
        exist_ok=True
    )

    src = str(
        job / "source.mp4"
    )

    slug = re.sub(
        r"[^a-zA-Z0-9]+",
        "-",
        Path(name).stem
    ).strip("-").lower() or "movie"


    # -----------------------------------------
    # Download
    # -----------------------------------------

    log(
        "Downloading original..."
    )

    download(
        video["id"],
        src
    )


    # -----------------------------------------
    # Probe
    # -----------------------------------------

    dur, w, h, fps = probe(src)

    short = min(w, h)

    log(
        f"Duration {dur / 60:.1f} min, "
        f"{w}x{h}, "
        f"{fps:.2f} fps"
    )


    # -----------------------------------------
    # Screenshots / thumbnail
    # -----------------------------------------

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


    # -----------------------------------------
    # Gemini
    # -----------------------------------------

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


    # -----------------------------------------
    # Transcode + upload
    # -----------------------------------------

    targets = sorted({
        t
        for t in RESOLUTIONS
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


    # -----------------------------------------
    # Upload images
    # -----------------------------------------

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


    # -----------------------------------------
    # Labels
    # -----------------------------------------

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

        labels = (
            [unc]
            if unc
            else (
                [
                    g
                    for g in meta["genres"]
                ][:2]
                +
                [meta["language"]]
            )
        )


    labels = [
        str(l)[:40]
        for l in labels
        if l
    ][:8]


    # -----------------------------------------
    # Build Blogger content
    # -----------------------------------------

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
        if meta["language"].lower()
        != "unknown"
        else ""
    )


    body = {
        "kind": "blogger#post",

        "title": (
            f'{meta["title"]}'
            f'{ytxt}'
            f'{ltxt}'
            ' Movie - Watch Online & Download'
        ),

        "content": content,

        "labels": labels,
    }


    # -----------------------------------------
    # Blogger post
    # -----------------------------------------

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
        "(DRAFT)"
        if not PUBLISH
        else "(PUBLISHED)"
    )


    # -----------------------------------------
    # Move original to _processed
    # -----------------------------------------

    drive.files().update(
        fileId=video["id"],
        addParents=processed_folder,
        removeParents=INPUT_FOLDER,
        fields="id"
    ).execute()


    # -----------------------------------------
    # Cleanup
    # -----------------------------------------

    shutil.rmtree(
        job,
        ignore_errors=True
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
            "No new videos in the input folder. "
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


    for v in videos[:MAX_VIDEOS]:

        try:

            process(
                v,
                processed,
                output
            )

        except Exception:  # noqa

            failed += 1

            traceback.print_exc()


    return 1 if failed else 0


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    sys.exit(main())
