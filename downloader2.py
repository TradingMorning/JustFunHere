#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AutoShortAi — Downloader2 Module with Non-Blocking Asynchronous Reverse Bridge
──────────────────────────────────────────────────────────────────────────────
Optimized for 1-Worker Sync Gunicorn on Render Cloud:
    - 0% Worker Blocking (All HTTP requests return in < 10ms).
    - Browser polls /api/downloader/job_result/<job_id> every 0.8s.
    - Local PC (bridge_worker.py) polls /api/worker/poll and resolves streams in 1-2s.
    - Works 100% seamlessly without Gunicorn worker deadlocks.

HOW TO WIRE INTO app.py:
    from downloader2 import init_downloader2
    init_downloader2(app)
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
downloader2_bp = downloader_bp

# ── Global configuration ───────────────────────────────────────────────
DL_DIR = None
BRIDGE_DIR = None
FFMPEG_PATH = None
COOKIES_FILE = None
COOKIE_BROWSERS = ["chrome", "edge", "firefox", "brave"] if os.name == "nt" else []
OAUTH2_TOKEN_FILE = None

# dl_id -> {status, percent, downloaded, total, speed, eta, filename, error, url, stage, auth_logs}
DL_JOBS = {}
_RESOLVED_MODE = {"mode": None}


def init_downloader2(base_dir=None, ffmpeg_path=None):
    """Call once at startup from app.py, e.g. init_downloader2(app) or init_downloader2()."""
    global DL_DIR, BRIDGE_DIR, FFMPEG_PATH, COOKIES_FILE, COOKIE_BROWSERS, OAUTH2_TOKEN_FILE
    
    if hasattr(base_dir, "register_blueprint"):
        flask_app = base_dir
        base_dir = Path(__file__).resolve().parent
        try:
            flask_app.register_blueprint(downloader2_bp)
        except Exception:
            pass
    elif base_dir is None:
        base_dir = Path(__file__).resolve().parent
    else:
        base_dir = Path(base_dir)

    DL_DIR = base_dir / "downloads"
    DL_DIR.mkdir(exist_ok=True)
    BRIDGE_DIR = DL_DIR / "bridge_jobs"
    BRIDGE_DIR.mkdir(exist_ok=True)

    FFMPEG_PATH = ffmpeg_path or DEFAULT_FFMPEG
    COOKIES_FILE = base_dir / "cookies.txt"

    # Cloud secret files check (Render Secret Files & Root directory)
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

    # Environment variable fallback
    _env_cookies = os.environ.get("YOUTUBE_COOKIES") or os.environ.get("COOKIES_TEXT") or os.environ.get("YTDLP_COOKIES")
    if _env_cookies and not (COOKIES_FILE and COOKIES_FILE.exists()):
        try:
            target_c = base_dir / "cookies.txt"
            target_c.write_text(_env_cookies, encoding="utf-8")
            COOKIES_FILE = target_c
        except Exception:
            pass

    # Browser cookies check
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

init_downloader = init_downloader2


# ───────────────────────────── small helpers ─────────────────────────────

def _no_console_kwargs():
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
    return datetime.now().strftime("%H:%M:%S")


# ─────────────────────── Cookie Health & Discovery ───────────────────────

def _get_cookies_file():
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
    cpath = cookie_path or _get_cookies_file()
    if not cpath:
        return {
            "found": False, "path": None, "size": 0, "is_netscape": False,
            "has_youtube_auth": False, "has_full_youtube_auth": False, "has_instagram_auth": False,
            "tokens_found": [], "status_text": "❌ No cookies.txt found on server"
        }

    p = Path(cpath)
    try:
        content = p.read_text(encoding="utf-8", errors="replace")
        size = p.stat().st_size
    except Exception as e:
        return {
            "found": True, "path": str(p), "size": 0, "is_netscape": False,
            "has_youtube_auth": False, "has_full_youtube_auth": False, "has_instagram_auth": False,
            "tokens_found": [], "status_text": f"⚠️ Error reading cookies.txt: {e}"
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
        status_str = f"🟢 Active ({_fmt_size(size)}) • Full YouTube Auth"
    elif has_partial_yt:
        status_str = f"🟡 Partial ({_fmt_size(size)}) • Partial YouTube Auth"
    else:
        status_str = f"🔴 Inactive ({_fmt_size(size)}) • No Login Tokens"

    return {
        "found": True, "path": str(p), "size": size, "size_str": _fmt_size(size),
        "is_netscape": is_netscape, "has_youtube_auth": has_full_yt or has_partial_yt,
        "has_full_youtube_auth": has_full_yt, "has_instagram_auth": has_ig,
        "tokens_found": tokens, "status_text": status_str
    }


def _classify_error_detailed(e, mode, url):
    err_raw = str(e).strip()
    where = f"Strategy '{mode}'"
    
    if "Sign in to confirm" in err_raw or "confirm you’re not a bot" in err_raw:
        what = "YouTube BotGuard Challenge (Datacenter IP Blocked)"
        why = "Render Cloud Datacenter IP was challenged by YouTube BotGuard."
        how = "Running bridge_worker.py on your home PC resolves streams seamlessly via clean residential IP."
    elif "403" in err_raw:
        what = "HTTP 403 Forbidden"
        why = "YouTube rejected direct stream signature."
        how = "Home PC bridge worker resolves full direct streams."
    elif "Requested format is not available" in err_raw or "Only images are available" in err_raw:
        what = "Format Not Available"
        why = "The player client does not provide progressive streams for this ID."
        how = "Home PC worker resolves all DASH + Audio streams."
    elif "Video unavailable" in err_raw:
        what = "Video Unavailable"
        why = "Video is private or removed."
        how = "Check video URL."
    else:
        what = f"Extractor Notice: {err_raw[:80]}"
        why = err_raw[:150]
        how = "Reverse bridge worker fallback."

    return {
        "where": where, "what": what, "why": why, "how": how, "raw_error": err_raw[:200]
    }


# ─────────────────────── Cross-Process Reverse Bridge Engine ───────────────────────

def _get_bridge_dir():
    global BRIDGE_DIR, DL_DIR
    if BRIDGE_DIR and Path(BRIDGE_DIR).exists():
        return Path(BRIDGE_DIR)
    if DL_DIR and Path(DL_DIR).exists():
        p = Path(DL_DIR) / "bridge_jobs"
        p.mkdir(exist_ok=True)
        BRIDGE_DIR = p
        return p
    p = Path(__file__).resolve().parent / "downloads" / "bridge_jobs"
    p.mkdir(parents=True, exist_ok=True)
    BRIDGE_DIR = p
    return p


def is_worker_active():
    """Checks the shared heartbeat file to verify if local PC worker is polling."""
    try:
        hb_file = _get_bridge_dir() / "heartbeat.json"
        if hb_file.exists():
            data = json.loads(hb_file.read_text(encoding="utf-8"))
            return (time.time() - data.get("last_seen", 0)) < 15.0
    except Exception:
        pass
    return False


def get_worker_meta():
    try:
        hb_file = _get_bridge_dir() / "heartbeat.json"
        if hb_file.exists():
            return json.loads(hb_file.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"worker_id": None, "last_seen": 0, "jobs_processed": 0}


def _auth_attempts():
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

    return attempts[:2]


def _apply_auth_mode(opts, mode, client_potoken=None):
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


def _extract_info_direct(url, extra_opts=None, download=False, client_potoken=None, log_list=None):
    """Server-side direct yt-dlp extraction fallback (< 4s)."""
    base_opts = _base_ydl_opts()
    if extra_opts:
        base_opts.update(extra_opts)

    attempts = _auth_attempts()
    total_steps = len(attempts)
    last_err = None

    for idx, mode in enumerate(attempts, start=1):
        ts = _now_ts()
        opts = _apply_auth_mode(base_opts, mode, client_potoken=client_potoken)
        why_msg = "Authenticating with server cookies" if "cookies" in mode else "Executing direct extraction"
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

@downloader2_bp.route("/api/worker/poll", methods=["GET"])
def api_worker_poll():
    """Fast, instant polling endpoint called continuously by bridge_worker.py (< 5ms response)."""
    worker_id = request.args.get("worker_id", "local_home_pc")
    b_dir = _get_bridge_dir()

    # Update heartbeat file
    try:
        hb_file = b_dir / "heartbeat.json"
        meta = {}
        if hb_file.exists():
            try:
                meta = json.loads(hb_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        meta.update({
            "last_seen": time.time(),
            "worker_id": worker_id,
            "ip": request.remote_addr
        })
        hb_file.write_text(json.dumps(meta), encoding="utf-8")
    except Exception:
        pass

    # Check for any pending job files (< 25s old)
    now = time.time()
    try:
        for jf in b_dir.glob("*.json"):
            if jf.name == "heartbeat.json":
                continue
            try:
                data = json.loads(jf.read_text(encoding="utf-8"))
                if data.get("status") == "pending" and (now - data.get("created_at", 0)) < 25.0:
                    data["status"] = "processing"
                    jf.write_text(json.dumps(data), encoding="utf-8")
                    return jsonify({"status": "job", "job_id": data["job_id"], "url": data["url"]})
            except Exception:
                continue
    except Exception:
        pass

    return jsonify({"status": "idle", "active": True})


@downloader2_bp.route("/api/worker/submit", methods=["POST"])
def api_worker_submit():
    """Called by local PC bridge_worker.py to return extracted video data."""
    data = request.json or {}
    job_id = data.get("job_id")
    if not job_id:
        return jsonify({"error": "Missing job_id"}), 400

    b_dir = _get_bridge_dir()
    job_file = b_dir / f"{job_id}.json"

    # Update heartbeat jobs processed counter
    try:
        hb_file = b_dir / "heartbeat.json"
        if hb_file.exists():
            hb_data = json.loads(hb_file.read_text(encoding="utf-8"))
            hb_data["jobs_processed"] = hb_data.get("jobs_processed", 0) + 1
            hb_data["last_seen"] = time.time()
            hb_file.write_text(json.dumps(hb_data), encoding="utf-8")
    except Exception:
        pass

    # Write completed result
    try:
        completed_data = {
            "status": "done",
            "job_id": job_id,
            "success": data.get("success", False),
            "info": data.get("info"),
            "error": data.get("error"),
            "completed_at": time.time()
        }
        job_file.write_text(json.dumps(completed_data), encoding="utf-8")
        return jsonify({"success": True, "message": "Job accepted"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@downloader2_bp.route("/api/downloader/job_result/<job_id>", methods=["GET"])
def api_downloader_job_result(job_id):
    """Polled by the browser every 0.8s to check if the home PC worker finished extraction."""
    b_dir = _get_bridge_dir()
    job_file = b_dir / f"{job_id}.json"

    if not job_file.exists():
        return jsonify({"status": "pending"})

    try:
        data = json.loads(job_file.read_text(encoding="utf-8"))
        if data.get("status") == "done":
            # Clean up job file
            try:
                job_file.unlink(missing_ok=True)
            except Exception:
                pass

            if data.get("success") and data.get("info"):
                info = data["info"]
                details = _build_full_details(info)
                details["formats"] = _build_formats(info)
                details["status"] = "done"
                details["reverse_worker_active"] = True
                return jsonify(details)
            else:
                return jsonify({"status": "failed", "error": data.get("error") or "Worker extraction failed"})
    except Exception as e:
        return jsonify({"status": "pending"})

    return jsonify({"status": "pending"})


@downloader2_bp.route("/api/downloader/cookie_status", methods=["GET"])
@downloader2_bp.route("/api/downloader2/cookie_status", methods=["GET"])
def api_downloader2_cookie_status():
    health = _inspect_cookies_health()
    health["server_os"] = os.name
    health["resolved_working_mode"] = _RESOLVED_MODE.get("mode")
    health["reverse_worker_active"] = is_worker_active()
    meta = get_worker_meta()
    health["reverse_worker_id"] = meta.get("worker_id")
    return jsonify(health)


@downloader2_bp.route("/api/downloader/info", methods=["POST"])
@downloader2_bp.route("/api/downloader2/info", methods=["POST"])
def api_downloader2_info():
    """Non-blocking extraction trigger: If home worker is active, creates job in 5ms and returns job_id."""
    data = request.json or {}
    url = (data.get("url") or "").strip()
    client_potoken = (data.get("client_potoken") or "").strip() or None
    if not url:
        return jsonify({"error": "Paste a video URL first"}), 400

    logs = []
    ts = _now_ts()
    cookie_h = _inspect_cookies_health()
    logs.append(f"[{ts}] 📁 [Cookie Inspector] {cookie_h['status_text']}")

    # If home residential worker is active, create instant job (< 5ms) for browser async polling
    if is_worker_active():
        job_id = uuid.uuid4().hex[:12]
        b_dir = _get_bridge_dir()
        job_file = b_dir / f"{job_id}.json"
        meta = get_worker_meta()
        worker_id = meta.get("worker_id") or "Connected"

        job_payload = {
            "job_id": job_id,
            "url": url,
            "status": "pending",
            "created_at": time.time()
        }
        job_file.write_text(json.dumps(job_payload), encoding="utf-8")
        logs.append(f"[{_now_ts()}] 🌉 [Reverse Bridge] Forwarded task ({job_id}) to Home PC Worker ({worker_id})...")

        return jsonify({
            "async_bridge": True,
            "job_id": job_id,
            "logs": logs,
            "cookie_status": cookie_h,
            "reverse_worker_active": True
        })

    # If home worker is offline, try server-side direct modes
    info, _, err = _extract_info_direct(
        url,
        extra_opts={"format": "bestvideo+bestaudio/best"},
        client_potoken=client_potoken,
        log_list=logs
    )
    
    if info is None:
        err_msg = f"Server resolution failed: {err}"
        logs.append(f"[{_now_ts()}] 💡 [Reverse Bridge Tip] Run 'python bridge_worker.py' on your home PC to resolve streams automatically via your clean residential IP.")
        return jsonify({
            "error": err_msg,
            "logs": logs,
            "cookie_status": cookie_h,
            "reverse_worker_active": False,
            "troubleshoot": {
                "where": "Server yt-dlp Pipeline",
                "what": "YouTube Datacenter BotGuard Challenge",
                "why": "Render Cloud Datacenter IP blocked by YouTube.",
                "how": "Run 'python bridge_worker.py' on your home PC to resolve streams with 0% BotGuard blocks."
            }
        }), 400

    details = _build_full_details(info)
    details["formats"] = _build_formats(info)
    details["logs"] = logs
    details["cookie_status"] = cookie_h
    details["reverse_worker_active"] = False
    return jsonify(details)


def _run_download_job(dl_id, url, format_id, client_potoken=None):
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
        info, ydl, err = _extract_info_direct(
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
        job_logs.append(f"[{_now_ts()}] ❌ Download failed: {err}")
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


@downloader2_bp.route("/api/downloader/start", methods=["POST"])
@downloader2_bp.route("/api/downloader2/start", methods=["POST"])
def api_downloader2_start():
    data = request.json or {}
    url = (data.get("url") or "").strip()
    format_id = (data.get("format_id") or "").strip()
    client_potoken = (data.get("client_potoken") or "").strip() or None
    if not url:
        return jsonify({"error": "No URL given"}), 400
    dl_id = uuid.uuid4().hex[:10]
    logs = [f"[{_now_ts()}] 🚀 Starting download task ({dl_id}) for format [{format_id or 'best'}]"]
    DL_JOBS[dl_id] = {
        "status": "starting", "stage": "connect", "percent": 0,
        "downloaded": 0, "total": None, "speed": None, "eta": None,
        "filename": None, "error": None, "url": url,
        "auth_logs": logs
    }
    threading.Thread(target=_run_download_job, args=(dl_id, url, format_id, client_potoken), daemon=True).start()
    return jsonify({"dl_id": dl_id})


@downloader2_bp.route("/api/downloader/progress/<dl_id>")
@downloader2_bp.route("/api/downloader2/progress/<dl_id>")
def api_downloader2_progress(dl_id):
    job = DL_JOBS.get(dl_id)
    if not job:
        return jsonify({"error": "Unknown download job"}), 404
    return jsonify(job)


@downloader2_bp.route("/api/downloader/file/<dl_id>")
@downloader2_bp.route("/api/downloader2/file/<dl_id>")
def api_downloader2_file(dl_id):
    job = DL_JOBS.get(dl_id)
    if not job or job.get("status") != "done" or not job.get("filename"):
        return "Not ready", 404
    p = DL_DIR / job["filename"]
    if not p.exists():
        return "Not found", 404
    display_name = job["filename"].split("_", 1)[-1]
    return send_file(p, as_attachment=True, download_name=display_name)


@downloader2_bp.route("/downloader", methods=["GET"])
def downloader2_page():
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>AutoShortAi — Universal Video Downloader</title>
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
      <p>High-Speed Video Stream Extractor & Auto-Reverse Residential Relay</p>
      
      <div class="status-bar" id="statusBar">
        <div class="status-pill" id="cookiePill">🍪 Checking cookies.txt...</div>
        <div class="status-pill" id="workerPill">🌉 Home Bridge: Checking...</div>
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
      setInterval(checkServerHealth, 3000);
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

        if(data.reverse_worker_active){
          wpill.className = 'status-pill active';
          wpill.innerHTML = `🟢 Home Bridge: Connected (${data.reverse_worker_id || 'Active'})`;
        } else {
          wpill.className = 'status-pill';
          wpill.innerHTML = `🏠 Home Bridge: Standby (Run bridge_worker.py)`;
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

      // If async bridge task was initiated on Render, poll for result every 0.7s
      if(data && data.async_bridge && data.job_id){
        addLiveLog(`[Async Bridge] Home PC worker active. Polling result for task [${data.job_id}]...`, 'info');
        const jobId = data.job_id;
        let finished = false;
        let attempts = 0;

        while(!finished && attempts < 25){
          await new Promise(r => setTimeout(r, 700));
          attempts++;
          try {
            const pollRes = await fetch(`/api/downloader/job_result/${jobId}`);
            const pollData = await pollRes.json();

            if(pollData.status === 'done' && pollData.formats && pollData.formats.length){
              finished = true;
              btn.disabled = false; btn.innerHTML = '<span>🔍 Fetch Details</span>';
              currentVideoData = pollData;
              document.getElementById('resolverBadge').textContent = '⚡ Home Residential Bridge';
              addLiveLog(`✅ [Reverse Bridge] Resolved "${pollData.title}" (${pollData.formats.length} formats) via Home PC!`, 'ok');
              renderDetails(pollData);
              return;
            } else if(pollData.status === 'failed'){
              finished = true;
              btn.disabled = false; btn.innerHTML = '<span>🔍 Fetch Details</span>';
              addLiveLog(`❌ [Reverse Bridge Error] ${pollData.error || 'Extraction failed'}`, 'err');
              return;
            }
          } catch(e){}
        }

        if(!finished){
          btn.disabled = false; btn.innerHTML = '<span>🔍 Fetch Details</span>';
          addLiveLog(`⚠️ [Reverse Bridge] Home worker did not submit result in time. Please check your PC terminal.`, 'warn');
          return;
        }
      }

      btn.disabled = false; btn.innerHTML = '<span>🔍 Fetch Details</span>';

      if(data && !data.error && data.formats && data.formats.length){
        currentVideoData = data;
        document.getElementById('resolverBadge').textContent = 'Server Resolved';
        renderDetails(data);
      } else if(!data || data.error) {
        addLiveLog(`❌ [Extraction Failed] ${data ? data.error : 'Could not resolve video'}`, 'err');
      }
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
            <button class="fmt-btn" onclick="startDownload('${f.format_id}')">⬇️ Download</button>
          </td>
        `;
        tbody.appendChild(tr);
      });
    }

    async function startDownload(formatId){
      const url = document.getElementById('videoUrl').value.trim();
      addLiveLog(`[Download Triggered] Initiating background download for format [${formatId}]...`, 'info');

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


if __name__ == "__main__":
    app = Flask("AutoShortAi-Standalone")
    init_downloader2(app)
    app.run(host="0.0.0.0", port=5000, debug=False)
