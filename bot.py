"""Movie Bot: Drive source -> FFmpeg -> Streamtape + VCDN -> Blogger."""
import hashlib, html, http.client, json, os, re, shutil, subprocess, sys, time, traceback
import urllib.error, urllib.parse, urllib.request
from pathlib import Path
from google import genai
from google.auth.transport.requests import Request
from google.genai import types
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

CLIENT_ID=os.environ["GOOGLE_CLIENT_ID"]
CLIENT_SECRET=os.environ["GOOGLE_CLIENT_SECRET"]
REFRESH_TOKEN=os.environ["GOOGLE_REFRESH_TOKEN"]
GEMINI_KEY=os.environ["GEMINI_API_KEY"]
BLOG_ID=os.environ["BLOG_ID"]
INPUT_FOLDER=os.environ["DRIVE_INPUT_FOLDER_ID"]
VCDN_API_KEY=os.environ["VCDN_API_KEY"].strip()
STREAMTAPE_LOGIN=os.environ["STREAMTAPE_LOGIN"].strip()
STREAMTAPE_KEY=os.environ["STREAMTAPE_KEY"].strip()
STREAMTAPE_FOLDER=os.environ.get("STREAMTAPE_FOLDER","").strip()
GEMINI_MODEL=os.environ.get("GEMINI_MODEL","gemini-3.6-flash").strip()
RESOLUTIONS=sorted({int(x) for x in os.environ.get("RESOLUTIONS","480,720,1080").split(",") if x.strip().isdigit()})
MAX_VIDEOS=int(os.environ.get("MAX_VIDEOS","1"))
PUBLISH=os.environ.get("PUBLISH","false").lower()=="true"
LANGUAGE_HINT=os.environ.get("LANGUAGE_HINT","").strip()
AUDIO_MINUTES=int(os.environ.get("AUDIO_MINUTES","10"))
WAIT_SECONDS=int(os.environ.get("WAIT_SECONDS","20"))
DIRECTOR_NAME=os.environ.get("DIRECTOR_NAME","").strip()
WORK=Path("work")
CRF={480:24,720:23,1080:22,1440:22,2160:21}
SCOPES=["https://www.googleapis.com/auth/drive","https://www.googleapis.com/auth/blogger"]

def log(*a): print(*a,flush=True)
def retry(fn,tries=4,delay=5):
    last=None
    for i in range(tries):
        try:return fn()
        except Exception as e:
            last=e
            if i==tries-1: raise
            log(f"  retry {i+1}/{tries-1}: {e}");time.sleep(delay*(i+1))
    raise last

def run(cmd):
    log("  Running:"," ".join(map(str,cmd)))
    p=subprocess.run(cmd,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
    if p.returncode:
        raise RuntimeError(f"Command failed ({p.returncode}):\n{p.stdout[-12000:]}")
    return p.stdout

creds=Credentials(None,refresh_token=REFRESH_TOKEN,token_uri="https://oauth2.googleapis.com/token",client_id=CLIENT_ID,client_secret=CLIENT_SECRET,scopes=SCOPES)
creds.refresh(Request())
drive=build("drive","v3",credentials=creds,cache_discovery=False)
blogger=build("blogger","v3",credentials=creds,cache_discovery=False)
gclient=genai.Client(api_key=GEMINI_KEY)

# ---------------- Drive: source only ----------------
def ensure_folder(name):
    q=f"'{INPUT_FOLDER}' in parents and name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    r=drive.files().list(q=q,fields="files(id,name)").execute()
    if r.get("files"): return r["files"][0]["id"]
    return drive.files().create(body={"name":name,"mimeType":"application/vnd.google-apps.folder","parents":[INPUT_FOLDER]},fields="id").execute()["id"]

def list_videos():
    q=f"'{INPUT_FOLDER}' in parents and mimeType contains 'video/' and trashed=false"
    return drive.files().list(q=q,fields="files(id,name,size)",orderBy="createdTime").execute().get("files",[])

def download(file_id,dest):
    req=drive.files().get_media(fileId=file_id)
    with open(dest,"wb") as f:
        dl=MediaIoBaseDownload(f,req,chunksize=64*1024*1024);done=False
        while not done:
            st,done=retry(dl.next_chunk)
            if st: log(f"  download {int(st.progress()*100)}%")

def move_source(video_id,processed_folder):
    drive.files().update(fileId=video_id,addParents=processed_folder,removeParents=INPUT_FOLDER,fields="id,parents").execute()

# ---------------- Streamtape ----------------
ST_BASE="https://api.streamtape.com"

def st_api(path,params=None,method="GET",data=None,timeout=120):
    params=dict(params or {});params.update({"login":STREAMTAPE_LOGIN,"key":STREAMTAPE_KEY})
    url=ST_BASE+path+"?"+urllib.parse.urlencode(params)
    def reqfn():
        req=urllib.request.Request(url,data=data,method=method,headers={"User-Agent":"MovieBot/1.0","Accept":"application/json"})
        try:
            with urllib.request.urlopen(req,timeout=timeout) as r:
                raw=r.read().decode("utf-8","replace"); obj=json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Streamtape HTTP {e.code}: {e.read().decode('utf-8','replace')[:2000]}")
        if obj.get("status")!=200: raise RuntimeError(f"Streamtape API error: {obj}")
        return obj.get("result")
    return retry(reqfn,tries=4,delay=3)

def st_list(folder=None):
    return st_api("/file/listfolder",{"folder":folder} if folder else {}).get("files",[])

def st_find(name,size=None,folder=None):
    try:
        files=st_list(folder)
        exact=[f for f in files if f.get("name")==name]
        if size is not None:
            exact=[f for f in exact if int(f.get("size",-1) or -1)==int(size)] or exact
        return exact[-1] if exact else None
    except Exception as e:
        log("  Streamtape list warning:",e);return None

def st_upload(path):
    name=os.path.basename(path);size=os.path.getsize(path)
    existing=st_find(name,size,STREAMTAPE_FOLDER or None)
    if existing and existing.get("link"):
        log("  Streamtape existing file reused:",existing["link"]);return existing
    sha=hashlib.sha256()
    with open(path,"rb") as f:
        for b in iter(lambda:f.read(8*1024*1024),b""): sha.update(b)
    params={"sha256":sha.hexdigest()}
    if STREAMTAPE_FOLDER: params["folder"]=STREAMTAPE_FOLDER
    up=st_api("/file/ul",params)
    url=up.get("url")
    if not url: raise RuntimeError(f"Streamtape upload URL missing: {up}")
    p=urllib.parse.urlsplit(url);conn=http.client.HTTPSConnection(p.netloc,timeout=1800)
    boundary="----MovieBotBoundary7MA4YWxk"
    prefix=(f"--{boundary}\r\nContent-Disposition: form-data; name=\"file1\"; filename=\"{name.replace(chr(34),chr(39))}\"\r\nContent-Type: video/mp4\r\n\r\n").encode()
    suffix=f"\r\n--{boundary}--\r\n".encode()
    try:
        conn.putrequest("POST",p.path or "/")
        conn.putheader("Content-Type",f"multipart/form-data; boundary={boundary}")
        conn.putheader("Content-Length",str(len(prefix)+size+len(suffix)))
        conn.putheader("User-Agent","MovieBot/1.0");conn.endheaders();conn.send(prefix)
        sent=0;last=-1
        with open(path,"rb") as f:
            while True:
                b=f.read(16*1024*1024)
                if not b:break
                conn.send(b);sent+=len(b);pct=int(sent*100/size) if size else 100
                if pct>=last+10 or pct==100:log(f"  Streamtape upload {pct}%");last=pct
        conn.send(suffix);resp=conn.getresponse();raw=resp.read().decode("utf-8","replace")
        if not 200<=resp.status<300: raise RuntimeError(f"Streamtape upload HTTP {resp.status}: {raw[:2000]}")
        result=json.loads(raw).get("result",{}) if raw else {}
    finally: conn.close()
    fid=result.get("id") or result.get("fileid") or result.get("file_id") or result.get("linkid")
    link=result.get("link")
    deadline=time.time()+8*60;found=None
    while time.time()<deadline:
        if fid:
            try:
                info=st_api("/file/info",{"file":fid})
                if isinstance(info,dict):
                    for k,v in info.items():
                        if isinstance(v,dict):
                            found=v;break
            except Exception: pass
        if not found: found=st_find(name,size,STREAMTAPE_FOLDER or None)
        if found:
            conv=found.get("converted") is True or str(found.get("convert","")).lower() in ("converted","complete","ready")
            if conv or found.get("link"):
                break
        time.sleep(5)
    if not found:
        raise RuntimeError(f"Streamtape upload succeeded but file was not found: {name}")
    link=found.get("link") or link
    linkid=found.get("linkid") or found.get("id") or fid
    if not link and linkid:
        link="https://streamtape.com/v/"+str(linkid)+"/"+urllib.parse.quote(name)
    if not link: raise RuntimeError(f"Streamtape stable link missing: {found}")
    found.update({"link":link,"id":found.get("id") or fid,"linkid":linkid})
    log("  Streamtape ready:",link);return found

def st_splash(file_id):
    try:return str(st_api("/file/getsplash",{"file":file_id}))
    except Exception as e:log("  Streamtape splash warning:",e);return ""

# ---------------- VCDN ----------------
# Canonical VCDN REST API host. The documented upload flow is:
# init -> POST chunks -> complete -> poll video status.
VCDN_HOST="cdn.vcdn.me"
VCDN_CHUNK_SIZE=8*1024*1024
VCDN_MIN_CHUNK_SIZE=256*1024


def vheaders(ct=None):
    h={
        "Authorization":f"Bearer {VCDN_API_KEY}",
        "X-API-Key":VCDN_API_KEY,
        "Accept":"application/json",
        "User-Agent":"MovieBot/1.0",
    }
    if ct:
        h["Content-Type"]=ct
    return h


def vjson(method,path,payload=None,timeout=120):
    body=json.dumps(payload).encode() if payload is not None else None
    headers=vheaders("application/json" if payload is not None else None)
    def request_once():
        req=urllib.request.Request(
            "https://"+VCDN_HOST+path,
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(req,timeout=timeout) as r:
                raw=r.read().decode("utf-8","replace")
                return json.loads(raw or "{}")
        except urllib.error.HTTPError as e:
            detail=e.read().decode("utf-8","replace")[:3000]
            raise RuntimeError(f"VCDN {method} {path}: HTTP {e.code}: {detail}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"VCDN {method} {path}: network error: {e}")
    return retry(request_once,tries=4,delay=3)


def vcdn_upload_chunk(upload_id,chunk,start,total_size,is_final=False):
    """Upload one sequential chunk to VCDN's documented chunk endpoint."""
    end=start+len(chunk)-1
    path=f"/api/v1/upload/{urllib.parse.quote(str(upload_id),safe='')}/chunk"
    headers=vheaders("application/octet-stream")
    headers["Content-Length"]=str(len(chunk))

    def request_once():
        req=urllib.request.Request(
            "https://"+VCDN_HOST+path,
            data=chunk,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req,timeout=1800) as r:
                raw=r.read().decode("utf-8","replace")
                return r.status,raw
        except urllib.error.HTTPError as e:
            detail=e.read().decode("utf-8","replace")[:3000]
            raise RuntimeError(f"VCDN chunk HTTP {e.code}: {detail}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"VCDN chunk network error: {e}")

    return retry(request_once,tries=4,delay=4)


def vcdn_upload(path,title):
    size=os.path.getsize(path)
    name=os.path.basename(path)
    log(f"Uploading {name} to VCDN ({size} bytes)...")

    # Do not use the /videos multipart endpoint here. In the previous run it
    # returned HTTP 413 for a ~20 MB file. The documented upload API is safer:
    # init -> chunk -> complete.
    init=vjson(
        "POST",
        "/api/v1/upload/init",
        {"filename":name,"title":title},
        timeout=120,
    )

    upload_id=init.get("upload_id") or init.get("uploadId")
    if not upload_id:
        raise RuntimeError(f"VCDN init did not return upload_id: {init}")

    log("  VCDN upload ID:",upload_id)

    # Start at 8 MB. If the VCDN reverse proxy rejects that chunk with 413,
    # restart the upload with 4 MB, then 1 MB. This avoids ever sending the
    # whole video as one HTTP request.
    chunk_size=VCDN_CHUNK_SIZE
    last_error=None
    upload_success=False

    while chunk_size>=VCDN_MIN_CHUNK_SIZE:
        try:
            sent=0
            last_pct=-1
            with open(path,"rb") as f:
                while sent<size:
                    want=min(chunk_size,size-sent)
                    chunk=f.read(want)
                    if not chunk:
                        raise RuntimeError("Unexpected end of file during VCDN upload")

                    vcdn_upload_chunk(upload_id,chunk,sent,size,sent+len(chunk)>=size)
                    sent+=len(chunk)
                    pct=int(sent*100/size) if size else 100
                    if pct>=last_pct+5 or pct==100:
                        log(f"  VCDN upload {pct}% ({sent}/{size} bytes)")
                        last_pct=pct

            log(f"  VCDN chunks uploaded successfully using {chunk_size//1024//1024} MB chunks")
            upload_success=True
            break

        except RuntimeError as e:
            last_error=e
            msg=str(e)
            if "HTTP 413" not in msg and "Request Entity Too Large" not in msg:
                raise
            if chunk_size//2<VCDN_MIN_CHUNK_SIZE:
                break
            chunk_size//=2
            log(f"  VCDN chunk too large; retrying upload with {chunk_size//1024//1024} MB chunks")
            # A failed init/upload may have left an incomplete upload object.
            # Create a fresh upload session before restarting from byte 0.
            init=vjson(
                "POST",
                "/api/v1/upload/init",
                {"filename":name,"title":title},
                timeout=120,
            )
            upload_id=init.get("upload_id") or init.get("uploadId")
            if not upload_id:
                raise RuntimeError(f"VCDN retry init did not return upload_id: {init}")
            log("  New VCDN upload ID:",upload_id)

    if not upload_success:
        raise RuntimeError(f"VCDN upload failed: {last_error}")

    # The VCDN API uses snake_case: upload_id.
    complete=vjson(
        "POST",
        "/api/v1/upload/complete",
        {"upload_id":upload_id},
        timeout=120,
    )

    vid=complete.get("id") or complete.get("video_id") or complete.get("videoId")
    if not vid:
        raise RuntimeError(f"VCDN complete did not return video id: {complete}")

    emb=complete.get("embed_url") or complete.get("embedUrl")
    play=complete.get("playback_url") or complete.get("playbackUrl")
    status=str(complete.get("status") or "processing").lower()

    if not emb:
        emb=f"https://embed.vcdn.me/{vid}"

    deadline=time.time()+900
    while time.time()<deadline:
        if status in ("failed","error"):
            raise RuntimeError(f"VCDN processing failed: {complete}")

        if status in ("ready","processed","complete","completed"):
            break

        time.sleep(5)
        info=vjson(
            "GET",
            f"/api/v1/videos/{urllib.parse.quote(str(vid),safe='')}",
            timeout=120,
        )
        status=str(info.get("status") or status).lower()
        emb=info.get("embed_url") or info.get("embedUrl") or emb
        play=info.get("playback_url") or info.get("playbackUrl") or play
        log("  VCDN status:",status)

    if status in ("failed","error"):
        raise RuntimeError(f"VCDN processing failed for {vid}")
    if not emb:
        emb=f"https://embed.vcdn.me/{vid}"

    log("  VCDN ready:",emb)
    return {
        "id":vid,
        "embed_url":emb,
        "playback_url":play,
        "status":status,
    }

# ---------------- FFmpeg ----------------
def parse_fps(v):
    try:
        a,b=str(v).split("/");return float(a)/float(b) if float(b) else 30.0
    except:return 30.0

def probe(path):
    d=json.loads(subprocess.check_output(["ffprobe","-v","error","-select_streams","v:0","-show_entries","stream=width,height,avg_frame_rate,r_frame_rate","-show_entries","format=duration","-of","json",path]))
    s=d["streams"][0];return float(d["format"]["duration"]),int(s["width"]),int(s["height"]),parse_fps(s.get("avg_frame_rate") or s.get("r_frame_rate"))

def even(n):return max(2,int(n)//2*2)
def target_size(target,w,h):
    if w>=h:return even(round(w*even(target)/h)),even(target)
    return even(target),even(round(h*even(target)/w))

def transcode(src,target,w,h,out):
    ow,oh=target_size(target,w,h);crf=CRF.get(target,23);vf=f"scale={ow}:{oh}:flags=lanczos"
    for preset,crf2 in (("veryfast",crf),("ultrafast",min(30,crf+2))):
        try:
            run(["ffmpeg","-hide_banner","-y","-i",src,"-map","0:v:0","-map","0:a:0?","-vf",vf,"-c:v","libx264","-preset",preset,"-crf",str(crf2),"-pix_fmt","yuv420p","-c:a","aac","-b:a","128k","-sn","-dn","-movflags","+faststart",out]);probe(out);log(f"  Created {ow}x{oh}: {out}");return
        except Exception as e:
            log(f"  FFmpeg {preset} failed: {e}");
            if os.path.exists(out):os.remove(out)
    raise RuntimeError(f"FFmpeg conversion failed for {target}p")

def analysis_inputs(src,dur,outdir):
    frames=[]
    for i in range(12):
        t=dur*(i+1)/13;p=outdir/f"an_{i}.jpg";run(["ffmpeg","-y","-loglevel","error","-ss",f"{t:.2f}","-i",src,"-frames:v","1","-vf","scale=512:-2","-q:v","5",str(p)]);frames.append(p.read_bytes())
    audio=outdir/"an_audio.mp3";start=dur*.1;run(["ffmpeg","-y","-loglevel","error","-ss",f"{start:.2f}","-t",str(min(AUDIO_MINUTES*60,max(1,dur-start))),"-i",src,"-vn","-ac","1","-ar","16000","-b:a","32k",str(audio)])
    return frames,audio.read_bytes()

# ---------------- Gemini ----------------
FALLBACK_LABELS=["Bollywood Content","Desi Junction","Dual Audio","Hindi Dubbed","Hindi TV Shows","Web Series","WWE","Hollywood Movies","Malayalam Movies","Marathi Movies","Mobile Movies","Multi Audio","Pakistani Movies","PC Games","Pre Release","Punjabi Movies","Single Video Songs","Tamil Movies","Telugu Movies","Trailers","Uncategorized"]
BLOCKED=re.compile(r"18\+|adult|xxx|hevc|x265",re.I);YEAR_RE=re.compile(r"(?<!\d)(19[5-9]\d|20[0-4]\d)(?!\d)")
def clean_hint(n):
    t=Path(n).stem;t=re.sub(r"[#@]\S+"," ",t);t=re.sub(r"[_.\-]+"," ",t);t=re.sub(r"[^\w\s]"," ",t);return re.sub(r"\s+"," ",t).strip()
def site_labels():
    try:
        url=blogger.blogs().get(blogId=BLOG_ID).execute()["url"];req=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0"});page=urllib.request.urlopen(req,timeout=30).read().decode("utf-8","ignore");labs=[]
        for x in re.findall(r"/search/label/([^\"'?&#<>\s/]+)",page):
            x=urllib.parse.unquote_plus(x).strip()
            if x and x.lower() not in {z.lower() for z in labs}:labs.append(x)
    except Exception as e:log("  Label read warning:",e);labs=[]
    for x in FALLBACK_LABELS:
        if len(labs)<3 and x.lower() not in {z.lower() for z in labs}:labs.append(x)
    return [x for x in labs if not BLOCKED.search(x)][:60]
def analyze(name,frames,audio,labels):
    hint=clean_hint(name);year=int(YEAR_RE.search(Path(name).stem).group(1)) if YEAR_RE.search(Path(name).stem) else None
    prompt=f'''You are a film writer. Return ONLY valid JSON. Write natural English. Base everything only on the supplied frames/audio and filename hint. Never invent cast, awards, ratings, box office or specific plot facts. No piracy wording. Title 1-6 words, no year. Filename hint: {hint}. Language hint: {LANGUAGE_HINT}. Director: {DIRECTOR_NAME}. Exact available labels: {json.dumps(labels)}. Keys: title, tagline, synopsis (2 short paragraphs), review (3 short paragraphs), themes (3-5), faq (4 objects with q/a), genres (1-3), language, content_rating (General audience/Teen and above/Mature audience), tags (up to 6), labels (1-4 only from exact list).'''
    parts=[types.Part.from_bytes(data=b,mime_type="image/jpeg") for b in frames]+[types.Part.from_bytes(data=audio,mime_type="audio/mp3")];data={}
    for model in dict.fromkeys([GEMINI_MODEL,"gemini-flash-latest"]):
        try:
            r=retry(
                lambda:gclient.models.generate_content(
                    model=model,
                    contents=[prompt,*parts],
                    config=types.GenerateContentConfig(response_mime_type="application/json")
                ),
                tries=2,
                delay=4
            )
            data=json.loads(re.sub(r"^```json|```$","",r.text.strip(),flags=re.I).strip())
            log("  Gemini:",model)
            break
        except Exception as e:
            msg=str(e)
            log("  Gemini failed:",e)
            # A 429 free-tier quota error is a daily/project limit. Trying a
            # second model immediately usually wastes another request and does
            # not help, so fall back to filename-based metadata.
            if "429" in msg or "RESOURCE_EXHAUSTED" in msg or "quota" in msg.lower():
                log("  Gemini quota exhausted; using safe fallback metadata.")
                break
    syn=data.get("synopsis") or ["An original film."];syn=syn if isinstance(syn,list) else re.split(r"\n\s*\n",str(syn))
    canon={x.lower():x for x in labels};chosen=[canon[x.strip().lower()] for x in data.get("labels",[]) if str(x).strip().lower() in canon][:4]
    return {"title":str(data.get("title") or hint or "Untitled Film").strip(),"tagline":str(data.get("tagline") or "").strip(),"synopsis":[str(x).strip() for x in syn if str(x).strip()],"review":[str(x).strip() for x in (data.get("review") or [])],"themes":[str(x) for x in data.get("themes",[])][:5],"faq":[x for x in data.get("faq",[]) if isinstance(x,dict) and x.get("q") and x.get("a")][:4],"genres":[str(x) for x in data.get("genres",["Drama"])][:3],"language":str(data.get("language") or LANGUAGE_HINT or "Unknown"),"release_year":year,"content_rating":str(data.get("content_rating") or "General audience"),"tags":[str(x) for x in data.get("tags",[])][:6],"labels":chosen}

# ---------------- HTML ----------------
def human(n):
    n=float(n);return f"{n/1024**3:.1f}GB" if n>=1024**3 else f"{n/1024**2:.0f}MB"
def runtime(s):
    s=int(s);return f"{s//3600} h {(s%3600)//60} min" if s>=3600 else f"{s//60} min" if s>=60 else f"{s} sec"
def fps_text(x):return str(round(x)) if abs(x-round(x))<.05 else f"{x:.2f}"
TIMER='''<script>(function(){var WAIT=%d;document.querySelectorAll('a.mv-dl[data-url]').forEach(function(b){var label=b.innerHTML,busy=false;b.addEventListener('click',function(e){e.preventDefault();if(busy)return;busy=true;var left=WAIT;b.innerHTML='Please wait '+left+' seconds...';var t=setInterval(function(){left--;if(left>0){b.innerHTML='Please wait '+left+' seconds...';return}clearInterval(t);b.innerHTML='Opening download...';window.location.href=b.getAttribute('data-url');setTimeout(function(){b.innerHTML=label;busy=false},6000)},1000)})})})();</script>'''%WAIT_SECONDS

def build_html(meta,thumb_url,downloads,vcdn,dur,fps):
    e=html.escape;title=e(meta["title"]);lang=e(meta["language"]);genres=e(", ".join(meta["genres"]));qualities=" - ".join(f"{x['target']}p" for x in downloads);sizes=" - ".join(human(x["size"]) for x in downloads)
    parts=[]
    if thumb_url:parts.append(f'<div style="text-align:center"><img src="{e(thumb_url,quote=True)}" alt="{title}" width="270" style="max-width:70%;height:auto;border-radius:8px"/></div>')
    parts.append(f'<p style="text-align:center"><b>{title}</b></p>')
    if meta["tagline"]:parts.append(f'<p style="text-align:center"><i>{e(meta["tagline"])}</i></p>')
    parts.append('<h3 style="text-align:center">Movie Info</h3><p>'+"<br/>".join([f"<b>Movie Name:</b> {title}",f"<b>Runtime:</b> {runtime(dur)}",f"<b>Genres:</b> {genres}",f"<b>Language:</b> {lang}",f"<b>Quality:</b> {qualities}",f"<b>Frame Rate:</b> {fps_text(fps)}fps",f"<b>Size:</b> {sizes}",f"<b>Content Advisory:</b> {e(meta['content_rating'])}"])+"</p>")
    parts.append('<h3 style="text-align:center">Movie Synopsis / Plot</h3>'+"".join(f"<p>{e(p)}</p>" for p in meta["synopsis"]))
    emb=vcdn["embed_url"];parts.append(f'<h3 style="text-align:center">Watch {title} Online</h3><div style="width:100%;background:#000;border-radius:8px;overflow:hidden"><iframe src="{e(emb,quote=True)}" width="100%" height="420" frameborder="0" allow="autoplay; encrypted-media; picture-in-picture" allowfullscreen style="border:0;display:block"></iframe></div>')
    if meta["review"]:parts.append(f'<h3 style="text-align:center">{title} - Film Review and Analysis</h3>'+"".join(f"<p>{e(p)}</p>" for p in meta["review"]))
    if meta["themes"]:parts.append('<h3 style="text-align:center">Themes</h3><ul>'+''.join(f'<li>{e(x)}</li>' for x in meta["themes"])+'</ul>')
    parts.append('<hr style="border:0;border-top:1px solid rgba(255,255,255,.5);margin:22px 0"/><h3 style="text-align:center">Download Links</h3>')
    for x in downloads:
        parts.append(f'<h4 style="text-align:center;color:#fff;font-size:19px">{title} {x["target"]}p x264 [{human(x["size"])}]</h4><a class="mv-dl" data-url="{e(x["link"],quote=True)}" href="{e(x["link"],quote=True)}" rel="noopener" style="display:block;width:260px;max-width:90%;margin:0 auto 28px;padding:18px 10px;text-align:center;color:#fff;font-weight:800;font-size:19px;text-decoration:none;background:linear-gradient(90deg,#57a51c,#1f4fb4);box-shadow:0 8px 14px rgba(0,0,0,.45)">&#11015;&#9889; DOWNLOAD NOW &#9889;&#11015;</a>')
    if meta["faq"]:parts.append(f'<h3 style="text-align:center">{title} - FAQ</h3>'+''.join(f'<h4>{e(str(f["q"]))}</h4><p>{e(str(f["a"]))}</p>' for f in meta["faq"]))
    parts.append('<h3 style="text-align:center;color:#f0a0ff">Winding Up &#10084;&#65039;</h3>');parts.append(TIMER);return "\n".join(parts)

# ---------------- Blogger safe verification ----------------
def blogger_created_ok(post_id,title):
    try:
        p=blogger.posts().get(blogId=BLOG_ID,postId=post_id,fetchBody=False).execute()
        return str(p.get("id"))==str(post_id)
    except Exception as e:log("  Blogger GET verification:",e)
    try:
        r=blogger.posts().search(blogId=BLOG_ID,q=title,fetchBodies=False).execute()
        return any(str(x.get("id"))==str(post_id) for x in r.get("items",[]))
    except Exception as e:log("  Blogger search verification:",e)
    return False

def find_existing_post(title):
    try:
        r=blogger.posts().search(blogId=BLOG_ID,q=title,fetchBodies=False).execute()
        for p in r.get("items",[]):
            if str(p.get("title","")).strip()==str(title).strip():
                return p
    except Exception as e:
        log("  Existing-post check warning:",e)
    return None

def create_post(body):
    existing=find_existing_post(body["title"])
    if existing and existing.get("id"):
        log("  Existing Blogger post found; not creating a duplicate:",existing.get("id"))
        return existing
    # Do NOT retry posts.insert automatically: if Blogger creates the post but the
    # HTTP response is lost, a retry can create a duplicate.
    try:
        post=blogger.posts().insert(blogId=BLOG_ID,body=body,isDraft=not PUBLISH,fetchBody=False).execute()
    except Exception as e:
        # One last search handles the case where the server created the post but
        # the client received an error/timeout.
        recovered=find_existing_post(body["title"])
        if recovered and recovered.get("id"):
            log("  Blogger insert reported an error, but the post was found afterward; treating it as success.")
            return recovered
        raise
    pid=post.get("id")
    if not pid:raise RuntimeError("Blogger insert returned no post ID")
    log("  Blogger post created:",pid,post.get("url") or "")
    if blogger_created_ok(pid,body["title"]):log("  Blogger post verified.")
    else:log("  WARNING: Blogger create succeeded but API verification returned no confirmation; NOT retrying create to avoid duplicate post.")
    return post

# ---------------- Process ----------------
def target_resolutions(w,h):
    short=min(w,h);t=sorted(x for x in RESOLUTIONS if x<=short)
    return t or [even(short)]

def process(video,processed_folder):
    name=video["name"];job=WORK/video["id"];job.mkdir(parents=True,exist_ok=True);src=str(job/"source.mp4");slug=re.sub(r"[^a-zA-Z0-9]+","-",Path(name).stem).strip("-").lower() or "movie"
    log(f"\n=== Processing: {name} ===");download(video["id"],src);dur,w,h,fps=probe(src);log(f"  Source: {w}x{h}, {fps:.2f}fps, {dur/60:.1f} min")
    frames,audio=analysis_inputs(src,dur,job);labels=site_labels();meta=analyze(name,frames,audio,labels);log("  Title:",meta["title"]);targets=target_resolutions(w,h);log("  Targets:",targets)
    downloads=[];vcdn=None;vcdn_target=max(targets)
    for target in targets:
        out=str(job/f"{slug}_{target}p.mp4");transcode(src,target,w,h,out);size=os.path.getsize(out);st=st_upload(out);fid=st.get("id") or st.get("linkid");
        if not fid:raise RuntimeError(f"Streamtape file ID missing for {out}: {st}")
        downloads.append({"target":target,"link":st["link"],"id":fid,"size":size})
        if target==vcdn_target:vcdn=vcdn_upload(out,meta["title"])
        os.remove(out)
    if not vcdn or not vcdn.get("embed_url"):raise RuntimeError("VCDN did not return embed URL")
    thumb_url=st_splash(downloads[-1]["id"])
    content=build_html(meta,thumb_url,downloads,vcdn,dur,fps)
    year=f" ({meta['release_year']})" if meta["release_year"] else "";lang=f" {meta['language']}" if meta["language"].lower()!="unknown" else "";labs=meta["labels"] or (meta["genres"][:2]+([meta["language"]] if meta["language"]!="Unknown" else []));labs=[str(x)[:40] for x in labs if x][:8]
    body={"kind":"blogger#post","title":f"{meta['title']}{year}{lang} Movie - Watch Online & Download","content":content,"labels":labs};post=create_post(body)
    # Only after all external publication steps succeed, move source out of input.
    move_source(video["id"],processed_folder);log("  Original Drive source moved to _processed.");shutil.rmtree(job,ignore_errors=True);log("  Local job cleaned.")

def main():
    WORK.mkdir(exist_ok=True);videos=list_videos()
    if not videos:log("No new videos.");return 0
    processed=ensure_folder("_processed");failed=0
    for video in videos[:MAX_VIDEOS]:
        try:process(video,processed)
        except Exception as e:
            failed+=1;log("\n================================\nMOVIE PROCESSING FAILED\n================================");log(str(e));traceback.print_exc();log("Original Drive source was NOT moved to _processed.")
    return 1 if failed else 0

if __name__=="__main__":
    try:sys.exit(main())
    finally:
        shutil.rmtree(WORK,ignore_errors=True)
        log("Global local work directory cleaned.")
