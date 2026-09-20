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


# ---------- general helpers ----------
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


# ---------- ffmpeg helpers ----------
def parse_fps(*vals):
    for v in vals:
        try:
            a, b = str(v).split("/")
            a = float(a)
            b = float(b)

            if b and a / b > 0:
                return a / b

        except Exception:  # noqa
            pass

    return 30.0


# IMPORTANT:
# 23.976 -> 24
# 29.97  -> 30
# 59.94  -> 60
def fmt_fps(x):
    return str(round(x))


def probe(path):
    out = subprocess.check_output([
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height,avg_frame_rate,r_frame_rate:format=duration",
        "-of",
        "json",
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
            p
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
            str(p)
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
        str(audio)
    ])

    return frames, audio.read_bytes()


def transcode(src, target, w, h, out):
    """
    target = length of the SHORT side (480/720/1080):
    works for landscape and vertical.

    NOTE:
    No -r option is used here.
    Therefore the source FPS is not forcefully changed.
    """

    crf = CRF.get(target, 23)

    vf = (
        f"scale=-2:{target}"
        if w >= h
        else f"scale={target}:-2"
    )

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
            }
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
          trailer, song, etc.). Ignore labels about video encoding or file format.
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

        "synopsis": as_paragraphs(
            data.get("synopsis")
        ) or [
            "An original film."
        ],

        "review": as_paragraphs(
            data.get("review")
        ),

        "themes": [
            str(t)
            for t in (
                data.get("themes")
                or []
            )
        ][:5],

        "faq": faq[:4],

        "genres": data.get("genres")
        or ["Drama"],

        "language": data.get("language")
        or LANGUAGE_HINT
        or "Unknown",

        "release_year": year,

        "content_rating": data.get(
            "content_rating"
        ) or "General audience",

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


# ---------- download timer ----------
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
          'Please wait ' + left + ' seconds...';

        var t = setInterval(function () {
          left--;

          if (left > 0) {
            b.innerHTML =
              'Please wait ' + left + ' seconds...';

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


# ==========================================================
# CUSTOM VIDEO PLAYER
# ==========================================================
#
# IMPORTANT:
# This is deliberately a NORMAL triple-quoted string.
# It is NOT an f-string.
#
# Therefore JavaScript { } and CSS { } cannot cause:
# SyntaxError: f-string: single '}' is not allowed
#
# Python values are inserted later using .replace().
# ==========================================================

PLAYER_TEMPLATE = r"""
<style>
.mv-player-wrap {
    width: 100%;
    max-width: 100%;
    margin: 18px auto 28px;
    box-sizing: border-box;
}

.mv-player {
    position: relative;
    width: 100%;
    aspect-ratio: 16 / 9;
    background: #000;
    overflow: hidden;
    border-radius: 4px;
    font-family: Arial, Helvetica, sans-serif;
    user-select: none;
    -webkit-user-select: none;
    box-sizing: border-box;
}

.mv-player *,
.mv-player *::before,
.mv-player *::after {
    box-sizing: border-box;
}

.mv-player video {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    display: block;
    background: #000;
    object-fit: contain;
}

.mv-player .mv-language {
    position: absolute;
    left: 14px;
    top: 12px;
    z-index: 8;
    padding: 5px 9px;
    border-radius: 5px;
    background: rgba(0, 0, 0, .58);
    color: #7dff43;
    font-size: 12px;
    font-weight: 700;
    line-height: 1;
    pointer-events: none;
}

.mv-player .mv-center-play {
    position: absolute;
    left: 50%;
    top: 50%;
    z-index: 7;
    transform: translate(-50%, -50%);
    width: 70px;
    height: 70px;
    border: 0;
    border-radius: 50%;
    background: rgba(70, 184, 42, .96);
    color: #fff;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 29px;
    padding-left: 5px;
    box-shadow: 0 3px 18px rgba(0,0,0,.38);
}

.mv-player .mv-center-play:hover {
    background: rgba(87, 205, 50, 1);
}

.mv-player .mv-controls {
    position: absolute;
    left: 0;
    right: 0;
    bottom: 0;
    z-index: 10;
    padding: 0 13px 10px;
    background: linear-gradient(
        to top,
        rgba(0,0,0,.88),
        rgba(0,0,0,.38),
        transparent
    );
    opacity: 1;
    transition: opacity .2s ease;
}

.mv-player .mv-progress-area {
    position: relative;
    height: 18px;
    padding-top: 8px;
    cursor: pointer;
}

.mv-player .mv-progress-bg {
    position: absolute;
    left: 0;
    right: 0;
    top: 8px;
    height: 4px;
    border-radius: 5px;
    background: rgba(255,255,255,.25);
}

.mv-player .mv-buffer {
    position: absolute;
    left: 0;
    top: 8px;
    height: 4px;
    width: 0;
    border-radius: 5px;
    background: rgba(255,255,255,.42);
    pointer-events: none;
}

.mv-player .mv-progress {
    position: absolute;
    left: 0;
    top: 8px;
    height: 4px;
    width: 0;
    border-radius: 5px;
    background: #58c62d;
    pointer-events: none;
}

.mv-player .mv-handle {
    position: absolute;
    top: 5px;
    left: 0;
    width: 10px;
    height: 10px;
    margin-left: -5px;
    border-radius: 50%;
    background: #65d637;
    box-shadow: 0 0 0 2px rgba(0,0,0,.15);
    pointer-events: none;
}

.mv-player .mv-bottom {
    min-height: 38px;
    display: flex;
    align-items: center;
    gap: 10px;
}

.mv-player button {
    border: 0;
    outline: 0;
}

.mv-player .mv-btn {
    width: 30px;
    height: 30px;
    padding: 0;
    background: transparent;
    color: #fff;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    font-size: 18px;
    flex: 0 0 auto;
}

.mv-player .mv-btn:hover {
    color: #75df45;
}

.mv-player .mv-time {
    color: #fff;
    font-size: 12px;
    white-space: nowrap;
    line-height: 30px;
}

.mv-player .mv-volume-wrap {
    display: flex;
    align-items: center;
    gap: 5px;
    min-width: 0;
}

.mv-player .mv-volume {
    width: 70px;
    height: 4px;
    cursor: pointer;
    accent-color: #62cc37;
}

.mv-player .mv-spacer {
    flex: 1;
}

.mv-player .mv-settings {
    position: relative;
}

.mv-player .mv-menu {
    position: absolute;
    right: 0;
    bottom: 42px;
    width: 185px;
    max-height: 330px;
    overflow-y: auto;
    padding: 8px 0;
    border-radius: 7px;
    background: rgba(18,18,18,.98);
    box-shadow: 0 6px 28px rgba(0,0,0,.55);
    display: none;
    color: #fff;
}

.mv-player .mv-menu.open {
    display: block;
}

.mv-player .mv-menu-title {
    padding: 7px 13px 5px;
    color: #9c9c9c;
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
}

.mv-player .mv-option {
    display: block;
    width: 100%;
    padding: 9px 13px;
    text-align: left;
    color: #fff;
    background: transparent;
    border: 0;
    cursor: pointer;
    font-size: 13px;
}

.mv-player .mv-option:hover {
    background: rgba(255,255,255,.09);
}

.mv-player .mv-option.active {
    color: #68d63c;
}

.mv-player .mv-option.hidden-quality {
    display: none;
}

.mv-player .mv-loading {
    position: absolute;
    left: 50%;
    top: 50%;
    z-index: 6;
    transform: translate(-50%, -50%);
    width: 42px;
    height: 42px;
    border: 4px solid rgba(255,255,255,.25);
    border-top-color: #62d83b;
    border-radius: 50%;
    animation: mvspin .8s linear infinite;
    display: none;
}

.mv-player .mv-error {
    position: absolute;
    left: 50%;
    top: 58%;
    z-index: 9;
    transform: translate(-50%, -50%);
    width: 90%;
    text-align: center;
    color: #fff;
    font-size: 13px;
    background: rgba(0,0,0,.65);
    padding: 8px 12px;
    border-radius: 5px;
    display: none;
}

@keyframes mvspin {
    from {
        transform: translate(-50%, -50%) rotate(0deg);
    }

    to {
        transform: translate(-50%, -50%) rotate(360deg);
    }
}

@media (max-width: 520px) {
    .mv-player .mv-bottom {
        gap: 5px;
    }

    .mv-player .mv-volume {
        width: 50px;
    }

    .mv-player .mv-time {
        font-size: 11px;
    }

    .mv-player .mv-btn {
        width: 27px;
        font-size: 16px;
    }

    .mv-player .mv-center-play {
        width: 60px;
        height: 60px;
        font-size: 25px;
    }

    .mv-player .mv-language {
        left: 9px;
        top: 8px;
        font-size: 11px;
    }
}
</style>

<div class="mv-player-wrap">
    <div class="mv-player" id="mvPlayerUnique">

        <video
            id="mvVideoUnique"
            playsinline
            preload="metadata"
            controlslist="nodownload"
        ></video>

        <div
            class="mv-language"
            id="mvLanguageUnique"
        >__PLAYER_LANGUAGE__</div>

        <button
            class="mv-center-play"
            id="mvCenterPlayUnique"
            type="button"
            aria-label="Play"
        >▶</button>

        <div
            class="mv-loading"
            id="mvLoadingUnique"
        ></div>

        <div
            class="mv-error"
            id="mvErrorUnique"
        >Unable to load this video.</div>

        <div class="mv-controls">

            <div
                class="mv-progress-area"
                id="mvProgressAreaUnique"
            >
                <div class="mv-progress-bg"></div>

                <div
                    class="mv-buffer"
                    id="mvBufferUnique"
                ></div>

                <div
                    class="mv-progress"
                    id="mvProgressUnique"
                ></div>

                <div
                    class="mv-handle"
                    id="mvHandleUnique"
                ></div>
            </div>

            <div class="mv-bottom">

                <button
                    class="mv-btn"
                    id="mvPlayUnique"
                    type="button"
                    aria-label="Play/Pause"
                >▶</button>

                <div
                    class="mv-time"
                    id="mvTimeUnique"
                >00:00 / 00:00</div>

                <div class="mv-volume-wrap">

                    <button
                        class="mv-btn"
                        id="mvMuteUnique"
                        type="button"
                        aria-label="Mute"
                    >🔊</button>

                    <input
                        class="mv-volume"
                        id="mvVolumeUnique"
                        type="range"
                        min="0"
                        max="1"
                        step="0.01"
                        value="1"
                        aria-label="Volume"
                    >

                </div>

                <div class="mv-spacer"></div>

                <div class="mv-settings">

                    <button
                        class="mv-btn"
                        id="mvSettingsBtnUnique"
                        type="button"
                        aria-label="Settings"
                    >⚙</button>

                    <div
                        class="mv-menu"
                        id="mvSettingsMenuUnique"
                    >

                        <div class="mv-menu-title">
                            Quality
                        </div>

                        <button
                            class="mv-option"
                            data-quality="auto"
                            type="button"
                        >Auto</button>

                        <button
                            class="mv-option"
                            data-quality="1080"
                            type="button"
                        >1080p</button>

                        <button
                            class="mv-option"
                            data-quality="720"
                            type="button"
                        >720p</button>

                        <button
                            class="mv-option"
                            data-quality="480"
                            type="button"
                        >480p</button>

                        <button
                            class="mv-option"
                            data-quality="360"
                            type="button"
                        >360p</button>

                        <div class="mv-menu-title">
                            Language
                        </div>

                        <button
                            class="mv-option mv-language-option"
                            data-language="Hindi"
                            type="button"
                        >Hindi</button>

                        <button
                            class="mv-option mv-language-option"
                            data-language="English"
                            type="button"
                        >English</button>

                        <button
                            class="mv-option mv-language-option"
                            data-language="Bengali"
                            type="button"
                        >Bengali</button>

                        <button
                            class="mv-option mv-language-option"
                            data-language="Arabic"
                            type="button"
                        >Arabic</button>

                        <div class="mv-menu-title">
                            Speed
                        </div>

                        <button
                            class="mv-option mv-speed-option"
                            data-speed="0.5"
                            type="button"
                        >0.5x</button>

                        <button
                            class="mv-option mv-speed-option"
                            data-speed="0.75"
                            type="button"
                        >0.75x</button>

                        <button
                            class="mv-option mv-speed-option active"
                            data-speed="1"
                            type="button"
                        >1x</button>

                        <button
                            class="mv-option mv-speed-option"
                            data-speed="1.25"
                            type="button"
                        >1.25x</button>

                        <button
                            class="mv-option mv-speed-option"
                            data-speed="1.5"
                            type="button"
                        >1.5x</button>

                        <button
                            class="mv-option mv-speed-option"
                            data-speed="2"
                            type="button"
                        >2x</button>

                        <div class="mv-menu-title">
                            Screen
                        </div>

                        <button
                            class="mv-option"
                            id="mvFullscreenMenuUnique"
                            type="button"
                        >Fullscreen</button>

                    </div>
                </div>

                <button
                    class="mv-btn"
                    id="mvFullscreenUnique"
                    type="button"
                    aria-label="Fullscreen"
                >⛶</button>

            </div>
        </div>
    </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/hls.js@0.14.17"></script>

<script>
(function () {

    var VIDEO_SOURCES = __VIDEO_SOURCES__;

    var DEFAULT_LANGUAGE = __PLAYER_LANGUAGE_JSON__;

    var video = document.getElementById(
        'mvVideoUnique'
    );

    var player = document.getElementById(
        'mvPlayerUnique'
    );

    var centerPlay = document.getElementById(
        'mvCenterPlayUnique'
    );

    var playBtn = document.getElementById(
        'mvPlayUnique'
    );

    var muteBtn = document.getElementById(
        'mvMuteUnique'
    );

    var volume = document.getElementById(
        'mvVolumeUnique'
    );

    var timeText = document.getElementById(
        'mvTimeUnique'
    );

    var progressArea = document.getElementById(
        'mvProgressAreaUnique'
    );

    var progress = document.getElementById(
        'mvProgressUnique'
    );

    var buffer = document.getElementById(
        'mvBufferUnique'
    );

    var handle = document.getElementById(
        'mvHandleUnique'
    );

    var settingsBtn = document.getElementById(
        'mvSettingsBtnUnique'
    );

    var settingsMenu = document.getElementById(
        'mvSettingsMenuUnique'
    );

    var fullscreenBtn = document.getElementById(
        'mvFullscreenUnique'
    );

    var fullscreenMenu = document.getElementById(
        'mvFullscreenMenuUnique'
    );

    var languageLabel = document.getElementById(
        'mvLanguageUnique'
    );

    var loading = document.getElementById(
        'mvLoadingUnique'
    );

    var errorBox = document.getElementById(
        'mvErrorUnique'
    );

    var hls = null;

    var currentQuality = null;

    function formatTime(seconds) {

        if (!isFinite(seconds)) {
            return '00:00';
        }

        seconds = Math.max(
            0,
            Math.floor(seconds)
        );

        var hours = Math.floor(
            seconds / 3600
        );

        var minutes = Math.floor(
            (seconds % 3600) / 60
        );

        var secs = seconds % 60;

        if (hours > 0) {

            return String(hours).padStart(2, '0') +
                ':' +
                String(minutes).padStart(2, '0') +
                ':' +
                String(secs).padStart(2, '0');

        }

        return String(minutes).padStart(2, '0') +
            ':' +
            String(secs).padStart(2, '0');
    }


    function showLoading(show) {

        loading.style.display =
            show ? 'block' : 'none';

    }


    function showError(show) {

        errorBox.style.display =
            show ? 'block' : 'none';

    }


    function updatePlayButton() {

        if (video.paused) {

            playBtn.innerHTML = '▶';
            centerPlay.style.display = 'flex';

        } else {

            playBtn.innerHTML = '❚❚';
            centerPlay.style.display = 'none';

        }

    }


    function updateMuteButton() {

        if (
            video.muted ||
            video.volume === 0
        ) {

            muteBtn.innerHTML = '🔇';

        } else {

            muteBtn.innerHTML = '🔊';

        }

    }


    function updateTime() {

        var duration =
            video.duration || 0;

        var current =
            video.currentTime || 0;

        timeText.innerHTML =
            formatTime(current) +
            ' / ' +
            formatTime(duration);

        var percent = 0;

        if (duration > 0) {

            percent =
                (current / duration) * 100;

        }

        progress.style.width =
            percent + '%';

        handle.style.left =
            percent + '%';

    }


    function updateBuffer() {

        try {

            if (
                video.buffered &&
                video.buffered.length &&
                video.duration
            ) {

                var end =
                    video.buffered.end(
                        video.buffered.length - 1
                    );

                var percent =
                    (end / video.duration) * 100;

                buffer.style.width =
                    Math.min(
                        100,
                        percent
                    ) + '%';

            }

        } catch (e) {}

    }


    function seekFromEvent(e) {

        var rect =
            progressArea.getBoundingClientRect();

        var x =
            e.clientX - rect.left;

        var ratio =
            x / rect.width;

        ratio =
            Math.max(
                0,
                Math.min(1, ratio)
            );

        if (video.duration) {

            video.currentTime =
                ratio * video.duration;

        }

    }


    function setActiveQuality(q) {

        var options =
            settingsMenu.querySelectorAll(
                '.mv-option[data-quality]'
            );

        for (
            var i = 0;
            i < options.length;
            i++
        ) {

            var item =
                options[i];

            item.classList.toggle(
                'active',
                item.getAttribute('data-quality') === q
            );

        }

    }


    function switchSource(url, quality) {

        if (!url) {
            showError(true);
            return;
        }

        var currentTime =
            video.currentTime || 0;

        var wasPlaying =
            !video.paused;

        showError(false);
        showLoading(true);

        currentQuality = quality;

        setActiveQuality(quality);

        if (hls) {

            try {
                hls.destroy();
            } catch (e) {}

            hls = null;

        }

        var isHls =
            /\.m3u8($|\?)/i.test(url);

        if (
            isHls &&
            window.Hls &&
            Hls.isSupported()
        ) {

            hls = new Hls();

            hls.loadSource(url);
            hls.attachMedia(video);

            hls.on(
                Hls.Events.MANIFEST_PARSED,
                function () {

                    try {
                        video.currentTime =
                            currentTime;
                    } catch (e) {}

                    if (wasPlaying) {
                        video.play().catch(
                            function () {}
                        );
                    }

                    showLoading(false);

                }
            );

            hls.on(
                Hls.Events.ERROR,
                function () {
                    showLoading(false);
                }
            );

            return;
        }

        video.src = url;

        video.addEventListener(
            'loadedmetadata',
            function restoreTimeOnce() {

                video.removeEventListener(
                    'loadedmetadata',
                    restoreTimeOnce
                );

                try {

                    if (
                        currentTime > 0 &&
                        isFinite(video.duration)
                    ) {

                        video.currentTime =
                            Math.min(
                                currentTime,
                                Math.max(
                                    0,
                                    video.duration - 0.5
                                )
                            );

                    }

                } catch (e) {}

                if (wasPlaying) {

                    video.play().catch(
                        function () {}
                    );

                }

                showLoading(false);

            }
        );

        video.load();

    }


    function chooseInitialSource() {

        var preferred = [
            '720',
            '1080',
            '480',
            '360'
        ];

        for (
            var i = 0;
            i < preferred.length;
            i++
        ) {

            var q = preferred[i];

            if (VIDEO_SOURCES[q]) {

                return {
                    quality: q,
                    url: VIDEO_SOURCES[q]
                };

            }

        }

        if (VIDEO_SOURCES.hls) {

            return {
                quality: 'auto',
                url: VIDEO_SOURCES.hls
            };

        }

        return null;

    }


    function setupQualityOptions() {

        var options =
            settingsMenu.querySelectorAll(
                '.mv-option[data-quality]'
            );

        for (
            var i = 0;
            i < options.length;
            i++
        ) {

            var item =
                options[i];

            var q =
                item.getAttribute(
                    'data-quality'
                );

            if (
                q !== 'auto' &&
                !VIDEO_SOURCES[q]
            ) {

                item.classList.add(
                    'hidden-quality'
                );

            }

        }

    }


    function setupLanguage() {

        var normalized =
            String(DEFAULT_LANGUAGE || '')
                .trim()
                .toLowerCase();

        if (!normalized) {
            normalized = 'unknown';
        }

        languageLabel.innerHTML =
            DEFAULT_LANGUAGE || 'Language';

        var options =
            settingsMenu.querySelectorAll(
                '.mv-language-option'
            );

        for (
            var i = 0;
            i < options.length;
            i++
        ) {

            var item = options[i];

            var lang =
                item.getAttribute(
                    'data-language'
                );

            if (
                lang.toLowerCase() ===
                normalized
            ) {

                item.classList.add(
                    'active'
                );

            } else {

                item.classList.remove(
                    'active'
                );

            }

        }

    }


    function setSpeed(speed) {

        var value =
            parseFloat(speed);

        if (!isFinite(value)) {
            value = 1;
        }

        video.playbackRate = value;

        var options =
            settingsMenu.querySelectorAll(
                '.mv-speed-option'
            );

        for (
            var i = 0;
            i < options.length;
            i++
        ) {

            var item =
                options[i];

            item.classList.toggle(
                'active',
                parseFloat(
                    item.getAttribute('data-speed')
                ) === value
            );

        }

    }


    function toggleFullscreen() {

        if (!document.fullscreenElement) {

            if (player.requestFullscreen) {

                player.requestFullscreen().catch(
                    function () {}
                );

            } else if (
                player.webkitRequestFullscreen
            ) {

                player.webkitRequestFullscreen();

            }

        } else {

            if (document.exitFullscreen) {

                document.exitFullscreen().catch(
                    function () {}
                );

            } else if (
                document.webkitExitFullscreen
            ) {

                document.webkitExitFullscreen();

            }

        }

    }


    centerPlay.addEventListener(
        'click',
        function () {

            video.play().catch(
                function () {}
            );

        }
    );


    playBtn.addEventListener(
        'click',
        function () {

            if (video.paused) {

                video.play().catch(
                    function () {}
                );

            } else {

                video.pause();

            }

        }
    );


    video.addEventListener(
        'play',
        updatePlayButton
    );


    video.addEventListener(
        'pause',
        updatePlayButton
    );


    video.addEventListener(
        'timeupdate',
        updateTime
    );


    video.add
