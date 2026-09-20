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
    "LANGUAGE_HINT", ""
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
    "DIRECTOR_NAME", ""
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
    subprocess.run(
        cmd,
        check=True
    )


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

gclient = genai.Client(
    api_key=GEMINI_KEY
)


# ---------- Drive helpers ----------
def ensure_folder(name):
    q = (
        f"'{INPUT_FOLDER}' in parents and "
        f"name='{name}' and "
        "mimeType='application/vnd.google-apps.folder' "
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

        except Exception:
            pass

    return 30.0


def fmt_fps(x):
    """
    Display FPS as a whole number.

    23.976 -> 24
    24     -> 24
    29.97  -> 30
    30     -> 30

    This changes the displayed FPS text only.
    It does NOT force the video itself to 24fps.
    """
    return str(
        int(round(float(x)))
    )


def probe(path):
    out = subprocess.check_output(
        [
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
        ]
    )

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
        t = dur * (
            i + 1
        ) / (
            SCREENSHOTS + 1
        )

        p = str(
            outdir /
            f"shot_{i + 1}.jpg"
        )

        run(
            [
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
            ]
        )

        files.append(p)

    return files


def make_thumbnail(src, dur, w, h, outdir):
    """9:16 portrait thumbnail, 720x1280, centre crop from a frame at 35%."""

    if w * 16 >= h * 9:
        ch = h // 2 * 2
        cw = int(
            h * 9 / 16
        ) // 2 * 2
    else:
        cw = w // 2 * 2
        ch = int(
            w * 16 / 9
        ) // 2 * 2

    p = str(
        outdir /
        "thumb_9x16.jpg"
    )

    run(
        [
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
        ]
    )

    return p


def analysis_inputs(src, dur, outdir):
    frames = []

    n = 12

    for i in range(n):
        t = dur * (
            i + 1
        ) / (
            n + 1
        )

        p = outdir / f"an_{i}.jpg"

        run(
            [
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
            ]
        )

        frames.append(
            p.read_bytes()
        )

    audio = (
        outdir /
        "an_audio.mp3"
    )

    start = dur * 0.10

    run(
        [
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
        ]
    )

    return (
        frames,
        audio.read_bytes()
    )


def transcode(src, target, w, h, out):
    """target = length of the SHORT side (480/720/1080)."""

    crf = CRF.get(
        target,
        23
    )

    vf = (
        f"scale=-2:{target}"
        if w >= h
        else f"scale={target}:-2"
    )

    run(
        [
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
        ]
    )


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
            labels.append(name)

    return labels


def get_site_labels():
    """Read real labels from the blog menu."""

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
    """Release year from filename if available."""

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

    for model in dict.fromkeys(
        [
            GEMINI_MODEL,
            "gemini-flash-latest"
        ]
    ):
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
            data.get("faq")
            or []
        )
        if isinstance(f, dict)
        and f.get("q")
        and f.get("a")
    ]

    return {
        "title": (
            data.get("title")
            or hint
            or "Untitled Film"
        ),
        "tagline": (
            data.get("tagline")
            or ""
        ),
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
            for t in (
                data.get("themes")
                or []
            )
        ][:5],
        "faq": faq[:4],
        "genres": (
            data.get("genres")
            or ["Drama"]
        ),
        "language": (
            data.get("language")
            or LANGUAGE_HINT
            or "Unknown"
        ),
        "release_year": year,
        "content_rating": (
            data.get("content_rating")
            or "General audience"
        ),
        "tags": (
            data.get("tags")
            or []
        ),
        "labels": pick_labels(
            data.get("labels"),
            site_labels
        ),
    }


# ---------- post helpers ----------
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


# ---------- download timer ----------
TIMER_SCRIPT = """<script>
(function () {
  var WAIT = %d;

  var btns =
    document.querySelectorAll(
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
</script>""" % WAIT_SECONDS


# ============================================================
# CUSTOM PLAYER
#
# IMPORTANT:
# DO NOT change this to an f-string.
# This is the fix for:
# SyntaxError: single '}' is not allowed
# ============================================================

PLAYER_TEMPLATE = r'''<style>

.mv-player{
  --mv-green:#39ff72;
  --mv-panel:rgba(10,14,16,.96);

  position:relative;
  width:100%;
  max-width:1100px;
  margin:20px auto;

  background:#000;
  overflow:hidden;
  border-radius:10px;

  box-shadow:
    0 12px 35px rgba(0,0,0,.55);

  aspect-ratio:16/9;

  font-family:
    Arial,
    Helvetica,
    sans-serif;

  color:#fff;
}

.mv-player *{
  box-sizing:border-box;
}

.mv-player video{
  position:absolute;

  inset:0;

  width:100%;
  height:100%;

  background:#000;

  object-fit:contain;
}

.mv-player .mv-language{
  position:absolute;

  z-index:5;

  top:15px;
  left:15px;

  padding:6px 11px;

  border-radius:6px;

  background:rgba(0,0,0,.72);

  color:var(--mv-green);

  font-size:13px;
  font-weight:800;

  line-height:1;

  pointer-events:none;
}

.mv-player .mv-center-play{
  position:absolute;

  z-index:7;

  left:50%;
  top:50%;

  transform:
    translate(-50%,-50%);

  width:72px;
  height:72px;

  border:0;

  border-radius:50%;

  background:
    var(--mv-green);

  color:#000;

  display:flex;

  align-items:center;
  justify-content:center;

  cursor:pointer;

  font-size:28px;

  font-weight:900;

  box-shadow:
    0 6px 25px rgba(0,0,0,.5);
}

.mv-player .mv-center-play:hover{
  transform:
    translate(-50%,-50%)
    scale(1.05);
}

.mv-player .mv-controls{
  position:absolute;

  z-index:8;

  left:0;
  right:0;
  bottom:0;

  padding:
    40px 14px 12px;

  background:
    linear-gradient(
      to top,
      rgba(0,0,0,.94),
      rgba(0,0,0,.45),
      transparent
    );

  opacity:1;

  transition:
    opacity .2s ease;
}

.mv-player .mv-progress-wrap{
  position:relative;

  width:100%;

  height:5px;

  margin-bottom:10px;

  cursor:pointer;
}

.mv-player .mv-progress-bg,
.mv-player .mv-buffer,
.mv-player .mv-progress{
  position:absolute;

  left:0;
  top:0;

  height:100%;

  border-radius:10px;
}

.mv-player .mv-progress-bg{
  width:100%;

  background:
    rgba(255,255,255,.22);
}

.mv-player .mv-buffer{
  width:0;

  background:
    rgba(255,255,255,.35);
}

.mv-player .mv-progress{
  width:0;

  background:
    var(--mv-green);
}

.mv-player .mv-progress-handle{
  position:absolute;

  top:50%;
  left:0;

  width:13px;
  height:13px;

  border-radius:50%;

  background:
    var(--mv-green);

  transform:
    translate(-50%,-50%);

  box-shadow:
    0 0 7px
    rgba(57,255,114,.8);
}

.mv-player .mv-row{
  display:flex;

  align-items:center;

  gap:10px;

  min-width:0;
}

.mv-player .mv-btn{
  width:34px;
  height:34px;

  padding:0;

  border:0;

  background:transparent;

  color:#fff;

  cursor:pointer;

  border-radius:5px;

  display:flex;

  align-items:center;
  justify-content:center;

  font-size:18px;

  flex:0 0 auto;
}

.mv-player .mv-btn:hover{
  background:
    rgba(255,255,255,.12);
}

.mv-player .mv-time{
  min-width:90px;

  font-size:13px;

  color:#fff;

  user-select:none;

  white-space:nowrap;
}

.mv-player .mv-spacer{
  flex:1;
}

.mv-player .mv-volume{
  width:75px;

  accent-color:
    var(--mv-green);
}

.mv-player .mv-settings{
  position:relative;
}

.mv-player .mv-menu{
  display:none;

  position:absolute;

  right:0;
  bottom:45px;

  width:230px;

  max-width:
    calc(100vw - 30px);

  background:
    var(--mv-panel);

  border:
    1px solid
    rgba(255,255,255,.12);

  border-radius:8px;

  padding:10px;

  box-shadow:
    0 10px 35px
    rgba(0,0,0,.65);

  z-index:20;
}

.mv-player .mv-menu.show{
  display:block;
}

.mv-player .mv-menu-title{
  padding:6px 8px;

  color:#aaa;

  font-size:11px;

  font-weight:800;

  text-transform:uppercase;

  letter-spacing:.08em;
}

.mv-player .mv-option{
  display:flex;

  align-items:center;
  justify-content:space-between;

  width:100%;

  padding:9px 8px;

  border:0;

  border-radius:5px;

  background:transparent;

  color:#fff;

  text-align:left;

  cursor:pointer;

  font-size:13px;
}

.mv-player .mv-option:hover{
  background:
    rgba(255,255,255,.09);
}

.mv-player .mv-option.active{
  color:
    var(--mv-green);

  font-weight:800;
}

.mv-player .mv-submenu{
  border-top:
    1px solid
    rgba(255,255,255,.08);

  margin-top:6px;

  padding-top:6px;
}

.mv-player .mv-error{
  position:absolute;

  z-index:9;

  left:50%;
  top:20%;

  transform:
    translateX(-50%);

  width:90%;

  text-align:center;

  display:none;

  padding:10px 14px;

  border-radius:6px;

  background:
    rgba(150,0,0,.8);

  color:#fff;

  font-size:13px;
}

@media(max-width:600px){

  .mv-player{
    border-radius:6px;
  }

  .mv-player .mv-controls{
    padding-left:8px;
    padding-right:8px;
  }

  .mv-player .mv-time{
    min-width:74px;
    font-size:11px;
  }

  .mv-player .mv-volume{
    width:52px;
  }

  .mv-player .mv-center-play{
    width:62px;
    height:62px;
  }

  .mv-player .mv-language{
    top:9px;
    left:9px;
    font-size:11px;
  }

}

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
    __PLAYER_LANGUAGE__
  </div>


  <div
    class="mv-error"
    id="mvError">

    Video could not be loaded.
    Please try another quality.

  </div>


  <button
    class="mv-center-play"
    id="mvCenterPlay"
    type="button"
    aria-label="Play">

    ▶

  </button>


  <div
    class="mv-controls"
    id="mvControls">


    <div
      class="mv-progress-wrap"
      id="mvProgressWrap">

      <div
        class="mv-progress-bg">
      </div>

      <div
        class="mv-buffer"
        id="mvBuffer">
      </div>

      <div
        class="mv-progress"
        id="mvProgress">
      </div>

      <div
        class="mv-progress-handle"
        id="mvHandle">
      </div>

    </div>


    <div class="mv-row">


      <button
        class="mv-btn"
        id="mvPlay"
        type="button"
        aria-label="Play/Pause">

        ▶

      </button>


      <div
        class="mv-time"
        id="mvTime">

        00:00 / 00:00

      </div>


      <button
        class="mv-btn"
        id="mvMute"
        type="button"
        aria-label="Mute">

        🔊

      </button>


      <input
        class="mv-volume"
        id="mvVolume"
        type="range"
        min="0"
        max="1"
        step="0.05"
        value="1"
        aria-label="Volume">


      <div class="mv-spacer">
      </div>


      <div class="mv-settings">


        <button
          class="mv-btn"
          id="mvSettings"
          type="button"
          aria-label="Settings">

          ⚙

        </button>


        <div
          class="mv-menu"
          id="mvMenu">


          <div class="mv-menu-title">
            Quality
          </div>


          <button
            class="mv-option"
            type="button"
            data-quality="auto">

            Auto

            <span
              class="mv-check">
            </span>

          </button>


          <button
            class="mv-option"
            type="button"
            data-quality="1080">

            1080p

            <span
              class="mv-check">
            </span>

          </button>


          <button
            class="mv-option"
            type="button"
            data-quality="720">

            720p

            <span
              class="mv-check">
            </span>

          </button>


          <button
            class="mv-option"
            type="button"
            data-quality="480">

            480p

            <span
              class="mv-check">
            </span>

          </button>


          <button
            class="mv-option"
            type="button"
            data-quality="360">

            360p

            <span
              class="mv-check">
            </span>

          </button>


          <div class="mv-submenu">


            <div class="mv-menu-title">
              Language
            </div>


            <button
              class="mv-option mv-language-option"
              type="button"
              data-language="Hindi">

              Hindi

            </button>


            <button
              class="mv-option mv-language-option"
              type="button"
              data-language="English">

              English

            </button>


            <button
              class="mv-option mv-language-option"
              type="button"
              data-language="Bengali">

              Bengali

            </button>


            <button
              class="mv-option mv-language-option"
              type="button"
              data-language="Arabic">

              Arabic

            </button>


          </div>


          <div class="mv-submenu">


            <div class="mv-menu-title">
              Speed
            </div>


            <button
              class="mv-option mv-speed-option"
              type="button"
              data-speed="0.5">

              0.5x

            </button>


            <button
              class="mv-option mv-speed-option"
              type="button"
              data-speed="0.75">

              0.75x

            </button>


            <button
              class="mv-option mv-speed-option active"
              type="button"
              data-speed="1">

              1x

            </button>


            <button
              class="mv-option mv-speed-option"
              type="button"
              data-speed="1.25">

              1.25x

            </button>


            <button
              class="mv-option mv-speed-option"
              type="button"
              data-speed="1.5">

              1.5x

            </button>


            <button
              class="mv-option mv-speed-option"
              type="button"
              data-speed="2">

              2x

            </button>


          </div>


          <div class="mv-submenu">


            <button
              class="mv-option"
              id="mvFullscreenMenu"
              type="button">

              Fullscreen

            </button>


          </div>


        </div>

      </div>


      <button
        class="mv-btn"
        id="mvFullscreen"
        type="button"
        aria-label="Fullscreen">

        ⛶

      </button>


    </div>

  </div>

</div>


<script src="https://cdn.jsdelivr.net/npm/hls.js@0.14.17"></script>


<script>
(function () {

  'use strict';


  var VIDEO_SOURCES =
    __SOURCES_JSON__;


  var DEFAULT_LANGUAGE =
    __PLAYER_LANGUAGE_JSON__;


  var player =
    document.getElementById(
      'mvPlayer'
    );


  var video =
    document.getElementById(
      'mvVideo'
    );


  if (!player || !video) {
    return;
  }


  var playBtn =
    document.getElementById(
      'mvPlay'
    );


  var centerPlay =
    document.getElementById(
      'mvCenterPlay'
    );


  var muteBtn =
    document.getElementById(
      'mvMute'
    );


  var volume =
    document.getElementById(
      'mvVolume'
    );


  var progressWrap =
    document.getElementById(
      'mvProgressWrap'
    );


  var progress =
    document.getElementById(
      'mvProgress'
    );


  var buffer =
    document.getElementById(
      'mvBuffer'
    );


  var handle =
    document.getElementById(
      'mvHandle'
    );


  var timeText =
    document.getElementById(
      'mvTime'
    );


  var settingsBtn =
    document.getElementById(
      'mvSettings'
    );


  var menu =
    document.getElementById(
      'mvMenu'
    );


  var fullscreenBtn =
    document.getElementById(
      'mvFullscreen'
    );


  var fullscreenMenu =
    document.getElementById(
      'mvFullscreenMenu'
    );


  var languageLabel =
    document.getElementById(
      'mvLanguage'
    );


  var errorBox =
    document.getElementById(
      'mvError'
    );


  var hls = null;

  var currentQuality =
    'auto';

  var language =
    DEFAULT_LANGUAGE ||
    'Hindi';


  languageLabel.textContent =
    language;


  function getSource(quality) {

    if (!VIDEO_SOURCES) {
      return '';
    }

    return (
      VIDEO_SOURCES[
        String(quality)
      ] || ''
    );

  }


  function getAutoSource() {

    return (
      getSource('720') ||
      getSource('1080') ||
      getSource('480') ||
      getSource('360') ||
      ''
    );

  }


  function formatTime(seconds) {

    if (
      !isFinite(seconds) ||
      seconds < 0
    ) {
      seconds = 0;
    }


    seconds =
      Math.floor(seconds);


    var h =
      Math.floor(
        seconds / 3600
      );


    var m =
      Math.floor(
        (seconds % 3600) / 60
      );


    var s =
      seconds % 60;


    if (h > 0) {

      return (
        String(h).padStart(2, '0') +
        ':' +
        String(m).padStart(2, '0') +
        ':' +
        String(s).padStart(2, '0')
      );

    }


    return (
      String(m).padStart(2, '0') +
      ':' +
      String(s).padStart(2, '0')
    );

  }


  function updateTime() {

    var duration =
      video.duration || 0;


    var current =
      video.currentTime || 0;


    timeText.textContent =
      formatTime(current) +
      ' / ' +
      formatTime(duration);


    if (duration > 0) {

      var percent =
        (current / duration) * 100;


      progress.style.width =
        percent + '%';


      handle.style.left =
        percent + '%';

    } else {

      progress.style.width =
        '0%';

      handle.style.left =
        '0%';

    }

  }


  function updateBuffer() {

    try {

      if (
        !video.buffered.length ||
        !video.duration
      ) {

        buffer.style.width =
          '0%';

        return;
      }


      var end =
        video.buffered.end(
          video.buffered.length - 1
        );


      var percent =
        Math.min(
          100,
          (end / video.duration) * 100
        );


      buffer.style.width =
        percent + '%';


    } catch (e) {

      buffer.style.width =
        '0%';

    }

  }


  function updatePlayButtons() {

    if (video.paused) {

      playBtn.textContent =
        '▶';

      centerPlay.textContent =
        '▶';

      centerPlay.style.display =
        'flex';

    } else {

      playBtn.textContent =
        '❚❚';

      centerPlay.style.display =
        'none';

    }

  }


  function showError(show) {

    errorBox.style.display =
      show
        ? 'block'
        : 'none';

  }


  function destroyHls() {

    if (hls) {

      try {
        hls.destroy();
      } catch (e) {
      }

      hls = null;

    }

  }


  function playAfterSwitch(
    shouldPlay
  ) {

    if (!shouldPlay) {
      return;
    }


    var promise =
      video.play();


    if (
      promise &&
      promise.catch
    ) {

      promise.catch(
        function () {}
      );

    }

  }


  function switchSource(
    url,
    shouldPlay,
    savedTime
  ) {

    if (!url) {

      showError(true);

      return;

    }


    showError(false);


    destroyHls();


    var wasPlaying =
      typeof shouldPlay === 'boolean'
        ? shouldPlay
        : !video.paused;


    var oldTime =
      typeof savedTime === 'number'
        ? savedTime
        : (
            video.currentTime ||
            0
          );


    video.pause();


    video.src = url;


    video.load();


    function restore() {

      try {

        if (
          oldTime > 0 &&
          isFinite(video.duration)
        ) {

          video.currentTime =
            Math.min(
              oldTime,
              Math.max(
                0,
                video.duration - 0.25
              )
            );

        } else if (
          oldTime > 0
        ) {

          video.currentTime =
            oldTime;

        }

      } catch (e) {
      }


      playAfterSwitch(
        wasPlaying
      );


      updateTime();
      updateBuffer();

    }


    video.addEventListener(
      'loadedmetadata',
      restore,
      {
        once: true
      }
    );

  }


  function loadQuality(
    quality
  ) {

    currentQuality =
      quality;


    var url = '';


    if (
      quality === 'auto'
    ) {

      url =
        getAutoSource();

    } else {

      url =
        getSource(quality);

    }


    if (!url) {

      showError(true);

      return;

    }


    var currentTime =
      video.currentTime || 0;


    var wasPlaying =
      !video.paused;


    if (
      quality === 'auto' &&
      VIDEO_SOURCES.hls &&
      window.Hls &&
      Hls.isSupported()
    ) {

      showError(false);


      destroyHls();


      hls = new Hls({
        autoStartLoad: true
      });


      hls.loadSource(
        VIDEO_SOURCES.hls
      );


      hls.attachMedia(
        video
      );


      hls.on(
        Hls.Events.MANIFEST_PARSED,
        function () {

          if (
            currentTime > 0
          ) {

            try {

              video.currentTime =
                currentTime;

            } catch (e) {
            }

          }


          playAfterSwitch(
            wasPlaying
          );

        }
      );


      return;

    }


    switchSource(
      url,
      wasPlaying,
      currentTime
    );

  }


  function setActiveQuality() {

    var options =
      menu.querySelectorAll(
        '.mv-option[data-quality]'
      );


    for (
      var i = 0;
      i < options.length;
      i++
    ) {

      var option =
        options[i];


      var q =
        option.getAttribute(
          'data-quality'
        );


      option.classList.toggle(
        'active',
        q === currentQuality
      );


      var available =
        q === 'auto'
          ? !!getAutoSource()
          : !!getSource(q);


      if (!available) {

        option.style.display =
          'none';

      } else {

        option.style.display =
          'flex';

      }

    }

  }


  function setLanguage(
    lang
  ) {

    language =
      lang ||
      language ||
      'Hindi';


    languageLabel.textContent =
      language;


    var options =
      menu.querySelectorAll(
        '.mv-language-option'
      );


    for (
      var i = 0;
      i < options.length;
      i++
    ) {

      options[i].classList.toggle(
        'active',
        options[i].getAttribute(
          'data-language'
        ) === language
      );

    }

  }


  function setSpeed(
    speed
  ) {

    var value =
      parseFloat(speed);


    if (!isFinite(value)) {
      value = 1;
    }


    video.playbackRate =
      value;


    var options =
      menu.querySelectorAll(
        '.mv-speed-option'
      );


    for (
      var i = 0;
     
