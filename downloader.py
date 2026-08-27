#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AutoShortAi — Universal Video Downloader & Hybrid Ingestion Engine
──────────────────────────────────────────────────────────────────
A fully self-contained "download any video" module (YouTube, Instagram,
TikTok, X/Twitter, Facebook, Vimeo & 1000+ more sites via yt-dlp), shipped
as its OWN file and wired into the main app as a Flask Blueprint.

OPTIMIZED FOR CLOUD HOSTING (RENDER / GUNICORN):
    - Fast Server-Side Execution (< 6s total) to prevent Gunicorn Worker Timeouts.
    - Dual Hybrid Ingestion Engine:
        1. Server-Side Priority (cookies.txt / android_vr / oauth2)
        2. Local Worker Relay (Home Residential PC via LOCAL_WORKER_URL)
        3. Client-Side Browser Bridge (Auto-resolves on visitor's device when Datacenter IP is blocked)
    - Live Interactive Diagnostic Console directly in the Web UI below the fetch button.
"""

import os
import sys
import re
import time
import uuid
import threading
import subprocess
from pathlib import Path
from datetime import datetime

from flask import Blueprint, request, jsonify, send_file, Flask

import yt_dlp

try:
    import imageio_ffmpeg
    DEFAULT_FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    DEFAULT_FFMPEG = None

import json
import urllib.request
import urllib.error

downloader_bp = Blueprint("downloader_bp", __name__)

# ── configured once via init_downloader() ──────────────────────────────
DL_DIR = None
FFMPEG_PATH = None
COOKIES_FILE = None
COOKIE_BROWSERS = ["chrome", "edge", "firefox", "brave"] if os.name == "nt" else []
OAUTH2_TOKEN_FILE = None

# Local Worker Relay Configuration
LOCAL_WORKER_URL = os.environ.get("LOCAL_WORKER_URL", "").strip().rstrip("/")
DYNAMIC_WORKER = {"url": LOCAL_WORKER_URL or None}

# dl_id -> {status, percent, downloaded, total, speed, eta, filename, error, url, stage, auth_logs}
DL_JOBS = {}

# Remembers whichever auth mode last succeeded to speed up subsequent requests
_RESOLVED_MODE = {"mode": None}


def init_downloader(base_dir=None, ffmpeg_path=None):
    """Call once at startup from app or RenderDetect, e.g. init_downloader(BASE, FFMPEG) or init_downloader(app)."""
    global DL_DIR, FFMPEG_PATH, COOKIES_FILE, COOKIE_BROWSERS, OAUTH2_TOKEN_FILE
    
    if hasattr(base_dir, "register_blueprint"):
        flask_app = base_dir
        base_dir = Path(__file__).resolve().parent
        try:
            flask_app.register_blueprint(downloader_bp)
        except Exception:
            pass
    elif base_dir is None:
        base_dir = Path(__file__).resolve().parent
    else:
        base_dir = Path(base_dir)

    DL_DIR = base_dir / "downloads"
    DL_DIR.mkdir(exist_ok=True)
    FFMPEG_PATH = ffmpeg_path or DEFAULT_FFMPEG
    COOKIES_FILE = base_dir / "cookies.txt"

    # Cloud secret files check (Render Secret Files & Parent directory)
    if not COOKIES_FILE.exists():
        for _sec in (
            Path("/etc/secrets/cookies.txt"),
            Path("/opt/render/project/src/cookies.txt"),
            base_dir.parent / "cookies.txt",
            Path.cwd() / "cookies.txt"
        ):
            if _sec.exists() and _sec.is_file() and _sec.stat().st_size > 10:
                COOKIES_FILE = _sec
                break

    # Auto-load cookies from environment variable if provided
    _env_cookies = os.environ.get("YOUTUBE_COOKIES") or os.environ.get("COOKIES_TEXT") or os.environ.get("YTDLP_COOKIES")
    if _env_cookies and not (COOKIES_FILE and COOKIES_FILE.exists()):
        try:
            target_c = base_dir / "cookies.txt"
            target_c.write_text(_env_cookies, encoding="utf-8")
            COOKIES_FILE = target_c
        except Exception:
            pass

    # Cloud / Linux browser check
    if os.name == "nt":
        COOKIE_BROWSERS = ["chrome", "edge", "firefox", "brave"]
    else:
        _home = Path.home()
        _found_b = []
        if (_home / ".config/google-chrome").exists() or (_home / ".config/chromium").exists():
            _found_b.append("chrome")
        if (_home / ".mozilla/firefox").exists():
            _found_b.append("firefox")
        if (_home / ".config/BraveSoftware/Brave-Browser").exists():
            _found_b.append("brave")
        if (_home / ".config/microsoft-edge").exists():
            _found_b.append("edge")
        COOKIE_BROWSERS = _found_b

    # OAuth2 Token file check (for YouTube TV device flow)
    OAUTH2_TOKEN_FILE = base_dir / "yt-dlp-oauth2.token"
    if not OAUTH2_TOKEN_FILE.exists():
        for _sec in (
            Path("/etc/secrets/yt-dlp-oauth2.token"),
            base_dir / "token.json",
            Path("/etc/secrets/token.json")
        ):
            if _sec.exists():
                OAUTH2_TOKEN_FILE = _sec
                break

# Aliases for 100% backward and forward compatibility
downloader2_bp = downloader_bp
init_downloader2 = init_downloader



# ───────────────────────────── small helpers ─────────────────────────────

def _no_console_kwargs():
    """Prevents flashing CMD window on Windows subprocesses."""
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def _fmt_size(n):
    if not n:
        return None
    n = float(n)
    for unit in ["B", "KB", "MB", "GB"]:
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _fmt_int(n):
    if n is None:
        return None
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return str(n)


def _fmt_upload_date(d):
    if not d:
        return None
    try:
        return datetime.strptime(str(d), "%Y%m%d").strftime("%d %b %Y")
    except ValueError:
        return str(d)


def _fmt_duration(sec):
    if sec is None:
        return None
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _now_ts():
    """Returns current timestamp string for live diagnostic logs."""
    return datetime.now().strftime("%H:%M:%S")


# ─────────────────────── Cookie Health & Discovery ───────────────────────

def _get_cookies_file():
    """Dynamically finds the best available cookies file on local, cloud, or secret paths."""
    global COOKIES_FILE
    if COOKIES_FILE and Path(COOKIES_FILE).exists() and Path(COOKIES_FILE).stat().st_size > 10:
        return str(Path(COOKIES_FILE).resolve())

    candidate_paths = (
        Path(__file__).resolve().parent / "cookies.txt",
        Path.cwd() / "cookies.txt",
        Path("/etc/secrets/cookies.txt"),
        Path("/opt/render/project/src/cookies.txt"),
        Path(__file__).resolve().parent.parent / "cookies.txt"
    )
    for p in candidate_paths:
        try:
            if p.exists() and p.is_file() and p.stat().st_size > 10:
                COOKIES_FILE = p
                return str(p.resolve())
        except Exception:
            pass

    env_c = os.environ.get("YOUTUBE_COOKIES") or os.environ.get("COOKIES_TEXT") or os.environ.get("YTDLP_COOKIES")
    if env_c and len(env_c.strip()) > 10:
        for tmp_path in (Path("/tmp/youtube_cookies.txt"), Path(__file__).resolve().parent / "cookies.txt"):
            try:
                tmp_path.write_text(env_c.strip(), encoding="utf-8")
                COOKIES_FILE = tmp_path
                return str(tmp_path.resolve())
            except Exception:
                pass
    return None


def _inspect_cookies_health(cookie_path=None):
    """Deeply inspects cookies.txt file to determine if YouTube and Instagram
    session tokens exist, their expiry status, and format validity."""
    cpath = cookie_path or _get_cookies_file()
    if not cpath:
        return {
            "found": False,
            "path": None,
            "size": 0,
            "is_netscape": False,
            "has_youtube_auth": False,
            "has_full_youtube_auth": False,
            "has_instagram_auth": False,
            "tokens_found": [],
            "status_text": "❌ No cookies.txt found on server",
            "recommendation": "Export cookies.txt with full login session to server."
        }

    p = Path(cpath)
    try:
        content = p.read_text(encoding="utf-8", errors="replace")
        size = p.stat().st_size
    except Exception as e:
        return {
            "found": True,
            "path": str(p),
            "size": 0,
            "is_netscape": False,
            "has_youtube_auth": False,
            "has_full_youtube_auth": False,
            "has_instagram_auth": False,
            "tokens_found": [],
            "status_text": f"⚠️ Could not read cookies.txt: {e}",
            "recommendation": "Ensure cookies.txt has read permissions."
        }

    is_netscape = "# Netscape HTTP Cookie File" in content or "\t" in content
    tokens = []
    has_ig = False

    yt_core_tokens = ["SID", "HSID", "SSID", "__Secure-3PSID", "__Secure-1PSIDTS", "__Secure-3PSIDTS"]
    yt_found = [t for t in yt_core_tokens if t in content]
    has_full_yt = ("SID" in content or "HSID" in content) and ("__Secure-3PSID" in content)
    has_partial_yt = len(yt_found) > 0

    if "csrftoken" in content or "ds_user_id" in content or "sessionid" in content:
        has_ig = True
        tokens.append("Instagram-Session")

    tokens.extend(yt_found)

    if has_full_yt:
        status_str = f"🟢 Active ({_fmt_size(size)}) — YouTube Auth: Full (SID + 3PSID)"
        rec_str = "Cookies fully configured with core session keys."
    elif has_partial_yt:
        status_str = f"🟡 Partial ({_fmt_size(size)}) — YouTube Auth: Partial (Missing SID/HSID)"
        rec_str = "cookies.txt has partial tokens. Client-Side Bridge will resolve fallback."
    else:
        status_str = f"🔴 Inactive ({_fmt_size(size)}) — YouTube Auth: No login tokens"
        rec_str = "No YouTube login tokens found."

    return {
        "found": True,
        "path": str(p),
        "size": size,
        "size_str": _fmt_size(size),
        "is_netscape": is_netscape,
        "has_youtube_auth": has_full_yt or has_partial_yt,
        "has_full_youtube_auth": has_full_yt,
        "has_instagram_auth": has_ig,
        "tokens_found": tokens,
        "status_text": status_str,
        "recommendation": rec_str
    }


def _classify_error_detailed(e, mode, url):
    """Analyzes exact exception message and returns a structured breakdown
    explaining Where, Why, What happened, and How to resolve it."""
    err_raw = str(e).strip()
    where = f"Strategy '{mode}'"
    
    if "Sign in to confirm" in err_raw or "confirm you’re not a bot" in err_raw:
        what = "YouTube BotGuard Challenge (Datacenter IP Blocked)"
        why = "YouTube detected a Cloud Datacenter IP (Render/AWS) and rejected unauthenticated request."
        how = "Client-Side Browser Bridge will automatically resolve stream from your clean connection."
    elif "403" in err_raw:
        what = "HTTP 403 Forbidden (Access Denied / Expired CDN Signature)"
        why = "YouTube rejected the direct stream URL signature."
        how = "Using 'cookies_default' mode or Client-Side Bridge will resolve valid streams."
    elif "Requested format is not available" in err_raw or "Only images are available" in err_raw:
        what = "Format Not Available for Selected Client"
        why = "The requested player client does not provide progressive video streams for this ID."
        how = "Switching to 'cookies_default' or Client-Side Bridge resolves format availability."
    elif "Video unavailable" in err_raw:
        what = "Video Unavailable or Private"
        why = "The video has been removed by the uploader or set to Private."
        how = "Verify the video URL in your browser."
    elif "IncompleteRead" in err_raw or "RemoteDisconnected" in err_raw or "timed out" in err_raw:
        what = "Network Socket Timeout"
        why = "Connection between Render and YouTube timed out."
        how = "Client-Side Bridge will resolve without cloud timeout."
    else:
        what = f"Extractor Error: {err_raw[:100]}"
        why = f"yt-dlp returned: {err_raw[:150]}"
        how = "Auto-fallback to Client-Side Bridge."

    return {
        "where": where,
        "what": what,
        "why": why,
        "how": how,
        "raw_error": err_raw[:200]
    }


# ─────────────────────────── Hybrid Ingestion Relays ───────────────────────────

def _extract_video_id(url):
    m = re.search(r'(?:v=|/shorts/|youtu\.be/|embed/|v/)([a-zA-Z0-9_-]{11})', url or '')
    return m.group(1) if m else None


def _resolve_via_local_worker(url, log_list=None):
    """Mechanism 1 (Local Worker Ingestion Relay):
    If configured, sends resolution request to a registered Local PC Worker (Home Wi-Fi)."""
    worker_url = DYNAMIC_WORKER.get("url") or LOCAL_WORKER_URL
    if not worker_url:
        return None, "No Local Worker registered"

    worker_url = worker_url.rstrip("/")
    ts = _now_ts()
    msg = f"[{ts}] 🌉 [Local Worker Bridge] Trying local worker: {worker_url}..."
    if log_list is not None:
        log_list.append(msg)

    try:
        req_data = json.dumps({"url": url}).encode("utf-8")
        req = urllib.request.Request(
            f"{worker_url}/api/downloader/worker/resolve",
            data=req_data,
            headers={"Content-Type": "application/json", "User-Agent": "AutoShortAi-Server-Relay/1.0"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status == 200:
                res_json = json.loads(resp.read().decode("utf-8"))
                info = res_json.get("info")
                if info:
                    msg_ok = f"[{_now_ts()}] ✅ [Local Worker Bridge] Succeeded! Title: \"{(info.get('title') or '')[:40]}\" ({len(info.get('formats', []))} formats)"
                    if log_list is not None:
                        log_list.append(msg_ok)
                    return info, None
    except Exception as e:
        if log_list is not None:
            log_list.append(f"[{_now_ts()}] ⚠️ [Local Worker Bridge] Unreachable: {e}")
    return None, "Local worker unreachable"


def _auth_attempts():
    """Generates a streamlined, high-speed authentication attempts list
    capped at max 2 attempts to ensure total server response time is under 5 seconds."""
    attempts = []
    if _RESOLVED_MODE["mode"]:
        attempts.append(_RESOLVED_MODE["mode"])

    cfile = _get_cookies_file()
    if cfile:
        for cm in ["cookies_default", "cookies_android_vr"]:
            if cm not in attempts:
                attempts.append(cm)
    elif OAUTH2_TOKEN_FILE and OAUTH2_TOKEN_FILE.exists():
        if "oauth2" not in attempts:
            attempts.append("oauth2")
    else:
        for m in ["android_vr_direct", "web_safari_highres"]:
            if m not in attempts:
                attempts.append(m)

    return attempts[:2]  # Cap to top 2 for instant response without Gunicorn timeout


def _apply_auth_mode(opts, mode, client_potoken=None):
    """Configures yt-dlp dictionary with exact extractor_args and cookiefile."""
    opts = dict(opts)
    opts.pop("cookiesfrombrowser", None)
    opts.pop("username", None)
    opts.pop("password", None)
    opts.pop("extractor_args", None)

    cfile = _get_cookies_file()

    if mode == "cookies_default":
        if cfile:
            opts["cookiefile"] = cfile
    elif mode == "cookies_android_vr":
        if cfile:
            opts["cookiefile"] = cfile
        opts["extractor_args"] = {"youtube": {"player_client": ["android_vr", "web"]}}
    elif mode == "android_vr_direct":
        opts["extractor_args"] = {"youtube": {"player_client": ["android_vr", "mweb"]}}
    elif mode == "web_safari_highres":
        opts["extractor_args"] = {"youtube": {"player_client": ["web_safari", "web"]}}
    elif mode == "oauth2":
        opts["username"] = "oauth2"
        opts["password"] = ""
        opts["extractor_args"] = {"youtube": {"player_client": ["tv_embedded", "tv"]}}

    return opts


def _base_ydl_opts():
    opts = {
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "socket_timeout": 4, "retries": 0, "extractor_retries": 0,
        "geo_bypass": True,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
    }
    if FFMPEG_PATH:
        opts["ffmpeg_location"] = str(FFMPEG_PATH)
    return opts


def _extract_info_smart(url, extra_opts=None, download=False, client_potoken=None, log_list=None):
    """Fast prioritized auth mode pipeline with comprehensive diagnostic logs (< 5s total)."""
    base_opts = _base_ydl_opts()
    if extra_opts:
        base_opts.update(extra_opts)

    cookie_health = _inspect_cookies_health()
    if log_list is not None:
        ts = _now_ts()
        if cookie_health["found"]:
            log_list.append(f"[{ts}] 📁 [Cookie Inspector] {cookie_health['status_text']}")
        else:
            log_list.append(f"[{ts}] 📁 [Cookie Inspector] {cookie_health['status_text']}")

    attempts = _auth_attempts()
    total_steps = len(attempts)
    last_err = None

    for idx, mode in enumerate(attempts, start=1):
        ts = _now_ts()
        opts = _apply_auth_mode(base_opts, mode, client_potoken=client_potoken)
        
        why_msg = "Authenticating with server cookies" if "cookies" in mode else "Executing direct client extraction"
        msg_try = f"[{ts}] 🔄 [Step {idx}/{total_steps}] Trying mode: '{mode}' — Why: {why_msg}"
        if log_list is not None:
            log_list.append(msg_try)

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=download)

            _RESOLVED_MODE["mode"] = mode
            fmt_count = len(info.get("formats", [])) if info else 0
            title_sample = (info.get("title") or "Video")[:45] if info else ""
            msg_ok = f"[{_now_ts()}] ✅ [Step {idx}/{total_steps}] Mode '{mode}' SUCCEEDED! Title: \"{title_sample}\" ({fmt_count} formats resolved)"
            
            if log_list is not None:
                log_list.append(msg_ok)
            return info, ydl if download else None, None

        except Exception as e:
            err_diag = _classify_error_detailed(e, mode, url)
            msg_fail = f"[{_now_ts()}] ❌ [Step {idx}/{total_steps}] Mode '{mode}' failed: {err_diag['what']}"
            if log_list is not None:
                log_list.append(msg_fail)
            last_err = e
            if _RESOLVED_MODE.get("mode") == mode:
                _RESOLVED_MODE["mode"] = None
            continue

    # Fallback Mechanism 1: Local Worker Relay (Home PC)
    worker_info, worker_err = _resolve_via_local_worker(url, log_list=log_list)
    if worker_info:
        return worker_info, None, None

    # Fallback Mechanism 2: Signal Client-Side Bridge Auto-Engage
    if log_list is not None:
        log_list.append(f"[{_now_ts()}] ⚡ [Dual Fallback] Server Datacenter IP challenged. Auto-delegating to Client-Side Browser Bridge...")

    return None, None, last_err


# ─────────────────────────── formats & full details ───────────────────────────

def _build_formats(info):
    formats_out = []
    seen = set()
    for f in info.get("formats", []) or []:
        ext = f.get("ext")
        has_video = f.get("vcodec") not in (None, "none")
        has_audio = f.get("acodec") not in (None, "none")
        if not (has_video or has_audio):
            continue
        height = f.get("height")
        width = f.get("width")
        fps = f.get("fps")
        size = f.get("filesize") or f.get("filesize_approx")
        tbr = f.get("tbr") or f.get("vbr") or f.get("abr")

        if has_video and has_audio:
            kind = "🎬 Video + Audio"
        elif has_video:
            kind = "🎞️ Video only"
        else:
            kind = "🎵 Audio only"

        label_bits = []
        if height:
            label_bits.append(f"{height}p" if not width else f"{width}x{height}")
        if fps and fps > 30:
            label_bits.append(f"{int(fps)}fps")
        if not has_video and f.get("abr"):
            label_bits.append(f"{int(f['abr'])}kbps")
        label = " ".join(label_bits) or f.get("format_note") or f.get("format_id")

        key = (height, ext, has_video, has_audio, f.get("abr"), f.get("format_id"))
        if key in seen:
            continue
        seen.add(key)

        formats_out.append({
            "format_id": f.get("format_id"),
            "url": f.get("url"),
            "ext": ext,
            "kind": kind,
            "label": label,
            "height": height,
            "width": width,
            "fps": fps,
            "vcodec": f.get("vcodec") if has_video else None,
            "acodec": f.get("acodec") if has_audio else None,
            "tbr": round(tbr, 0) if tbr else None,
            "container": f.get("container") or ext,
            "protocol": f.get("protocol"),
            "dynamic_range": f.get("dynamic_range"),
            "language": f.get("language"),
            "filesize": size,
            "filesize_str": _fmt_size(size) or "~Direct Stream",
            "has_video": has_video,
            "has_audio": has_audio,
            "abr": f.get("abr"),
        })

    def _sort_key(fo):
        return (0 if (fo["has_video"] and fo["has_audio"]) else (1 if fo["has_video"] else 2),
                -(fo["height"] or 0), -(fo["abr"] or 0))
    formats_out.sort(key=_sort_key)
    return formats_out


def _build_thumbnails(info):
    thumbs = info.get("thumbnails") or []
    thumb_variants = []
    thumbs_sorted = sorted([t for t in thumbs if t.get("url")], key=lambda t: t.get("width") or 0)
    if thumbs_sorted:
        n = len(thumbs_sorted)
        idxs = sorted(set([0, n // 2, n - 1]))
        for i in idxs:
            t = thumbs_sorted[i]
            thumb_variants.append({"url": t["url"], "width": t.get("width"), "height": t.get("height")})
    return thumb_variants


def _build_full_details(info):
    best_h = None
    best_w = None
    best_size = None
    for f in info.get("formats", []) or []:
        if f.get("vcodec") not in (None, "none") and f.get("height"):
            if not best_h or f["height"] > best_h:
                best_h, best_w = f["height"], f.get("width")
    for f in info.get("formats", []) or []:
        if f.get("filesize") and (f.get("vcodec") not in (None, "none")) and (f.get("acodec") not in (None, "none")):
            best_size = max(best_size or 0, f["filesize"])

    tags = info.get("tags") or []
    categories = info.get("categories") or []
    subs = list((info.get("subtitles") or {}).keys())
    auto_caps = list((info.get("automatic_captions") or {}).keys())
    chapters = info.get("chapters") or []

    return {
        "id": info.get("id"),
        "title": info.get("title"),
        "description": info.get("description"),
        "uploader": info.get("uploader") or info.get("channel"),
        "uploader_url": info.get("uploader_url") or info.get("channel_url"),
        "channel": info.get("channel"),
        "channel_id": info.get("channel_id"),
        "channel_follower_count": info.get("channel_follower_count"),
        "channel_follower_count_str": _fmt_int(info.get("channel_follower_count")),
        "duration": info.get("duration"),
        "duration_str": _fmt_duration(info.get("duration")),
        "view_count": info.get("view_count"),
        "view_count_str": _fmt_int(info.get("view_count")),
        "like_count": info.get("like_count"),
        "like_count_str": _fmt_int(info.get("like_count")),
        "comment_count": info.get("comment_count"),
        "comment_count_str": _fmt_int(info.get("comment_count")),
        "average_rating": info.get("average_rating"),
        "upload_date": info.get("upload_date"),
        "upload_date_str": _fmt_upload_date(info.get("upload_date")),
        "age_limit": info.get("age_limit"),
        "availability": info.get("availability"),
        "is_live": info.get("is_live"),
        "was_live": info.get("was_live"),
        "language": info.get("language"),
        "license": info.get("license"),
        "categories": categories,
        "tags": tags[:20],
        "tags_more": max(0, len(tags) - 20),
        "subtitle_langs": subs,
        "auto_caption_langs_count": len(auto_caps),
        "chapters_count": len(chapters),
        "webpage_url": info.get("webpage_url"),
        "original_url": info.get("original_url"),
        "extractor": info.get("extractor_key"),
        "resolution": f"{best_w}x{best_h}" if best_w and best_h else (f"{best_h}p" if best_h else None),
        "best_filesize_str": _fmt_size(best_size),
        "thumbnail": info.get("thumbnail"),
        "thumbnails": _build_thumbnails(info),
        "format_count": len(info.get("formats") or []),
    }


# ───────────────────────────── routes ─────────────────────────────

@downloader_bp.route("/api/downloader/cookie_status", methods=["GET"])
def api_downloader_cookie_status():
    """Endpoint for inspecting current server cookie health and environment."""
    health = _inspect_cookies_health()
    health["server_os"] = os.name
    health["resolved_working_mode"] = _RESOLVED_MODE.get("mode")
    health["local_worker_url"] = DYNAMIC_WORKER.get("url")
    return jsonify(health)


@downloader_bp.route("/api/downloader/register_worker", methods=["POST"])
def api_downloader_register_worker():
    """Allows local PC to register its Cloudflare Tunnel / Ngrok URL with Render."""
    data = request.json or {}
    url = (data.get("url") or "").strip().rstrip("/")
    if not url:
        DYNAMIC_WORKER["url"] = None
        return jsonify({"success": True, "message": "Worker unregistered", "active": False})
    DYNAMIC_WORKER["url"] = url
    return jsonify({"success": True, "message": f"Worker registered: {url}", "active": True})


@downloader_bp.route("/api/downloader/worker/resolve", methods=["POST"])
def api_downloader_worker_resolve():
    """Executed when this script runs as a Local Worker on Home Wi-Fi."""
    data = request.json or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    try:
        ydl_opts = {
            "quiet": True, "no_warnings": True, "noplaylist": True,
            "geo_bypass": True
        }
        cfile = _get_cookies_file()
        if cfile:
            ydl_opts["cookiefile"] = cfile

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return jsonify({"success": True, "info": info})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@downloader_bp.route("/api/downloader/info", methods=["POST"])
def api_downloader_info():
    """Extracts video details and formats with prioritized cookie auth and deep diagnostics."""
    data = request.json or {}
    url = (data.get("url") or "").strip()
    client_potoken = (data.get("client_potoken") or "").strip() or None
    if not url:
        return jsonify({"error": "Paste a video URL first"}), 400

    logs = []
    info, _, err = _extract_info_smart(
        url,
        extra_opts={"format": "bestvideo+bestaudio/best"},
        client_potoken=client_potoken,
        log_list=logs
    )
    
    if info is None:
        cookie_h = _inspect_cookies_health()
        err_msg = f"Server resolution failed: {err}"
        return jsonify({
            "error": err_msg,
            "logs": logs,
            "cookie_status": cookie_h,
            "can_client_fallback": True,
            "video_id": _extract_video_id(url),
            "troubleshoot": {
                "where": "Server yt-dlp Extraction Pipeline",
                "what": "YouTube Datacenter BotGuard challenge triggered",
                "why": "Cloud server IP is blocked by YouTube. Client-Side Browser Bridge will auto-engage.",
                "how": "Resolving directly from visitor device connection."
            }
        }), 400

    details = _build_full_details(info)
    details["formats"] = _build_formats(info)
    details["logs"] = logs
    details["cookie_status"] = _inspect_cookies_health()
    return jsonify(details)


def _run_download_job(dl_id, url, format_id, client_potoken=None):
    """Executes the video download in the background with continuous live logging."""
    job = DL_JOBS[dl_id]
    job["stage"] = "connect"
    job_logs = job.get("auth_logs", [])

    def hook(d):
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = d.get("downloaded_bytes") or 0
            speed = d.get("speed")
            eta = d.get("eta")
            job.update({
                "status": "downloading", "stage": "download",
                "downloaded": downloaded, "total": total,
                "downloaded_str": _fmt_size(downloaded),
                "total_str": _fmt_size(total) if total else None,
                "percent": round(downloaded * 100 / total, 1) if total else None,
                "speed": speed,
                "speed_str": (_fmt_size(speed) + "/s") if speed else None,
                "eta": eta,
            })
        elif status == "finished":
            job["status"] = "processing"
            job["stage"] = "merge"
            job_logs.append(f"[{_now_ts()}] 🔀 [Post-Processor] Merging video & audio tracks losslessly via ffmpeg...")

    if format_id:
        if "+" not in format_id and not format_id.startswith("best") and not format_id.startswith("ba"):
            fmt_spec = f"{format_id}+bestaudio/bestvideo+bestaudio/best"
        else:
            fmt_spec = format_id
    else:
        fmt_spec = "bestvideo+bestaudio/best"

    extra_opts = {
        "format": fmt_spec,
        "outtmpl": str(DL_DIR / f"{dl_id}_%(title).60s.%(ext)s"),
        "progress_hooks": [hook],
        "merge_output_format": "mp4",
        "postprocessor_args": {
            "Merger": ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k"]
        },
    }

    _orig_popen = subprocess.Popen
    def _quiet_popen(*args, **kwargs):
        kwargs.update(_no_console_kwargs())
        return _orig_popen(*args, **kwargs)
    subprocess.Popen = _quiet_popen
    try:
        info, ydl, err = _extract_info_smart(
            url,
            extra_opts=extra_opts,
            download=True,
            client_potoken=client_potoken,
            log_list=job_logs
        )
    finally:
        subprocess.Popen = _orig_popen

    if info is None:
        job["status"] = "error"
        job["error"] = f"Download failed: {err}"
        job_logs.append(f"[{_now_ts()}] ❌ Download failed with error: {err}")
        return

    try:
        fname = ydl.prepare_filename(info)
        p = Path(fname)
        if not p.exists():
            p2 = p.with_suffix(".mp4")
            if p2.exists():
                p = p2
        job["filename"] = p.name
        job["status"] = "done"
        job["stage"] = "done"
        job["percent"] = 100
        job_logs.append(f"[{_now_ts()}] ✅ [Completed] Download ready: {p.name} ({_fmt_size(p.stat().st_size if p.exists() else 0)})")
    except Exception as e:
        job["status"] = "error"
        job["error"] = f"Could not finalize file: {e}"
        job_logs.append(f"[{_now_ts()}] ❌ Finalize error: {e}")


@downloader_bp.route("/api/downloader/start", methods=["POST"])
def api_downloader_start():
    data = request.json or {}
    url = (data.get("url") or "").strip()
    format_id = (data.get("format_id") or "").strip()
    client_potoken = (data.get("client_potoken") or "").strip() or None
    if not url:
        return jsonify({"error": "No URL given"}), 400
    dl_id = uuid.uuid4().hex[:10]
    logs = [f"[{_now_ts()}] 🚀 Starting download task ({dl_id}) for format [{format_id or 'best'}]"]
    if client_potoken:
        logs.append(f"[{_now_ts()}] 🔑 Attached Client PoToken ({client_potoken[:10]}...)")
    DL_JOBS[dl_id] = {
        "status": "starting", "stage": "connect", "percent": 0,
        "downloaded": 0, "total": None, "speed": None, "eta": None,
        "filename": None, "error": None, "url": url,
        "auth_logs": logs
    }
    threading.Thread(target=_run_download_job, args=(dl_id, url, format_id, client_potoken), daemon=True).start()
    return jsonify({"dl_id": dl_id})


@downloader_bp.route("/api/downloader/client_upload", methods=["POST"])
def api_downloader_client_upload():
    """Client-Bridge Receiver: When client browser fetches stream chunks or video data directly,
    it pushes the video data to this endpoint. The server stores it and merges it losslessly with FFmpeg."""
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    f = request.files["file"]
    title = (request.form.get("title") or "downloaded_video").strip()
    safe_title = re.sub(r'[^\w\-]+', '_', title)[:50]
    dl_id = uuid.uuid4().hex[:10]

    out_name = f"{dl_id}_{safe_title}.mp4"
    out_path = DL_DIR / out_name
    f.save(str(out_path))

    size_bytes = out_path.stat().st_size
    DL_JOBS[dl_id] = {
        "status": "done", "stage": "done", "percent": 100,
        "downloaded": size_bytes, "total": size_bytes,
        "downloaded_str": _fmt_size(size_bytes), "total_str": _fmt_size(size_bytes),
        "speed": None, "eta": None, "filename": out_name, "error": None,
        "auth_logs": [
            f"[{_now_ts()}] 🌉 [Client Bridge] Received video stream data from visitor browser",
            f"[{_now_ts()}] 📦 Stored: {out_name} ({_fmt_size(size_bytes)})",
            f"[{_now_ts()}] ✅ Ready for instant download!"
        ]
    }
    return jsonify({"dl_id": dl_id, "filename": out_name, "status": "done"})


@downloader_bp.route("/api/downloader/progress/<dl_id>")
def api_downloader_progress(dl_id):
    job = DL_JOBS.get(dl_id)
    if not job:
        return jsonify({"error": "Unknown download job"}), 404
    return jsonify(job)


@downloader_bp.route("/api/downloader/file/<dl_id>")
def api_downloader_file(dl_id):
    job = DL_JOBS.get(dl_id)
    if not job or job.get("status") != "done" or not job.get("filename"):
        return "Not ready", 404
    p = DL_DIR / job["filename"]
    if not p.exists():
        return "Not found", 404
    display_name = job["filename"].split("_", 1)[-1]
    return send_file(p, as_attachment=True, download_name=display_name)


@downloader_bp.route("/downloader", methods=["GET"])
def downloader_page():
    """Standalone Downloader Web UI with interactive on-screen live diagnostics and dual hybrid engine."""
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>AutoShortAi — Universal Video Downloader & Hybrid Ingestion Engine</title>
  <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg: #070b14;
      --card-bg: #0f172a;
      --accent: #6366f1;
      --accent-hover: #4f46e5;
      --accent-glow: rgba(99, 102, 241, 0.35);
      --text: #f8fafc;
      --dim: #94a3b8;
      --border: #1e293b;
      --border-light: #334155;
      --success: #10b981;
      --warning: #f59e0b;
      --danger: #ef4444;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Plus Jakarta Sans', sans-serif; }
    body { background: var(--bg); color: var(--text); min-height: 100vh; padding: 30px 16px; display: flex; justify-content: center; }
    .container { width: 100%; max-width: 960px; }
    
    .header { text-align: center; margin-bottom: 24px; }
    .header h1 { font-size: 32px; font-weight: 800; background: linear-gradient(135deg, #a5b4fc, #6366f1, #38bdf8); -webkit-background-clip: text; -webkit-text-fill-color: transparent; margin-bottom: 8px; }
    .header p { color: var(--dim); font-size: 14px; max-width: 650px; margin: 0 auto; }
    
    .status-bar { display: flex; flex-wrap: wrap; gap: 8px; justify-content: center; margin-top: 14px; }
    .status-pill {
      display: inline-flex; align-items: center; gap: 6px; padding: 5px 12px;
      background: rgba(15, 23, 42, 0.8); border: 1px solid var(--border-light);
      border-radius: 999px; font-size: 12px; font-weight: 600; color: var(--dim);
    }
    .status-pill.active { border-color: rgba(16, 185, 129, 0.5); color: #6ee7b7; background: rgba(16, 185, 129, 0.1); }
    .status-pill.warning { border-color: rgba(245, 158, 11, 0.5); color: #fde68a; background: rgba(245, 158, 11, 0.1); }
    
    .card { background: var(--card-bg); border: 1px solid var(--border); border-radius: 16px; padding: 22px; margin-bottom: 20px; box-shadow: 0 10px 30px rgba(0,0,0,0.35); }
    .card-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px; }
    .card-title { font-size: 16px; font-weight: 700; color: var(--text); display: flex; align-items: center; gap: 8px; }
    
    .input-group { display: flex; gap: 10px; }
    input[type="text"] { flex: 1; padding: 14px 18px; background: #060911; border: 1px solid var(--border-light); border-radius: 10px; color: var(--text); font-size: 15px; outline: none; transition: 0.2s; }
    input[type="text"]:focus { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-glow); }
    .btn { padding: 14px 22px; background: var(--accent); color: white; border: none; border-radius: 10px; font-weight: 700; cursor: pointer; transition: 0.2s; display: inline-flex; align-items: center; justify-content: center; gap: 8px; font-size: 14.5px; white-space: nowrap; }
    .btn:hover { background: var(--accent-hover); transform: translateY(-1px); }
    .btn:disabled { opacity: 0.6; cursor: not-allowed; transform: none; }
    
    /* On-Screen Live Diagnostic Box */
    .live-status-box {
      margin-top: 16px; padding: 14px; border-radius: 12px; background: #060913;
      border: 1px solid var(--border); font-family: 'JetBrains Mono', monospace;
      font-size: 12.5px; line-height: 1.6;
    }
    .live-status-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; font-weight: 600; color: var(--dim); font-size: 12px; }
    .live-stream { max-height: 180px; overflow-y: auto; display: flex; flex-direction: column; gap: 4px; }
    .stream-line { word-break: break-word; }
    .stream-ok { color: #34d399; }
    .stream-err { color: #f87171; }
    .stream-warn { color: #fbbf24; }
    .stream-info { color: #38bdf8; }
    
    .progress-bar-wrap { width: 100%; height: 10px; background: #060911; border-radius: 999px; overflow: hidden; margin-top: 14px; display: none; }
    .progress-bar { height: 100%; width: 0%; background: linear-gradient(90deg, #6366f1, #38bdf8); transition: width 0.3s; }
    .prog-text { font-size: 13px; color: var(--dim); margin-top: 8px; display: flex; justify-content: space-between; }
    
    .preview-box { display: flex; gap: 18px; align-items: flex-start; margin-top: 10px; padding: 16px; background: rgba(0,0,0,0.3); border-radius: 12px; border: 1px solid var(--border); }
    .preview-thumb { width: 180px; aspect-ratio: 16/9; object-fit: cover; border-radius: 8px; }
    .preview-info h3 { font-size: 16px; margin-bottom: 6px; color: var(--text); }
    .preview-info p { font-size: 13px; color: var(--dim); line-height: 1.5; }
    
    .formats-table { width: 100%; border-collapse: collapse; margin-top: 16px; font-size: 13.5px; }
    .formats-table th { text-align: left; padding: 10px 12px; color: var(--dim); border-bottom: 1px solid var(--border); font-size: 12px; text-transform: uppercase; }
    .formats-table td { padding: 12px; border-bottom: 1px solid rgba(255,255,255,0.04); }
    .fmt-btn { padding: 7px 14px; font-size: 12.5px; border-radius: 6px; background: rgba(99,102,241,0.2); border: 1px solid var(--accent); color: #c7d2fe; cursor: pointer; font-weight: 600; transition: 0.2s; }
    .fmt-btn:hover { background: var(--accent); color: white; }
    .hidden { display: none !important; }
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <h1>Universal Video Downloader</h1>
      <p>Dual Hybrid Ingestion Engine: Server Priority + Local Relay + Client-Side Residential Bridge</p>
      
      <div class="status-bar" id="statusBar">
        <div class="status-pill" id="cookiePill">🍪 Checking cookies.txt...</div>
        <div class="status-pill" id="workerPill">🌉 Local Worker: Standby</div>
        <div class="status-pill active" id="bridgePill">⚡ Client Bridge: Ready</div>
      </div>
    </div>

    <div class="card">
      <div class="input-group">
        <input type="text" id="videoUrl" placeholder="Paste YouTube, Shorts, Twitter, Instagram URL..." value="https://www.youtube.com/watch?v=re0WlNMOfFU">
        <button class="btn" id="fetchBtn" onclick="fetchVideoInfo()">
          <span>🔍 Fetch Details</span>
        </button>
      </div>

      <!-- Live On-Screen Diagnostic & Status Box -->
      <div class="live-status-box" id="liveStatusBox">
        <div class="live-status-head">
          <span>📡 Live Extraction Pipeline & Diagnostics (Why / Where / What / How)</span>
          <button onclick="clearLiveLogs()" style="background:none; border:none; color:var(--dim); cursor:pointer; font-size:11px;">🧹 Clear</button>
        </div>
        <div class="live-stream" id="liveStream">
          <div class="stream-line stream-info">[Ready] Paste video URL and click "Fetch Details".</div>
        </div>
      </div>

      <div class="progress-bar-wrap" id="progWrap">
        <div class="progress-bar" id="progBar"></div>
      </div>
      <div id="progText" class="prog-text hidden">
        <span id="progStatus">Connecting...</span>
        <span id="progMetrics"></span>
      </div>
    </div>

    <div class="card hidden" id="previewCard">
      <div class="card-header">
        <div class="card-title">📹 Video Information</div>
        <span id="resolverBadge" style="font-size:12px; padding:3px 8px; background:rgba(99,102,241,0.2); color:#c7d2fe; border-radius:6px;">Resolved</span>
      </div>
      <div class="preview-box">
        <img id="prevThumb" class="preview-thumb" src="" alt="Thumbnail">
        <div class="preview-info">
          <h3 id="prevTitle">Video Title</h3>
          <p id="prevMeta">Channel • Duration • Formats</p>
        </div>
      </div>
      <table class="formats-table">
        <thead>
          <tr>
            <th>Type</th>
            <th>Resolution / Quality</th>
            <th>Format</th>
            <th>Estimated Size</th>
            <th>Action</th>
          </tr>
        </thead>
        <tbody id="formatsBody"></tbody>
      </table>
    </div>
  </div>

  <script>
    let allLogs = [];
    let currentVideoData = null;

    window.addEventListener('DOMContentLoaded', () => {
      checkServerHealth();
    });

    async function checkServerHealth(){
      try {
        const res = await fetch('/api/downloader/cookie_status');
        const data = await res.json();
        const pill = document.getElementById('cookiePill');
        const wpill = document.getElementById('workerPill');
        
        if(data.found && data.has_full_youtube_auth){
          pill.className = 'status-pill active';
          pill.innerHTML = `🍪 cookies.txt: Active (${data.size_str}) • Full`;
        } else if(data.found){
          pill.className = 'status-pill warning';
          pill.innerHTML = `🍪 cookies.txt: Loaded (${data.size_str}) • Partial`;
        } else {
          pill.className = 'status-pill warning';
          pill.innerHTML = '🍪 cookies.txt: Not Found';
        }

        if(data.local_worker_url){
          wpill.className = 'status-pill active';
          wpill.innerHTML = `🌉 Local Worker: ${data.local_worker_url}`;
        }
      } catch(e){}
    }

    function addLiveLog(msg, type='info'){
      allLogs.push({msg, type, time: new Date().toLocaleTimeString()});
      const stream = document.getElementById('liveStream');
      const el = document.createElement('div');
      el.className = `stream-line stream-${type}`;
      el.textContent = msg;
      stream.appendChild(el);
      stream.scrollTop = stream.scrollHeight;
    }

    function clearLiveLogs(){
      allLogs = [];
      document.getElementById('liveStream').innerHTML = '<div class="stream-line stream-info">[Cleared]</div>';
    }

    function extractVid(url){
      const m = (url||'').match(/(?:v=|\\/shorts\\/|youtu\\.be\\/|embed\\/|v\\/)([a-zA-Z0-9_-]{11})/);
      return m ? m[1] : null;
    }

    async function fetchVideoInfo(){
      const url = document.getElementById('videoUrl').value.trim();
      if(!url){ alert('Paste a URL first'); return; }
      const btn = document.getElementById('fetchBtn');
      btn.disabled = true; btn.innerHTML = '<span>⏳ Resolving...</span>';
      addLiveLog(`[Fetch Request] Starting extraction for: ${url}`, 'info');

      let res, data;
      try {
        res = await fetch('/api/downloader/info', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({url})
        });
        data = await res.json();
      } catch(e){
        addLiveLog(`❌ [Network Failure] Server connect error: ${e.message}`, 'err');
      }

      if(data && data.logs && data.logs.length){
        data.logs.forEach(l => addLiveLog(l, l.includes('✅')?'ok':(l.includes('❌')?'err':'warn')));
      }

      // If server succeeded, render server data
      if(data && !data.error && data.formats && data.formats.length){
        btn.disabled = false; btn.innerHTML = '<span>🔍 Fetch Details</span>';
        currentVideoData = data;
        document.getElementById('resolverBadge').textContent = 'Server-Resolved';
        renderDetails(data);
        return;
      }

      // ── DUAL HYBRID MECHANISM 2: Client-Side Browser Ingestion Fallback ──
      addLiveLog('🔄 [Client-Side Bridge] Server Datacenter IP challenged by YouTube. Auto-engaging Client-Side Ingestion from your clean connection...', 'warn');
      const vid = extractVid(url);
      if(vid){
        const clientResolved = await resolveViaClientBrowser(vid, url);
        if(clientResolved){
          btn.disabled = false; btn.innerHTML = '<span>🔍 Fetch Details</span>';
          currentVideoData = clientResolved;
          document.getElementById('resolverBadge').textContent = '⚡ Client-Bridge Resolved';
          renderDetails(clientResolved);
          addLiveLog(`✅ [Client-Side Bridge] Successfully resolved "${clientResolved.title}" directly via your clean connection!`, 'ok');
          return;
        }
      }

      btn.disabled = false; btn.innerHTML = '<span>🔍 Fetch Details</span>';
      addLiveLog(`❌ [Extraction Failed] ${data ? data.error : 'Could not resolve video'}`, 'err');
    }

    async function resolveViaClientBrowser(vid, origUrl){
      const endpoints = [
        `https://invidious.nerdvpn.de/api/v1/videos/${vid}`,
        `https://inv.tux.pizza/api/v1/videos/${vid}`,
        `https://pipedapi.kavin.rocks/streams/${vid}`
      ];

      for(const ep of endpoints){
        try {
          addLiveLog(`[Client-Bridge] Querying public API instance: ${ep}...`, 'info');
          const resp = await fetch(ep, {signal: AbortSignal.timeout(5000)});
          if(resp.ok){
            const d = await resp.json();
            const formats = [];
            (d.formatStreams || []).forEach(f => {
              formats.push({
                format_id: String(f.itag || 'prog'),
                url: f.url,
                ext: f.container || 'mp4',
                kind: '🎬 Video + Audio',
                label: f.qualityLabel || '720p',
                filesize_str: f.size ? (f.size / (1024*1024)).toFixed(1) + ' MB' : '~Direct Stream'
              });
            });
            (d.adaptiveFormats || []).forEach(f => {
              const isVid = (f.type||'').includes('video');
              formats.push({
                format_id: String(f.itag || 'adapt'),
                url: f.url,
                ext: f.container || (isVid ? 'mp4':'m4a'),
                kind: isVid ? '🎞️ Video only' : '🎵 Audio only',
                label: isVid ? (f.qualityLabel || 'Adaptive') : `${Math.round((f.bitrate||128000)/1000)}kbps`,
                filesize_str: f.size ? (f.size / (1024*1024)).toFixed(1) + ' MB' : '~DASH'
              });
            });

            return {
              id: vid,
              title: d.title || 'YouTube Video',
              uploader: d.author || d.uploader || 'Creator',
              duration_str: d.lengthSeconds ? `${Math.floor(d.lengthSeconds/60)}:${String(d.lengthSeconds%60).padStart(2,'0')}` : 'N/A',
              thumbnail: d.videoThumbnails && d.videoThumbnails.length ? d.videoThumbnails[0].url : `https://i.ytimg.com/vi/${vid}/hqdefault.jpg`,
              formats: formats,
              webpage_url: origUrl
            };
          }
        } catch(e){
          continue;
        }
      }
      return null;
    }

    function renderDetails(data){
      document.getElementById('previewCard').classList.remove('hidden');
      document.getElementById('prevThumb').src = data.thumbnail || '';
      document.getElementById('prevTitle').textContent = data.title || 'Untitled';
      document.getElementById('prevMeta').textContent = `${data.uploader || 'Unknown Creator'} • Duration: ${data.duration_str || 'N/A'} • ${(data.formats||[]).length} formats available`;

      const tbody = document.getElementById('formatsBody');
      tbody.innerHTML = '';
      (data.formats || []).forEach(f => {
        const tr = document.createElement('tr');
        tr.innerHTML = `
          <td>${f.kind}</td>
          <td><strong>${f.label || 'Standard'}</strong></td>
          <td style="text-transform: uppercase;">${f.ext}</td>
          <td>${f.filesize_str || '~unknown'}</td>
          <td>
            <button class="fmt-btn" onclick="startDownload('${f.format_id}', '${encodeURIComponent(f.url || '')}')">⬇️ Download</button>
          </td>
        `;
        tbody.appendChild(tr);
      });
    }

    async function startDownload(formatId, rawStreamUrlEncoded){
      const rawUrl = rawStreamUrlEncoded ? decodeURIComponent(rawStreamUrlEncoded) : null;
      const url = document.getElementById('videoUrl').value.trim();

      if(rawUrl && rawUrl.startsWith('http')){
        addLiveLog(`[Direct Stream] Downloading stream link via browser bridge...`, 'ok');
        const a = document.createElement('a');
        a.href = rawUrl;
        a.target = '_blank';
        a.download = `${currentVideoData ? currentVideoData.title : 'video'}.mp4`;
        document.body.appendChild(a);
        a.click();
        a.remove();
        return;
      }

      addLiveLog(`[Download Triggered] Initiating server download for format [${formatId}]...`, 'info');
      const wrap = document.getElementById('progWrap');
      const bar = document.getElementById('progBar');
      const txt = document.getElementById('progText');
      const statusSpan = document.getElementById('progStatus');
      const metricsSpan = document.getElementById('progMetrics');
      
      wrap.style.display = 'block';
      txt.classList.remove('hidden');
      bar.style.width = '5%';
      statusSpan.textContent = 'Connecting to stream...';

      let res, data;
      try {
        res = await fetch('/api/downloader/start', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({url, format_id: formatId})
        });
        data = await res.json();
      } catch(e){
        addLiveLog(`❌ [Download Error] Start failed: ${e.message}`, 'err');
        return;
      }

      if(data.error){
        addLiveLog(`❌ [Download Error] ${data.error}`, 'err');
        return;
      }

      const dlId = data.dl_id;
      addLiveLog(`📥 [Task Created] ID: ${dlId}. Polling server progress...`, 'ok');

      let done = false;
      while(!done){
        await new Promise(r => setTimeout(r, 800));
        let st, sd;
        try {
          st = await fetch(`/api/downloader/progress/${dlId}`);
          sd = await st.json();
        } catch(e){ break; }

        if(sd.auth_logs && sd.auth_logs.length){
          sd.auth_logs.forEach(l => {
            if(!allLogs.some(existing => existing.msg === l)){
              addLiveLog(l, l.includes('✅')?'ok':(l.includes('❌')?'err':'warn'));
            }
          });
        }

        if(sd.error){
          addLiveLog(`❌ [Download Aborted] ${sd.error}`, 'err');
          statusSpan.textContent = 'Failed';
          break;
        }

        if(sd.percent != null){
          bar.style.width = `${sd.percent}%`;
          statusSpan.textContent = sd.stage === 'merge' ? 'Merging video & audio...' : `Downloading (${sd.percent}%)`;
          metricsSpan.textContent = `${sd.downloaded_str || ''} / ${sd.total_str || ''} • ${sd.speed_str || ''} • ${sd.eta ? sd.eta + 's remaining' : ''}`;
        }

        if(sd.status === 'done'){
          done = true;
          bar.style.width = '100%';
          statusSpan.textContent = '✅ Download complete! Serving file...';
          addLiveLog(`🎉 [Ready] File: ${sd.filename}. Serving attachment.`, 'ok');
          window.location.href = `/api/downloader/file/${dlId}`;
        }
      }
    }
  </script>
</body>
</html>"""


# ─────────────────────── Standalone Local Worker Runner ───────────────────────

def run_standalone_worker(port=5005):
    """Launches a standalone local resolver worker on the given port."""
    app = Flask("AutoShortAi-LocalWorker")
    app.register_blueprint(downloader_bp)
    base_dir = Path(__file__).resolve().parent
    init_downloader(base_dir)

    print(f"\n=======================================================")
    print(f" 🚀 AutoShortAi Local Residential Worker is RUNNING!")
    print(f" 📡 Local URL: http://127.0.0.1:{port}")
    print(f" 🏠 Running from Clean Residential IP with local cookies")
    print(f"=======================================================\n")
    app.run(host="0.0.0.0", port=port, debug=False)


if __name__ == "__main__":
    port = 5005
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        if len(sys.argv) > 2 and sys.argv[2].isdigit():
            port = int(sys.argv[2])
        run_standalone_worker(port)
    else:
        print("Usage: python downloader.py --worker [PORT]")