#!/usr/bin/env python3
"""
Shorts Maker - POORI WEBSITE EK FILE MEIN (backend + HTML page + deploy files)

Local chalane ke liye (apne computer par):
    1. Python aur ffmpeg install karo.
    2. pip install flask yt-dlp openai-whisper
    3. python app.py
    4. Browser mein kholo: http://localhost:5000

Live (server) ke liye:
    python app.py write-deploy
    Isse Dockerfile, docker-compose.yml, Caddyfile, .env.example, requirements.txt
    aur DEPLOY.md (step-by-step guide) isi folder mein ban jaate hain.

Settings (environment variables, sab optional):
    HOST            default 127.0.0.1
    PORT            default 5000
    WORK_DIR        default ./work
    MAX_MINUTES     video ki max length (default 120)
    JOBS_PER_HOUR   ek IP se ghante mein kitne jobs (default 3)
    MAX_ACTIVE_JOBS ek saath max queued+running jobs (default 5)
    KEEP_MINUTES    ban chuke shorts kitni der baad delete (default 60)
    TRUST_PROXY     1 agar Caddy/Nginx ke peeche ho (sahi IP ke liye)
    COOKIES_FILE    optional yt-dlp cookies file ka path

Sirf apni videos ya jinki permission ho, unhi se shorts banao.
"""
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from flask import Flask, Response, abort, jsonify, request, send_from_directory
from werkzeug.middleware.proxy_fix import ProxyFix

BASE = Path(__file__).parent


def env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


WORK = Path(os.environ.get("WORK_DIR", BASE / "work"))
MAX_MINUTES = env_int("MAX_MINUTES", 120)
JOBS_PER_HOUR = env_int("JOBS_PER_HOUR", 3)
MAX_ACTIVE = env_int("MAX_ACTIVE_JOBS", 5)
KEEP_MINUTES = env_int("KEEP_MINUTES", 60)
COOKIES_FILE = os.environ.get("COOKIES_FILE")

app = Flask(__name__)
if os.environ.get("TRUST_PROXY") == "1":
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

JOBS = {}
LOCK = threading.Semaphore(1)  # ek time par ek hi heavy job
_models = {}
_hits = {}
_hits_lock = threading.Lock()
PUBLIC_KEYS = ("status", "message", "clips", "title", "used_heatmap")


# ---------- helpers ----------
def set_job(jid, **kw):
    JOBS[jid].update(kw)


def srt_time(t):
    t = max(t, 0)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    ms = int(round((s - int(s)) * 1000))
    if ms == 1000:
        ms = 999
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{ms:03d}"


def fmt(t):
    m, s = divmod(int(t), 60)
    return f"{m}:{s:02d}"


def get_model(name):
    import whisper

    if name not in _models:
        _models[name] = whisper.load_model(name)
    return _models[name]


def heat_avg(heatmap, a, b):
    vals = [h["value"] for h in heatmap if h["end_time"] > a and h["start_time"] < b]
    return sum(vals) / len(vals) if vals else 0.0


def pick_clips(segments, heatmap, count, min_len, max_len):
    """Viral score: YouTube 'Most replayed' heatmap (agar mile) + bolne ki density + ?/! bonus."""
    windows = []
    n = len(segments)
    for i in range(n):
        j = i
        while j + 1 < n and segments[j + 1]["end"] - segments[i]["start"] <= max_len:
            j += 1
        start, end = segments[i]["start"], segments[j]["end"]
        dur = end - start
        if dur < min_len:
            continue
        text = " ".join(s["text"] for s in segments[i : j + 1])
        density = min(len(text.split()) / dur / 4, 1.0)
        bonus = 0.05 * (text.count("?") + text.count("!"))
        if heatmap:
            score = 0.7 * heat_avg(heatmap, start, end) + 0.3 * density + bonus
        else:
            score = density + bonus
        windows.append((score, start, end))
    windows.sort(reverse=True)

    chosen = []
    for score, start, end in windows:
        if all(end <= cs or start >= ce for _, cs, ce in chosen):
            chosen.append((score, start, end))
        if len(chosen) >= count:
            break
    if not chosen and segments:  # chhoti video
        s0 = segments[0]["start"]
        chosen = [(0, s0, min(segments[-1]["end"], s0 + max_len))]
    return sorted((s, e) for _, s, e in chosen)


def write_srt(segments, start, end, path):
    lines, idx = [], 1
    for s in segments:
        if s["end"] <= start or s["start"] >= end:
            continue
        a = max(s["start"], start) - start
        b = min(s["end"], end) - start
        lines.append(f"{idx}\n{srt_time(a)} --> {srt_time(b)}\n{s['text']}\n")
        idx += 1
    path.write_text("\n".join(lines), encoding="utf-8")


def make_clip(video, start, end, srt_name, out_name, jdir, captions):
    vf = "crop='min(iw,ih*9/16)':'min(ih,iw*16/9)',scale=1080:1920"
    if captions:
        style = (
            "FontName=Arial,FontSize=10,Bold=1,PrimaryColour=&H00FFFFFF,"
            "OutlineColour=&H00000000,Outline=2,Alignment=2,MarginV=60"
        )
        vf += f",subtitles={srt_name}:force_style='{style}'"
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-ss", f"{start:.2f}", "-to", f"{end:.2f}",
            "-i", str(video.resolve()),
            "-vf", vf,
            "-c:v", "libx264", "-preset", "fast", "-crf", "21",
            "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
            out_name,
        ],
        check=True,
        cwd=jdir,
        timeout=600,
    )


# ---------- job runner ----------
def process(jid, url, count, model_name, lang, min_len, max_len, captions):
    jdir = WORK / jid
    try:
        set_job(jid, status="queued", message="Queue mein hai, thoda ruko...")
        with LOCK:
            from yt_dlp import YoutubeDL

            jdir.mkdir(parents=True, exist_ok=True)
            set_job(jid, status="working", message="Video check ho raha hai...")
            base_opts = {
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                "socket_timeout": 30,
            }
            if COOKIES_FILE and Path(COOKIES_FILE).exists():
                base_opts["cookiefile"] = COOKIES_FILE
            with YoutubeDL(base_opts) as ydl:
                info = ydl.extract_info(url, download=False)
            if (info.get("duration") or 0) > MAX_MINUTES * 60:
                raise RuntimeError(f"Video {MAX_MINUTES} minute se lambi hai.")
            if info.get("is_live"):
                raise RuntimeError("Live video support nahi hai.")
            heatmap = info.get("heatmap")

            set_job(jid, message="Video download ho raha hai...")
            opts = dict(
                base_opts,
                format="bv*[height<=1080]+ba/b[height<=1080]",
                merge_output_format="mp4",
                outtmpl=str(jdir / "source.%(ext)s"),
            )
            with YoutubeDL(opts) as ydl:
                ydl.download([url])
            video = next(jdir.glob("source.*"))

            set_job(jid, message="Transcript ban raha hai (thoda time lagega)...")
            model = get_model(model_name)
            kw = {"language": lang} if lang else {}
            result = model.transcribe(str(video), **kw)
            segments = [
                {"start": s["start"], "end": s["end"], "text": s["text"].strip()}
                for s in result["segments"]
            ]
            if not segments:
                raise RuntimeError("Video mein speech nahi mili.")

            set_job(jid, message="Viral parts dhoondh raha hai...")
            ranges = pick_clips(segments, heatmap, count, min_len, max_len)

            clips = []
            for n, (start, end) in enumerate(ranges, 1):
                set_job(jid, message=f"Short {n}/{len(ranges)} ban raha hai...")
                srt_name, out_name = f"short_{n}.srt", f"short_{n}.mp4"
                write_srt(segments, start, end, jdir / srt_name)
                make_clip(video, start, end, srt_name, out_name, jdir, captions)
                preview = " ".join(
                    s["text"] for s in segments if s["start"] >= start and s["end"] <= end
                )[:140]
                clips.append(
                    {"file": out_name, "start": fmt(start), "end": fmt(end), "text": preview}
                )

            video.unlink(missing_ok=True)  # disk bachao
            set_job(
                jid,
                status="done",
                message="Ho gaya!",
                clips=clips,
                title=info.get("title", ""),
                used_heatmap=bool(heatmap),
                finished=time.time(),
            )
    except Exception as e:  # noqa: BLE001
        shutil.rmtree(jdir, ignore_errors=True)
        set_job(jid, status="error", message=f"Error: {e}", finished=time.time())


# ---------- cleanup ----------
def cleanup_loop():
    while True:
        time.sleep(60)
        now = time.time()
        for jid, job in list(JOBS.items()):
            fin = job.get("finished")
            if fin and now - fin > KEEP_MINUTES * 60:
                shutil.rmtree(WORK / jid, ignore_errors=True)
                JOBS.pop(jid, None)
        with _hits_lock:
            for ip in list(_hits):
                _hits[ip] = [t for t in _hits[ip] if now - t < 3600]
                if not _hits[ip]:
                    del _hits[ip]


def startup():
    WORK.mkdir(parents=True, exist_ok=True)
    for p in WORK.iterdir():  # restart ke baad purani files hatao (JOBS memory mein hai)
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
    threading.Thread(target=cleanup_loop, daemon=True).start()


if not (len(sys.argv) > 1 and sys.argv[1] == "write-deploy"):
    startup()


def rate_ok(ip):
    now = time.time()
    with _hits_lock:
        arr = [t for t in _hits.get(ip, []) if now - t < 3600]
        if len(arr) >= JOBS_PER_HOUR:
            _hits[ip] = arr
            return False
        arr.append(now)
        _hits[ip] = arr
        return True


def active_jobs():
    return sum(1 for j in JOBS.values() if j.get("status") in ("queued", "working"))


# ---------- routes ----------
@app.after_request
def headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


@app.get("/")
def index():
    return Response(INDEX_HTML, mimetype="text/html")


@app.get("/healthz")
def healthz():
    return jsonify(ok=True, active=active_jobs())


@app.post("/api/process")
def api_process():
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url.startswith(("http://", "https://")) or len(url) > 500:
        return jsonify(error="Sahi link daalo (https:// se shuru)."), 400
    if not data.get("rights"):
        return jsonify(error="Pehle confirm karo ki video aapki hai ya aapke paas permission hai."), 400
    try:
        count = max(1, min(int(data.get("count", 3)), 6))
    except (TypeError, ValueError):
        count = 3
    model = data.get("model") if data.get("model") in ("tiny", "base", "small") else "base"
    lang = data.get("lang") if data.get("lang") in ("hi", "en") else None
    captions = bool(data.get("captions", True))

    if active_jobs() >= MAX_ACTIVE:
        return jsonify(error="Abhi server busy hai. Thodi der baad try karo."), 503
    ip = request.remote_addr or "?"
    if not rate_ok(ip):
        return jsonify(error=f"Ek ghante mein {JOBS_PER_HOUR} se zyada jobs nahi. Baad mein try karo."), 429

    jid = uuid.uuid4().hex[:12]
    JOBS[jid] = {"status": "queued", "message": "Shuru ho raha hai...", "clips": []}
    threading.Thread(
        target=process,
        args=(jid, url, count, model, lang, 30, 55, captions),
        daemon=True,
    ).start()
    return jsonify(job_id=jid)


@app.get("/api/status/<jid>")
def api_status(jid):
    job = JOBS.get(jid)
    if not job:
        abort(404)
    return jsonify({k: job[k] for k in PUBLIC_KEYS if k in job})


@app.get("/files/<jid>/<name>")
def files(jid, name):
    job = JOBS.get(jid)
    if not job or not name.endswith(".mp4") or name.startswith("source"):
        abort(404)
    return send_from_directory(
        WORK / jid, name, as_attachment=request.args.get("download") == "1"
    )


# ======================================================================
# EMBEDDED FRONTEND (index.html)
# ======================================================================
INDEX_HTML = r'''<!DOCTYPE html>
<html lang="hi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Shorts Maker</title>
<style>
  :root {
    --bg: #f6f7fb; --card: #ffffff; --text: #14151a; --muted: #6b6f7b;
    --line: #e2e4ec; --accent: #e5322d; --accent-text: #ffffff;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #101116; --card: #1a1c24; --text: #f2f3f7; --muted: #9a9eac;
      --line: #2c2f3a; --accent: #ff4d47; --accent-text: #ffffff;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    line-height: 1.5;
  }
  .wrap { max-width: 920px; margin: 0 auto; padding: 32px 16px 64px; }
  h1 { font-size: 1.7rem; margin: 0 0 4px; }
  .sub { color: var(--muted); margin: 0 0 24px; }
  .card {
    background: var(--card); border: 1px solid var(--line);
    border-radius: 14px; padding: 20px;
  }
  label { display: block; font-size: .85rem; color: var(--muted); margin-bottom: 6px; }
  input[type=text], select {
    width: 100%; padding: 11px 12px; font-size: 1rem; color: var(--text);
    background: var(--bg); border: 1px solid var(--line); border-radius: 10px;
  }
  .row { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 14px; margin-top: 14px; }
  .check { display: flex; align-items: center; gap: 8px; margin-top: 14px; font-size: .95rem; }
  button.primary {
    margin-top: 18px; width: 100%; padding: 13px; font-size: 1rem; font-weight: 600;
    background: var(--accent); color: var(--accent-text); border: 0; border-radius: 10px; cursor: pointer;
  }
  button.primary:disabled { opacity: .55; cursor: not-allowed; }
  .status { margin-top: 18px; display: none; align-items: center; gap: 10px; }
  .spinner {
    width: 18px; height: 18px; border-radius: 50%;
    border: 3px solid var(--line); border-top-color: var(--accent);
    animation: spin 0.9s linear infinite; flex: none;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  .error { color: var(--accent); }
  .note { font-size: .85rem; color: var(--muted); margin-top: 14px; }
  #results { margin-top: 28px; }
  #results h2 { font-size: 1.15rem; margin: 0 0 4px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 16px; margin-top: 14px; }
  .clip { background: var(--card); border: 1px solid var(--line); border-radius: 14px; padding: 12px; }
  .clip video { width: 100%; aspect-ratio: 9 / 16; background: #000; border-radius: 10px; display: block; }
  .clip .time { font-size: .85rem; color: var(--muted); margin: 8px 0 4px; }
  .clip .txt { font-size: .85rem; margin-bottom: 10px; min-height: 2.6em; }
  .clip a.dl {
    display: block; text-align: center; text-decoration: none; padding: 10px;
    border-radius: 9px; font-weight: 600; background: var(--accent); color: var(--accent-text);
  }
</style>
</head>
<body>
<div class="wrap">
  <h1>Shorts Maker</h1>
  <p class="sub">Video ka link daalo, viral parts se captions wale vertical shorts pao aur download karo.</p>

  <div class="card">
    <label for="url">Video ka link</label>
    <input type="text" id="url" placeholder="https://www.youtube.com/watch?v=...">

    <div class="row">
      <div>
        <label for="count">Kitne shorts</label>
        <select id="count">
          <option>1</option><option>2</option><option selected>3</option>
          <option>4</option><option>5</option><option>6</option>
        </select>
      </div>
      <div>
        <label for="lang">Video ki language</label>
        <select id="lang">
          <option value="">Auto detect</option>
          <option value="hi">Hindi</option>
          <option value="en">English</option>
        </select>
      </div>
      <div>
        <label for="model">Transcript quality</label>
        <select id="model">
          <option value="tiny">Tez (kam accurate)</option>
          <option value="base" selected>Balanced</option>
          <option value="small">Accurate (dheema)</option>
        </select>
      </div>
    </div>

    <label class="check"><input type="checkbox" id="captions" checked> Captions lagao</label>
    <label class="check" style="align-items:flex-start">
      <input type="checkbox" id="rights" style="margin-top:4px">
      <span>Main confirm karta/karti hoon ki ye video meri hai ya mere paas iska permission hai, aur main iski YouTube ki terms ka palan karunga/karungi.</span>
    </label>

    <button class="primary" id="go">Shorts banao</button>

    <div class="status" id="status">
      <div class="spinner" id="spinner"></div>
      <span id="msg"></span>
    </div>
    <p class="note">Sirf apni videos ya jinki permission ho, unhi se shorts banao. Doosron ke content par copyright claim aa sakta hai. Ban chuke shorts kuch der baad apne aap delete ho jaate hain, isliye jaldi download kar lo.</p>
  </div>
  <p class="note" style="text-align:center">Contact / takedown: <a href="mailto:your@email.com">your@email.com</a></p>

  <div id="results"></div>
</div>

<script>
const $ = (id) => document.getElementById(id);
let timer = null;

function setStatus(text, isError, busy) {
  $("status").style.display = "flex";
  $("msg").textContent = text;
  $("msg").className = isError ? "error" : "";
  $("spinner").style.display = busy ? "block" : "none";
}

$("go").addEventListener("click", async () => {
  const url = $("url").value.trim();
  if (!url) { setStatus("Pehle link daalo.", true, false); return; }
  if (!$("rights").checked) { setStatus("Pehle permission wala box tick karo.", true, false); return; }

  $("go").disabled = true;
  $("results").innerHTML = "";
  setStatus("Shuru ho raha hai...", false, true);

  try {
    const res = await fetch("/api/process", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url,
        count: Number($("count").value),
        lang: $("lang").value,
        model: $("model").value,
        captions: $("captions").checked,
        rights: $("rights").checked
      })
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Kuch gadbad ho gayi.");
    poll(data.job_id);
  } catch (e) {
    setStatus(e.message, true, false);
    $("go").disabled = false;
  }
});

function poll(jobId) {
  clearInterval(timer);
  timer = setInterval(async () => {
    try {
      const res = await fetch("/api/status/" + jobId);
      const job = await res.json();
      if (job.status === "error") {
        clearInterval(timer);
        setStatus(job.message, true, false);
        $("go").disabled = false;
      } else if (job.status === "done") {
        clearInterval(timer);
        setStatus(job.message, false, false);
        $("go").disabled = false;
        render(jobId, job);
      } else {
        setStatus(job.message, false, true);
      }
    } catch (e) {
      clearInterval(timer);
      setStatus("Server se connection toot gaya.", true, false);
      $("go").disabled = false;
    }
  }, 2000);
}

function render(jobId, job) {
  const box = $("results");
  box.innerHTML = "";

  const h = document.createElement("h2");
  h.textContent = job.clips.length + " shorts tayyar";
  box.appendChild(h);

  const sub = document.createElement("div");
  sub.className = "note";
  sub.textContent = (job.title ? job.title + " | " : "") +
    (job.used_heatmap ? "YouTube ke 'most replayed' hisson ke hisaab se chune gaye." : "Bolne ki density ke hisaab se chune gaye.");
  box.appendChild(sub);

  const grid = document.createElement("div");
  grid.className = "grid";

  job.clips.forEach((c, i) => {
    const card = document.createElement("div");
    card.className = "clip";

    const v = document.createElement("video");
    v.controls = true;
    v.preload = "metadata";
    v.src = "/files/" + jobId + "/" + c.file;

    const t = document.createElement("div");
    t.className = "time";
    t.textContent = "Short " + (i + 1) + " | " + c.start + " - " + c.end;

    const p = document.createElement("div");
    p.className = "txt";
    p.textContent = c.text;

    const a = document.createElement("a");
    a.className = "dl";
    a.textContent = "Download";
    a.href = "/files/" + jobId + "/" + c.file + "?download=1";

    card.append(v, t, p, a);
    grid.appendChild(card);
  });

  box.appendChild(grid);
}
</script>
</body>
</html>
'''

# ======================================================================
# EMBEDDED DEPLOY FILES (python app.py write-deploy se ban jaati hain)
# ======================================================================
DEPLOY_FILES = {
    'Dockerfile': r'''FROM python:3.11-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# CPU-only torch (chhota image, GPU ki zaroorat nahi)
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

COPY app.py ./

RUN useradd -m appuser && mkdir -p /data && chown appuser /data
USER appuser

ENV WORK_DIR=/data/work \
    XDG_CACHE_HOME=/data/cache \
    TRUST_PROXY=1

VOLUME /data
EXPOSE 5000

# 1 worker zaroori hai: jobs memory mein rehte hain. Threads se kaam chalta hai.
CMD ["gunicorn", "-w", "1", "--threads", "8", "--timeout", "300", "-b", "0.0.0.0:5000", "app:app"]
''',
    'docker-compose.yml': r'''services:
  app:
    build: .
    restart: unless-stopped
    environment:
      - JOBS_PER_HOUR=3
      - MAX_ACTIVE_JOBS=5
      - KEEP_MINUTES=60
      - MAX_MINUTES=120
    volumes:
      - data:/data
    expose:
      - "5000"

  caddy:
    image: caddy:2
    restart: unless-stopped
    ports:
      - "80:80"
      - "443:443"
    environment:
      - DOMAIN=${DOMAIN}
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
      - caddy_config:/config
    depends_on:
      - app

volumes:
  data:
  caddy_data:
  caddy_config:
''',
    'Caddyfile': r'''{$DOMAIN} {
	request_body {
		max_size 1MB
	}
	reverse_proxy app:5000
}
''',
    '.env.example': r'''# Is file ko ".env" naam se copy karo aur apna domain likho
DOMAIN=yourdomain.com
''',
    'requirements.txt': r'''flask
yt-dlp
openai-whisper
''',
    'DEPLOY.md': r'''# Live karne ka guide (Docker + HTTPS)

## Kya chahiye
- Ek Linux VPS (Ubuntu 22.04/24.04). Whisper CPU par chalta hai, isliye kam se kam 2 vCPU aur 4 GB RAM le lo.
  Zyada RAM/CPU = shorts jaldi banenge.
- Ek domain (jaise shorts.yourdomain.com).

## Steps
1. Domain ke DNS mein **A record** banao jo VPS ke IP par point kare.
2. VPS par Docker install karo:
   `curl -fsSL https://get.docker.com | sh`
3. Apne computer par (jahan `pip install flask` ho chuka ho) ye chalao:
   `python app.py write-deploy`
   Isse Dockerfile, docker-compose.yml, Caddyfile, .env.example, requirements.txt aur DEPLOY.md ban jaate hain.
   Ab `app.py` aur ye saari files VPS par copy karo (scp ya git se), jaise `/opt/shorts-site`.
4. Us folder mein:
   `cp .env.example .env`
   `.env` kholo aur `DOMAIN=` mein apna domain likho.
5. Firewall mein port 80 aur 443 kholo.
6. Chalao:
   `docker compose up -d --build`
7. 1-2 minute baad `https://yourdomain.com` kholo. HTTPS certificate Caddy apne aap le leta hai.

## Roz ke kaam
- Logs dekhna: `docker compose logs -f app`
- yt-dlp update (YouTube badalta rehta hai, ye har 1-2 hafte mein karo):
  `docker compose build --no-cache app && docker compose up -d`
- Band karna: `docker compose down`

## Settings badalna
`docker-compose.yml` ke `environment` mein:
- `JOBS_PER_HOUR` ek IP ki limit
- `MAX_ACTIVE_JOBS` ek saath kitne jobs queue/run ho sakte hain
- `KEEP_MINUTES` shorts kitni der baad delete hon
- `MAX_MINUTES` video ki max length

`index.html` ke neeche `your@email.com` ko apni asli email se badalo (contact / takedown ke liye).

## Zaroori chetavaniyan
1. **YouTube server IP block kar deta hai.** Datacenter IPs par "Sign in to confirm you're not a bot" jaisa error aam hai. Ye is code se theek nahi hota.
   Kuch log apne account ki cookies file (`COOKIES_FILE`) lagate hain, par isme apne account ke ban hone ka risk hai.
   Pakka nahi hai ki aapke server par kaam karega. Pehle ek chhote test se dekho.
2. **Terms aur copyright.** Doosron ke YouTube videos download karwana YouTube ki terms ke against hai, aur unke clips se
   copyright claim aa sakta hai. Website par sirf wahi use hone do jinke paas rights hon. Consent checkbox sirf ek chetavani hai, kanooni suraksha nahi.
   Public service chalane se pehle kisi wakeel se poochna behtar hai.
3. **Kharcha:** server ka bill, bandwidth, aur CPU. Rate limit isiliye hai. Bina login ke koi bhi use kar sakta hai, to zarurat ho to
   login ya payment baad mein jodna.
4. **Ek hi server par chalta hai.** Jobs memory mein rehte hain, to restart par chal rahe jobs chale jaate hain. Zyada traffic ke liye queue (jaise Redis) aur alag worker chahiye.
''',
}


def write_deploy():
    for name, text in DEPLOY_FILES.items():
        p = Path(name)
        if p.exists():
            print(f"skip (pehle se hai): {name}")
            continue
        p.write_text(text, encoding="utf-8")
        print(f"bana diya: {name}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "write-deploy":
        write_deploy()
        sys.exit(0)
    app.run(
        host=os.environ.get("HOST", "127.0.0.1"),
        port=env_int("PORT", 5000),
        debug=False,
    )
