"""
Movie Bot: Drive video -> multi-resolution -> screenshots + 9:16 thumbnail
-> Gemini title/description/labels -> Blogger post (draft by default).
Runs on GitHub Actions. All settings come from environment variables.

PLAYER:
- Custom responsive 16:9 HTML5 video player
- Quality selector
- Language selector
- Playback speed
- Fullscreen
- Progress / buffer / volume / mute
- Google Drive uploaded MP4 sources
- 23.976 FPS is DISPLAYED as 24 FPS
- Original source FPS is NOT forcibly changed during transcoding
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
    os.environ.get(
        "MAX_VIDEOS",
        "1"
    )
)

PUBLISH = (
    os.environ.get(
        "PUBLISH",
        "false"
    ).lower()
    == "true"
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
    2160: 21
}

WORK = Path("work")

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/blogger",
]


# ============================================================
# LOG
# ============================================================

def log(*a):
    print(*a, flush=True)


# ============================================================
# RETRY
# ============================================================

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

            time.sleep(
                5 * (i + 1)
            )


# ============================================================
# RUN COMMAND
# ============================================================

def run(cmd):

    subprocess.run(
        cmd,
        check=True
    )


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
        "mimeType":
            "application/vnd.google-apps.folder",
        "parents": [INPUT_FOLDER]
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
                    "  download "
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

        _, resp = retry(
            req.next_chunk
        )

    fid = resp["id"]

    retry(
        lambda:
        drive.permissions().create(
            fileId=fid,
            body={
                "type": "anyone",
                "role": "reader"
            }
        ).execute()
    )

    return fid


# ============================================================
# FFMPEG / FPS
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
    """
    Display FPS as a whole number.

    Examples:
    23.976 -> 24
    24    -> 24
    29.97 -> 30
    30    -> 30

    IMPORTANT:
    This only changes the displayed label.
    It does NOT change the actual video FPS.
    """

    try:
        return str(round(float(x)))
    except Exception:
        return "24"


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


# ============================================================
# SCREENSHOTS
# ============================================================

def make_screenshots(
    src,
    dur,
    outdir
):

    files = []

    for i in range(SCREENSHOTS):

        t = (
            dur *
            (i + 1) /
            (SCREENSHOTS + 1)
        )

        p = str(
            outdir /
            f"shot_{i + 1}.jpg"
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


# ============================================================
# THUMBNAIL
# ============================================================

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
    centre crop from a frame at 35%.
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
        outdir /
        "thumb_9x16.jpg"
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


# ============================================================
# GEMINI ANALYSIS INPUTS
# ============================================================

def analysis_inputs(
    src,
    dur,
    outdir
):

    frames = []

    n = 12

    for i in range(n):

        t = (
            dur *
            (i + 1) /
            (n + 1)
        )

        p = (
            outdir /
            f"an_{i}.jpg"
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
        outdir /
        "an_audio.mp3"
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


# ============================================================
# TRANSCODE
# ============================================================

def transcode(
    src,
    target,
    w,
    h,
    out
):

    """
    target = length of the SHORT side
    (480/720/1080).

    IMPORTANT:
    No '-r' is used here.

    Therefore the original FPS is preserved
    as much as possible.

    Example:
    23.976 source -> output remains 23.976
    24 source     -> output remains 24
    25 source     -> output remains 25

    The Blogger information label rounds
    23.976 -> 24fps.
    """

    crf = CRF.get(
        target,
        23
    )

    if w >= h:

        vf = (
            f"scale=-2:{target}"
        )

    else:

        vf = (
            f"scale={target}:-2"
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

        name = (
            urllib.parse
            .unquote_plus(f)
            .strip()
        )

        if (
            name
            and
            name.lower() not in seen
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

        labels = parse_labels(
            page
        )

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
            str(r)
            .strip()
            .lower()
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

    if m:
        return int(m.group(1))

    return None


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
You are a film writer. You are publishing an ORIGINAL film, on its own director's film
blog. You get 12 frames spread across the film and an audio sample.

File name hint (may be messy):
"{hint}"

Language hint:
"{LANGUAGE_HINT}"

Director name:
"{DIRECTOR_NAME}"

Rules:

- Write everything in your own words, in natural English.
- Never copy text from any website, film or review.
- Base it ONLY on what you can actually see and hear in the frames and audio.
- If you are unsure, stay general and talk about mood, visuals, sound and themes instead of specific plot facts.
- Never invent cast, crew, awards, festivals, ratings, box office or plot facts you cannot see.
- No piracy words.
- The title must be a real film title of 1-6 words.
- No hashtags, emojis, year or words like "trending reels".
- If the file name hint is messy, invent a fitting title from what the film is about.

Return ONLY JSON with these keys:

title: the film title,

tagline: one sentence, max 20 words,

synopsis: 2 short paragraphs (about 120 words), spoiler-light, separated by a blank line,

review: 3-4 paragraphs (about 300 words) analysing tone, visual style and camera work,
sound and music, performances in general terms, themes and who will enjoy the film,
separated by blank lines,

themes: list of 3-5 short phrases,

faq: list of 4 objects {{"q": "...", "a": "..."}}
with 1-2 sentence answers about the film,

genres: list of 1-3 genres,

language: main spoken language,

content_rating: one of
"General audience", "Teen and above", "Mature audience",

tags: list of up to 6 short keywords,

labels: pick 1-4 categories that best fit this film,
ONLY from this exact list
(copy the spelling exactly):

{json.dumps(site_labels)}

Judge by language spoken, film industry/country,
and type (movie, web series, trailer, song, etc.).
Ignore labels about video encoding or file format.
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
                lambda:
                gclient.models.generate_content(
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
            data.get("content_rating")
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
# HELPERS
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


def img_url(fid):

    return (
        f"https://lh3.googleusercontent.com/d/{fid}"
    )


# ============================================================
# DOWNLOAD TIMER
# ============================================================

TIMER_SCRIPT = """<script>
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

          b.style.opacity =
            '0.85';

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
                b.getAttribute(
                  'data-fid'
                ) +
                '&export=download&confirm=t';

              setTimeout(
                function () {

                  b.innerHTML =
                    label;

                  b.style.opacity =
                    '1';

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
</script>""" % WAIT_SECONDS


# ============================================================
# CUSTOM VIDEO PLAYER
# ============================================================

def build_player(
    outputs,
    language
):

    """
    Builds the custom player.

    outputs:
        [(height, drive_file_id, size), ...]

    The Drive IDs are injected directly into
    the JavaScript configuration.
    """

    source_map = {}

    for h, fid, size in outputs:

        source_map[str(h)] = (
            "https://drive.usercontent.google.com/"
            "download?id="
            + fid
            + "&export=download&confirm=t"
        )

    sources_json = json.dumps(
        source_map,
        ensure_ascii=False
    )

    default_language = (
        language
        if language
        else "Hindi"
    )

    default_language_js = json.dumps(
        default_language
    )

    player = f"""
<style>

*{{
    box-sizing:border-box;
    -webkit-tap-highlight-color:transparent;
}}

.mv-player{{
    position:relative;
    width:100%;
    max-width:1280px;
    margin:20px auto;
    background:#000;
    aspect-ratio:16/9;
    overflow:hidden;
    color:#fff;
    font-family:Arial,Helvetica,sans-serif;
}}

.mv-player video{{
    width:100%;
    height:100%;
    position:absolute;
    inset:0;
    background:#000;
    object-fit:contain;
}}

.mv-language{{
    position:absolute;
    top:16px;
    left:16px;
    z-index:20;
    background:#35a94b;
    color:#000;
    border:0;
    border-radius:4px;
    padding:8px 15px;
    font-size:14px;
    font-weight:bold;
}}

.mv-center-play{{
    position:absolute;
    left:50%;
    top:50%;
    transform:translate(-50%,-50%);
    width:70px;
    height:70px;
    border:0;
    border-radius:50%;
    background:#35a94b;
    display:flex;
    align-items:center;
    justify-content:center;
    z-index:15;
    cursor:pointer;
}}

.mv-center-play svg{{
    width:32px;
    height:32px;
    fill:#000;
    margin-left:4px;
}}

.mv-controls{{
    position:absolute;
    left:0;
    right:0;
    bottom:0;
    z-index:30;
    padding:0 16px 9px;
    background:linear-gradient(
        to top,
        rgba(0,0,0,.98),
        rgba(0,0,0,.7),
        transparent
    );
}}

.mv-progress-area{{
    width:100%;
    height:28px;
    display:flex;
    align-items:center;
    cursor:pointer;
}}

.mv-progress{{
    position:relative;
    width:100%;
    height:4px;
    background:#777;
    border-radius:5px;
}}

.mv-buffer,
.mv-progress-fill{{
    position:absolute;
    left:0;
    top:0;
    height:100%;
    border-radius:5px;
}}

.mv-buffer{{
    width:0;
    background:#aaa;
}}

.mv-progress-fill{{
    width:0;
    background:#35a94b;
}}

.mv-handle{{
    position:absolute;
    top:50%;
    left:0;
    width:13px;
    height:13px;
    transform:translate(-50%,-50%);
    background:#fff;
    border-radius:50%;
}}

.mv-row{{
    display:flex;
    align-items:center;
    height:40px;
    gap:2px;
}}

.mv-btn{{
    width:38px;
    height:38px;
    border:0;
    background:transparent;
    color:#fff;
    display:flex;
    align-items:center;
    justify-content:center;
    cursor:pointer;
}}

.mv-btn svg{{
    width:21px;
    height:21px;
    fill:#fff;
}}

.mv-time{{
    font-size:13px;
    white-space:nowrap;
    margin-left:3px;
}}

.mv-spacer{{
    flex:1;
}}

.mv-volume{{
    width:75px;
    height:4px;
}}

.mv-settings{{
    position:absolute;
    right:16px;
    bottom:67px;
    width:235px;
    max-height:350px;
    overflow-y:auto;
    background:rgba(20,20,20,.98);
    border-radius:6px;
    z-index:100;
    display:none;
    box-shadow:0 5px 25px rgba(0,0,0,.7);
}}

.mv-settings.show{{
    display:block;
}}

.mv-settings-title{{
    padding:13px;
    border-bottom:1px solid #444;
    font-weight:bold;
}}

.mv-section{{
    padding:7px 0;
    border-bottom:1px solid #333;
}}

.mv-section:last-child{{
    border-bottom:0;
}}

.mv-label{{
    padding:6px 13px;
    font-size:11px;
    color:#999;
    text-transform:uppercase;
}}

.mv-option{{
    width:100%;
    border:0;
    background:transparent;
    color:#fff;
    padding:11px 13px;
    display:flex;
    align-items:center;
    justify-content:space-between;
    text-align:left;
    cursor:pointer;
    font-size:14px;
}}

.mv-option:hover{{
    background:#292929;
}}

.mv-option.active{{
    color:#35a94b;
}}

.mv-check{{
    visibility:hidden;
}}

.mv-option.active .mv-check{{
    visibility:visible;
}}

@media(max-width:600px){{

    .mv-language{{
        top:10px;
        left:10px;
        padding:7px 12px;
        font-size:12px;
    }}

    .mv-center-play{{
        width:60px;
        height:60px;
    }}

    .mv-center-play svg{{
        width:27px;
        height:27px;
    }}

    .mv-controls{{
        padding:0 8px 5px;
    }}

    .mv-btn{{
        width:34px;
        height:34px;
    }}

    .mv-btn svg{{
        width:18px;
        height:18px;
    }}

    .mv-volume{{
        display:none;
    }}

    .mv-time{{
        font-size:11px;
    }}

    .mv-settings{{
        right:8px;
        bottom:58px;
        width:205px;
    }}

}}

</style>


<div class="mv-player" id="mvPlayer">

    <video
        id="mvVideo"
        playsinline
        preload="metadata">
    </video>


    <div
        class="mv-language"
        id="mvLanguage">
        {html.escape(default_language)}
    </div>


    <button
        class="mv-center-play"
        id="mvCenterPlay">

        <svg viewBox="0 0 24 24">
            <path d="M8 5v14l11-7z"/>
        </svg>

    </button>


    <div
        class="mv-settings"
        id="mvSettings">

        <div class="mv-settings-title">
            Settings
        </div>


        <div class="mv-section">

            <div class="mv-label">
                Quality
            </div>

            <div id="mvQualityList">

                <button
                    class="mv-option active"
                    data-quality="auto">

                    <span>Auto</span>
                    <span class="mv-check">✓</span>

                </button>

                <button
                    class="mv-option"
                    data-quality="1080">

                    <span>1080p</span>
                    <span class="mv-check">✓</span>

                </button>

                <button
                    class="mv-option"
                    data-quality="720">

                    <span>720p</span>
                    <span class="mv-check">✓</span>

                </button>

                <button
                    class="mv-option"
                    data-quality="480">

                    <span>480p</span>
                    <span class="mv-check">✓</span>

                </button>

                <button
                    class="mv-option"
                    data-quality="360">

                    <span>360p</span>
                    <span class="mv-check">✓</span>

                </button>

            </div>

        </div>


        <div class="mv-section">

            <div class="mv-label">
                Language
            </div>

            <div id="mvLanguageList">

                <button
                    class="mv-option active"
                    data-language="hi">

                    <span>Hindi</span>
                    <span class="mv-check">✓</span>

                </button>

                <button
                    class="mv-option"
                    data-language="en">

                    <span>English</span>
                    <span class="mv-check">✓</span>

                </button>

                <button
                    class="mv-option"
                    data-language="bn">

                    <span>Bengali</span>
                    <span class="mv-check">✓</span>

                </button>

                <button
                    class="mv-option"
                    data-language="ar">

                    <span>Arabic</span>
                    <span class="mv-check">✓</span>

                </button>

            </div>

        </div>


        <div class="mv-section">

            <div class="mv-label">
                Speed
            </div>

            <div id="mvSpeedList">

                <button
                    class="mv-option"
                    data-speed="0.5">

                    <span>0.5x</span>
                    <span class="mv-check">✓</span>

                </button>

                <button
                    class="mv-option"
                    data-speed="0.75">

                    <span>0.75x</span>
                    <span class="mv-check">✓</span>

                </button>

                <button
                    class="mv-option active"
                    data-speed="1">

                    <span>Normal</span>
                    <span class="mv-check">✓</span>

                </button>

                <button
                    class="mv-option"
                    data-speed="1.25">

                    <span>1.25x</span>
                    <span class="mv-check">✓</span>

                </button>

                <button
                    class="mv-option"
                    data-speed="1.5">

                    <span>1.5x</span>
                    <span class="mv-check">✓</span>

                </button>

                <button
                    class="mv-option"
                    data-speed="2">

                    <span>2x</span>
                    <span class="mv-check">✓</span>

                </button>

            </div>

        </div>


        <div class="mv-section">

            <button
                class="mv-option"
                id="mvFullscreenOption">

                <span>Fullscreen</span>
                <span>⛶</span>

            </button>

        </div>

    </div>


    <div class="mv-controls">

        <div
            class="mv-progress-area"
            id="mvProgressArea">

            <div class="mv-progress">

                <div
                    class="mv-buffer"
                    id="mvBuffer">
                </div>

                <div
                    class="mv-progress-fill"
                    id="mvFill">
                </div>

                <div
                    class="mv-handle"
                    id="mvHandle">
                </div>

            </div>

        </div>


        <div class="mv-row">

            <button
                class="mv-btn"
                id="mvPlay">

                <svg
                    id="mvPlayIcon"
                    viewBox="0 0 24 24">

                    <path d="M8 5v14l11-7z"/>

                </svg>

            </button>


            <div class="mv-time">

                <span id="mvCurrent">
                    0:00
                </span>

                &nbsp;/&nbsp;

                <span id="mvTotal">
                    0:00
                </span>

            </div>


            <button
                class="mv-btn"
                id="mvMute">

                <svg viewBox="0 0 24 24">

                    <path d="
                    M4 9v6h4l5 4V5L8 9H4z
                    M16 8.5a5 5 0 0 1 0 7
                    M18.5 6a8.5 8.5 0 0 1 0 12"/>

                </svg>

            </button>


            <input
                class="mv-volume"
                id="mvVolume"
                type="range"
                min="0"
                max="1"
                step="0.01"
                value="1">


            <div class="mv-spacer"></div>


            <button
                class="mv-btn"
                id="mvSettingsButton">

                <svg viewBox="0 0 24 24">

                    <path d="
                    M19.43 12.98
                    c.04-.32.07-.65.07-.98
                    s-.02-.66-.07-.98
                    l2.11-1.65
                    -.2-.35-2.49-4.31
                    -.42.18-2.49 1
                    c-.51-.4-1.08-.73-1.69-.98
                    L13.95 2h-3.9
                    l-.3 2.91
                    c-.61.25-1.18.59-1.69.98
                    l-2.49-1-.42-.18
                    -2.49 4.31-.2.35
                    2.11 1.65
                    c-.04.32-.08.65-.08.98
                    s.03.66.08.98
                    l-2.11 1.65
                    .2.35 2.49 4.31
                    .42-.18 2.49-1
                    c.51.4 1.08.73 1.69.98
                    L10.05 22h3.9
                    l.3-2.91
                    c.61-.25 1.18-.58 1.69-.98
                    l2.49 1 .42.18
                    2.49-4.31.2-.35
                    -2.11-1.65z
                    M12 15.5
                    A3.5 3.5 0 1 1
                    12 8.5
                    A3.5 3.5 0 0 1
                    12 15.5z"/>

                </svg>

            </button>


            <button
                class="mv-btn"
                id="mvFullscreen">

                <svg viewBox="0 0 24 24">

                    <path d="
                    M4 4h6v2H6v4H4V4z
                    M14 4h6v6h-2V6h-4V4z
                    M4 14h2v4h4v2H4v-6z
                    M18 14h2v6h-6v-2h4v-4z"/>

                </svg>

            </button>

        </div>

    </div>

</div>


<script>

(function(){{

    /* ======================================================
       VIDEO SOURCES GENERATED BY BOT
       ====================================================== */

    const VIDEO_SOURCES =
        {sources_json};


    const DEFAULT_LANGUAGE =
        {default_language_js};


    /* ======================================================
       ELEMENTS
       ====================================================== */

    const video =
        document.getElementById(
            "mvVideo"
        );

    const player =
        document.getElementById(
            "mvPlayer"
        );

    const play =
        document.getElementById(
            "mvPlay"
        );

    const centerPlay =
        document.getElementById(
            "mvCenterPlay"
        );

    const playIcon =
        document.getElementById(
            "mvPlayIcon"
        );

    const current =
        document.getElementById(
            "mvCurrent"
        );

    const total =
        document.getElementById(
            "mvTotal"
        );

    const fill =
        document.getElementById(
            "mvFill"
        );

    const handle =
        document.getElementById(
            "mvHandle"
        );

    const buffer =
        document.getElementById(
            "mvBuffer"
        );

    const progressArea =
        document.getElementById(
            "mvProgressArea"
        );

    const volume =
        document.getElementById(
            "mvVolume"
        );

    const mute =
        document.getElementById(
            "mvMute"
        );

    const settingsButton =
        document.getElementById(
            "mvSettingsButton"
        );

    const settings =
        document.getElementById(
            "mvSettings"
        );

    const fullscreen =
        document.getElementById(
            "mvFullscreen"
        );

    const fullscreenOption =
        document.getElementById(
            "mvFullscreenOption"
        );

    const languageLabel =
        document.getElementById(
            "mvLanguage"
        );


    /* ======================================================
       FORMAT TIME
       ====================================================== */

    function formatTime(seconds){{

        if(!isFinite(seconds))
            return "0:00";

        seconds =
            Math.floor(seconds);

        const minutes =
            Math.floor(seconds / 60);

        const secs =
            seconds % 60;

        return (
            minutes +
            ":" +
            String(secs)
                .padStart(2,"0")
        );

    }}


    /* ======================================================
       LOAD VIDEO
       ====================================================== */

    function loadVideo(
        quality,
        restoreTime,
        wasPlaying
    ){{

        const url =
            VIDEO_SOURCES[
                String(quality)
            ];

        if(!url)
            return;

        video.src = url;

        video.load();

        video.addEventListener(
            "loadedmetadata",
            function restore(){{
                
                if(
                    restoreTime !== null &&
                    isFinite(restoreTime)
                ){{
                    try{{
                        video.currentTime =
                            Math.min(
                                restoreTime,
                                video.duration || restoreTime
                            );
                    }}catch(e){{}}
                }}

                if(wasPlaying){{
                    video.play()
                        .catch(function(){{}});
                }}

                video.removeEventListener(
                    "loadedmetadata",
                    restore
                );

            }}
        );

    }}


    /* ======================================================
       INITIAL QUALITY

       Prefer 720p if available.
       Otherwise highest available.
       ====================================================== */

    let currentQuality = null;

    if(VIDEO_SOURCES["720"]){{
        currentQuality = "720";
    }}
    else if(VIDEO_SOURCES["1080"]){{
        currentQuality = "1080";
    }}
    else if(VIDEO_SOURCES["480"]){{
        currentQuality = "480";
    }}
    else if(VIDEO_SOURCES["360"]){{
        currentQuality = "360";
    }}


    if(currentQuality){{
        loadVideo(
            currentQuality,
            null,
            false
        );
    }}


    /* ======================================================
       PLAY / PAUSE
       ====================================================== */

    function togglePlay(){{

        if(video.paused){{

            video.play()
                .catch(function(){{}});

        }}else{{

            video.pause();

        }}

    }}


    play.addEventListener(
        "click",
        togglePlay
    );

    centerPlay.addEventListener(
        "click",
        togglePlay
    );


    video.addEventListener(
        "play",
        function(){{

            playIcon.innerHTML =
                '<path d="M7 5h4v14H7zM13 5h4v14h-4z"/>';

            centerPlay.style.display =
                "none";

        }}
    );


    video.addEventListener(
        "pause",
        function(){{

            playIcon.innerHTML =
                '<path d="M8 5v14l11-7z"/>';

            centerPlay.style.display =
                "flex";

        }}
    );


    /* ======================================================
       METADATA
       ====================================================== */

    video.addEventListener(
        "loadedmetadata",
        function(){{

            total.textContent =
                formatTime(
                    video.duration
                );

        }}
    );


    /* ======================================================
       TIME
       ====================================================== */

    video.addEventListener(
        "timeupdate",
        function(){{

            if(!video.duration)
                return;

            const percent =
                (
                    video.currentTime /
                    video.duration
                ) * 100;

            fill.style.width =
                percent + "%";

            handle.style.left =
                percent + "%";

            current.textContent =
                formatTime(
                    video.currentTime
                );

        }}
    );


    /* ======================================================
       SEEK
       ====================================================== */

    progressArea.addEventListener(
        "click",
        function(e){{

            if(!video.duration)
                return;

            const rect =
                this.getBoundingClientRect();

            const percent =
                (
                    e.clientX -
                    rect.left
                ) / rect.width;

            video.currentTime =
                percent *
                video.duration;

        }}
    );


    /* ======================================================
       BUFFER
       ====================================================== */

    video.addEventListener(
        "progress",
        function(){{

            if(
                !video.duration ||
                !video.buffered.length
            )
                return;

            try{{

                const end =
                    video.buffered.end(
                        video.buffered.length - 1
                    );

                const percent =
                    (
                        end /
                        video.duration
                    ) * 100;

                buffer.style.width =
                    Math.min(
                        percent,
                        100
                    ) + "%";

            }}catch(e){{}}

        }}
    );


    /* ======================================================
       VOLUME
       ====================================================== */

    volume.addEventListener(
        "input",
        function(){{

            video.volume =
                Number(this.value);

            video.muted =
                video.volume === 0;

        }}
    );


    mute.addEventListener(
        "click",
        function(){{

            video.muted =
                !video.muted;

            volume.value =
                video.muted
                ? 0
                : video.volume || 1;

        }}
    );


    /* ======================================================
       SETTINGS
       ====================================================== */

    settingsButton.addEventListener(
        "click",
        function(e){{

            e.stopPropagation();

            settings.classList.toggle(
                "show"
            );

        }}
    );


    settings.addEventListener(
        "click",
        function(e){{

            e.stopPropagation();

        }}
    );


    document.addEventListener(
        "click",
        function(){{

            settings.classList.remove(
                "show"
            );

        }}
    );


    /* ======================================================
       QUALITY
       ====================================================== */

    document
        .getElementById(
            "mvQualityList"
        )
        .querySelectorAll(
            ".mv-option"
        )
        .forEach(
            function(button){{

                button.addEventListener(
                    "click",
                    function(){{

                        const quality =
                            this.dataset.quality;


                        /*
                         * AUTO
                         *
                         * With separate MP4 files,
                         * true adaptive Auto is not possible.
                         *
                         * Therefore Auto keeps the
                         * currently selected source.
                         */

                        if(
                            quality === "auto"
                        ){{

                            document
                                .querySelectorAll(
                                    "#mvQualityList .mv-option"
                                )
                                .forEach(
                                    function(b){{
                                        b.classList.remove(
                                            "active"
                                        );
                                    }}
                                );

                            this.classList.add(
                                "active"
                            );

                            return;

                        }}


                        const url =
                            VIDEO_SOURCES[
                                quality
                            ];

                        if(!url)
                            return;


                        const wasPlaying =
                            !video.paused;

                        const time =
                            video.currentTime;


                        currentQuality =
                            quality;


                        document
                            .querySelectorAll(
                                "#mvQualityList .mv-option"
                            )
                            .forEach(
                                function(b){{
                                    b.classList.remove(
                                        "active"
                                    );
                                }}
                            );

                        this.classList.add(
                            "active"
                        );


                        loadVideo(
                            quality,
                            time,
                            wasPlaying
                        );

                    }}
                );

            }
        );


    /* ======================================================
       LANGUAGE
       ====================================================== */

    document
        .getElementById(
            "mvLanguageList"
        )
        .querySelectorAll(
            ".mv-option"
        )
        .forEach(
            function(button){{

                button.addEventListener(
                    "click",
                    function(){{

                        /*
                         * The bot currently uploads
                         * one audio-language version.
                         *
                         * This selector changes the
                         * visible language label.
                         *
                         * Real audio switching requires
                         * separate language sources or
                         * HLS audio tracks.
                         */

                        const name =
                            this.querySelector(
                                "span"
                            ).textContent;

                        languageLabel.textContent =
                            name;


                        document
                            .querySelectorAll(
                                "#mvLanguageList .mv-option"
                            )
                            .forEach(
                                function(b){{
                                    b.classList.remove(
                                        "active"
                                    );
                                }}
                            );

                        this.classList.add(
                            "active"
                        );

                    }}
                );

            }}
        );


    /* ======================================================
       SPEED
       ====================================================== */

    document
        .getElementById(
            "mvSpeedList"
        )
        .querySelectorAll(
            ".mv-option"
        )
        .forEach(
            function(button){{

                button.addEventListener(
                    "click",
                    function(){{

                        video.playbackRate =
                            Number(
                                this.dataset.speed
                            );


                        document
                            .querySelectorAll(
                                "#mvSpeedList .mv-option"
                            )
                            .forEach(
                                function(b){{
                                    b.classList.remove(
                                        "active"
                                    );
                                }}
                            );

                        this.classList.add(
                            "active"
                        );

                    }}
                );

            }}
        );


    /* ======================================================
       FULLSCREEN
       ====================================================== */

    async function goFullscreen(){{

        try{{

            if(
                !document.fullscreenElement
            ){{

                if(
                    player.requestFullscreen
                ){{
                    await player.requestFullscreen();
                }}
                else if(
                    player.webkitRequestFullscreen
                ){{
                    player.webkitRequestFullscreen();
                }}

            }}
            else{{

                if(
                    document.exitFullscreen
                ){{
                    await document.exitFullscreen();
                }}

            }}

        }}
        catch(error){{

            console.log(error);

        }}

    }}


    fullscreen.addEventListener(
        "click",
        goFullscreen
    );

    fullscreenOption.addEventListener(
        "click",
        goFullscreen
    );


    /* ======================================================
       DOUBLE CLICK FULLSCREEN
       ====================================================== */

    video.addEventListener(
        "dblclick",
        goFullscreen
    );


    /* ======================================================
       KEYBOARD CONTROLS
       ====================================================== */

    document.addEventListener(
        "keydown",
        function(e){{

            if(
                e.target &&
                (
                    e.target.tagName === "INPUT" ||
                    e.target.tagName === "TEXTAREA"
                )
            ){{
                return;
            }}


            if(e.code === "Space"){{

                e.preventDefault();

                togglePlay();

            }}


            if(e.code === "ArrowRight"){{

                video.currentTime =
                    Math.min(
                        video.duration || 0,
                        video.currentTime + 5
                    );

            }}


            if(e.code === "ArrowLeft"){{

                video.currentTime =
                    Math.max(
                        0,
                        video.currentTime - 5
                    );

            }}

        }}
    );


}})();

</script>
"""

    return player


# ============================================================
# POST HTML
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

    # IMPORTANT:
    # 23.976 -> 24fps
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
    # BUTTON STYLE
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
        "background:linear-gradient("
        "90deg,#57a51c,#1f4fb4"
        ");"
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
        '<h3 style="text-align:center">{}</h3>'
    )


    # ========================================================
    # INFO
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
    # HTML PARTS
    # ========================================================

    parts = [

        f'''
        <div style="text-align:center">
            <img
                src="{img_url(thumb_id)}"
                alt="{title}"
                width="270"
                style="
                    max-width:60%;
                    height:auto;
                    border-radius:8px
                "
            />
        </div>
        ''',

        f'''
        <p style="text-align:center">
            <b>
                {title}{ytxt}
            </b>
            {
                " - " + lang + " film"
                if lang_known
                else ""
            }
        </p>
        '''
    ]


    if meta["tagline"]:

        parts.append(
            f'''
            <p style="text-align:center">
                <i>
                    {e(meta["tagline"])}
                </i>
            </p>
            '''
        )


    parts.append(
        f"<p>{syn[0]}</p>"
    )


    parts.append(
        h3.format(
            "Movie Info"
        )
    )


    parts.append(
        "<p>"
        +
        "<br/>".join(info)
        +
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
    # CUSTOM PLAYER
    # ========================================================

    parts.append(
        h3.format(
            f"Watch {title} Online"
        )
    )


    parts.append(
        build_player(
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
            h3.format(
                "Themes"
            )
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
        h3.format(
            "Screenshots"
        )
    )

    for fid in shot_ids:

        parts.append(
            f'''
            <p style="text-align:center">
                <img
                    src="{img_url(fid)}"
                    alt="{title} screenshot"
                    style="
                        max-width:100%;
                        height:auto
                    "
                />
            </p>
            '''
        )


    parts.append(hr)


    # ========================================================
    # DOWNLOAD LINKS
    # ========================================================

    parts.append(
        h3.format(
            "Download Links"
        )
    )


    for h, fid, size in outputs:

        direct = (
            "https://drive.usercontent.google.com/"
            "download?id="
            f"{fid}"
            "&amp;export=download"
            "&amp;confirm=t"
        )

        parts.append(
            f'''
            <h4 style="{head}">
                {title}{ytxt}{lang_tag}
                {h}p x264 {fps_txt}fps
                [{human(size)}]
            </h4>
            '''
        )

        parts.append(
            f'''
            <a
                class="mv-dl"
                data-fid="{fid}"
                href="{direct}"
                rel="noopener"
                style="{btn}"
            >
                &#11015;&#9889;
                DOWNLOAD NOW
                &#9889;&#11015;
            </a>
            '''
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
                f"""
                <h4>
                    {e(str(f['q']))}
                </h4>

                <p>
                    {e(str(f['a']))}
                </p>
                """
            )


    # ========================================================
    # END
    # ========================================================

    parts.append(
        '''
        <h3
            style="
                text-align:center;
                color:#f0a0ff
            "
        >
            Winding Up &#10084;&#65039;
        </h3>
        '''
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
        WORK /
        video["id"]
    )

    job.mkdir(
        parents=True,
        exist_ok=True
    )

    src = str(
        job /
        "source.mp4"
    )

    slug = re.sub(
        r"[^a-zA-Z0-9]+",
        "-",
        Path(name).stem
    ).strip("-").lower() or "movie"


    # ========================================================
    # DOWNLOAD ORIGINAL
    # ========================================================

    log(
        "Downloading original..."
    )

    download(
        video["id"],
        src
    )


    # ========================================================
    # PROBE
    # ========================================================

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
        f"{fps:.3f} fps "
        f"(display: {fmt_fps(fps)}fps)"
    )


    # ========================================================
    # SCREENSHOTS / THUMBNAIL
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
    # GEMINI
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
        "  Labels:",
        meta["labels"]
    )


    # ========================================================
    # RESOLUTIONS
    # ========================================================

    targets = sorted({
        t
        for t in RESOLUTIONS
        if t <= short * 1.05
    }) or [short]


    outputs = []


    # ========================================================
    # TRANSCODE + UPLOAD
    # ========================================================

    for t in targets:

        out = str(
            job /
            f"{slug}_{t}p.mp4"
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

        os.remove(
            out
        )


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
    # BUILD BLOGGER HTML
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
            " Movie - Watch Online & Download",

        "content":
            content,

        "labels":
            labels
    }


    # ========================================================
    # BLOGGER POST
    # ========================================================

    post = retry(
        lambda:
        blogger.posts().insert(
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
    # MOVE ORIGINAL TO PROCESSED
    # ========================================================

    drive.files().update(
        fileId=video["id"],
        addParents=processed_folder,
        removeParents=INPUT_FOLDER,
        fields="id"
    ).execute()


    # ========================================================
    # CLEAN WORK DIRECTORY
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

        except Exception:

            failed += 1

            traceback.print_exc()


    return (
        1
        if failed
        else 0
    )


# ============================================================
# ENTRY
# ============================================================

if __name__ == "__main__":

    sys.exit(
        main()
    )
