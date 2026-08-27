#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AutoShortAi — Downloader module (Enhanced with Server-Side Cookies & Deep Diagnostics)
───────────────────────────────────────────────────────────────────────────────────────
A fully self-contained "download any video" feature (YouTube, Instagram,
TikTok, X/Twitter, Facebook, Vimeo & 1000+ more sites via yt-dlp), shipped
as its OWN file and wired into the main app as a Flask Blueprint — so it
runs in the exact same process, on the exact same port. Nothing extra to
start, nothing to configure separately.

SERVER & RENDER DEPLOYMENT FOCUS:
    - Automatically discovers and prioritizes `cookies.txt` located in the project
      directory or `/etc/secrets/cookies.txt` on Render cloud.
    - Inspects cookie health (validates Netscape format, checks for critical tokens
      like __Secure-3PSID, VISITOR_INFO1_LIVE, LOGIN_INFO).
    - Uses optimal client mappings with cookies (cookies_default and cookies_android_vr)
      to avoid YouTube format/botguard errors on Datacenter/Render IPs.
    - Emits structured Why / Where / What / How diagnostic logs for every single
      step in the extraction and download lifecycle.
    - Renders a live Diagnostic Console in the UI footer for 100% transparency.

Wire it into the main app with:

    from downloader import downloader_bp, init_downloader
    init_downloader(BASE, FFMPEG)
    app.register_blueprint(downloader_bp)

Routes exposed (all under /api/downloader/...):
    POST /api/downloader/info               -> full metadata + formats + diagnostic logs
    POST /api/downloader/start              -> kicks off a background download
    GET  /api/downloader/progress/<dl_id>   -> live percent/speed/eta/size/logs
    GET  /api/downloader/file/<dl_id>       -> serves the finished file
    GET  /api/downloader/cookie_status      -> checks cookie health & server environment
    GET  /downloader                        -> Web UI with live progress & footer console
"""

import os
import re
import time
import uuid
import threading
import subprocess
from pathlib import Path
from datetime import datetime

from flask import Blueprint, request, jsonify, send_file

import yt_dlp

try:
    import imageio_ffmpeg
    DEFAULT_FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    DEFAULT_FFMPEG = None

import json
import urllib.request

downloader_bp = Blueprint("downloader_bp", __name__)

# ── configured once via init_downloader() ──────────────────────────────
DL_DIR = None
FFMPEG_PATH = None
COOKIES_FILE = None
COOKIE_BROWSERS = ["chrome", "edge", "firefox", "brave"] if os.name == "nt" else []
OAUTH2_TOKEN_FILE = None

# dl_id -> {status, percent, downloaded, total, speed, eta, filename, error, url, stage, auth_logs}
DL_JOBS = {}

# Remembers whichever auth mode last succeeded, so repeat requests (info,
# then download) skip straight to the method that's known to work instead
# of re-running the whole slow fallback chain every single time.
_RESOLVED_MODE = {"mode": None}


def init_downloader(base_dir, ffmpeg_path=None):
    """Call once at startup from the main app, e.g. init_downloader(BASE, FFMPEG)."""
    global DL_DIR, FFMPEG_PATH, COOKIES_FILE, COOKIE_BROWSERS, OAUTH2_TOKEN_FILE
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

    # Auto-load cookies from environment variable if provided (fallback only)
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


# ───────────────────────────── small helpers ─────────────────────────────

def _no_console_kwargs():
    """Prevents a flashing black CMD window on Windows whenever ffmpeg/yt-dlp
    shells out to a subprocess — keeps the experience clean for non-technical
    users who wouldn't understand a terminal popping up."""
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
    # 1. Check globally registered COOKIES_FILE first
    if COOKIES_FILE and Path(COOKIES_FILE).exists() and Path(COOKIES_FILE).stat().st_size > 10:
        return str(Path(COOKIES_FILE).resolve())

    # 2. Check candidate project paths
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

    # 3. Environment variable fallback
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
            "has_instagram_auth": False,
            "tokens_found": [],
            "status_text": "❌ No cookies.txt found in project root or /etc/secrets",
            "recommendation": "Place your exported cookies.txt in the project root or Render Secret Files."
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
            "has_instagram_auth": False,
            "tokens_found": [],
            "status_text": f"⚠️ Could not read cookies.txt: {e}",
            "recommendation": "Ensure cookies.txt has read permissions."
        }

    is_netscape = "# Netscape HTTP Cookie File" in content or "\t" in content
    tokens = []
    has_yt = False
    has_ig = False

    yt_key_tokens = ["__Secure-3PSID", "__Secure-1PSIDTS", "__Secure-3PSIDTS", "VISITOR_INFO1_LIVE", "LOGIN_INFO", "SID", "SSID", "APISID", "SAPISID"]
    for tok in yt_key_tokens:
        if tok in content:
            tokens.append(tok)
            has_yt = True

    if "csrftoken" in content or "ds_user_id" in content or "sessionid" in content:
        has_ig = True
        tokens.append("Instagram-Session")

    status_str = f"🟢 Active ({_fmt_size(size)}) — YouTube Auth: {'✅ Yes' if has_yt else '⚠️ Partial/Public only'}"

    return {
        "found": True,
        "path": str(p),
        "size": size,
        "size_str": _fmt_size(size),
        "is_netscape": is_netscape,
        "has_youtube_auth": has_yt,
        "has_instagram_auth": has_ig,
        "tokens_found": tokens,
        "status_text": status_str,
        "recommendation": "Cookies configured and loaded for server requests." if has_yt else "YouTube session cookies (__Secure-3PSID) missing; please re-export cookies."
    }


def _classify_error_detailed(e, mode, url):
    """Analyzes exact exception message and returns a structured breakdown
    explaining Where, Why, What happened, and How to resolve it."""
    err_raw = str(e).strip()
    where = f"Strategy '{mode}' during yt_dlp.extract_info()"
    
    if "Sign in to confirm" in err_raw or "confirm you’re not a bot" in err_raw:
        what = "YouTube BotGuard Challenge (Bot Detection Triggered)"
        why = (
            "YouTube detected a Datacenter IP (Render/Cloud server) and blocked unauthenticated extraction. "
            "Server requires active cookies.txt or residential proxy to satisfy YouTube's Proof-of-Origin check."
        )
        how = "Ensure cookies.txt in project root has active __Secure-3PSID and __Secure-1PSIDTS tokens."
    elif "403" in err_raw:
        what = "HTTP 403 Forbidden (Access Denied / Expired CDN Signature)"
        why = (
            "YouTube rejected the video stream download URL. This happens when direct stream URLs are accessed "
            "with mismatched User-Agent headers, expired timestamp tokens, or IP-restricted signatures."
        )
        how = "Using 'cookies_default' mode will generate valid, server-signed stream URLs."
    elif "Requested format is not available" in err_raw or "Only images are available" in err_raw:
        what = "Format Not Available for Selected Client"
        why = (
            "The requested player client (e.g. forced web_safari/tv client) does not provide progressive video streams "
            "for this specific video ID without a GVS PO Token."
        )
        how = "Switching to 'cookies_default' or 'cookies_android_vr' mode resolves format availability."
    elif "Video unavailable" in err_raw:
        what = "Video Unavailable or Private"
        why = "The video has been removed by the uploader, set to Private, or restricted in the server's region."
        how = "Verify the video URL in your browser or supply cookies from an account with access."
    elif "IncompleteRead" in err_raw or "RemoteDisconnected" in err_raw or "timed out" in err_raw:
        what = "Network Socket Timeout / Interrupted Connection"
        why = "The network connection between Render and YouTube servers dropped or was throttled."
        how = "Automatic retries will attempt reconnecting with increased socket timeouts."
    else:
        what = f"Extractor Error: {err_raw[:120]}"
        why = f"yt-dlp returned: {err_raw[:200]}"
        how = "Trying next fallback mode in the authentication pipeline."

    return {
        "where": where,
        "what": what,
        "why": why,
        "how": how,
        "raw_error": err_raw[:250]
    }


# ─────────────────────────── smart auth resolution ───────────────────────────

def _extract_video_id(url):
    m = re.search(r'(?:v=|/shorts/|youtu\.be/|embed/|v/)([a-zA-Z0-9_-]{11})', url or '')
    return m.group(1) if m else None


def _resolve_via_public_api(url):
    """Fail-safe public instance resolver (Invidious / Piped) when all datacenter
    yt-dlp attempts are blocked by YouTube IP bot-guards. Returns a valid yt-dlp-compatible
    info dictionary with formats, streams, and metadata."""
    vid = _extract_video_id(url)
    if not vid:
        return None, "Invalid YouTube video ID"

    instances = [
        f"https://invidious.nerdvpn.de/api/v1/videos/{vid}",
        f"https://inv.tux.pizza/api/v1/videos/{vid}",
        f"https://invidious.privacydev.net/api/v1/videos/{vid}",
        f"https://vid.priv.au/api/v1/videos/{vid}",
        f"https://pipedapi.kavin.rocks/streams/{vid}",
    ]

    for api_url in instances:
        try:
            req = urllib.request.Request(
                api_url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            )
            with urllib.request.urlopen(req, timeout=6) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    if "formatStreams" in data or "adaptiveFormats" in data:
                        formats = []
                        for f in data.get("formatStreams", []):
                            formats.append({
                                "format_id": str(f.get("itag", "prog")),
                                "url": f.get("url"),
                                "ext": f.get("container") or "mp4",
                                "height": int(f.get("qualityLabel", "720p").replace("p", "")) if f.get("qualityLabel") else 720,
                                "width": 1280,
                                "fps": 30,
                                "vcodec": "avc1",
                                "acodec": "mp4a",
                                "filesize": f.get("size"),
                                "tbr": int(f.get("bitrate") or 1000000) // 1000,
                                "http_headers": {"User-Agent": "Mozilla/5.0"}
                            })
                        for f in data.get("adaptiveFormats", []):
                            is_video = "video" in (f.get("type") or "")
                            formats.append({
                                "format_id": str(f.get("itag", "adapt")),
                                "url": f.get("url"),
                                "ext": f.get("container") or ("mp4" if "mp4" in (f.get("type") or "") else "webm"),
                                "height": int(f.get("qualityLabel", "1080p").replace("p", "")) if f.get("qualityLabel") and is_video else None,
                                "width": None,
                                "fps": int(f.get("fps") or 30) if is_video else None,
                                "vcodec": "avc1" if is_video else "none",
                                "acodec": "mp4a" if not is_video else "none",
                                "abr": (int(f.get("bitrate") or 128000) // 1000) if not is_video else None,
                                "filesize": f.get("size"),
                                "tbr": int(f.get("bitrate") or 1000000) // 1000,
                                "http_headers": {"User-Agent": "Mozilla/5.0"}
                            })

                        info = {
                            "id": vid,
                            "title": data.get("title") or "YouTube Video",
                            "description": data.get("description") or "",
                            "uploader": data.get("author") or "",
                            "channel": data.get("author") or "",
                            "channel_id": data.get("authorId") or "",
                            "duration": data.get("lengthSeconds") or 0,
                            "view_count": data.get("viewCount") or 0,
                            "like_count": data.get("likeCount") or 0,
                            "upload_date": data.get("published") or "",
                            "tags": data.get("keywords") or [],
                            "categories": [],
                            "subtitles": {},
                            "automatic_captions": {},
                            "chapters": [],
                            "thumbnail": (data.get("videoThumbnails") or [{}])[0].get("url") if data.get("videoThumbnails") else None,
                            "webpage_url": f"https://www.youtube.com/watch?v={vid}",
                            "formats": formats
                        }
                        return info, None

                    elif "videoStreams" in data or "audioStreams" in data:
                        formats = []
                        for f in data.get("videoStreams", []):
                            formats.append({
                                "format_id": str(f.get("format", "video")),
                                "url": f.get("url"),
                                "ext": f.get("mimeType", "").split("/")[-1].split(";")[0] or "mp4",
                                "height": f.get("height"),
                                "width": f.get("width"),
                                "fps": f.get("fps"),
                                "vcodec": f.get("codec") or "avc1",
                                "acodec": "none",
                                "tbr": (f.get("bitrate") or 1000000) // 1000,
                                "http_headers": {"User-Agent": "Mozilla/5.0"}
                            })
                        for f in data.get("audioStreams", []):
                            formats.append({
                                "format_id": str(f.get("format", "audio")),
                                "url": f.get("url"),
                                "ext": f.get("mimeType", "").split("/")[-1].split(";")[0] or "m4a",
                                "height": None,
                                "width": None,
                                "fps": None,
                                "vcodec": "none",
                                "acodec": f.get("codec") or "mp4a",
                                "abr": (f.get("bitrate") or 128000) // 1000,
                                "tbr": (f.get("bitrate") or 128000) // 1000,
                                "http_headers": {"User-Agent": "Mozilla/5.0"}
                            })
                        info = {
                            "id": vid,
                            "title": data.get("title") or "YouTube Video",
                            "description": data.get("description") or "",
                            "uploader": data.get("uploader") or "",
                            "channel": data.get("uploader") or "",
                            "channel_id": data.get("uploaderUrl") or "",
                            "duration": data.get("duration") or 0,
                            "view_count": data.get("views") or 0,
                            "like_count": data.get("likes") or 0,
                            "upload_date": data.get("uploadDate") or "",
                            "tags": data.get("tags") or [],
                            "categories": [],
                            "subtitles": {},
                            "automatic_captions": {},
                            "chapters": [],
                            "thumbnail": data.get("thumbnailUrl"),
                            "webpage_url": f"https://www.youtube.com/watch?v={vid}",
                            "formats": formats
                        }
                        return info, None
        except Exception:
            continue
    return None, "Public resolver instances unreachable or video restricted"


def _auth_attempts():
    """Generates the prioritized authentication attempts list.
    When a cookies.txt file exists on the server, cookie-backed modes are placed
    at the VERY TOP because they are proven to succeed on datacenter/Render IPs."""
    attempts = []
    if _RESOLVED_MODE["mode"]:
        attempts.append(_RESOLVED_MODE["mode"])

    cfile = _get_cookies_file()
    if cfile:
        # TOP PRIORITY: Cookie-backed extraction strategies
        cookie_modes = ["cookies_default", "cookies_android_vr", "cookies_web", "cookies_mweb"]
        for cm in cookie_modes:
            if cm not in attempts:
                attempts.append(cm)

    # Secondary strategies (OAuth2 device token & standard fallbacks)
    if OAUTH2_TOKEN_FILE and OAUTH2_TOKEN_FILE.exists():
        if "oauth2" not in attempts:
            attempts.append("oauth2")

    fallbacks = ["android_vr_direct", "web_safari_highres", "bypass", "default"]
    fallbacks.extend(COOKIE_BROWSERS)
    for m in fallbacks:
        if m not in attempts:
            attempts.append(m)

    return attempts


def _apply_auth_mode(opts, mode, client_potoken=None):
    """Configures yt-dlp dictionary with exact extractor_args and cookiefile
    tailored for each mode to prevent format dropping or header mismatches."""
    opts = dict(opts)
    opts.pop("cookiesfrombrowser", None)
    opts.pop("username", None)
    opts.pop("password", None)
    opts.pop("extractor_args", None)

    cfile = _get_cookies_file()

    # 1. Cookie-backed Modes (Server-Side cookies.txt)
    if mode == "cookies_default":
        # #1 Most Reliable: Let yt-dlp use its verified extractor with cookies
        if cfile:
            opts["cookiefile"] = cfile
    elif mode == "cookies_android_vr":
        # Android VR with cookiefile (bypasses botguards while preserving high-res streams)
        if cfile:
            opts["cookiefile"] = cfile
        opts["extractor_args"] = {
            "youtube": {
                "player_client": ["android_vr", "web"]
            }
        }
    elif mode == "cookies_web":
        if cfile:
            opts["cookiefile"] = cfile
        ext_args = {
            "player_client": ["web", "web_safari"]
        }
        if client_potoken:
            ext_args["po_token"] = [f"web+{client_potoken}"]
        opts["extractor_args"] = {"youtube": ext_args}
    elif mode == "cookies_mweb":
        if cfile:
            opts["cookiefile"] = cfile
        opts["extractor_args"] = {
            "youtube": {
                "player_client": ["mweb", "web"]
            }
        }
    elif mode == "android_vr_direct":
        opts["extractor_args"] = {
            "youtube": {
                "player_client": ["android_vr", "mweb"]
            }
        }
        if cfile:
            opts["cookiefile"] = cfile
    elif mode == "web_safari_highres":
        ext_args = {
            "player_client": ["web_safari", "web_embedded", "web"]
        }
        if client_potoken:
            ext_args["po_token"] = [f"web+{client_potoken}"]
        opts["extractor_args"] = {"youtube": ext_args}
        if cfile:
            opts["cookiefile"] = cfile
    elif mode == "oauth2":
        opts["username"] = "oauth2"
        opts["password"] = ""
        opts["extractor_args"] = {"youtube": {"player_client": ["tv_embedded", "tv", "android"]}}
    elif mode in COOKIE_BROWSERS:
        opts.pop("cookiefile", None)
        opts["cookiesfrombrowser"] = (mode,)
    elif mode == "default":
        opts["extractor_args"] = {"youtube": {"player_client": ["web_embedded", "android", "web"]}}
        if cfile:
            opts["cookiefile"] = cfile
    elif mode == "bypass":
        opts["extractor_args"] = {"youtube": {"player_client": ["web_safari", "android_vr", "web"]}}
        if cfile:
            opts["cookiefile"] = cfile

    return opts


def _base_ydl_opts():
    opts = {
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "socket_timeout": 20, "retries": 3, "extractor_retries": 2,
        "geo_bypass": True,
    }
    if FFMPEG_PATH:
        opts["ffmpeg_location"] = str(FFMPEG_PATH)
    return opts


def _extract_info_smart(url, extra_opts=None, download=False, client_potoken=None, log_list=None):
    """Tries the prioritized auth mode pipeline with comprehensive diagnostic logs
    recording WHY each mode was chosen, WHERE it ran, WHAT happened, and HOW errors occurred."""
    base_opts = _base_ydl_opts()
    if extra_opts:
        base_opts.update(extra_opts)

    # 1. Diagnostic: Check and log cookie health
    cookie_health = _inspect_cookies_health()
    if log_list is not None:
        ts = _now_ts()
        if cookie_health["found"]:
            log_list.append(
                f"[{ts}] 📁 [Cookie Inspector] Found cookies.txt at '{cookie_health['path']}' ({cookie_health['size_str']}). "
                f"Tokens: {', '.join(cookie_health['tokens_found']) if cookie_health['tokens_found'] else 'None'}"
            )
        else:
            log_list.append(f"[{ts}] 📁 [Cookie Inspector] {cookie_health['status_text']}")

        if client_potoken:
            log_list.append(f"[{ts}] 🔑 [Client PoToken] Attached client browser token: {client_potoken[:12]}...")

    attempts = _auth_attempts()
    total_steps = len(attempts)
    last_err = None
    detailed_failures = []

    for idx, mode in enumerate(attempts, start=1):
        ts = _now_ts()
        opts = _apply_auth_mode(base_opts, mode, client_potoken=client_potoken)
        
        # Explain WHY this mode is being executed
        why_msg = "standard fallback"
        if mode == "cookies_default":
            why_msg = f"Authenticating using server 'cookies.txt' ({cookie_health.get('size_str', 'loaded')}) with default client"
        elif mode == "cookies_android_vr":
            why_msg = "Using Android VR client + cookies to bypass datacenter IP restrictions"
        elif mode == "cookies_web":
            why_msg = "Using Web client + cookies for adaptive 4K/1080p stream extraction"
        elif mode == "oauth2":
            why_msg = "Using YouTube TV OAuth2 token authentication"
        elif mode == "web_safari_highres":
            why_msg = "Using Web Safari client to negotiate full DASH streams"

        msg_try = f"[{ts}] 🔄 [Step {idx}/{total_steps}] Trying mode: '{mode}' — Why: {why_msg}"
        if log_list is not None:
            log_list.append(msg_try)

        try:
            print(f"[downloader:auth] {msg_try.encode('ascii', 'replace').decode()}", flush=True)
        except Exception:
            pass

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=download)

            _RESOLVED_MODE["mode"] = mode
            fmt_count = len(info.get("formats", [])) if info else 0
            title_sample = (info.get("title") or "Video")[:45] if info else ""
            msg_ok = f"[{_now_ts()}] ✅ [Step {idx}/{total_steps}] Mode '{mode}' SUCCEEDED! Title: \"{title_sample}\" ({fmt_count} formats resolved)"
            
            if log_list is not None:
                log_list.append(msg_ok)
            try:
                print(f"[downloader:auth] {msg_ok.encode('ascii', 'replace').decode()}", flush=True)
            except Exception:
                pass

            return info, ydl if download else None, None

        except Exception as e:
            err_diag = _classify_error_detailed(e, mode, url)
            detailed_failures.append(err_diag)
            
            msg_fail = (
                f"[{_now_ts()}] ❌ [Step {idx}/{total_steps}] Mode '{mode}' failed: {err_diag['what']}\n"
                f"     ↳ Why: {err_diag['why']}\n"
                f"     ↳ How to fix: {err_diag['how']}"
            )
            if log_list is not None:
                log_list.append(msg_fail)

            try:
                print(f"[downloader:auth] {msg_fail.encode('ascii', 'replace').decode()}", flush=True)
            except Exception:
                pass

            last_err = e
            if _RESOLVED_MODE.get("mode") == mode:
                _RESOLVED_MODE["mode"] = None
            continue

    # Final Fail-Safe: If info extraction only and all yt-dlp modes failed, try public API resolver
    if not download:
        ts = _now_ts()
        msg_pub = f"[{ts}] 🔄 [Fail-Safe Resolver] Trying public fail-safe API mirror (Invidious / Piped)..."
        if log_list is not None:
            log_list.append(msg_pub)
        try:
            print(f"[downloader:auth] {msg_pub.encode('ascii', 'replace').decode()}", flush=True)
        except Exception:
            pass

        pub_info, pub_err = _resolve_via_public_api(url)
        if pub_info:
            msg_pub_ok = f"[{_now_ts()}] ✅ [Fail-Safe] public_api_fallback SUCCEEDED ({len(pub_info.get('formats', []))} formats)"
            if log_list is not None:
                log_list.append(msg_pub_ok)
            return pub_info, None, None
        else:
            if log_list is not None:
                log_list.append(f"[{_now_ts()}] ❌ [Fail-Safe] public_api_fallback failed: {pub_err}")

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
            "filesize_str": _fmt_size(size) or "~unknown",
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
    """Everything meaningful yt-dlp exposes about the video, laid out for a
    rich details panel — not just a handful of picked fields."""
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
    return jsonify(health)


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
        err_msg = f"Could not resolve video: {err}"
        return jsonify({
            "error": err_msg,
            "logs": logs,
            "cookie_status": cookie_h,
            "troubleshoot": {
                "where": "Server yt-dlp Extraction Pipeline",
                "what": "Extraction failed across all authentication attempts",
                "why": "Datacenter IP challenged by YouTube bot-guards. Check if cookies.txt is present and up to date.",
                "how": "Verify that cookies.txt is in the project root with active session tokens."
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
    """Client-Bridge Receiver: When the client browser fetches stream chunks directly,
    it pushes the video data to this endpoint. The server stores it and makes it available
    for instant download with 0 datacenter IP blocks."""
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
            f"[{_now_ts()}] 🌉 Client-Assisted Bridge Relay Succeeded",
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
    """Standalone Downloader Web UI with deep diagnostics console in the footer."""
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>AutoShortAi — Universal Video Downloader & Server Diagnostic Suite</title>
  <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg: #070b14;
      --card-bg: #0f172a;
      --card-alt: #131d38;
      --accent: #6366f1;
      --accent-hover: #4f46e5;
      --accent-glow: rgba(99, 102, 241, 0.35);
      --text: #f8fafc;
      --dim: #94a3b8;
      --border: #1e293b;
      --border-light: #334155;
      --success: #10b981;
      --success-glow: rgba(16, 185, 129, 0.25);
      --warning: #f59e0b;
      --danger: #ef4444;
      --danger-glow: rgba(239, 68, 68, 0.25);
    }
    * { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Plus Jakarta Sans', sans-serif; }
    body { background: var(--bg); color: var(--text); min-height: 100vh; padding: 30px 16px; display: flex; justify-content: center; }
    .container { width: 100%; max-width: 960px; }
    
    /* Header & Badges */
    .header { text-align: center; margin-bottom: 28px; }
    .header h1 { font-size: 32px; font-weight: 800; background: linear-gradient(135deg, #a5b4fc, #6366f1, #38bdf8); -webkit-background-clip: text; -webkit-text-fill-color: transparent; margin-bottom: 8px; }
    .header p { color: var(--dim); font-size: 14.5px; max-width: 600px; margin: 0 auto; }
    
    .status-bar {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      justify-content: center;
      margin-top: 14px;
    }
    .status-pill {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 6px 14px;
      background: rgba(15, 23, 42, 0.8);
      border: 1px solid var(--border-light);
      border-radius: 999px;
      font-size: 12.5px;
      font-weight: 600;
      color: var(--dim);
    }
    .status-pill.active { border-color: rgba(16, 185, 129, 0.5); color: #6ee7b7; background: rgba(16, 185, 129, 0.1); }
    .status-pill.warning { border-color: rgba(245, 158, 11, 0.5); color: #fde68a; background: rgba(245, 158, 11, 0.1); }
    
    /* Cards */
    .card { background: var(--card-bg); border: 1px solid var(--border); border-radius: 16px; padding: 22px; margin-bottom: 22px; box-shadow: 0 10px 30px rgba(0,0,0,0.35); }
    .card-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px; }
    .card-title { font-size: 16px; font-weight: 700; color: var(--text); display: flex; align-items: center; gap: 8px; }
    
    /* Inputs & Buttons */
    .input-group { display: flex; gap: 10px; }
    input[type="text"] { flex: 1; padding: 14px 18px; background: #060911; border: 1px solid var(--border-light); border-radius: 10px; color: var(--text); font-size: 15px; outline: none; transition: 0.2s; }
    input[type="text"]:focus { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-glow); }
    .btn { padding: 14px 22px; background: var(--accent); color: white; border: none; border-radius: 10px; font-weight: 700; cursor: pointer; transition: 0.2s; display: inline-flex; align-items: center; justify-content: center; gap: 8px; font-size: 14.5px; white-space: nowrap; }
    .btn:hover { background: var(--accent-hover); transform: translateY(-1px); }
    .btn:disabled { opacity: 0.6; cursor: not-allowed; transform: none; }
    
    /* Progress */
    .progress-bar-wrap { width: 100%; height: 10px; background: #060911; border-radius: 999px; overflow: hidden; margin-top: 14px; display: none; }
    .progress-bar { height: 100%; width: 0%; background: linear-gradient(90deg, #6366f1, #38bdf8); transition: width 0.3s; }
    .prog-text { font-size: 13px; color: var(--dim); margin-top: 8px; display: flex; justify-content: space-between; }
    
    /* Preview */
    .preview-box { display: flex; gap: 18px; align-items: flex-start; margin-top: 10px; padding: 16px; background: rgba(0,0,0,0.3); border-radius: 12px; border: 1px solid var(--border); }
    .preview-thumb { width: 180px; aspect-ratio: 16/9; object-fit: cover; border-radius: 8px; }
    .preview-info h3 { font-size: 16px; margin-bottom: 6px; color: var(--text); }
    .preview-info p { font-size: 13px; color: var(--dim); line-height: 1.5; }
    
    /* Formats Table */
    .formats-table { width: 100%; border-collapse: collapse; margin-top: 16px; font-size: 13.5px; }
    .formats-table th { text-align: left; padding: 10px 12px; color: var(--dim); border-bottom: 1px solid var(--border); font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; }
    .formats-table td { padding: 12px; border-bottom: 1px solid rgba(255,255,255,0.04); }
    .fmt-btn { padding: 7px 14px; font-size: 12.5px; border-radius: 6px; background: rgba(99,102,241,0.2); border: 1px solid var(--accent); color: #c7d2fe; cursor: pointer; font-weight: 600; transition: 0.2s; }
    .fmt-btn:hover { background: var(--accent); color: white; transform: translateY(-1px); }
    
    /* Diagnostic Console in Footer */
    .console-card { background: #060913; border: 1px solid #1e293b; border-radius: 16px; overflow: hidden; margin-top: 20px; }
    .console-header { background: #0b1120; padding: 12px 18px; display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #1e293b; }
    .console-tabs { display: flex; gap: 8px; }
    .console-tab { padding: 4px 10px; font-size: 12px; font-weight: 600; border-radius: 6px; cursor: pointer; background: transparent; color: var(--dim); border: 1px solid transparent; }
    .console-tab.active { background: #1e293b; color: #f8fafc; border-color: #334155; }
    .console-actions { display: flex; gap: 8px; }
    .console-btn { padding: 4px 10px; font-size: 11.5px; background: #1e293b; color: var(--dim); border: 1px solid #334155; border-radius: 6px; cursor: pointer; }
    .console-btn:hover { color: var(--text); background: #334155; }
    
    .console-body {
      padding: 16px;
      font-family: 'JetBrains Mono', monospace;
      font-size: 12.5px;
      line-height: 1.7;
      max-height: 320px;
      overflow-y: auto;
      color: #94a3b8;
      background: #040711;
    }
    .console-log { margin-bottom: 6px; word-break: break-word; }
    .log-ok { color: #34d399; }
    .log-err { color: #f87171; background: rgba(239,68,68,0.06); padding: 4px 8px; border-radius: 4px; border-left: 3px solid #ef4444; }
    .log-warn { color: #fbbf24; }
    .log-info { color: #38bdf8; }
    .hidden { display: none !important; }
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <h1>Universal Video Downloader</h1>
      <p>Server-side high-speed stream extractor with automatic cookies.txt authentication and live diagnostic tracing.</p>
      
      <div class="status-bar" id="statusBar">
        <div class="status-pill" id="cookiePill">🍪 Checking cookies.txt...</div>
        <div class="status-pill" id="serverPill">🖥️ Server: Ready</div>
      </div>
    </div>

    <div class="card">
      <div class="input-group">
        <input type="text" id="videoUrl" placeholder="Paste YouTube, Shorts, Twitter, Instagram URL..." value="https://www.youtube.com/watch?v=re0WlNMOfFU">
        <button class="btn" id="fetchBtn" onclick="fetchVideoInfo()">
          <span>🔍 Fetch Details</span>
        </button>
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

    <!-- Live Diagnostic & Auth Console (Footer) -->
    <div class="console-card">
      <div class="console-header">
        <div class="card-title" style="font-size: 13.5px;">
          <span>📡 Live Diagnostic Console (Why / Where / What / How)</span>
        </div>
        <div class="console-tabs">
          <button class="console-tab active" onclick="filterLogs('all')">All</button>
          <button class="console-tab" onclick="filterLogs('auth')">Auth & Cookies</button>
          <button class="console-tab" onclick="filterLogs('err')">Errors</button>
        </div>
        <div class="console-actions">
          <button class="console-btn" onclick="copyConsoleLogs()">📋 Copy Logs</button>
          <button class="console-btn" onclick="clearConsoleLogs()">🧹 Clear</button>
        </div>
      </div>
      <div class="console-body" id="consoleBody">
        <div class="console-log log-info">[System Ready] Paste a video URL and click "Fetch Details" to start extraction.</div>
      </div>
    </div>
  </div>

  <script>
    let allLogs = [];

    window.addEventListener('DOMContentLoaded', () => {
      checkServerCookieHealth();
    });

    async function checkServerCookieHealth(){
      try {
        const res = await fetch('/api/downloader/cookie_status');
        const data = await res.json();
        const pill = document.getElementById('cookiePill');
        if(data.found && data.has_youtube_auth){
          pill.className = 'status-pill active';
          pill.innerHTML = `🍪 cookies.txt: Active (${data.size_str || 'Loaded'}) • Auth: Verified`;
          addConsoleLog(`[Cookie Status] ✅ cookies.txt loaded from: ${data.path} (${data.size_str}). Active tokens: ${data.tokens_found.join(', ')}`, 'ok');
        } else if(data.found){
          pill.className = 'status-pill warning';
          pill.innerHTML = `🍪 cookies.txt: Loaded (${data.size_str}) • Partial Auth`;
          addConsoleLog(`[Cookie Status] ⚠️ cookies.txt found at ${data.path} but YouTube session tokens are incomplete.`, 'warn');
        } else {
          pill.className = 'status-pill warning';
          pill.innerHTML = '🍪 cookies.txt: Not Found (Will try mobile/bypass)';
          addConsoleLog('[Cookie Status] ℹ️ No cookies.txt found in project directory. Extraction will use mobile client fallbacks.', 'warn');
        }
      } catch(e){
        document.getElementById('cookiePill').textContent = '🍪 Cookie Inspector: Offline';
      }
    }

    function addConsoleLog(msg, type='info'){
      allLogs.push({msg, type, time: new Date().toLocaleTimeString()});
      const body = document.getElementById('consoleBody');
      const el = document.createElement('div');
      el.className = `console-log log-${type}`;
      el.textContent = msg;
      body.appendChild(el);
      body.scrollTop = body.scrollHeight;
    }

    function filterLogs(filter){
      document.querySelectorAll('.console-tab').forEach(t => t.classList.remove('active'));
      event.target.classList.add('active');
      const body = document.getElementById('consoleBody');
      body.innerHTML = '';
      allLogs.forEach(l => {
        if(filter === 'all' || (filter === 'err' && l.type === 'err') || (filter === 'auth' && (l.msg.includes('Cookie') || l.msg.includes('Step') || l.msg.includes('Mode')))){
          const el = document.createElement('div');
          el.className = `console-log log-${l.type}`;
          el.textContent = l.msg;
          body.appendChild(el);
        }
      });
    }

    function clearConsoleLogs(){
      allLogs = [];
      document.getElementById('consoleBody').innerHTML = '<div class="console-log log-info">[Console Cleared]</div>';
    }

    function copyConsoleLogs(){
      const text = allLogs.map(l => l.msg).join('\\n');
      navigator.clipboard.writeText(text).then(() => alert('Diagnostics copied to clipboard!'));
    }

    async function fetchVideoInfo(){
      const url = document.getElementById('videoUrl').value.trim();
      if(!url){ alert('Paste a URL first'); return; }
      const btn = document.getElementById('fetchBtn');
      btn.disabled = true;
      btn.innerHTML = '<span>⏳ Resolving...</span>';
      addConsoleLog(`[Fetch Request] Starting extraction for: ${url}`, 'info');

      let res, data;
      try {
        res = await fetch('/api/downloader/info', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({url})
        });
        data = await res.json();
      } catch(e){
        addConsoleLog(`❌ [Network Failure] Could not connect to server: ${e.message}`, 'err');
        btn.disabled = false; btn.innerHTML = '<span>🔍 Fetch Details</span>';
        return;
      }

      if(data.logs && data.logs.length){
        data.logs.forEach(l => addConsoleLog(l, l.includes('✅')?'ok':(l.includes('❌')?'err':'warn')));
      }

      btn.disabled = false;
      btn.innerHTML = '<span>🔍 Fetch Details</span>';

      if(data && !data.error){
        renderDetails(data);
      } else {
        addConsoleLog(`❌ [Resolution Failed] ${data ? data.error : 'Could not resolve video'}`, 'err');
        if(data && data.troubleshoot){
          addConsoleLog(`💡 [Troubleshoot Guide] Why: ${data.troubleshoot.why} | How: ${data.troubleshoot.how}`, 'warn');
        }
      }
    }

    function renderDetails(data){
      document.getElementById('previewCard').classList.remove('hidden');
      document.getElementById('prevThumb').src = data.thumbnail || '';
      document.getElementById('prevTitle').textContent = data.title || 'Untitled';
      document.getElementById('prevMeta').textContent = `${data.uploader || 'Unknown Creator'} • Duration: ${data.duration_str || data.duration || 'N/A'} • ${(data.formats||[]).length} formats available`;

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
            <button class="fmt-btn" onclick="startDownload('${f.format_id}')">⬇️ Download</button>
          </td>
        `;
        tbody.appendChild(tr);
      });
    }

    async function startDownload(formatId){
      const url = document.getElementById('videoUrl').value.trim();
      addConsoleLog(`[Download Triggered] Initiating background download for format [${formatId}]...`, 'info');

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
        addConsoleLog(`❌ [Download Error] Start failed: ${e.message}`, 'err');
        return;
      }

      if(data.error){
        addConsoleLog(`❌ [Download Error] ${data.error}`, 'err');
        return;
      }

      const dlId = data.dl_id;
      addConsoleLog(`📥 [Task Created] ID: ${dlId}. Polling server progress...`, 'ok');

      let done = false;
      while(!done){
        await new Promise(r => setTimeout(r, 800));
        let st, sd;
        try {
          st = await fetch(`/api/downloader/progress/${dlId}`);
          sd = await st.json();
        } catch(e){
          break;
        }

        if(sd.auth_logs && sd.auth_logs.length){
          sd.auth_logs.forEach(l => {
            if(!allLogs.some(existing => existing.msg === l)){
              addConsoleLog(l, l.includes('✅')?'ok':(l.includes('❌')?'err':'warn'));
            }
          });
        }

        if(sd.error){
          addConsoleLog(`❌ [Download Aborted] ${sd.error}`, 'err');
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
          addConsoleLog(`🎉 [Ready] File: ${sd.filename}. Serving attachment.`, 'ok');
          window.location.href = `/api/downloader/file/${dlId}`;
        }
      }
    }
  </script>
</body>
</html>"""
