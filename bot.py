"""
Movie Bot:
Google Drive video
    -> download
    -> multi-resolution FFmpeg
    -> screenshots + 9:16 thumbnail
    -> Gemini title/description/labels
    -> Internet Archive upload
    -> Blogger post

Storage:
- Google Drive: original input + processed source
- Internet Archive: videos + thumbnail + screenshots
- Blogger: embeds/links Internet Archive files

Download buttons:
- Same design as before
- Same countdown timer
- Direct Internet Archive download URL

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
from datetime import datetime
from pathlib import Path

import internetarchive

from google import genai
from google.auth.transport.requests import Request
from google.genai import types
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload


# ============================================================
# SETTINGS
# ============================================================

CLIENT_ID = os.environ["GOOGLE_CLIENT_ID"]
CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]
REFRESH_TOKEN = os.environ["GOOGLE_REFRESH_TOKEN"]

GEMINI_KEY = os.environ["GEMINI_API_KEY"]

BLOG_ID = os.environ["BLOG_ID"]
INPUT_FOLDER = os.environ["DRIVE_INPUT_FOLDER_ID"]

# Internet Archive
IA_ACCESS_KEY = os.environ["IA_ACCESS_KEY"]
IA_SECRET_KEY = os.environ["IA_SECRET_KEY"]

IA_IDENTIFIER_PREFIX = os.environ.get(
    "IA_IDENTIFIER_PREFIX",
    "movie-bot"
).strip()

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

            time.sleep(
                5 * (i + 1)
            )


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

creds.refresh(
    Request()
)

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
# GOOGLE DRIVE HELPERS
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
        fields="files(id,name,size,createdTime)",
        orderBy="createdTime"
    ).execute()

    return res.get("files", [])


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
        st.get("r_frame_rate")
    )

    return (
        float(d["format"]["duration"]),
        int(st["width"]),
        int(st["height"]),
        fps,
    )


def make_screenshots(
    src,
    dur,
    outdir
):
    files = []

    for i in range(SCREENSHOTS):

        t = dur * (
            i + 1
        ) / (
            SCREENSHOTS + 1
        )

        p = (
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
            str(p),
        ])

        files.append(str(p))

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
    Output: 720x1280.
    Uses frame at 35%.
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

    p = (
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
        str(p),
    ])

    return str(p)


def analysis_inputs(
    src,
    dur,
    outdir
):
    frames = []

    n = 12

    for i in range(n):

        t = dur * (
            i + 1
        ) / (
            n + 1
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
            str(p),
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
        str(audio),
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
    target = short-side resolution.

    Landscape:
        scale=-2:target

    Portrait:
        scale=target:-2
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
        out,
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
            seen.add(
                name.lower()
            )

            labels.append(
                name
            )

    return labels


def get_site_labels():
    """
    Read real label names from blog menu.
    """

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

    return re.sub(
        r"\s+",
        " ",
        t
    ).strip()


def analyze(
    filename_hint,
    frames,
    audio_bytes,
    site_labels
):

    hint = clean_hint(
        filename_hint
    )

    year = datetime.now().year

    prompt = f"""
You are a film writer. You are publishing an ORIGINAL film,
on its own director's film blog.

You get 12 frames spread across the film and an audio sample.

File name hint:
"{hint}"

Language hint:
"{LANGUAGE_HINT}"

Director name:
"{DIRECTOR_NAME}"

Rules:

- Write everything in your own words, in natural English.
- Never copy text from any website, film or review.
- Base it ONLY on what you can actually see and hear.
- If unsure, stay general.
- Never invent cast, crew, awards, festivals, ratings,
  box office or plot facts you cannot see.
- No piracy words such as leaked, HD print, free download
  full movie, WEB-DL, dual audio, 300mb.
- The title must be a real film title of 1-6 words.
- No hashtags.
- No emojis.
- No words like "trending reels".
- If the filename is messy, invent a fitting title
  from what the film appears to be about.

Return ONLY JSON.

Keys:

title:
film title

tagline:
one sentence, maximum 20 words

synopsis:
2 short paragraphs, about 120 words,
spoiler-light, separated by blank line

review:
3-4 paragraphs, about 300 words,
analysing tone, visual style, camera work,
sound, music, performances in general terms,
themes and who will enjoy the film

themes:
list of 3-5 short phrases

faq:
list of 4 objects:
{{"q": "...", "a": "..."}}

genres:
list of 1-3 genres

language:
main spoken language

release_year:
integer

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
- content type

Ignore labels about:
- video encoding
- file format
- resolution
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

            raw = (
                resp.text
                .strip()
            )

            raw = re.sub(
                r"^```json|```$",
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
            data.get("release_year")
            or year,

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
# INTERNET ARCHIVE
# ============================================================

def make_ia_identifier(
    meta,
    original_name,
    drive_id
):
    """
    Creates a unique IA identifier.

    Example:
    movie-bot-my-movie-a1b2c3d4
    """

    title = (
        meta.get("title")
        or Path(original_name).stem
        or "movie"
    )

    slug = re.sub(
        r"[^a-zA-Z0-9]+",
        "-",
        title
    )

    slug = slug.strip(
        "-"
    ).lower()

    if not slug:
        slug = "movie"

    # Last 8 chars of Drive file ID
    # prevents duplicate IA identifiers
    suffix = re.sub(
        r"[^a-zA-Z0-9]",
        "",
        str(drive_id)
    )[-8:].lower()

    return (
        f"{IA_IDENTIFIER_PREFIX}-"
        f"{slug}-"
        f"{suffix}"
    )


def ia_public_url(
    identifier,
    filename
):
    return (
        "https://archive.org/download/"
        f"{urllib.parse.quote(identifier)}/"
        f"{urllib.parse.quote(filename)}"
    )


def upload_to_internet_archive(
    identifier,
    files,
    meta
):
    """
    Upload all files into ONE Internet Archive item.

    files:
        [
            {
                "path": "...",
                "name": "...",
                "mediatype": "movies/image"
            }
        ]
    """

    upload_map = {}

    for item in files:

        upload_map[
            item["name"]
        ] = item["path"]

    metadata = {
        "title": meta["title"],
        "mediatype": "movies",
        "description": (
            meta.get("tagline")
            or meta["title"]
        ),
        "language": meta.get(
            "language",
            ""
        ),
        "year": str(
            meta.get(
                "release_year",
                datetime.now().year
            )
        ),
        "subject": ", ".join(
            str(x)
            for x in (
                meta.get("genres")
                or []
            )
        ),
    }

    log(
        f"  Internet Archive item: "
        f"{identifier}"
    )

    log(
        f"  Uploading "
        f"{len(upload_map)} files..."
    )

    result = retry(
        lambda: internetarchive.upload(
            identifier,
            files=upload_map,
            metadata=metadata,
            access_key=IA_ACCESS_KEY,
            secret_key=IA_SECRET_KEY,
            retries=5,
            verbose=True,
        ),
        tries=3
    )

    log(
        "  Internet Archive upload complete."
    )

    urls = {}

    for item in files:

        name = item["name"]

        urls[name] = ia_public_url(
            identifier,
            name
        )

    return urls


# ============================================================
# HTML HELPERS
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

TIMER_SCRIPT = """<script>
(function () {

  var WAIT = %d;

  var btns = document.querySelectorAll(
    'a.mv-dl[data-url]'
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
                b.getAttribute(
                  'data-url'
                );

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
</script>""" % WAIT_SECONDS


# ============================================================
# BLOGGER HTML
# ============================================================

def build_html(
    meta,
    thumb_url,
    shot_urls,
    outputs,
    fps,
    dur
):

    e = html.escape

    title = e(
        meta["title"]
    )

    year = meta[
        "release_year"
    ]

    lang = e(
        meta["language"]
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
        human(size)
        for _, _, size in outputs
    )

    # Prefer 720p player
    player_url = next(
        (
            url
            for h, url, _
            in outputs
            if h == 720
        ),
        outputs[-1][1]
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
        "background:linear-gradient("
        "90deg,#57a51c,#1f4fb4);"
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
        '<h3 style="text-align:center">'
        '{}'
        '</h3>'
    )

    # --------------------------------
    # Movie information
    # --------------------------------

    info = [
        f"<b>Movie Name:</b> {title}",
        f"<b>Release Year:</b> {year}",
    ]

    if DIRECTOR_NAME:

        info.append(
            f"<b>Directed by:</b> "
            f"{e(DIRECTOR_NAME)}"
        )

    info += [
        f"<b>Language:</b> {lang}",
        f"<b>Runtime:</b> "
        f"{fmt_runtime(dur)}",
        f"<b>Genres:</b> {genres}",
        f"<b>Content Advisory:</b> "
        f"{e(meta['content_rating'])}",
        f"<b>Quality:</b> {qualities}",
        f"<b>Frame Rate:</b> "
        f"{fps_txt}fps",
        f"<b>Size:</b> {sizes}",
    ]

    # --------------------------------
    # Start HTML
    # --------------------------------

    parts = [

        (
            '<div style="text-align:center">'
            f'<img src="{e(thumb_url)}" '
            f'alt="{title}" '
            'width="270" '
            'style="max-width:60%;'
            'height:auto;'
            'border-radius:8px"/>'
            '</div>'
        ),

        (
            '<p style="text-align:center">'
            f'<b>{title} ({year})</b> '
            f'- {lang} film'
            '</p>'
        ),
    ]

    if meta["tagline"]:

        parts.append(
            '<p style="text-align:center">'
            f'<i>{e(meta["tagline"])}</i>'
            '</p>'
        )

    if syn:

        parts.append(
            f"<p>{syn[0]}</p>"
        )

    # --------------------------------
    # Movie Info
    # --------------------------------

    parts.append(
        h3.format(
            "Movie Info"
        )
    )

    parts.append(
        "<p>" +
        "<br/>".join(info) +
        "</p>"
    )

    # --------------------------------
    # Synopsis
    # --------------------------------

    parts.append(
        h3.format(
            "Movie Synopsis / Plot"
        )
    )

    parts += [
        f"<p>{p}</p>"
        for p in syn
    ]

    # --------------------------------
    # Watch Online
    # --------------------------------

    parts.append(
        f"<h3>Watch {title} Online</h3>"
    )

    parts.append(
        '<video controls '
        'playsinline '
        'preload="metadata" '
        'style="width:100%;'
        'max-width:100%;'
        'height:auto;" '
        f'poster="{e(thumb_url)}">'
        f'<source src="{e(player_url)}" '
        'type="video/mp4">'
        'Your browser does not support '
        'HTML5 video.'
        '</video>'
    )

    # --------------------------------
    # Review
    # --------------------------------

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

    # --------------------------------
    # Themes
    # --------------------------------

    if meta["themes"]:

        parts.append(
            h3.format(
                "Themes"
            )
        )

        parts.append(
            "<ul>" +
            "".join(
                f"<li>{e(t)}</li>"
                for t in meta["themes"]
            ) +
            "</ul>"
        )

    # --------------------------------
    # Screenshots
    # --------------------------------

    parts.append(
        h3.format(
            "Screenshots"
        )
    )

    for url in shot_urls:

        parts.append(
            '<p style="text-align:center">'
            f'<img src="{e(url)}" '
            f'alt="{title} screenshot" '
            'style="max-width:100%;'
            'height:auto"/>'
            '</p>'
        )

    parts.append(
        hr
    )

    # --------------------------------
    # Download Links
    # --------------------------------

    parts.append(
        h3.format(
            "Download Links"
        )
    )

    for h, url, size in outputs:

        parts.append(
            f'<h4 style="{head}">'
            f'{title} ({year}) '
            f'<span style="color:#f2f200">'
            f'{{{lang}}}'
            f'</span> '
            f'{h}p x264 '
            f'{fps_txt}fps '
            f'[{human(size)}]'
            f'</h4>'
        )

        # SAME BUTTON DESIGN
        # URL is now Internet Archive
        parts.append(
            f'<a class="mv-dl" '
            f'data-url="{e(url)}" '
            f'href="{e(url)}" '
            f'rel="noopener" '
            f'style="{btn}">'
            '&#11015;&#9889;'
            'DOWNLOAD NOW'
            '&#9889;&#11015;'
            '</a>'
        )

    parts.append(
        hr
    )

    # --------------------------------
    # FAQ
    # --------------------------------

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

    # --------------------------------
    # Footer
    # --------------------------------

    parts.append(
        '<h3 style="text-align:center;'
        'color:#f0a0ff">'
        'Winding Up '
        '&#10084;&#65039;'
        '</h3>'
    )

    parts.append(
        TIMER_SCRIPT
    )

    return "\n".join(
        parts
    )


# ============================================================
# MAIN PROCESS
# ============================================================

def process(
    video,
    processed_folder
):

    name = video["name"]

    drive_id = video["id"]

    log(
        f"\n=== Processing: "
        f"{name} ==="
    )

    job = (
        WORK /
        drive_id
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
    ).strip(
        "-"
    ).lower()

    if not slug:
        slug = "movie"

    # --------------------------------
    # Download original
    # --------------------------------

    log(
        "Downloading original..."
    )

    download(
        drive_id,
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
        f"Duration "
        f"{dur / 60:.1f} min, "
        f"{w}x{h}, "
        f"{fps:.2f} fps"
    )

    # --------------------------------
    # Screenshots + thumbnail
    # --------------------------------

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

    # --------------------------------
    # Gemini
    # --------------------------------

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

    # --------------------------------
    # Resolution targets
    # --------------------------------

    targets = sorted({
        t
        for t in RESOLUTIONS
        if t <= short * 1.05
    })

    if not targets:
        targets = [short]

    # --------------------------------
    # Create converted videos
    # --------------------------------

    converted = []

    for t in targets:

        out = str(
            job /
            f"{slug}_{t}p.mp4"
        )

        log(
            f"Converting to "
            f"{t}p..."
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
            f"  Created "
            f"{t}p "
            f"({human(size)})"
        )

        converted.append({
            "path": out,
            "name":
                f"{slug}_{t}p.mp4",
            "height": t,
            "size": size,
            "mediatype":
                "movies",
        })

    # --------------------------------
    # Internet Archive identifier
    # --------------------------------

    identifier = make_ia_identifier(
        meta,
        name,
        drive_id
    )

    log(
        "Internet Archive identifier:",
        identifier
    )

    # --------------------------------
    # Prepare IA files
    # --------------------------------

    ia_files = []

    # Videos
    for item in converted:

        ia_files.append({
            "path":
                item["path"],

            "name":
                item["name"],

            "mediatype":
                "movies",
        })

    # Thumbnail
    ia_files.append({
        "path":
            thumb,

        "name":
            "thumbnail_9x16.jpg",

        "mediatype":
            "image",
    })

    # Screenshots
    for i, shot in enumerate(
        shots,
        start=1
    ):

        ia_files.append({
            "path":
                shot,

            "name":
                f"screenshot_{i}.jpg",

            "mediatype":
                "image",
        })

    # --------------------------------
    # Upload everything to IA
    # --------------------------------

    ia_urls = (
        upload_to_internet_archive(
            identifier,
            ia_files,
            meta
        )
    )

    # --------------------------------
    # Build video output list
    # --------------------------------

    outputs = []

    for item in converted:

        outputs.append((
            item["height"],
            ia_urls[item["name"]],
            item["size"],
        ))

    # Sort by resolution
    outputs.sort(
        key=lambda x: x[0]
    )

    # --------------------------------
    # Thumbnail URL
    # --------------------------------

    thumb_url = ia_urls[
        "thumbnail_9x16.jpg"
    ]

    # --------------------------------
    # Screenshot URLs
    # --------------------------------

    shot_urls = [
        ia_urls[
            f"screenshot_{i}.jpg"
        ]
        for i in range(
            1,
            len(shots) + 1
        )
    ]

    # --------------------------------
    # Blogger labels
    # --------------------------------

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
            labels = [unc]

        else:

            labels = (
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

    # --------------------------------
    # Build Blogger HTML
    # --------------------------------

    content = build_html(
        meta,
        thumb_url,
        shot_urls,
        outputs,
        fps,
        dur
    )

    # --------------------------------
    # Blogger post
    # --------------------------------

    body = {
        "kind":
            "blogger#post",

        "title": (
            f'{meta["title"]} '
            f'({meta["release_year"]}) '
            f'{meta["language"]} Movie - '
            'Watch Online & Download'
        ),

        "content":
            content,

        "labels":
            labels,
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
        post.get("url")
        or post.get("id"),
        "(DRAFT)"
        if not PUBLISH
        else "(PUBLISHED)"
    )

    # --------------------------------
    # Move original to _processed
    # --------------------------------

    drive.files().update(
        fileId=drive_id,
        addParents=processed_folder,
        removeParents=INPUT_FOLDER,
        fields="id"
    ).execute()

    # --------------------------------
    # Cleanup local work
    # --------------------------------

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
            "No new videos in "
            "the input folder. "
            "Nothing to do."
        )

        return 0

    # Only processed folder remains
    processed = ensure_folder(
        "_processed"
    )

    failed = 0

    for v in videos[:MAX_VIDEOS]:

        try:

            process(
                v,
                processed
            )

        except Exception:

            failed += 1

            log(
                "\n!!! PROCESSING FAILED !!!"
            )

            traceback.print_exc()

    return (
        1
        if failed
        else 0
    )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    sys.exit(
        main()
    )
