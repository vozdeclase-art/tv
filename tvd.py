#!/usr/bin/env python3
"""
TVD — TV Argentina en vivo.

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
import signal
import tempfile
import threading
import subprocess
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
import concurrent.futures

try:
    import yt_dlp
except ImportError:
    yt_dlp = None


# ─── Canales regulares ──────────────────────────────────────────────────
SITE_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"

REGULAR_CHANNELS = {
    "trece": {
        "name": "Canal 13",
        # solo funciona por http:// (el https tiene certificado inválido y
        # ffmpeg aborta con "cannot open input file")
        "url": "http://livetrx01.vodgc.net/eltrecetv/index.m3u8",
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
    "nettv": {
        "name": "NetTV",
        "url": "https://unlimited1-us.dps.live/nettv/nettv.smil/playlist.m3u8",
        "referrer": "",
        "ua": SITE_UA,
    },
}

# Canales deportivos de futbollibretv.sx. El sitio embebe un player que arma
# el m3u8 con token en cada consulta, así que la URL real se resuelve al dar
# play (no se puede guardar fija).
SITE_HOSTS = ["https://streamtp99a.sbs"]

SPORTS_CHANNELS = [
    # ── Argentina ──
    ("dsports", "DSports"),
    ("dsports2", "DSports 2"),
    ("espn", "ESPN"),
    ("espn2", "ESPN 2"),
    ("espn3", "ESPN 3"),
    ("espn4", "ESPN 4"),
    ("espn5", "ESPN 5"),
    ("espn6", "ESPN 6"),
    ("espn7", "ESPN 7"),
    ("espnpremium", "ESPN Premium"),
    ("espndeportes", "ESPN Deportes"),
    ("fox_deportes_usa", "Fox Deportes USA"),
    ("liga1max", "Liga 1 MAX"),
    ("tntsports", "TNT Sports"),
    ("tntsportschile", "TNT Sports Chile"),
    ("tyc", "TyC Sports"),
    ("winsports", "Win Sports"),
    ("winplus", "Win Sports +"),
    ("tudn_usa", "TUDN USA"),
    ("azteca7", "Azteca 7"),
    # ── México ──
    ("universo", "Universo"),
    ("canal5mx", "Canal 5 MX"),
    ("espnmx", "ESPN MX"),
    # ── Brasil ──
    ("premiere1", "Premiere 1"),
    ("premiere2", "Premiere 2"),
    ("premiere3", "Premiere 3"),
    ("sporttvbr1", "Sport TV 1"),
    ("sportv2", "SportTV 2"),
    # ── Fox One ──
    ("foxone2", "Fox ONE 2"),
    # ── Canales de evento: solo dan señal con partido en curso ──
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
    ("espn4mx", "ESPN 4 MX"),
    ("sportv1", "SportTV 1"),
    ("universo_usa", "Universo USA"),
    ("univision_usa", "Univision USA"),
]

CHANNELS = {}
CHANNELS.update(REGULAR_CHANNELS)
for _sid, _name in SPORTS_CHANNELS:
    CHANNELS[_sid] = {"name": _name, "site": _sid}


def _site_get(url, referer, timeout=15):
    """GET simple; devuelve (url_final, bytes). Sigue redirects HTTP."""
    req = urllib.request.Request(url, headers={"User-Agent": SITE_UA, "Referer": referer})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.geturl(), resp.read()


_SITE_CACHE = {}
_SITE_TTL = 300    # 5 min; el token del player suele durar más


class _OffAir(RuntimeError):
    """La señal no existe en el proveedor (respuesta definitiva)."""


def _resolve_site_stream(site_id, cache_fail=True):
    """Devuelve el m3u8 real del player, cacheado. No se cachean fallos: cada
    petición reintenta la resolución (como la versión 01) para que canales
    transitorios (telefe, trece, espn, etc.) no queden bloqueados."""
    hit = _SITE_CACHE.get(site_id)
    if hit and time.time() - hit[2] < _SITE_TTL:
        return hit[0], hit[1], {"User-Agent": SITE_UA}
    try:
        out = _resolve_site_stream_now(site_id)
    except _OffAir:
        raise
    return out


def _resolve_site_stream_now(site_id):
    """Scraping efectivo del player (sin caché)."""
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
        final, raw = _site_get(url, origin + "/", timeout=4)
    except urllib.error.HTTPError as e:
        if e.code in (403, 404, 410):
            raise _OffAir("la señal está fuera del aire")
        raise RuntimeError("no se pudo consultar el canal")
    except Exception:
        # timeout del CDN: para TV en vivo equivale a señal caída; se
        # recuerda 60 s y se reintenta solo.
        raise _OffAir("la señal está fuera del aire")
    if b"#EXTM3U" not in raw[:16]:
        raise _OffAir("la señal está fuera del aire")
    ref = origin + "/"
    _SITE_CACHE[site_id] = (final, ref, time.time())
    return final, ref, {"User-Agent": SITE_UA}


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


def resolve_stream_list(ch_id):
    """Lista de candidatos (url, referrer, headers) para un canal.
    Los canales con 'urls' prueban varias fuentes en orden; el resto
    devuelve un único candidato."""
    ch = CHANNELS[ch_id]
    urls = ch.get("urls")
    if not urls:
        return [resolve_stream(ch_id)]
    referrer = ch.get("referrer", "")
    headers = {}
    if ua := ch.get("ua"):
        headers["User-Agent"] = ua
    return [(u, referrer, dict(headers)) for u in urls]


# ─── Streaming HLS ──────────────────────────────────────────────────────
STREAM_ROOT = os.path.join(tempfile.gettempdir(), "tvd")
os.makedirs(STREAM_ROOT, exist_ok=True)


def _out_dir(ch_id):
    """Directorio propio de esta instancia (prefijo con el PID): dos
    servidores simultáneos nunca comparten el mismo live.m3u8."""
    return os.path.join(STREAM_ROOT, f"{os.getpid()}_{ch_id}")


def _clean_stale_dirs():
    """Borra directorios de instancias que ya no existen."""
    me = os.getpid()
    try:
        names = os.listdir(STREAM_ROOT)
    except OSError:
        return
    for name in names:
        pid = name.split("_", 1)[0]
        if not pid.isdigit() or int(pid) == me:
            continue
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            shutil.rmtree(os.path.join(STREAM_ROOT, name), ignore_errors=True)
        except OSError:
            pass


def _clean_orphan_ffmpeg():
    """Mata ffmpeg huérfanos (su proceso padre ya murió) que siguen
    escribiendo en STREAM_ROOT desde otra instancia."""
    if not os.path.isdir("/proc"):
        return
    me = os.getpid()
    root = str(STREAM_ROOT).encode()
    for name in os.listdir("/proc"):
        if not name.isdigit() or int(name) == me:
            continue
        try:
            with open(f"/proc/{name}/cmdline", "rb") as fh:
                cmd = fh.read()
        except OSError:
            continue
        if not cmd:
            continue
        argv0 = cmd.split(b"\0", 1)[0]
        if b"ffmpeg" not in argv0 or root not in cmd:
            continue
        try:
            with open(f"/proc/{name}/stat", "rb") as fh:
                stat = fh.read().decode("utf-8", "replace")
            ppid = int(stat.rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        if ppid > 1:
            continue   # su padre sigue vivo: no es huérfano
        try:
            os.kill(int(name), signal.SIGTERM)
            print(f"[limpieza] ffmpeg huérfano (pid {name}) eliminado", flush=True)
        except OSError:
            pass


_clean_stale_dirs()
_clean_orphan_ffmpeg()


class Streamer:
    def __init__(self):
        self.lock = threading.Lock()
        self._play_lock = threading.Lock()
        self.proc = None
        self.channel = None
        self.state = "idle"
        self.error = None
        self.logfile = None
        self.backend = "ffmpeg" if shutil.which("ffmpeg") else "nada"
        self._respawns = 0
        self._playing_since = 0.0
        self._req = 0
        self._good = {}
        threading.Thread(target=self._watch_loop, daemon=True, name="tvd-watch").start()

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
        if ua or referrer:
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
            "-hls_list_size", "12",
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
        with self.lock:
            self._req += 1
            my_req = self._req
        with self._play_lock:
            with self.lock:
                if my_req != self._req:
                    raise RuntimeError("pedido cancelado")
            return self._play(ch_id, my_req)

    def _play(self, ch_id, my_req=None):
        if my_req is None:
            my_req = self._req
        with self.lock:
            prev_ch = self.channel
        self.stop()
        outdir = _out_dir(ch_id)
        shutil.rmtree(outdir, ignore_errors=True)
        os.makedirs(outdir, exist_ok=True)
        playlist = os.path.join(outdir, "live.m3u8")

        with self.lock:
            self.channel = ch_id
            self.state = "connecting"
            self.error = None

        def cancelled():
            return self._req != my_req

        candidates = resolve_stream_list(ch_id)
        start = self._good.get(ch_id, 0) % len(candidates)
        ordered = [((start + i) % len(candidates), candidates[(start + i) % len(candidates)])
                   for i in range(len(candidates))]

        last_err = None
        for idx, (url, referrer, headers) in ordered:
            if cancelled():
                raise RuntimeError("pedido cancelado")
            # 1º intento: remux directo (rápido, sin recodificar)
            proc = self._spawn(self._ffmpeg_cmd(url, referrer, headers, outdir, True), outdir)
            if self._wait_playlist(proc, playlist):
                with self.lock:
                    self.proc = proc
                    self.state = "playing"
                    self._playing_since = time.time()
                self._good[ch_id] = idx
                return f"/hls/{ch_id}/live.m3u8"
            if cancelled():
                self._kill(proc)
                raise RuntimeError("pedido cancelado")
            # 2º intento: recodificar (más compatible)
            self._kill(proc)
            shutil.rmtree(outdir, ignore_errors=True)
            os.makedirs(outdir, exist_ok=True)
            proc = self._spawn(self._ffmpeg_cmd(url, referrer, headers, outdir, False), outdir)
            if not self._wait_playlist(proc, playlist):
                err = self._tail_log(outdir) or "no se pudo abrir el stream"
                self._kill(proc)
                last_err = err
                continue
            with self.lock:
                self.proc = proc
                self.state = "playing"
                self._playing_since = time.time()
            self._good[ch_id] = idx
            return f"/hls/{ch_id}/live.m3u8"

        err = last_err or "no se pudo abrir el stream"
        shutil.rmtree(outdir, ignore_errors=True)
        if CHANNELS.get(ch_id, {}).get("site"):
            _SITE_CACHE.pop(ch_id, None)
        with self.lock:
            self.state = "error"
            self.error = err
        raise RuntimeError(err)

    def _respawn(self, ch):
        with self._play_lock:
            with self.lock:
                sane = (self.channel == ch and self.state == "playing"
                        and self.proc is not None and self.proc.poll() is not None)
            if not sane:
                return
            self._play(ch)

    def _watch_loop(self):
        while True:
            time.sleep(3)
            with self.lock:
                if self.state != "playing":
                    continue
                if self.proc is None or self.proc.poll() is None:
                    if self._playing_since and time.time() - self._playing_since > 30:
                        self._respawns = 0
                    continue
                ch = self.channel
                self._respawns += 1
                n = self._respawns
            if n > 10:
                with self.lock:
                    self.state = "error"
                    self.error = "la señal se cayó varias veces seguidas"
                    self._respawns = 0
                continue
            print(f"[watchdog] ffmpeg murió en {ch}; reintento {n}/10", flush=True)
            try:
                self._respawn(ch)
                print(f"[watchdog] {ch} reanudado", flush=True)
            except Exception as e:
                print(f"[watchdog] reintento falló: {e}", flush=True)

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

    def _kill_stray_ffmpeg(self):
        me = os.getpid()
        try:
            pids = [p for p in os.listdir("/proc") if p.isdigit()]
        except OSError:
            return
        for p in pids:
            try:
                with open(f"/proc/{p}/comm") as fh:
                    if fh.read().strip() != "ffmpeg":
                        continue
                with open(f"/proc/{p}/stat") as fh:
                    fields = fh.read().split()
                if len(fields) < 4 or int(fields[3]) != me:
                    continue
                os.kill(int(p), 9)
            except (OSError, ValueError):
                pass

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
            state = self.state
            if state == "playing" and not alive:
                state = "connecting"
            return {
                "state": state,
                "channel": self.channel,
                "name": CHANNELS[self.channel]["name"] if self.channel in CHANNELS else None,
                "error": self.error,
                "backend": self.backend,
            }


STREAMER = Streamer()

# ─── EPG (programación) vía mi.tv ───────────────────────────────────────
# El endpoint async acepta el offset UTC en minutos; con "0" las horas del
# HTML vienen ya en UTC, así que el cálculo de "ahora/luego" es directo.
EPG_SOURCES = {
    "telefe": "ar#telefe",
    "trece": "ar#el-trece",
    "america": "ar#america-2-capital",
    "tyc": "ar#tyc-sports",
    "tntsports": "ar#tnt-sports",
    "espn": "ar#espn",
    "espn2": "ar#espn-2",
    "espn3": "ar#espn-3",
    "espnpremium": "ar#fox-sports-premium",
    "fox1ar": "cl#fox-sports-1-hd",
    "foxar2": "ar#fox-sports-2",
    "foxar3": "ar#fox-sports-3",
    "azteca7": "mx#azteca-7",
    "winsports": "co#win-sports-hd",
    "sportv1": "br#sportv",
    "sportv2": "br#sportv2",
}
EPG_CACHE_DIR = os.path.join(tempfile.gettempdir(), "tvd-epg")
EPG_TTL = 1200  # 20 min
_EPG_ALL_LOCK = threading.Lock()


def _parse_epg_items(html):
    """Extrae (start, title, en_aire) de un fragmento de mi.tv."""
    items = []
    for m in re.finditer(r"<li[^>]*>(.*?)</li>", html, re.S):
        blk = m.group(1)
        if "program-link" not in blk:
            continue
        t = re.search(r'<span class="time">(\d{1,2}:\d{2})</span>', blk)
        ti = re.search(r"<h2>\s*([^<]+?)\s*</h2>", blk)
        if t and ti:
            title = re.sub(r"\s+", " ", ti.group(1)).strip()
            items.append({"start": t.group(1), "title": title,
                          "now": "progress-container" in blk})
    return items


def _epg_now_next(items):
    """(now, next) a partir de la lista; usa el marcador de la página o,
    de respaldo, el reloj UTC."""
    idx = next((i for i, it in enumerate(items) if it["now"]), None)
    if idx is None:
        hhmm = time.strftime("%H:%M", time.gmtime())
        pasados = [i for i, it in enumerate(items) if it["start"] <= hhmm]
        idx = pasados[-1] if pasados else None
    now = items[idx] if idx is not None else None
    nxt = items[idx + 1] if idx is not None and idx + 1 < len(items) else None
    if now:
        now = {"title": now["title"], "start": now["start"],
               "end": nxt["start"] if nxt else None}
    if nxt:
        nxt = {"title": nxt["title"], "start": nxt["start"]}
    return now, nxt


def _epg_fetch(site_id):
    """Devuelve {'now': ..., 'next': ...} con caché en disco de 20 min."""
    version, slug = site_id.split("#", 1)
    os.makedirs(EPG_CACHE_DIR, exist_ok=True)
    cache = os.path.join(EPG_CACHE_DIR, f"{version}_{slug}.json")
    if os.path.exists(cache):
        try:
            with open(cache, "r") as fh:
                data = json.load(fh)
            if time.time() - data.get("_ts", 0) < EPG_TTL:
                return data
        except (OSError, ValueError):
            pass
    url = f"https://www.mi.tv/{version}/async/channel/{slug}/0"
    req = urllib.request.Request(url, headers={
        "User-Agent": SITE_UA,
        "X-Requested-With": "XMLHttpRequest",
    })
    with urllib.request.urlopen(req, timeout=12) as resp:
        html = resp.read().decode("utf-8", "replace")
    items = _parse_epg_items(html)
    if not items:
        raise RuntimeError("guía vacía")
    now, nxt = _epg_now_next(items)
    data = {"_ts": time.time(), "now": now, "next": nxt}
    try:
        with open(cache, "w") as fh:
            json.dump(data, fh)
    except OSError:
        pass
    return data


_EPG_ALL = {"ts": 0, "data": {}}
_EPG_ALL_TTL = 90   # segundos


def _epg_all():
    """Programación 'ahora' de todos los canales con guía, en paralelo.
    Cacheado 90 s para no martillar mi.tv."""
    with _EPG_ALL_LOCK:
        if time.time() - _EPG_ALL["ts"] < _EPG_ALL_TTL and _EPG_ALL["data"]:
            return _EPG_ALL["data"]

        def one(ch_id):
            try:
                d = _epg_fetch(EPG_SOURCES[ch_id])
                now = d.get("now")
                return ch_id, (now.get("title") if now else None)
            except Exception:
                return ch_id, _EPG_ALL["data"].get(ch_id)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            out = dict(ex.map(one, list(EPG_SOURCES)))
        _EPG_ALL["ts"] = time.time()
        _EPG_ALL["data"] = out
        return out


# ─── Interfaz HTML ──────────────────────────────────────────────────────
def render_channel_buttons():
    regular = []
    for cid, ch in REGULAR_CHANNELS.items():
        regular.append(
            '<button class="ch-btn" data-ch="{cid}" data-name="{name}" onclick="playChannel(this)">'
            '<img class="ch-logo" src="/logos/{cid}" alt="" loading="lazy" onerror="this.remove()">'
            '<span class="ch-txt"><span class="ch-name">{name}</span>'
            '<span class="ch-now" data-ch="{cid}"></span></span></button>'.format(
                cid=cid, name=ch["name"]
            )
        )
    deportes = []
    for cid, ch in CHANNELS.items():
        if cid in REGULAR_CHANNELS:
            continue
        deportes.append(
            '<button class="ch-btn" data-ch="{cid}" data-name="{name}" onclick="playChannel(this)">'
            '<img class="ch-logo" src="/logos/{cid}" alt="" loading="lazy" onerror="this.remove()">'
            '<span class="ch-txt"><span class="ch-name">{name}</span>'
            '<span class="ch-now" data-ch="{cid}"></span></span></button>'.format(
                cid=cid, name=ch["name"]
            )
        )
    return {"regular": "\n".join(regular), "deportes": "\n".join(deportes)}


INDEX_HTML = """<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TVD</title>
<script src="https://cdn.jsdelivr.net/npm/hls.js@1"></script>
<style>
  :root{
    --tv-bg:#050608; --tube:#101418; --shadow:rgba(0,0,0,.7);
    --crt:#ffb63b; --crt2:#ff9e1f; --txt:#ffcf87; --txt2:#b08348;
    --red:#ff3d2e; --amber:#ffb63b; --green:#2ee67b; --btn:#241d12; --btn-h:#332818;
    --panel:#0d1116; --bevel:#242a32; --bevel-in:#0a0d11;
  }
  *{box-sizing:border-box; image-rendering:pixelated; image-rendering:crisp-edges}
  html,body{height:100%}
  body{margin:0;background:#000;color:var(--crt);font-family:Consolas,Monaco,'Courier New',monospace;overflow:hidden}
  .scanlines::after{
    content:"";position:fixed;inset:0;pointer-events:none;
    background:repeating-linear-gradient(0deg,rgba(159,239,175,.03) 0px,rgba(159,239,175,.03) 1px,transparent 1px,transparent 3px);
    mix-blend-mode:screen;z-index:20;
  }
  /* ── televisor a pantalla completa ── */
  .tv-frame{position:fixed;inset:0;display:flex;overflow:hidden;padding:8px;background:linear-gradient(180deg,#1d222b,#0b0d12);border:4px solid var(--bevel);box-shadow:inset 0 1px 0 #3a424d,inset 0 -1px 0 #070a0d}
  .tv-bezel{position:relative;flex:1;min-width:0;display:flex;min-height:0;background:radial-gradient(circle at 50% 2%,#1f232a,#070a0d 70%);border:2px solid var(--bevel-in);border-radius:4px;padding:10px}
  .screen{position:relative;flex:1;min-width:0;min-height:0;display:flex;flex-direction:column;background:#000;border:3px solid #05070a;border-radius:2px;overflow:hidden;box-shadow:inset 0 0 40px rgba(0,0,0,.9)}
  .screen video{flex:1;min-height:0;width:100%;display:block;background:#000;object-fit:contain}
  .crt-glow{position:absolute;inset:0;pointer-events:none;background:radial-gradient(circle at 50% 0%,rgba(255,182,59,.08),transparent 60%);mix-blend-mode:screen}
  /* ── letrero LED: programación del canal, fijo (sin scroll) ── */
  .sign{flex:none;min-width:0;height:36px;display:flex;align-items:center;background:linear-gradient(180deg,#0a0c0f,#040506);border-top:2px solid #181c22;overflow:hidden;position:relative;z-index:3;opacity:.3;transition:opacity .5s ease}
  .sign.on{opacity:1}
  .sign-logo{display:none;flex:none;height:22px;max-width:74px;object-fit:contain;margin:0 4px 0 12px;filter:drop-shadow(0 0 5px rgba(255,182,59,.55))}
  .sign.on .sign-logo.vis{display:block}
  .sign-view{flex:1;min-width:0;overflow:hidden;position:relative;margin:0 12px}
  .sign-view::after{content:"";position:absolute;inset:0;pointer-events:none;background-image:radial-gradient(circle,rgba(0,0,0,.6) .8px,transparent 1.1px);background-size:3px 3px;opacity:.5}
  .sign-track{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:14px;font-weight:bold;letter-spacing:5px;text-transform:uppercase;color:#463512}
  .sign.on .sign-track{color:var(--amber);text-shadow:0 0 5px rgba(255,182,59,.9),0 0 16px rgba(255,182,59,.45),0 0 34px rgba(255,182,59,.25)}
  #placeholder{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;text-align:center;flex-direction:column;gap:12px;color:var(--txt2);text-shadow:0 0 8px rgba(159,239,175,.15);letter-spacing:2px;text-transform:uppercase}
  /* ── botón burger ── */
  .menu-btn{position:fixed;top:14px;left:14px;z-index:40;width:46px;height:46px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:5px;padding:0;background:linear-gradient(180deg,#181c23,#0c1015);border:2px solid var(--bevel);border-radius:8px;cursor:pointer;box-shadow:0 10px 24px rgba(0,0,0,.65);transition:filter .12s ease}
  .menu-btn:hover{filter:brightness(1.3)}
  .menu-btn span{width:20px;height:3px;border-radius:2px;background:var(--crt);box-shadow:0 0 6px rgba(255,182,59,.45);transition:transform .16s ease,opacity .12s ease}
  .menu-btn.open span:nth-child(1){transform:translateY(8px) rotate(45deg)}
  .menu-btn.open span:nth-child(2){opacity:0}
  .menu-btn.open span:nth-child(3){transform:translateY(-8px) rotate(-45deg)}
  /* ── chip de estado flotante ── */
  .live-chip{position:fixed;top:16px;right:16px;z-index:30;max-width:50vw;padding:8px 12px;background:rgba(7,10,13,.85);border:1px solid var(--bevel-in);border-radius:6px;font-size:9px;letter-spacing:2px;text-transform:uppercase;color:var(--txt2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;pointer-events:none}
  .live-chip.ok{color:var(--crt);text-shadow:0 0 8px rgba(159,239,175,.5);border-color:#1b4731}
  .live-chip.err{color:var(--red);text-shadow:0 0 12px rgba(255,61,46,.5);border-color:#4a1712}
  .live-chip.wait{color:var(--amber);text-shadow:0 0 10px rgba(255,182,59,.45);border-color:#4a3a12}
  /* ── panel de canales (ocultable) ── */
  .controls{position:fixed;top:0;left:0;height:100%;width:330px;max-width:88vw;z-index:35;display:block;overflow-y:auto;overscroll-behavior:contain;padding:74px 14px 18px;background:linear-gradient(180deg,rgba(13,17,22,.97),rgba(4,7,10,.98));border-right:2px solid var(--bevel);transform:translateX(-105%);transition:transform .18s ease}
  .controls.open{transform:none;box-shadow:24px 0 70px rgba(0,0,0,.75)}
  .panel{background:linear-gradient(180deg,#181c23,#0c1015);border:3px solid var(--bevel);border-radius:6px;padding:12px;box-shadow:0 40px 80px var(--shadow),inset 0 1px 0 #3a424d,inset 0 -1px 0 #070a0d}
  .panel-title{margin:0 0 10px 0;font-size:10px;letter-spacing:6px;text-transform:uppercase;color:var(--txt2);text-align:center;text-shadow:0 0 8px rgba(255,182,59,.25)}
  .ch-hint{display:block;margin-top:3px;font-size:8px;letter-spacing:2px;color:var(--txt2);opacity:.55;font-weight:normal}
  .ch-search{width:100%;margin:0 0 10px 0;padding:10px 12px;font:inherit;font-size:11px;letter-spacing:2px;text-transform:uppercase;color:var(--crt);background:#070a0d;border:2px solid var(--bevel-in);border-radius:4px;outline:none;box-shadow:inset 0 2px 8px rgba(0,0,0,.6)}
  .ch-search::placeholder{color:var(--txt2);opacity:.7;letter-spacing:2px}
  .ch-search:focus{border-color:#4a3a12;box-shadow:inset 0 2px 8px rgba(0,0,0,.6),0 0 12px rgba(255,182,59,.2)}
  .ch-search::-webkit-search-cancel-button{-webkit-appearance:none;appearance:none;color:var(--txt2);cursor:pointer}
  .no-results{display:none;padding:14px 0 4px;text-align:center;font-size:9px;letter-spacing:3px;text-transform:uppercase;color:var(--red);text-shadow:0 0 10px rgba(255,61,46,.5)}
  .btn-grid{display:grid;grid-template-columns:1fr;gap:6px}
  .ch-section{margin:10px 0 6px 0;padding:4px 8px;font-size:9px;letter-spacing:4px;text-transform:uppercase;color:var(--txt2);border-bottom:1px solid var(--bevel-in)}
  .ch-btn{position:relative;display:flex;align-items:center;gap:9px;padding:8px 10px;font:inherit;font-size:10px;letter-spacing:1px;text-transform:uppercase;color:var(--crt);background:linear-gradient(180deg,var(--btn-h),var(--btn));border:2px solid var(--bevel-in);border-radius:4px;text-align:left;cursor:pointer;text-shadow:0 0 8px rgba(255,182,59,.25);transition:all .08s ease;overflow:hidden}
  .ch-btn:hover{filter:brightness(1.25)}
  .ch-btn:active{transform:translateY(1px)}
  .ch-btn.active{color:#1a1305;background:linear-gradient(180deg,var(--crt2),var(--crt));box-shadow:0 0 18px rgba(255,182,59,.45);text-shadow:none}
  .ch-logo{flex:none;height:24px;width:auto;max-width:46px;object-fit:contain;background:#fff;border-radius:2px;padding:1px 3px;box-shadow:0 1px 3px rgba(0,0,0,.5)}
  .ch-btn.active .ch-logo{filter:none}
  .ch-txt{flex:1;min-width:0;display:flex;flex-direction:column;gap:2px}
  .ch-name{font-weight:bold;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .ch-now{font-size:8px;letter-spacing:.3px;opacity:.72;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;text-transform:none}
  .ch-btn.active .ch-now{opacity:.85}
  .led{flex:none;width:6px;height:6px;border-radius:50%;background:var(--btn);border:1px solid #050608;box-shadow:inset 0 1px 0 rgba(255,255,255,.1)}
  .ch-btn.active .led{background:var(--red);box-shadow:0 0 10px var(--red)}
  .stop-btn{margin:0 0 8px 0;width:100%;padding:10px;font:inherit;font-size:10px;letter-spacing:4px;text-transform:uppercase;color:var(--crt);background:linear-gradient(180deg,#332818,#1c1610);border:2px solid var(--bevel-in);border-radius:4px;cursor:pointer;text-shadow:0 0 8px rgba(255,182,59,.25)}
  .stop-btn:active{transform:translateY(1px);filter:brightness(1.25)}
  .status-bar{margin-top:12px;display:flex;align-items:center;justify-content:space-between;padding:8px 10px;background:#070a0d;border:1px solid var(--bevel-in);border-radius:4px;font-size:9px;letter-spacing:2px;text-transform:uppercase;color:var(--txt2)}
  .status-bar .live{color:var(--txt2)}
  .status-bar.ok .live{color:var(--crt);text-shadow:0 0 8px rgba(159,239,175,.5)}
  .status-bar.err .live{color:var(--red);text-shadow:0 0 12px rgba(255,61,46,.6)}
  .status-bar.wait .live{color:var(--amber);text-shadow:0 0 10px rgba(255,182,59,.5)}
  .knobs{display:flex;gap:10px;margin-top:12px;justify-content:center}
  .knob{width:32px;height:32px;border-radius:50%;background:radial-gradient(circle at 50% 35%,#3a424d,#0f1318);border:2px solid var(--bevel-in);box-shadow:0 6px 12px rgba(0,0,0,.5),inset 0 1px 0 rgba(255,255,255,.15)}
  @media(max-width:520px){.controls{width:100%;max-width:100%}.btn-grid{grid-template-columns:1fr}.live-chip{max-width:42vw}.sign{height:30px}.sign-track{font-size:11px;letter-spacing:3px}.sign-logo{height:18px;max-width:54px;margin-left:8px}.sign-view{margin:0 8px}}
</style>
</head>
<body class="scanlines">
<div class="tv-frame">
  <div class="tv-bezel">
    <div class="screen">
      <video id="video" muted autoplay playsinline webkit-playsinline x5-playsinline></video>
      <div id="placeholder"></div>
      <div class="crt-glow"></div>
      <div class="sign" id="sign">
        <img class="sign-logo" id="sign-logo" alt="">
        <div class="sign-view"><div class="sign-track" id="sign-track"></div></div>
      </div>
    </div>
  </div>
</div>

<button class="menu-btn" id="menu-btn" aria-label="Abrir canales"><span></span><span></span><span></span></button>
<div class="live-chip" id="live-chip">SIN SEÑAL</div>

<aside class="controls" id="controls">
  <div class="panel">
    <button class="stop-btn" onclick="stopStream()">■ APAGAR</button>
    <div class="panel-title">CANALES<span class="ch-hint">↑ ↓ ZAPPING</span></div>
    <input class="ch-search" id="ch-search" type="search" placeholder="BUSCAR CANAL..." autocomplete="off" spellcheck="false">
    <div class="btn-grid">
      <div class="ch-section">CANALES</div>
      __CHANNELS_REGULAR__
      <div class="ch-section">DEPORTES</div>
      __CHANNELS_DEPORTES__
    </div>
    <div class="no-results" id="no-results">SIN RESULTADOS</div>
    <div class="status-bar" id="statusbar" style="display:none">
      <span class="live" id="status">SIN SEÑAL</span>
      <span class="live" id="backend">__BACKEND__</span>
    </div>
    <div class="knobs">
      <div class="knob"></div>
      <div class="knob"></div>
      <div class="knob"></div>
    </div>
  </div>
</aside>
<script>
const video = document.getElementById('video');
const ph = document.getElementById('placeholder');
const statusBar = document.getElementById('statusbar');
const statusEl = document.getElementById('status');
const chip = document.getElementById('live-chip');
const controls = document.getElementById('controls');
const menuBtn = document.getElementById('menu-btn');
const search = document.getElementById('ch-search');
const noResults = document.getElementById('no-results');
let hls = null, soundOn = false;
let currentCh = null, currentName = '', busy = false, attempts = 0, nonPlay = 0, coolReset = false;
let playToken = 0;   /* invalida respuestas de canales que ya no se quieren */
let lastPos = 0, lastMove = Date.now(), lastHlsFix = 0;

/* ── letrero LED: programación del canal, fijo bajo el video ── */
const signEl = document.getElementById('sign');
const signLogo = document.getElementById('sign-logo');
const signTrack = document.getElementById('sign-track');
let signCh = null, signEpg = '';
function setSign(ch, name, on){
  if(ch && ch !== signCh){
    signCh = ch;
    signEpg = '';   /* la guía del canal anterior ya no aplica */
    signLogo.classList.remove('vis');
    signLogo.onload = ()=>signLogo.classList.add('vis');
    signLogo.onerror = ()=>signLogo.classList.remove('vis');
    signLogo.src = '/logos/' + ch;
  }
  if(signEpg !== signTrack.textContent) signTrack.textContent = signEpg;
  signEl.classList.toggle('on', !!on && !!signEpg);
}

async function loadEpg(ch, name){
  if(!(ch in epgCache)){
    let d = null;
    try{ d = await (await fetch('/epg/' + ch)).json(); }catch(e){ d = null; }
    epgCache[ch] = (d && d.ok && d.now) ? d : null;
  }
  if(currentCh !== ch) return;   /* el usuario ya cambió de canal */
  const d = epgCache[ch];
  signEpg = d ? ('AHORA ' + d.now.title + (d.next && d.next.title ? '   ·   DESPUÉS ' + d.next.title : '')) : '';
  setSign(ch, name, true);
}
const epgCache = {};

/* programación "ahora" en cada botón del panel */
async function loadAllEpg(){
  let d = null;
  try{ d = await (await fetch('/epg-all')).json(); }catch(e){ return; }
  if(!d || !d.ok || !d.epg) return;
  document.querySelectorAll('.ch-now').forEach(el=>{
    const t = d.epg[el.dataset.ch];
    el.textContent = t ? t : '';
  });
}

document.addEventListener('pointerdown', ()=>{ soundOn = true; video.muted = false; }, true);

function filterChannels(){
  const q = search.value.trim().toLowerCase();
  let vis = 0;
  document.querySelectorAll('.ch-btn').forEach(b=>{
    const hit = !q || (b.dataset.name + ' ' + b.dataset.ch).toLowerCase().indexOf(q) !== -1;
    b.style.display = hit ? '' : 'none';
    if(hit) vis++;
  });
  noResults.style.display = (q && !vis) ? 'block' : 'none';
}
search.addEventListener('input', filterChannels);

function openMenu(){ controls.classList.add('open'); menuBtn.classList.add('open'); search.focus({preventScroll:true}); loadAllEpg(); }
function closeMenu(){ controls.classList.remove('open'); menuBtn.classList.remove('open'); }
function toggleMenu(){ controls.classList.contains('open') ? closeMenu() : openMenu(); }
menuBtn.addEventListener('click', (e)=>{ e.stopPropagation(); toggleMenu(); });
document.querySelector('.tv-frame').addEventListener('click', closeMenu);
document.addEventListener('keydown', (e)=>{
  if(e.key === 'Escape'){
    if(search.value){ search.value = ''; filterChannels(); return; }
    closeMenu();
    return;
  }
  /* zapping: flecha abajo = canal siguiente, flecha arriba = anterior */
  if(e.key === 'ArrowDown' || e.key === 'ArrowUp'){
    if(e.altKey || e.ctrlKey || e.metaKey) return;
    e.preventDefault();
    zap(e.key === 'ArrowDown' ? 1 : -1);
  }
});

/* lista de canales visibles (respeta el filtro de búsqueda) */
function visibleChannels(){
  return Array.prototype.slice.call(
    document.querySelectorAll('.ch-btn')
  ).filter(function(b){ return b.offsetParent !== null; });
}

/* salta al canal anterior (dir=-1) o siguiente (dir=1) */
function zap(dir){
  const list = visibleChannels();
  if(!list.length) return;
  const cur = list.findIndex(function(b){ return b.dataset.ch === currentCh; });
  let i;
  if(cur < 0) i = (dir > 0) ? 0 : list.length - 1;
  else i = (cur + dir + list.length) % list.length;
  const btn = list[i];
  if(!btn) return;
  if(controls.classList.contains('open')) btn.scrollIntoView({block:'nearest'});
  playChannel(btn, true);
}

function setStatus(text, cls){
  const t = text || 'SIN SEÑAL';
  statusEl.textContent = t;
  statusBar.className = 'status-bar ' + (cls || '');
  statusBar.style.display = 'flex';
  chip.textContent = t;
  chip.className = 'live-chip ' + (cls || '');
}

function markActive(ch){
  document.querySelectorAll('.ch-btn').forEach(b=>b.classList.toggle('active', b.dataset.ch === ch));
}

function loadStream(url){
  if(hls){ hls.destroy(); hls = null; }
  ph.style.display = 'none';
  video.muted = !soundOn;
  lastPos = video.currentTime;
  lastMove = Date.now();
  if(window.Hls && Hls.isSupported()){
    hls = new Hls({lowLatencyMode:true, liveSyncDurationCount:1, maxLiveSyncPlaybackRate:1.5, liveDurationInfinity:true});
    hls.loadSource(url);
    hls.attachMedia(video);
    hls.on(Hls.Events.MANIFEST_PARSED, ()=>{ lastMove = Date.now(); video.play().catch(()=>{}); });
    hls.on(Hls.Events.ERROR, (e,d)=>{
      if(!d) return;
      try{
        window.__hlsErr = window.__hlsErr || [];
        window.__hlsErr.push({t:d.type, d:d.details, f:!!d.fatal, at:Math.round(performance.now())});
        if(window.__hlsErr.length > 40) window.__hlsErr.shift();
      }catch(_){}
      if(!d.fatal) return;
      const now = Date.now();
      if(now - lastHlsFix > 4000){
        lastHlsFix = now;
        if(d.type === Hls.ErrorTypes.NETWORK_ERROR){ setStatus('RECONECTANDO...','wait'); hls.startLoad(); return; }
        if(d.type === Hls.ErrorTypes.MEDIA_ERROR){ setStatus('RECONECTANDO...','wait'); hls.recoverMediaError(); return; }
      }
      setStatus('ERROR DE REPRODUCCIÓN','err');
      retry();
    });
  } else if(video.canPlayType('application/vnd.apple.mpegurl')){
    video.src = url; video.play().catch(()=>{});
  } else {
    setStatus('NAVEGADOR SIN SOPORTE HLS','err');
  }
}

async function startChannel(ch, name){
  /* cambio de canal inmediato: si ya hay uno conectando, lo reemplaza */
  const myToken = ++playToken;
  busy = true;
  currentCh = ch; currentName = name;
  setStatus('CONECTANDO ' + name + '...','wait');
  try{
    const res = await fetch('/play/' + ch);
    const data = await res.json();
    if(myToken !== playToken) return;   /* el usuario ya cambió de nuevo */
    if(!data.ok){
      const msg = data.error || 'NO SE PUDO ABRIR';
      setStatus('SIN SEÑAL — ' + msg,'err');
      busy = false;
      if(/fuera del aire|desconocido|no se pudo consultar/i.test(msg)){
        currentCh = null;   /* no reintentar un canal que no existe */
        document.querySelectorAll('.ch-btn').forEach(b=>b.classList.remove('active'));
        /* cortar el audio/video del canal anterior: no debe seguir sonando */
        if(hls){ hls.destroy(); hls = null; }
        video.removeAttribute('src'); video.load(); video.muted = true;
        ph.style.display = 'flex';
        setSign(null, '', false);
      } else { retry(); }
      return;
    }
    loadStream(data.playlist);
    nonPlay = 0;
    setSign(ch, name, true);
    loadEpg(ch, name);
    setStatus('EN VIVO — ' + name,'ok');
  }catch(e){ if(myToken !== playToken) return; setStatus('ERROR DE CONEXIÓN','err'); busy = false; retry(); return; }
  busy = false;
}

function armCool(){
  if(!coolReset){ coolReset = true; setTimeout(()=>{ attempts = 0; coolReset = false; }, 20000); }
}

function retry(){
  if(busy || !currentCh) return;
  if(attempts >= 5){ armCool(); return; }
  attempts++;
  setTimeout(()=>{ if(!busy && currentCh) startChannel(currentCh, currentName); }, 1500 * attempts);
}

async function playChannel(btn, keepMenu){
  markActive(btn.dataset.ch);
  if(!keepMenu) closeMenu();
  await startChannel(btn.dataset.ch, btn.dataset.name);
}

async function stopStream(){
  try{ await fetch('/stop'); }catch(e){}
  currentCh = null; attempts = 0; nonPlay = 0; busy = false;
  signCh = null; signEpg = ''; signTrack.textContent = ''; setSign(null, '', false);
  if(hls){ hls.destroy(); hls = null; }
  video.removeAttribute('src'); video.load(); video.muted = true;
  ph.style.display = 'flex';
  document.querySelectorAll('.ch-btn').forEach(b=>b.classList.remove('active'));
  setStatus('DETENIDO','');
}

async function refresh(){
  let s = null;
  try{ s = await (await fetch('/status')).json(); }catch(e){ return; }
  if(busy) return;
  if(!currentCh){
    if(s.state === 'error') setStatus('ERROR: ' + (s.error || 'STREAM'),'err');
    return;
  }
  if(s.state === 'playing'){
    nonPlay = 0; attempts = 0;
    const sc = s.channel || currentCh, sn = s.name || currentName;
    setSign(sc, sn, true);
    if(!epgCache[sc]) loadEpg(sc, sn);   /* completa la guía si aún no está */
    setStatus('EN VIVO — ' + sn,'ok');
    return;
  }
  if(s.state === 'connecting'){ nonPlay = 0; setStatus('RECONECTANDO...','wait'); return; }
  if(s.state === 'error'){
    setStatus('ERROR: ' + (s.error || 'STREAM') + ' — REINTENTANDO','err');
    retry(); return;
  }
  nonPlay++;
  if(nonPlay >= 2){ setStatus('SEÑAL INTERRUMPIDA — RECONECTANDO...','wait'); retry(); }
}
setInterval(refresh, 2500);

/* si el video se congela >10 s con estado 'playing', recarga el stream */
setInterval(()=>{
  if(!currentCh || busy || document.hidden) return;
  if(video.readyState >= 3 && video.currentTime !== lastPos){
    lastPos = video.currentTime;
    lastMove = Date.now();
    return;
  }
  if(Date.now() - lastMove > 10000){
    lastMove = Date.now();
    if(attempts < 5){
      attempts++;
      setStatus('RECONECTANDO SEÑAL...','wait');
      startChannel(currentCh, currentName);
    } else { armCool(); }
  }
}, 1000);

function toggleFs(){
  const el = document.querySelector('.tv-frame');
  if(document.fullscreenElement){ document.exitFullscreen().catch(()=>{}); } else { el.requestFullscreen({navigationUI:'hide'}).catch(()=>{}); }
}
video.addEventListener('dblclick', toggleFs);
document.querySelector('.tv-frame').addEventListener('dblclick', toggleFs);
document.addEventListener('keydown', (e)=>{ if(e.key === 'f' || e.key === 'F') toggleFs(); });

/* canal por defecto al iniciar */
(function(){
  const btn = document.querySelector('.ch-btn[data-ch="telefe"]');
  if(btn) btn.classList.add('active');
  startChannel('telefe', 'Telefe');
  loadAllEpg();
  setInterval(loadAllEpg, 180000);   /* refresca la grilla cada 3 min */
})();
</script>
</body>
</html>
"""


# ─── Servidor HTTP ──────────────────────────────────────────────────────
LOGOS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logos")
_LOGO_CT = {
    ".png": "image/png", ".svg": "image/svg+xml", ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif",
}


def _fallback_logo(ch_id):
    """Placa SVG con las iniciales del canal cuando no hay logo en disco."""
    name = CHANNELS.get(ch_id, {}).get("name", ch_id)
    words = [w for w in re.split(r"\s+", name) if w]
    ini = "".join(w[0] for w in words[:3]).upper()
    ini = re.sub(r"[^A-Z0-9]", "", ini)[:3] or ch_id[:2].upper()
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48" '
        'viewBox="0 0 48 48"><rect width="48" height="48" rx="8" '
        'fill="#241d12" stroke="#ffb63b" stroke-width="2"/>'
        '<text x="24" y="25" text-anchor="middle" dominant-baseline="central" '
        'font-family="Arial,Helvetica,sans-serif" font-size="15" '
        f'font-weight="bold" fill="#ffb63b">{ini}</text></svg>'
    )
    return svg.encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype):
        if isinstance(body, str):
            body = body.encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj), "application/json; charset=utf-8")

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/":
            btns = render_channel_buttons()
            html = INDEX_HTML.replace("__CHANNELS_REGULAR__", btns["regular"])
            html = html.replace("__CHANNELS_DEPORTES__", btns["deportes"])
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
                # "fuera del aire" no es un error del servidor: es el estado
                # normal de un canal sin señal (p.ej. eventos sin partido).
                code = 200 if "fuera del aire" in str(e).lower() else 500
                self._json({"ok": False, "error": str(e)}, code)
            return

        if path.startswith("/logos/"):
            self._serve_logo(path[len("/logos/"):])
            return

        if path.startswith("/epg/"):
            self._serve_epg(path[len("/epg/"):])
            return

        if path == "/epg-all":
            self._json({"ok": True, "epg": _epg_all()})
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
        fpath = os.path.join(_out_dir(ch_id), os.path.basename(fname))
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

    def _serve_logo(self, ch_id):
        """Sirve el logo local de un canal (logos/<canal>.png, etc.). Si no
        hay archivo, genera una placa SVG con el nombre para que el botón no
        quede sin imagen."""
        if ch_id not in CHANNELS or "/" in ch_id or ".." in ch_id:
            self._send(404, "no encontrado", "text/plain; charset=utf-8")
            return
        for ext, ctype in _LOGO_CT.items():
            fpath = os.path.join(LOGOS_DIR, ch_id + ext)
            if os.path.exists(fpath):
                break
        else:
            self._send(200, _fallback_logo(ch_id),
                       "image/svg+xml; charset=utf-8")
            return
        try:
            with open(fpath, "rb") as fh:
                data = fh.read()
        except OSError:
            self._send(404, "no encontrado", "text/plain; charset=utf-8")
            return
        self._send(200, data, ctype)


    def _serve_epg(self, ch_id):
        """Programación (ahora/luego) del canal; sin guía → ok:false."""
        if ch_id not in CHANNELS or "/" in ch_id or ".." in ch_id:
            self._json({"ok": False, "error": "canal desconocido"}, 404)
            return
        site_id = EPG_SOURCES.get(ch_id)
        if not site_id:
            self._json({"ok": False, "error": "sin guía"})
            return
        try:
            data = _epg_fetch(site_id)
        except Exception as e:
            self._json({"ok": False, "error": str(e)[:100]})
            return
        self._json({"ok": True, "channel": ch_id,
                    "now": data.get("now"), "next": data.get("next")})


def run_server():
    port = int(os.environ.get("PORT", "8000"))
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print("=" * 50)
    print("  TVD — TV Argentina en vivo")
    print("=" * 50)
    print(f"  Motor de video: {STREAMER.backend}")
    print(f"  Abrí la interfaz en:  http://0.0.0.0:{port}")
    if STREAMER.backend == "nada":
        print("  ⚠ ffmpeg no encontrado: la reproducción no funcionará.")
    print("  Ctrl+C para salir.\n")

    # Render/envían SIGTERM al redesplegar: matar el ffmpeg propio y salir
    # limpio (si no, queda huérfano escribiendo hasta el próximo arranque).
    def _on_term(signum, frame):
        raise SystemExit(0)

    try:
        signal.signal(signal.SIGTERM, _on_term)
    except (ValueError, OSError):
        pass

    # Precalienta en segundo plano: resoluciones de canales de sitio, en
    # paralelo y con concurrencia acotada para no competir con el primer play
    # del usuario.
    def _prewarm():
        site_ch = [(cid, ch["site"]) for cid, ch in CHANNELS.items() if ch.get("site")]

        def _do_site(item):
            try:
                _resolve_site_stream(item[1], cache_fail=False)
            except Exception:
                pass

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
            futs = [ex.submit(_do_site, it) for it in site_ch]
            for f in futs:
                try:
                    f.result()
                except Exception:
                    pass
    threading.Thread(target=_prewarm, daemon=True, name="tvd-prewarm").start()

    try:
        httpd.serve_forever()
    except (KeyboardInterrupt, SystemExit):
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
                for url, _, _ in resolve_stream_list(ch_id):
                    print(f"{CHANNELS[ch_id]['name']}: {url}")
            except Exception as e:
                print(f"{ch_id}:  {e}")
        sys.exit(0)

    run_server()
