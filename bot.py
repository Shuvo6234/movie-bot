"""
Movie Bot:
Google Drive video
-> multi-resolution MP4
-> screenshots + 9:16 thumbnail
-> Gemini title/description/labels
-> Blogger post
-> Custom responsive HTML5 video player

Player features:
- Autoplay muted
- Play / Pause
- Progress bar
- Buffer bar
- Volume / Mute
- Quality selector
- Language display
- Playback speed
- Fullscreen
- Responsive 16:9
- Google Drive hosted MP4 sources

Runs on GitHub Actions.
All settings come from environment variables.
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

GEMINI_MODEL = os.environ.get(
    "GEMINI_MODEL",
    "gemini-3.6-flash"
)

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
    os.environ.get("PUBLISH", "false").lower()
    == "true"
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
# LOGGING / RETRY
# ============================================================

def log(*a):
    print(*a, flush=True)


def retry(fn, tries=4):
    for i in range(tries):
        try:
            return fn()
        except Exception as e:
            if i == tries - 1:
                raise

            log(
                f"  retry {i + 1} after error: {e}"
            )

            time.sleep(5 * (i + 1))


def run(cmd):
    subprocess.run(cmd, check=True)


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
# DRIVE HELPERS
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
        f"'{INPUT_FOLDER}' in parents "
        "and mimeType contains 'video/' "
        "and trashed=false"
    )

    res = drive.files().list(
        q=q,
        fields="files(id,name,size)",
        orderBy="createdTime"
    ).execute()

    return res["files"]


def download(file_id, dest):
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

    # Make public
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
# GOOGLE DRIVE VIDEO URL
# ============================================================

def drive_video_url(file_id):
    """
    Public Google Drive video URL.

    This URL is used directly by the HTML5 <video> player.
    """

    return (
        "https://drive.usercontent.google.com/"
        "download?id="
        + urllib.parse.quote(str(file_id))
        + "&export=download&confirm=t"
    )


def drive_download_url(file_id):
    """
    Download button URL.
    """

    return (
        "https://drive.usercontent.google.com/"
        "download?id="
        + urllib.parse.quote(str(file_id))
        + "&export=download&confirm=t"
    )


def img_url(fid):
    return (
        f"https://lh3.googleusercontent.com/d/{fid}"
    )


# ============================================================
# FFMPEG HELPERS
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
        t = (
            dur * (i + 1)
            / (SCREENSHOTS + 1)
        )

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


def make_thumbnail(
    src,
    dur,
    w,
    h,
    outdir
):
    """
    9:16 portrait thumbnail,
    720x1280,
    centre crop.
    """

    if w * 16 >= h * 9:
        ch = h // 2 * 2
        cw = (
            int(h * 9 / 16)
            // 2
            * 2
        )

    else:
        cw = w // 2 * 2
        ch = (
            int(w * 16 / 9)
            // 2
            * 2
        )

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


def analysis_inputs(
    src,
    dur,
    outdir
):
    frames = []

    n = 12

    for i in range(n):
        t = (
            dur * (i + 1)
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
            str(p)
        ])

        frames.append(
            p.read_bytes()
        )

    audio = (
        outdir
        / "an_audio.mp3"
    )

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

    return (
        frames,
        audio.read_bytes()
    )


def transcode(
    src,
    target,
    w,
    h,
    out
):
    """
    target = length of the SHORT side.

    Landscape:
        1920x1080 for 1080p
        1280x720  for 720p
        854x480    for 480p

    Portrait:
        1080x1920
        720x1280
        480x854
    """

    crf = CRF.get(
        target,
        23
    )

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

        out
    ])


# ============================================================
# BLOG LABELS
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
        r"/search/label/([^\"'?&#<>\s/]+)",
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
            and name.lower() not in seen
        ):
            seen.add(name.lower())
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
            }
        )

        page = (
            urllib.request
            .urlopen(
                req,
                timeout=30
            )
            .read()
            .decode(
                "utf-8",
                "ignore"
            )
        )

        labels = parse_labels(page)

        log(
            f"  Found {len(labels)} "
            "labels on the blog"
        )

    except Exception as e:
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


def pick_labels(
    raw,
    site_labels
):
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
    r"(?<!\d)(19[5-9]\d|20[0-4]\d)(?!\d)"
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

    return no_year or t


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

You are publishing an ORIGINAL film
on its own director's film blog.

You get 12 frames spread across
the film and an audio sample.

File name hint:
"{hint}"

Language hint:
"{LANGUAGE_HINT}"

Director name:
"{DIRECTOR_NAME}"

Rules:

- Write everything in your own words.
- Never copy text from any website,
  film or review.
- Base it ONLY on what you can actually
  see and hear.
- If unsure, stay general.
- Never invent cast, crew, awards,
  festivals, ratings, box office
  or plot facts you cannot see.
- No piracy words such as:
  leaked, HD print, free download,
  full movie, WEB-DL, dual audio,
  300mb.
- Title must be a real film title
  of 1-6 words.
- No hashtags.
- No emojis.
- No year in title.

Return ONLY JSON.

Keys:

title:
film title

tagline:
one sentence, max 20 words

synopsis:
2 short paragraphs,
about 120 words total

review:
3-4 paragraphs,
about 300 words total

themes:
3-5 short phrases

faq:
4 objects with q and a

genres:
1-3 genres

language:
main spoken language

content_rating:
one of:
General audience
Teen and above
Mature audience

tags:
up to 6 keywords

labels:
pick 1-4 categories ONLY
from this exact list:

{json.dumps(site_labels)}

Judge labels by:
language,
film industry/country,
type,
movie/web series/trailer/song.

Ignore video encoding
and file format labels.
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

        except Exception as e:
            log(
                f"  Gemini model {model} failed: {e}"
            )

    if not data:
        log(
            "  Using fallback text."
        )

    faq = [
        f
        for f in (
            data.get("faq") or []
        )
        if (
            isinstance(f, dict)
            and f.get("q")
            and f.get("a")
        )
    ]

    return {
        "title":
            data.get("title")
            or hint
            or "Untitled Film",

        "tagline":
            data.get("tagline")
            or "",

        "synopsis":
            as_paragraphs(
                data.get("synopsis")
            )
            or ["An original film."],

        "review":
            as_paragraphs(
                data.get("review")
            ),

        "themes":
            [
                str(t)
                for t in (
                    data.get("themes")
                    or []
                )
            ][:5],

        "faq":
            faq[:4],

        "genres":
            data.get("genres")
            or ["Drama"],

        "language":
            data.get("language")
            or LANGUAGE_HINT
            or "Unknown",

        "release_year":
            year,

        "content_rating":
            data.get(
                "content_rating"
            )
            or "General audience",

        "tags":
            data.get("tags")
            or [],

        "labels":
            pick_labels(
                data.get("labels"),
                site_labels
            ),
    }


# ============================================================
# FORMAT HELPERS
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

    m = sec // 60

    if m < 60:
        return f"{m} min"

    return (
        f"{m // 60} h "
        f"{m % 60} min"
    )


# ============================================================
# DOWNLOAD TIMER
# ============================================================

TIMER_SCRIPT = """
<script>
(function () {

  var WAIT = %d;

  var btns = document.querySelectorAll(
    'a.mv-dl[data-fid]'
  );

  for (var i = 0; i < btns.length; i++) {

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
            'Please wait ' +
            left +
            ' seconds...';

          var t = setInterval(
            function () {

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


# ============================================================
# CUSTOM VIDEO PLAYER
# ============================================================

def build_video_player(
    outputs,
    language
):
    """
    outputs:
        [(quality, drive_id, size), ...]

    The player uses Google Drive
    public files as MP4 sources.
    """

    sources = []

    for quality, fid, size in outputs:

        sources.append({
            "quality": int(quality),
            "url": drive_video_url(fid)
        })

    sources_json = json.dumps(
        sources,
        ensure_ascii=False
    )

    safe_language = html.escape(
        language or "Original"
    )

    player = f"""
<div class="mv-player-wrap">

<style>

.mv-player-wrap {{
    width:100%;
    max-width:1000px;
    margin:24px auto;
    font-family:Arial,Helvetica,sans-serif;
}}

.mv-player {{
    position:relative;
    width:100%;
    aspect-ratio:16 / 9;
    background:#000;
    overflow:hidden;
    border-radius:10px;
    box-shadow:
        0 10px 35px rgba(0,0,0,.45);
    color:#fff;
}}

.mv-player video {{
    position:absolute;
    inset:0;
    width:100%;
    height:100%;
    object-fit:contain;
    background:#000;
}}

.mv-center-play {{
    position:absolute;
    left:50%;
    top:50%;
    transform:translate(-50%,-50%);
    width:72px;
    height:72px;
    border:0;
    border-radius:50%;
    background:rgba(0,0,0,.72);
    color:#fff;
    font-size:30px;
    cursor:pointer;
    z-index:5;
    display:flex;
    align-items:center;
    justify-content:center;
}}

.mv-center-play:hover {{
    background:rgba(0,0,0,.9);
}}

.mv-player-controls {{
    position:absolute;
    left:0;
    right:0;
    bottom:0;
    padding:35px 12px 10px;
    background:
        linear-gradient(
            transparent,
            rgba(0,0,0,.88)
        );
    z-index:10;
}}

.mv-progress {{
    position:relative;
    width:100%;
    height:5px;
    background:rgba(255,255,255,.25);
    border-radius:10px;
    cursor:pointer;
    margin-bottom:10px;
}}

.mv-buffer {{
    position:absolute;
    left:0;
    top:0;
    height:100%;
    width:0;
    background:rgba(255,255,255,.35);
    border-radius:10px;
    pointer-events:none;
}}

.mv-played {{
    position:absolute;
    left:0;
    top:0;
    height:100%;
    width:0;
    background:#fff;
    border-radius:10px;
    pointer-events:none;
}}

.mv-progress-dot {{
    position:absolute;
    top:50%;
    left:0;
    transform:translate(-50%,-50%);
    width:13px;
    height:13px;
    border-radius:50%;
    background:#fff;
    box-shadow:0 0 8px rgba(0,0,0,.5);
    pointer-events:none;
}}

.mv-controls-row {{
    display:flex;
    align-items:center;
    gap:8px;
}}

.mv-btn {{
    border:0;
    background:transparent;
    color:#fff;
    cursor:pointer;
    font-size:16px;
    padding:7px;
    border-radius:6px;
}}

.mv-btn:hover {{
    background:rgba(255,255,255,.15);
}}

.mv-time {{
    font-size:13px;
    white-space:nowrap;
    min-width:82px;
}}

.mv-spacer {{
    flex:1;
}}

.mv-menu-wrap {{
    position:relative;
}}

.mv-menu {{
    position:absolute;
    right:0;
    bottom:43px;
    min-width:145px;
    background:
        rgba(18,18,18,.97);
    border:1px solid rgba(255,255,255,.15);
    border-radius:8px;
    padding:5px;
    display:none;
    z-index:50;
    box-shadow:
        0 8px 25px rgba(0,0,0,.5);
}}

.mv-menu.show {{
    display:block;
}}

.mv-menu button {{
    width:100%;
    border:0;
    background:transparent;
    color:#fff;
    padding:9px 10px;
    text-align:left;
    border-radius:5px;
    cursor:pointer;
    font-size:14px;
}}

.mv-menu button:hover {{
    background:rgba(255,255,255,.14);
}}

.mv-menu button.active {{
    background:#2d7cff;
}}

.mv-volume {{
    width:78px;
}}

@media(max-width:600px) {{

    .mv-player {{
        border-radius:5px;
    }}

    .mv-center-play {{
        width:58px;
        height:58px;
        font-size:24px;
    }}

    .mv-player-controls {{
        padding:28px 7px 6px;
    }}

    .mv-btn {{
        font-size:14px;
        padding:5px;
    }}

    .mv-time {{
        font-size:11px;
        min-width:65px;
    }}

    .mv-volume {{
        display:none;
    }}

    .mv-menu {{
        bottom:38px;
        min-width:125px;
    }}

}}

</style>


<div
    class="mv-player"
    data-mv-player
>

    <video
        data-mv-video
        playsinline
        webkit-playsinline
        preload="metadata"
        autoplay
        muted
    ></video>

    <button
        type="button"
        class="mv-center-play"
        data-mv-center
        aria-label="Play"
    >
        ▶
    </button>

    <div class="mv-player-controls">

        <div
            class="mv-progress"
            data-mv-progress
        >
            <div
                class="mv-buffer"
                data-mv-buffer
            ></div>

            <div
                class="mv-played"
                data-mv-played
            ></div>

            <div
                class="mv-progress-dot"
                data-mv-dot
            ></div>
        </div>

        <div class="mv-controls-row">

            <button
                type="button"
                class="mv-btn"
                data-mv-play
                aria-label="Play/Pause"
            >
                ▶
            </button>

            <button
                type="button"
                class="mv-btn"
                data-mv-mute
                aria-label="Mute"
            >
                🔊
            </button>

            <input
                class="mv-volume"
                data-mv-volume
                type="range"
                min="0"
                max="1"
                step="0.05"
                value="1"
            >

            <span
                class="mv-time"
                data-mv-time
            >
                0:00 / 0:00
            </span>

            <span class="mv-spacer"></span>


            <!-- QUALITY -->

            <div class="mv-menu-wrap">

                <button
                    type="button"
                    class="mv-btn"
                    data-mv-quality-btn
                >
                    ⚙ Quality
                </button>

                <div
                    class="mv-menu"
                    data-mv-quality-menu
                ></div>

            </div>


            <!-- LANGUAGE -->

            <div class="mv-menu-wrap">

                <button
                    type="button"
                    class="mv-btn"
                    data-mv-language-btn
                >
                    🌐 {safe_language}
                </button>

                <div
                    class="mv-menu"
                    data-mv-language-menu
                >
                    <button
                        type="button"
                        class="active"
                        disabled
                    >
                        🔊 {safe_language}
                    </button>
                </div>

            </div>


            <!-- SPEED -->

            <div class="mv-menu-wrap">

                <button
                    type="button"
                    class="mv-btn"
                    data-mv-speed-btn
                >
                    1x
                </button>

                <div
                    class="mv-menu"
                    data-mv-speed-menu
                >
                    <button data-speed="0.5">0.5x</button>
                    <button data-speed="0.75">0.75x</button>
                    <button data-speed="1" class="active">1x</button>
                    <button data-speed="1.25">1.25x</button>
                    <button data-speed="1.5">1.5x</button>
                    <button data-speed="2">2x</button>
                </div>

            </div>


            <!-- FULLSCREEN -->

            <button
                type="button"
                class="mv-btn"
                data-mv-fullscreen
                aria-label="Fullscreen"
            >
                ⛶
            </button>

        </div>

    </div>

</div>

</div>


<script>
(function() {{

    var players =
        document.querySelectorAll(
            '[data-mv-player]'
        );

    var SOURCES =
        {sources_json};

    function initPlayer(root) {{

        var video =
            root.querySelector(
                '[data-mv-video]'
            );

        var playBtn =
            root.querySelector(
                '[data-mv-play]'
            );

        var centerBtn =
            root.querySelector(
                '[data-mv-center]'
            );

        var muteBtn =
            root.querySelector(
                '[data-mv-mute]'
            );

        var volume =
            root.querySelector(
                '[data-mv-volume]'
            );

        var progress =
            root.querySelector(
                '[data-mv-progress]'
            );

        var buffer =
            root.querySelector(
                '[data-mv-buffer]'
            );

        var played =
            root.querySelector(
                '[data-mv-played]'
            );

        var dot =
            root.querySelector(
                '[data-mv-dot]'
            );

        var time =
            root.querySelector(
                '[data-mv-time]'
            );

        var qualityBtn =
            root.querySelector(
                '[data-mv-quality-btn]'
            );

        var qualityMenu =
            root.querySelector(
                '[data-mv-quality-menu]'
            );

        var languageBtn =
            root.querySelector(
                '[data-mv-language-btn]'
            );

        var languageMenu =
            root.querySelector(
                '[data-mv-language-menu]'
            );

        var speedBtn =
            root.querySelector(
                '[data-mv-speed-btn]'
            );

        var speedMenu =
            root.querySelector(
                '[data-mv-speed-menu]'
            );

        var fullscreenBtn =
            root.querySelector(
                '[data-mv-fullscreen]'
            );


        var currentQuality =
            'auto';


        function formatTime(sec) {{

            if (!isFinite(sec)) {{
                return '0:00';
            }}

            sec = Math.floor(sec);

            var h =
                Math.floor(sec / 3600);

            var m =
                Math.floor(
                    (sec % 3600) / 60
                );

            var s =
                sec % 60;

            if (h > 0) {{

                return (
                    h +
                    ':' +
                    String(m).padStart(2,'0') +
                    ':' +
                    String(s).padStart(2,'0')
                );

            }}

            return (
                m +
                ':' +
                String(s).padStart(2,'0')
            );
        }}


        function updateTime() {{

            var current =
                video.currentTime || 0;

            var duration =
                video.duration || 0;

            time.textContent =
                formatTime(current) +
                ' / ' +
                formatTime(duration);

            if (duration > 0) {{

                var percent =
                    (current / duration) * 100;

                played.style.width =
                    percent + '%';

                dot.style.left =
                    percent + '%';

            }}

        }}


        function updateBuffer() {{

            try {{

                if (
                    video.buffered.length &&
                    video.duration
                ) {{

                    var end =
                        video.buffered.end(
                            video.buffered.length - 1
                        );

                    var percent =
                        (end / video.duration) * 100;

                    buffer.style.width =
                        Math.min(
                            percent,
                            100
                        ) + '%';

                }}

            }} catch(e) {{}}

        }}


        function updatePlayButton() {{

            if (video.paused) {{

                playBtn.textContent = '▶';
                centerBtn.textContent = '▶';
                centerBtn.style.display = 'flex';

            }} else {{

                playBtn.textContent = '❚❚';
                centerBtn.textContent = '❚❚';

                setTimeout(
                    function() {{

                        if (!video.paused) {{
                            centerBtn.style.display =
                                'none';
                        }}

                    }},
                    500
                );

            }}

        }}


        function togglePlay() {{

            if (video.paused) {{

                video.play().catch(
                    function() {{}}
                );

            }} else {{

                video.pause();

            }}

        }}


        function setMute() {{

            video.muted =
                !video.muted;

            if (video.muted) {{

                muteBtn.textContent = '🔇';

            }} else {{

                muteBtn.textContent = '🔊';

            }}

        }}


        function selectAutoQuality() {{

            if (!SOURCES.length) {{
                return null;
            }}

            var width =
                window.innerWidth || 720;

            var wanted;

            if (width <= 480) {{
                wanted = 480;
            }} else if (width <= 720) {{
                wanted = 720;
            }} else {{
                wanted =
                    Math.max.apply(
                        null,
                        SOURCES.map(
                            function(s) {{
                                return s.quality;
                            }}
                        )
                    );
            }}

            var exact =
                SOURCES.find(
                    function(s) {{
                        return (
                            s.quality === wanted
                        );
                    }}
                );

            if (exact) {{
                return exact;
            }}

            return SOURCES[
                SOURCES.length - 1
            ];
        }}


        function findSource(q) {{

            return SOURCES.find(
                function(s) {{
                    return s.quality === q;
                }}
            );

        }}


        function loadSource(
            source,
            keepTime
        ) {{

            if (!source) {{
                return;
            }}

            var oldTime =
                video.currentTime || 0;

            var wasPlaying =
                !video.paused;

            video.src = source.url;

            video.load();

            video.addEventListener(
                'loadedmetadata',
                function onMeta() {{

                    video.removeEventListener(
                        'loadedmetadata',
                        onMeta
                    );

                    if (
                        keepTime &&
                        isFinite(oldTime) &&
                        oldTime > 0 &&
                        video.duration
                    ) {{

                        video.currentTime =
                            Math.min(
                                oldTime,
                                Math.max(
                                    0,
                                    video.duration - 0.5
                                )
                            );

                    }}

                    if (wasPlaying || keepTime) {{

                        video.play().catch(
                            function() {{}}
                        );

                    }}

                }
            );

        }}


        function buildQualityMenu() {{

            qualityMenu.innerHTML = '';

            var auto =
                document.createElement('button');

            auto.textContent =
                'Auto';

            if (currentQuality === 'auto') {{
                auto.className = 'active';
            }}

            auto.addEventListener(
                'click',
                function() {{

                    currentQuality = 'auto';

                    qualityBtn.textContent =
                        '⚙ Auto';

                    closeMenus();

                    var src =
                        selectAutoQuality();

                    loadSource(
                        src,
                        true
                    );

                    buildQualityMenu();

                }}
            );

            qualityMenu.appendChild(auto);


            var sorted =
                SOURCES.slice().sort(
                    function(a,b) {{
                        return (
                            b.quality -
                            a.quality
                        );
                    }}
                );


            sorted.forEach(
                function(source) {{

                    var b =
                        document.createElement(
                            'button'
                        );

                    b.textContent =
                        source.quality +
                        'p';

                    if (
                        currentQuality ===
                        source.quality
                    ) {{
                        b.className =
                            'active';
                    }}

                    b.addEventListener(
                        'click',
                        function() {{

                            currentQuality =
                                source.quality;

                            qualityBtn.textContent =
                                '⚙ ' +
                                source.quality +
                                'p';

                            closeMenus();

                            loadSource(
                                source,
                                true
                            );

                            buildQualityMenu();

                        }
                    );

                    qualityMenu.appendChild(b);

                }
            );

        }}


        function closeMenus() {{

            var menus =
                root.querySelectorAll(
                    '.mv-menu'
                );

            menus.forEach(
                function(menu) {{
                    menu.classList.remove(
                        'show'
                    );
                }}
            );

        }}


        function toggleMenu(menu) {{

            var open =
                menu.classList.contains(
                    'show'
                );

            closeMenus();

            if (!open) {{
                menu.classList.add(
                    'show'
                );
            }}

        }}


        playBtn.addEventListener(
            'click',
            togglePlay
        );

        centerBtn.addEventListener(
            'click',
            togglePlay
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

        video.addEventListener(
            'progress',
            updateBuffer
        );

        video.addEventListener(
            'loadedmetadata',
            updateTime
        );


        muteBtn.addEventListener(
            'click',
            setMute
        );


        volume.addEventListener(
            'input',
            function() {{

                video.volume =
                    parseFloat(
                        volume.value
                    );

                if (
                    video.volume > 0 &&
                    video.muted
                ) {{
                    video.muted = false;
                    muteBtn.textContent =
                        '🔊';
                }}

            }}
        );


        progress.addEventListener(
            'click',
            function(e) {{

                if (!video.duration) {{
                    return;
                }}

                var rect =
                    progress.getBoundingClientRect();

                var x =
                    e.clientX -
                    rect.left;

                var percent =
                    x / rect.width;

                video.currentTime =
                    percent *
                    video.duration;

            }}
        );


        qualityBtn.addEventListener(
            'click',
            function(e) {{

                e.stopPropagation();

                toggleMenu(
                    qualityMenu
                );

            }}
        );


        languageBtn.addEventListener(
            'click',
            function(e) {{

                e.stopPropagation();

                toggleMenu(
                    languageMenu
                );

            }}
        );


        speedBtn.addEventListener(
            'click',
            function(e) {{

                e.stopPropagation();

                toggleMenu(
                    speedMenu
                );

            }}
        );


        speedMenu
            .querySelectorAll(
                'button[data-speed]'
            )
            .forEach(
                function(btn) {{

                    btn.addEventListener(
                        'click',
                        function() {{

                            var speed =
                                parseFloat(
                                    btn.getAttribute(
                                        'data-speed'
                                    )
                                );

                            video.playbackRate =
                                speed;

                            speedBtn.textContent =
                                speed + 'x';

                            speedMenu
                                .querySelectorAll(
                                    'button'
                                )
                                .forEach(
                                    function(x) {{
                                        x.classList.remove(
                                            'active'
                                        );
                                    }}
                                );

                            btn.classList.add(
                                'active'
                            );

                            closeMenus();

                        }
                    );

                }
            );


        fullscreenBtn.addEventListener(
            'click',
            function() {{

                if (
                    document.fullscreenElement
                ) {{

                    document.exitFullscreen();

                }} else if (
                    root.requestFullscreen
                ) {{

                    root.requestFullscreen();

                }} else if (
                    root.webkitRequestFullscreen
                ) {{

                    root.webkitRequestFullscreen();

                }}

            }
        );


        document.addEventListener(
            'click',
            function() {{
                closeMenus();
            }}
        );


        root.addEventListener(
            'click',
            function(e) {{
                e.stopPropagation();
            }}
        );


        buildQualityMenu();


        /*
         * Initial source.
         *
         * Auto chooses a suitable quality
         * based on screen width.
         */
        var initial =
            selectAutoQuality();

        if (initial) {{

            currentQuality =
                'auto';

            loadSource(
                initial,
                false
            );

        }}


        /*
         * Browser autoplay policy normally
         * allows muted autoplay.
         */
        video.muted = true;

        video.play().catch(
            function() {{

                centerBtn.style.display =
                    'flex';

            }}
        );


        updatePlayButton();

    }}


    for (
        var i = 0;
        i < players.length;
        i++
    ) {{

        initPlayer(
            players[i]
        );

    }}

}})();
</script>
"""

    return player


# ============================================================
# BLOGGER HTML
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

    title = e(
        meta["title"]
    )

    year = meta["release_year"]

    ytxt = (
        f" ({year})"
        if year
        else ""
    )

    lang_known = (
        meta["language"]
        and
        meta["language"].lower()
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

    # ========================================================
    # DOWNLOAD BUTTON
    # ========================================================

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
        '<h3 style="'
        'text-align:center">'
        '{}'
        '</h3>'
    )

    # ========================================================
    # MOVIE INFO
    # ========================================================

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

    # ========================================================
    # PAGE PARTS
    # ========================================================

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
                " - "
                + lang
                + " film"
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

    # ========================================================
    # CUSTOM PLAYER
    # ========================================================

    parts.append(
        h3.format(
            f"Watch {title} Online"
        )
    )

    parts.append(
        build_video_player(
            outputs,
            meta["language"]
        )
    )

    # ========================================================
    # REVIEW
    # ========================================================

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

    # ========================================================
    # THEMES
    # ========================================================

    if meta["themes"]:

        parts.append(
            h3.format("Themes")
        )

        parts.append(
            "<ul>"
            +
            "".join(
                f"<li>{e(t)}</li>"
                for t in meta["themes"]
            )
            +
            "</ul>"
        )

    # ========================================================
    # SCREENSHOTS
    # ========================================================

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

    # ========================================================
    # DOWNLOADS
    # ========================================================

    parts.append(hr)

    parts.append(
        h3.format("Download Links")
    )

    for h, fid, size in outputs:

        direct = (
            drive_download_url(fid)
            .replace("&", "&amp;")
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
            f'<a '
            f'class="mv-dl" '
            f'data-fid="{e(fid)}" '
            f'href="{direct}" '
            f'rel="noopener" '
            f'style="{btn}">'
            '&#11015;&#9889;'
            'DOWNLOAD NOW'
            '&#9889;&#11015;'
            '</a>'
        )

    parts.append(hr)

    # ========================================================
    # FAQ
    # ========================================================

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

    # ========================================================
    # ENDING
    # ========================================================

    parts.append(
        '<h3 style="'
        'text-align:center;'
        'color:#f0a0ff">'
        'Winding Up &#10084;&#65039;'
        '</h3>'
    )

    parts.append(
        TIMER_SCRIPT
    )

    return "\n".join(parts)


# ============================================================
# MAIN PROCESS
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

    # ========================================================
    # DOWNLOAD SOURCE
    # ========================================================

    log(
        "Downloading original..."
    )

    download(
        video["id"],
        src
    )

    dur, w, h, fps = probe(
        src
    )

    short = min(
        w,
        h
    )

    log(
        f"Duration {dur / 60:.1f} min, "
        f"{w}x{h}, "
        f"{fps:.2f} fps"
    )

    # ========================================================
    # SCREENSHOTS
    # ========================================================

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

    # ========================================================
    # GEMINI ANALYSIS
    # ========================================================

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
        "  Language:",
        meta["language"]
    )

    log(
        "  Labels:",
        meta["labels"]
    )

    # ========================================================
    # RESOLUTION TARGETS
    # ========================================================

    targets = sorted(
        {
            t
            for t in RESOLUTIONS
            if t <= short * 1.05
        }
    )

    if not targets:
        targets = [short]

    # ========================================================
    # TRANSCODE + UPLOAD
    # ========================================================

    outputs = []

    for t in targets:

        out = str(
            job
            / f"{slug}_{t}p.mp4"
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

        size = os.path.getsize(
            out
        )

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
            (
                t,
                fid,
                size
            )
        )

        # Free disk
        os.remove(out)

    # ========================================================
    # UPLOAD IMAGES
    # ========================================================

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

    # ========================================================
    # LABELS
    # ========================================================

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
            else
            [
                g
                for g in meta["genres"]
            ][:2]
            +
            [meta["language"]]
        )

    labels = [
        str(l)[:40]
        for l in labels
        if l
    ][:8]

    # ========================================================
    # BUILD BLOGGER CONTENT
    # ========================================================

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
        "kind":
            "blogger#post",

        "title":
            f'{meta["title"]}'
            f'{ytxt}'
            f'{ltxt}'
            ' Movie - Watch Online & Download',

        "content":
            content,

        "labels":
            labels,
    }

    # ========================================================
    # CREATE BLOGGER POST
    # ========================================================

    post = retry(
        lambda: blogger.posts().insert(
            blogId=BLOG_ID,
            body=body,
            isDraft=not PUBLISH
        ).execute()
    )

    log(
        "Blogger post created:",
        post.get("url")
        or post.get("id"),
        "(DRAFT)"
        if not PUBLISH
        else "(PUBLISHED)"
    )

    # ========================================================
    # MOVE SOURCE VIDEO
    # ========================================================

    drive.files().update(
        fileId=video["id"],
        addParents=processed_folder,
        removeParents=INPUT_FOLDER,
        fields="id"
    ).execute()

    # ========================================================
    # CLEAN JOB
    # ========================================================

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
            "No new videos in the "
            "input folder. Nothing to do."
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

        except Exception:

            failed += 1

            traceback.print_exc()

    return (
        1
        if failed
        else 0
    )


if __name__ == "__main__":
    sys.exit(
        main()
)
