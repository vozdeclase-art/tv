#!/usr/bin/env python3
"""
mamirTV — TV Argentina en vivo.

Python levanta un servidor web local (solo librería estándar) que sirve una
interfaz HTML vistosa y reproduce los canales en el navegador usando HLS.
ffmpeg remuxea/transmite el stream a formato HLS que el navegador reproduce.

Sin tkinter. Funciona en Replit, Linux, macOS y Windows.

Uso:
    python3 ab.py                 # levanta la web (puerto 8000 por defecto)
    PORT=5000 python3 ab.py       # elegir puerto
    python3 ab.py --print-urls    # solo mostrar las URLs de los canales
    python3 ab.py --print-urls trece telefe

Dependencias: pip install yt-dlp   (ffmpeg debe estar en el PATH)
"""

import os
import re
import sys
import json
import time
import shutil
import tempfile
import threading
import subprocess
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

try:
    import yt_dlp
except ImportError:
    yt_dlp = None


# ─── Canales ────────────────────────────────────────────────────────────
CHANNELS = {
    "trece": {
        "name": "Canal 13",
        "url": "https://livetrx01.vodgc.net/eltrecetv/index.m3u8",
        "referrer": "https://vodgc.net/",
    },
    "telefe": {
        "name": "Telefe",
        "url": "http://45.134.141.161:2200/ARG/TELEFE_HD/index.m3u8",
        "referrer": "",
    },
    "america": {
        "name": "América TV",
        "url": "https://prepublish.f.qaotic.net/a07/americahls-100056/playlist_720p.m3u8",
        "referrer": "",
        "ua": "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36",
    },
    "tvpublica": {
        "name": "TV Pública",
        "url": "https://g1.vxral-hor.transport.edge-access.net/b16/ngrp:c7_vivo01_dai_source-20001_all/c7_vivo01_dai_source-20001_720p.m3u8",
        "referrer": "https://vmf.edge-apps.net/embed/live.php?streamname=c7live01-20034&autoplay=true",
    },
}

# Canales deportivos de futbollibretv.sx. El sitio embebe un player que arma
# el m3u8 con token en cada consulta, así que la URL real se resuelve al dar
# play (no se puede guardar fija).
SITE_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
SITE_HOSTS = ["https://streamtp99a.sbs", "https://streamtp-golden1.click"]

SPORTS_CHANNELS = [
    ("dsports", "DSports"),
    ("dsports2", "DSports 2"),
    ("dsportsplus", "DSports Plus"),
    ("espn", "ESPN"),
    ("espn2", "ESPN 2"),
    ("espn3", "ESPN 3"),
    ("espn4", "ESPN 4"),
    ("espn5", "ESPN 5"),
    ("espn6", "ESPN 6"),
    ("espn7", "ESPN 7"),
    ("espnpremium", "ESPN Premium"),
    ("espn4mx", "ESPN 4 MX"),
    ("espndeportes", "ESPN Deportes"),
    ("fox1ar", "Fox Sports 1"),
    ("foxar2", "Fox Sports 2"),
    ("foxar3", "Fox Sports 3"),
    ("fox_deportes_usa", "Fox Deportes USA"),
    ("liga1max", "Liga 1 MAX"),
    ("tntsports", "TNT Sports"),
    ("tntsportschile", "TNT Sports Chile"),
    ("tyc", "TyC Sports"),
    ("winsports", "Win Sports"),
    ("winplus", "Win Sports +"),
    ("tudnmx", "TUDN México"),
    ("tudn_usa", "TUDN USA"),
    ("azteca7", "Azteca 7"),
    ("universo", "Universo"),
    ("universo_usa", "Universo USA"),
    ("sportv1", "SportTV 1"),
    ("sportv2", "SportTV 2"),
    ("max1", "Max 1"),
    ("max2", "Max 2"),
    ("max3", "Max 3"),
    ("max4", "Max 4"),
    ("max5", "Max 5"),
    ("disney1", "Disney 1"),
    ("disney2", "Disney 2"),
    ("disney3", "Disney 3"),
    ("disney4", "Disney 4"),
    ("disney5", "Disney 5"),
    ("disney6", "Disney 6"),
    ("disney7", "Disney 7"),
    ("disney8", "Disney 8"),
    ("disney9", "Disney 9"),
    ("disney10", "Disney 10"),
    ("disney11", "Disney 11"),
    ("disney12", "Disney 12"),
    ("disney13", "Disney 13"),
]

for _sid, _name in SPORTS_CHANNELS:
    CHANNELS[_sid] = {"name": _name, "site": _sid}


def _site_get(url, referer):
    """GET simple; devuelve (url_final, bytes). Sigue redirects HTTP."""
    req = urllib.request.Request(url, headers={"User-Agent": SITE_UA, "Referer": referer})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.geturl(), resp.read()


def _resolve_site_stream(site_id):
    """Pide el player del sitio y devuelve (url, referrer, headers) del m3u8 real."""
    body = None
    page = None
    hosts = list(SITE_HOSTS)
    for _ in range(4):
        if not hosts:
            break
        host = hosts.pop(0)
        try:
            page, raw = _site_get(f"{host}/global1.php?stream={site_id}", f"{host}/")
        except Exception:
            continue
        body = raw.decode("utf-8", "replace")
        # el sitio redirige a otro dominio del mismo player
        if redirect := re.search(r'nuevoDominio\s*=\s*"([^"]+)"', body):
            hosts.insert(0, redirect.group(1))
            continue
        break
    else:
        raise RuntimeError("no se pudo consultar el canal")

    if not body or not (match := re.search(r'playbackURL\s*=\s*"([^"]+)"', body)):
        raise RuntimeError("el player no devolvió señal")

    url = match.group(1).replace("\\/", "/")
    origin = "{0.scheme}://{0.netloc}".format(urlparse(page))
    try:
        final, raw = _site_get(url, origin + "/")
    except Exception:
        raise RuntimeError("la señal está fuera del aire")
    if b"#EXTM3U" not in raw[:16]:
        raise RuntimeError("la señal está fuera del aire")
    return final, origin + "/", {"User-Agent": SITE_UA}


def resolve_stream(ch_id):
    """Devuelve (url, referrer, headers) listos para pasar a ffmpeg."""
    ch = CHANNELS[ch_id]

    if site_id := ch.get("site"):
        return _resolve_site_stream(site_id)

    url = ch["url"]
    referrer = ch.get("referrer", "")
    headers = {}

    if ch.get("ytdl"):
        if yt_dlp is None:
            raise RuntimeError("Falta yt-dlp: pip install yt-dlp")
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "geo_bypass": True,
            "extract_flat": False,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            url = info["url"]

    if params := ch.get("params"):
        url += "?" + params if "?" not in url else "&" + params
    if ua := ch.get("ua"):
        headers["User-Agent"] = ua
    return url, referrer, headers


# ─── Streaming HLS ──────────────────────────────────────────────────────
STREAM_ROOT = os.path.join(tempfile.gettempdir(), "mamirtv")
os.makedirs(STREAM_ROOT, exist_ok=True)


class Streamer:
    def __init__(self):
        self.lock = threading.Lock()
        self.proc = None
        self.channel = None
        self.state = "idle"
        self.error = None
        self.logfile = None
        self.backend = "ffmpeg" if shutil.which("ffmpeg") else "nada"

    def _ffmpeg_cmd(self, url, referrer, headers, outdir, copy=True):
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
        hdrs = ""
        if referrer:
            hdrs += f"Referer: {referrer}\r\n"
        for k, v in headers.items():
            hdrs += f"{k}: {v}\r\n"
        if hdrs:
            cmd += ["-headers", hdrs]
        ua = headers.get("User-Agent")
        if referrer:
            cmd += ["-user_agent", ua or "Mozilla/5.0"]
        cmd += ["-i", url]
        if copy:
            cmd += ["-c", "copy"]
        else:
            cmd += ["-c:v", "libx264", "-preset", "veryfast",
                    "-tune", "zerolatency", "-c:a", "aac"]
        cmd += [
            "-f", "hls",
            "-hls_time", "2",
            "-hls_list_size", "6",
            "-hls_flags", "delete_segments+append_list+omit_endlist",
            "-hls_segment_filename", os.path.join(outdir, "seg_%05d.ts"),
            os.path.join(outdir, "live.m3u8"),
        ]
        return cmd

    def _spawn(self, cmd, outdir):
        self.logfile = open(os.path.join(outdir, "ffmpeg.log"), "wb")
        return subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=self.logfile,
            stdin=subprocess.DEVNULL,
        )

    def play(self, ch_id):
        if self.backend == "nada":
            raise RuntimeError("ffmpeg no está instalado o no está en el PATH")

        self.stop()
        outdir = os.path.join(STREAM_ROOT, ch_id)
        shutil.rmtree(outdir, ignore_errors=True)
        os.makedirs(outdir, exist_ok=True)
        playlist = os.path.join(outdir, "live.m3u8")

        with self.lock:
            self.channel = ch_id
            self.state = "connecting"
            self.error = None

        url, referrer, headers = resolve_stream(ch_id)

        # 1º intento: remux directo (rápido, sin recodificar)
        proc = self._spawn(self._ffmpeg_cmd(url, referrer, headers, outdir, True), outdir)
        if not self._wait_playlist(proc, playlist):
            # 2º intento: recodificar (más compatible)
            self._kill(proc)
            shutil.rmtree(outdir, ignore_errors=True)
            os.makedirs(outdir, exist_ok=True)
            proc = self._spawn(self._ffmpeg_cmd(url, referrer, headers, outdir, False), outdir)
            if not self._wait_playlist(proc, playlist):
                err = self._tail_log(outdir) or "no se pudo abrir el stream"
                self._kill(proc)
                with self.lock:
                    self.state = "error"
                    self.error = err
                raise RuntimeError(err)

        with self.lock:
            self.proc = proc
            self.state = "playing"
        return f"/hls/{ch_id}/live.m3u8"

    def _wait_playlist(self, proc, playlist, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if os.path.exists(playlist) and os.path.getsize(playlist) > 0:
                return True
            if proc.poll() is not None:
                return False
            time.sleep(0.1)
        return False

    def _tail_log(self, outdir):
        path = os.path.join(outdir, "ffmpeg.log")
        try:
            with open(path, "r", errors="replace") as fh:
                lines = [l.strip() for l in fh.readlines() if l.strip()]
            return " | ".join(lines[-2:]) if lines else None
        except OSError:
            return None

    def _kill(self, proc):
        if not proc:
            return
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def stop(self):
        with self.lock:
            proc = self.proc
            self.proc = None
            self.channel = None
            self.state = "idle"
            self.error = None
        self._kill(proc)
        if self.logfile:
            try:
                self.logfile.close()
            except Exception:
                pass
            self.logfile = None

    def status(self):
        with self.lock:
            alive = self.proc is not None and self.proc.poll() is None
            state = self.state if (self.state != "playing" or alive) else "idle"
            return {
                "state": state,
                "channel": self.channel,
                "name": CHANNELS[self.channel]["name"] if self.channel in CHANNELS else None,
                "error": self.error,
                "backend": self.backend,
            }


STREAMER = Streamer()


# ─── Interfaz HTML ──────────────────────────────────────────────────────
def render_channel_buttons():
    out = []
    for cid, ch in CHANNELS.items():
        out.append(
            '<button class="ch-btn" data-ch="{cid}" data-name="{name}" onclick="playChannel(this)"><span class="led"></span>{name}</button>'.format(
                cid=cid, name=ch["name"]
            )
        )
    return "\n".join(out)


INDEX_HTML = """<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>mamirTV</title>
<script src="https://cdn.jsdelivr.net/npm/hls.js@1"></script>
<style>
  :root{
    --tv-bg:#050608; --tube:#101418; --shadow:rgba(0,0,0,.7);
    --crt:#9fefaf; --crt2:#7de38a; --txt:#baf2c1; --txt2:#7a9b7a;
    --red:#ff3d2e; --amber:#ffb63b; --green:#2ee67b; --btn:#1a1f24; --btn-h:#232931;
    --panel:#0d1116; --bevel:#242a32; --bevel-in:#0a0d11;
  }
  *{box-sizing:border-box; image-rendering:pixelated; image-rendering:crisp-edges}
  body{margin:0;background:#000;color:var(--crt);font-family:Consolas,Monaco,'Courier New',monospace;min-height:100vh;overflow-x:hidden}
  .scanlines::after{
    content:"";position:fixed;inset:0;pointer-events:none;
    background:repeating-linear-gradient(0deg,rgba(159,239,175,.03) 0px,rgba(159,239,175,.03) 1px,transparent 1px,transparent 3px);
    mix-blend-mode:screen;z-index:20;
  }
  .retro{max-width:1360px;margin:0 auto;padding:22px;display:flex;gap:22px;align-items:flex-start}
  .tv-frame{position:relative;background:linear-gradient(180deg,#1d222b,#0b0d12);border-radius:8px;padding:18px;border:4px solid var(--bevel);box-shadow:0 60px 120px var(--shadow),inset 0 1px 0 #3a424d,inset 0 -1px 0 #070a0d}
  .tv-bezel{position:relative;background:radial-gradient(circle at 50% 2%,#1f232a,#070a0d 70%);border-radius:6px;padding:20px;border:2px solid var(--bevel-in)}
  .screen{position:relative;width:100%;aspect-ratio:4/3;background:#000;border:3px solid #05070a;border-radius:2px;overflow:hidden;box-shadow:inset 0 0 40px rgba(0,0,0,.9)}
  .screen video{width:100%;height:100%;display:block;background:#000}
  .crt-glow{position:absolute;inset:0;pointer-events:none;background:radial-gradient(circle at 50% 0%,rgba(159,239,175,.08),transparent 60%);mix-blend-mode:screen}
  #placeholder{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;text-align:center;flex-direction:column;gap:12px;color:var(--txt2);text-shadow:0 0 8px rgba(159,239,175,.15);letter-spacing:2px;text-transform:uppercase}
  .brand{font-size:18px;color:var(--crt);text-shadow:0 0 16px rgba(159,239,175,.5)}
  .chassis{margin-top:12px;display:flex;align-items:center;justify-content:space-between;color:var(--txt2);font-size:11px;letter-spacing:3px;text-transform:uppercase;text-shadow:0 0 6px rgba(159,239,175,.25)}
  .controls{display:flex;flex-direction:column;gap:10px;min-width:270px;position:sticky;top:22px}
  .panel{background:linear-gradient(180deg,#181c23,#0c1015);border:3px solid var(--bevel);border-radius:6px;padding:14px;box-shadow:0 40px 80px var(--shadow),inset 0 1px 0 #3a424d,inset 0 -1px 0 #070a0d}
  .panel-title{margin:0 0 10px 0;font-size:10px;letter-spacing:6px;text-transform:uppercase;color:var(--txt2);text-align:center;text-shadow:0 0 8px rgba(159,239,175,.2)}
  .btn-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:8px}
  .ch-btn{position:relative;display:flex;align-items:center;gap:8px;padding:10px 12px;font:inherit;font-size:10px;letter-spacing:2px;text-transform:uppercase;color:var(--crt);background:linear-gradient(180deg,var(--btn-h),var(--btn));border:2px solid var(--bevel-in);border-radius:4px;text-align:left;cursor:pointer;text-shadow:0 0 8px rgba(159,239,175,.25);transition:all .08s ease}
  .ch-btn:hover{filter:brightness(1.25)}
  .ch-btn:active{transform:translateY(1px)}
  .ch-btn.active{color:var(--tube);background:linear-gradient(180deg,var(--crt2),var(--crt));box-shadow:0 0 18px rgba(159,239,175,.4);text-shadow:none}
  .led{width:6px;height:6px;border-radius:50%;background:var(--btn);border:1px solid #050608;box-shadow:inset 0 1px 0 rgba(255,255,255,.1)}
  .ch-btn.active .led{background:var(--red);box-shadow:0 0 10px var(--red)}
  .stop-btn{margin-top:8px;width:100%;padding:10px;font:inherit;font-size:10px;letter-spacing:4px;text-transform:uppercase;color:var(--crt);background:linear-gradient(180deg,#262b33,#151a21);border:2px solid var(--bevel-in);border-radius:4px;cursor:pointer;text-shadow:0 0 8px rgba(159,239,175,.25)}
  .stop-btn:active{transform:translateY(1px);filter:brightness(1.25)}
  .status-bar{margin-top:12px;display:flex;align-items:center;justify-content:space-between;padding:8px 10px;background:#070a0d;border:1px solid var(--bevel-in);border-radius:4px;font-size:9px;letter-spacing:3px;text-transform:uppercase;color:var(--txt2)}
  .status-bar .live{color:var(--txt2)}
  .status-bar.ok .live{color:var(--crt);text-shadow:0 0 8px rgba(159,239,175,.5)}
  .status-bar.err .live{color:var(--red);text-shadow:0 0 12px rgba(255,61,46,.6)}
  .status-bar.wait .live{color:var(--amber);text-shadow:0 0 10px rgba(255,182,59,.5)}
  .knobs{display:flex;gap:10px;margin-top:12px;justify-content:center}
  .knob{width:36px;height:36px;border-radius:50%;background:radial-gradient(circle at 50% 35%,#3a424d,#0f1318);border:2px solid var(--bevel-in);box-shadow:0 6px 12px rgba(0,0,0,.5),inset 0 1px 0 rgba(255,255,255,.15)}
  @media(max-width:1100px){.retro{flex-direction:column}.controls{position:static;width:100%}.btn-grid{grid-template-columns:repeat(4,1fr)}}
  @media(max-width:680px){.btn-grid{grid-template-columns:repeat(2,1fr)}}
</style>
</head>
<body >
<div class="retro">
  <div class="tv-frame">
    <div class="tv-bezel">
      <div class="screen">
        <video id="video" muted autoplay playsinline webkit-playsinline x5-playsinline></video>
        <div id="placeholder"></div>
        <div class="crt-glow"></div>
      </div>
    </div>
    
  </div>
  <aside class="controls">
    <div class="panel">
      
      <div class="btn-grid">
        __CHANNELS__
      </div>
      <button class="stop-btn" onclick="stopStream()">■</button>
    </div>
    <div class="status-bar" id="statusbar" style="display:none">
      <span class="live" id="status">SIN SEÑAL</span>
      <span class="live" id="backend">__BACKEND__</span>
    </div>
    <div class="knobs">
      <div class="knob"></div>
      <div class="knob"></div>
      <div class="knob"></div>
    </div>
  </aside>
</div>
<script>
const video = document.getElementById('video');
const ph = document.getElementById('placeholder');
const statusBar = document.getElementById('statusbar');
const statusEl = document.getElementById('status');
let hls = null;

function setStatus(text, cls){
  statusEl.textContent = text || 'SIN SEÑAL';
  statusBar.className = 'status-bar ' + (cls||'');
  statusBar.style.display = 'flex';
}

function loadStream(url){
  if(hls){ hls.destroy(); hls=null; }
  ph.style.display='none';
  video.muted=false;
  if(window.Hls && Hls.isSupported()){
    hls=new Hls({lowLatencyMode:true,liveSyncDurationCount:3,liveDurationInfinity:true});
    hls.loadSource(url);
    hls.attachMedia(video);
    hls.on(Hls.Events.MANIFEST_PARSED,()=>video.play().catch(()=>{}));
    hls.on(Hls.Events.ERROR,(e,d)=>{if(d&&d.fatal)setStatus('ERROR DE REPRODUCCIÓN','err');});
  } else if(video.canPlayType('application/vnd.apple.mpegurl')){
    video.src=url; video.play().catch(()=>{});
  } else {
    setStatus('NAVEGADOR SIN SOPORTE HLS','err');
  }
}

async function playChannel(btn){
  const ch=btn.dataset.ch, name=btn.dataset.name;
  document.querySelectorAll('.ch-btn').forEach(b=>b.classList.toggle('active',b===btn));
  setStatus('CONECTANDO '+name+'...','wait');
  try{
    const res=await fetch('/play/'+ch);
    const data=await res.json();
    if(!data.ok){ setStatus('ERROR: '+(data.error||'NO SE PUDO ABRIR'),'err'); return; }
    loadStream(data.playlist);
    setStatus('EN VIVO — '+name,'ok');
  }catch(e){ setStatus('ERROR DE CONEXIÓN','err'); }
}

async function stopStream(){
  try{await fetch('/stop');}catch(e){}
  if(hls){hls.destroy();hls=null;}
  video.removeAttribute('src');video.load();video.muted=true;
  ph.style.display='flex';
  document.querySelectorAll('.ch-btn').forEach(b=>b.classList.remove('active'));
  setStatus('DETENIDO','');
}

async function refresh(){
  try{
    const s=await (await fetch('/status')).json();
    if(s.state==='error') setStatus('ERROR: '+(s.error||'STREAM'),'err');
    else if(s.state==='connecting') setStatus('CONECTANDO...','wait');
  }catch(e){}
}
setInterval(refresh,2500);

function toggleFs(){
  const el=document.getElementById('video');
  if(document.fullscreenElement){document.exitFullscreen().catch(()=>{});} else {el.requestFullscreen({navigationUI:'hide'}).catch(()=>{});}
}
document.getElementById('video').addEventListener('dblclick', toggleFs);
document.querySelector(".tv-frame").addEventListener("dblclick", toggleFs);
document.addEventListener('keydown', (e)=>{if(e.key==='f'||e.key==='F') toggleFs();});
</script>
</body>
</html>
"""


# ─── Servidor HTTP ──────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj), "application/json; charset=utf-8")

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/":
            html = INDEX_HTML.replace("__CHANNELS__", render_channel_buttons())
            html = html.replace("__BACKEND__", STREAMER.backend)
            self._send(200, html, "text/html; charset=utf-8")
            return

        if path == "/status":
            self._json(STREAMER.status())
            return

        if path == "/stop":
            STREAMER.stop()
            self._json({"ok": True})
            return

        if path.startswith("/play/"):
            ch_id = path[len("/play/"):]
            if ch_id not in CHANNELS:
                self._json({"ok": False, "error": "canal desconocido"}, 404)
                return
            try:
                playlist = STREAMER.play(ch_id)
                self._json({"ok": True, "playlist": playlist,
                            "channel": ch_id, "name": CHANNELS[ch_id]["name"]})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
            return

        if path.startswith("/hls/"):
            self._serve_hls(path)
            return

        self._send(404, "no encontrado", "text/plain; charset=utf-8")

    def _serve_hls(self, path):
        parts = path.strip("/").split("/")
        if len(parts) != 3:
            self._send(404, "no encontrado", "text/plain; charset=utf-8")
            return
        _, ch_id, fname = parts
        if ch_id not in CHANNELS or not fname.endswith((".m3u8", ".ts")):
            self._send(404, "no encontrado", "text/plain; charset=utf-8")
            return
        fpath = os.path.join(STREAM_ROOT, ch_id, os.path.basename(fname))
        if not os.path.exists(fpath):
            self._send(404, "no encontrado", "text/plain; charset=utf-8")
            return
        ctype = "application/vnd.apple.mpegurl" if fname.endswith(".m3u8") else "video/mp2t"
        try:
            with open(fpath, "rb") as fh:
                data = fh.read()
        except OSError:
            self._send(404, "no encontrado", "text/plain; charset=utf-8")
            return
        self._send(200, data, ctype)


def run_server():
    port = int(os.environ.get("PORT", "8000"))
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print("=" * 50)
    print("  mamirTV — TV Argentina en vivo")
    print("=" * 50)
    print(f"  Motor de video: {STREAMER.backend}")
    print(f"  Abrí la interfaz en:  http://0.0.0.0:{port}")
    if STREAMER.backend == "nada":
        print("  ⚠ ffmpeg no encontrado: la reproducción no funcionará.")
    print("  Ctrl+C para salir.\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nCerrando...")
    finally:
        STREAMER.stop()
        httpd.server_close()


# ─── Entry ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    args = sys.argv[1:]

    if "--print-urls" in args:
        targets = [a for a in args if a in CHANNELS] or list(CHANNELS)
        for ch_id in targets:
            try:
                url, _, _ = resolve_stream(ch_id)
                print(f"{CHANNELS[ch_id]['name']}: {url}")
            except Exception as e:
                print(f"{ch_id}:  {e}")
        sys.exit(0)

    run_server()
