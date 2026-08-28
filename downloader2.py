#!/usr/bin/env python3
# -*- coding: utf-8 -*-
""" 
AutoShortAi — Downloader2 Module with Dual Extraction Engine (Home Bridge + Server PO-Token)
──────────────────────────────────────────────────────────────────────────────────────────
Features:
    1. Dual Button Extraction:
       - ⚡ Fetch via Home Bridge (0-Bandwidth Residential Stream URL Dispatcher)
       - 🛡️ Fetch via Server PO-Token (Cloud Server Engine)
    2. Zero-Bandwidth Home Bridge: Resolves direct Google CDN stream URLs in ~1.5s.
    3. Direct High-Speed Download & Cloud FFmpeg Lossless Merge.
    4. Real-time Live Pipeline Diagnostics (Why / Where / What / How).
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

from flask import Blueprint, request, jsonify, send_file, Flask, redirect

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

    OAUTH2_TOKEN_FILE = base_dir / "youtube_oauth2.json"


def _no_console_kwargs():
    """Suppresses flashing console windows on Windows."""
    kwargs = {}
    if os.name == "nt":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        kwargs["startupinfo"] = si
        kwargs["creationflags"] = 0x08000000
    return kwargs


def _now_ts():
    return datetime.now().strftime("%H:%M:%S")


def _fmt_size(num_bytes):
    if not num_bytes:
        return None
    for unit in ["B", "KB", "MB", "GB"]:
        if num_bytes < 1024:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} TB"


def _fmt_duration(seconds):
    if not seconds:
        return "N/A"
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _fmt_int(n):
    if n is None:
        return "N/A"
    try:
        return f"{int(n):,}"
    except (ValueError, TypeError):
        return str(n)


def _fmt_upload_date(d):
    if not d:
        return "N/A"
    d = str(d)
    if len(d) == 8:
        return f"{d[:4]}-{d[4:6]}-{d[6:]}"
    return d


def _get_cookies_file():
    if COOKIES_FILE and COOKIES_FILE.exists() and COOKIES_FILE.stat().st_size > 10:
        return COOKIES_FILE
    return None


def _inspect_cookies_health():
    """Checks cookies.txt health and YouTube session validity."""
    cfile = _get_cookies_file()
    if not cfile:
        return {
            "found": False,
            "path": None,
            "size_bytes": 0,
            "size_str": "0 B",
            "has_youtube_cookies": False,
            "has_full_youtube_auth": False,
            "status_text": "Not Found"
        }

    size = cfile.stat().st_size
    txt = ""
    try:
        txt = cfile.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        pass

    has_yt = (".youtube.com" in txt) or ("youtube.com" in txt)
    has_sid = ("SID" in txt)
    has_psid = ("__Secure-3PSID" in txt or "3PSID" in txt)
    has_login = has_sid and has_psid

    if has_login:
        status_text = f"🟢 Active ({_fmt_size(size)}) • Full YouTube Auth"
    elif has_yt:
        status_text = f"🟡 Loaded ({_fmt_size(size)}) • Basic YouTube Cookies"
    else:
        status_text = f"🟠 Generic Cookies ({_fmt_size(size)})"

    return {
        "found": True,
        "path": str(cfile),
        "size_bytes": size,
        "size_str": _fmt_size(size),
        "has_youtube_cookies": has_yt,
        "has_full_youtube_auth": has_login,
        "status_text": status_text
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
    """Checks heartbeat file to verify if home PC worker is polling."""
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


# ─────────────────────── Direct Extraction Engine (Server Fallback) ───────────────────────

def _build_ydl_opts(mode, extra_opts=None, client_potoken=None):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "geo_bypass": True,
        "socket_timeout": 15,
        "retries": 1,
        "extractor_retries": 1,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
    }
    if FFMPEG_PATH:
        opts["ffmpeg_location"] = FFMPEG_PATH

    cfile = _get_cookies_file()
    if "cookies" in mode and cfile:
        opts["cookiefile"] = str(cfile)

    if mode == "cookies_android_vr":
        opts["extractor_args"] = {"youtube": {"player_client": ["android_vr", "web"]}}
    elif mode == "cookies_ios":
        opts["extractor_args"] = {"youtube": {"player_client": ["ios", "web"]}}
    elif mode == "potoken" or client_potoken:
        if client_potoken:
            opts["extractor_args"] = {"youtube": {"po_token": [f"web+{client_potoken}"]}}

    if extra_opts:
        opts.update(extra_opts)
    return opts


def _extract_info_direct(url, extra_opts=None, download=False, client_potoken=None, log_list=None):
    modes = ["cookies_default", "cookies_android_vr", "potoken"]
    last_err = None

    for i, mode in enumerate(modes):
        ts = _now_ts()
        if log_list is not None:
            log_list.append(f"[{ts}] 🔄 [Server Step {i+1}/{len(modes)}] Mode: '{mode}'...")

        opts = _build_ydl_opts(mode, extra_opts=extra_opts, client_potoken=client_potoken)
        _orig_popen = subprocess.Popen
        def _quiet_popen(*args, **kwargs):
            kwargs.update(_no_console_kwargs())
            return _orig_popen(*args, **kwargs)
        subprocess.Popen = _quiet_popen

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=download)
                if log_list is not None:
                    log_list.append(f"[{_now_ts()}] ✅ [Server Step {i+1}] Succeeded with mode '{mode}'!")
                return info, ydl, None
        except Exception as e:
            last_err = str(e)
            if log_list is not None:
                log_list.append(f"[{_now_ts()}] ❌ [Server Step {i+1}] Mode '{mode}' failed: {last_err[:120]}")
        finally:
            subprocess.Popen = _orig_popen

    return None, None, last_err


# ─────────────────────────── Formats & Details Formatter ───────────────────────────

def _build_formats(info):
    """Builds clean, high-definition Video+Audio and Audio options."""
    formats_out = []
    seen_heights = set()

    for f in info.get("formats", []) or []:
        h = f.get("height")
        ext = f.get("ext") or "mp4"
        vcodec = f.get("vcodec")
        acodec = f.get("acodec")
        size = f.get("filesize") or f.get("filesize_approx")

        if h and h >= 144 and h not in seen_heights:
            seen_heights.add(h)
            
            if h >= 1080:
                quality_label = f"1080p Full HD"
            elif h >= 720:
                quality_label = f"720p HD"
            elif h >= 480:
                quality_label = f"480p SD"
            elif h >= 360:
                quality_label = f"360p Mobile"
            else:
                quality_label = f"{h}p"

            formats_out.append({
                "format_id": str(f.get("format_id")),
                "stream_url": f.get("url"),
                "ext": "mp4",
                "kind": "🎬 Video + Audio (MP4)",
                "label": quality_label,
                "height": h,
                "filesize_str": _fmt_size(size) or "~Direct Stream",
                "has_video": True,
                "has_audio": True
            })

    # Always ensure High Quality Audio is listed
    formats_out.append({
        "format_id": "bestaudio",
        "stream_url": (next((f.get("url") for f in info.get("formats", []) if f.get("acodec") not in (None, "none") and f.get("url")), None)),
        "ext": "mp3",
        "kind": "🎵 Audio Only (MP3)",
        "label": "High Quality 320kbps Audio",
        "height": 0,
        "filesize_str": "~Audio Stream",
        "has_video": False,
        "has_audio": True
    })

    formats_out.sort(key=lambda x: -(x.get("height") or 0))
    return formats_out


def _build_full_details(info):
    best_h = max([f.get("height") or 0 for f in info.get("formats", []) or []], default=720)
    return {
        "id": info.get("id"),
        "title": info.get("title"),
        "description": info.get("description"),
        "uploader": info.get("uploader") or info.get("channel"),
        "channel": info.get("channel"),
        "channel_id": info.get("channel_id"),
        "duration": info.get("duration"),
        "duration_str": _fmt_duration(info.get("duration")),
        "view_count": info.get("view_count"),
        "view_count_str": _fmt_int(info.get("view_count")),
        "like_count": info.get("like_count"),
        "like_count_str": _fmt_int(info.get("like_count")),
        "upload_date": info.get("upload_date"),
        "upload_date_str": _fmt_upload_date(info.get("upload_date")),
        "webpage_url": info.get("webpage_url"),
        "original_url": info.get("original_url"),
        "thumbnail": info.get("thumbnail"),
        "resolution": f"{best_h}p HD" if best_h else "HD",
        "format_count": len(info.get("formats") or []),
    }


# ───────────────────────────── Routes ─────────────────────────────

@downloader2_bp.route("/api/worker/poll", methods=["GET"])
def api_worker_poll():
    """Instant polling endpoint called continuously by bridge_worker.py (< 5ms response)."""
    worker_id = request.args.get("worker_id", "local_home_pc")
    b_dir = _get_bridge_dir()

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

    now = time.time()
    try:
        for jf in b_dir.glob("*.json"):
            if jf.name == "heartbeat.json":
                continue
            try:
                data = json.loads(jf.read_text(encoding="utf-8"))
                if data.get("status") == "pending" and (now - data.get("created_at", 0)) < 60.0:
                    data["status"] = "processing"
                    jf.write_text(json.dumps(data), encoding="utf-8")
                    return jsonify({
                        "status": "job",
                        "job_id": data["job_id"],
                        "type": data.get("type", "info"),
                        "url": data.get("url"),
                        "format_id": data.get("format_id")
                    })
            except Exception:
                continue
    except Exception:
        pass

    return jsonify({"status": "idle", "active": True})


@downloader2_bp.route("/api/worker/submit", methods=["POST"])
def api_worker_submit():
    """Called by local PC bridge_worker.py to return resolved stream URLs (< 25 KB)."""
    data = request.json or {}
    job_id = data.get("job_id")
    if not job_id:
        return jsonify({"error": "Missing job_id"}), 400

    b_dir = _get_bridge_dir()
    job_file = b_dir / f"{job_id}.json"

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
    """Polled by the browser every 0.7s to receive resolved stream URLs from home worker."""
    b_dir = _get_bridge_dir()
    job_file = b_dir / f"{job_id}.json"

    if not job_file.exists():
        return jsonify({"status": "pending"})

    try:
        data = json.loads(job_file.read_text(encoding="utf-8"))
        if data.get("status") == "done":
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
    except Exception:
        return jsonify({"status": "pending"})

    return jsonify({"status": "pending"})


@downloader2_bp.route("/api/downloader/cookie_status", methods=["GET"])
def api_downloader_cookie_status():
    health = _inspect_cookies_health()
    health["reverse_worker_active"] = is_worker_active()
    meta = get_worker_meta()
    health["reverse_worker_id"] = meta.get("worker_id")
    return jsonify(health)


@downloader2_bp.route("/api/downloader/info", methods=["POST"])
def api_downloader_info():
    """Dual Engine Extraction Route: Dispatches to Home Bridge or Server PO-Token."""
    data = request.json or {}
    url = (data.get("url") or "").strip()
    engine = data.get("engine", "bridge")  # "bridge" or "potoken"
    client_potoken = (data.get("client_potoken") or "").strip() or None

    if not url:
        return jsonify({"error": "Paste a video URL first"}), 400

    logs = []
    ts = _now_ts()
    cookie_h = _inspect_cookies_health()
    logs.append(f"[{ts}] 📁 [Cookie Inspector] {cookie_h['status_text']}")

    # 1. HOME BRIDGE ENGINE
    if engine == "bridge":
        if not is_worker_active():
            logs.append(f"[{_now_ts()}] ⚠️ [Home Bridge] Worker not connected. Run 'python bridge_worker.py' on your PC.")
            return jsonify({
                "error": "Home Bridge Worker is offline. Run 'python bridge_worker.py' on your PC or use the '🛡️ Fetch via Server PO-Token' button.",
                "logs": logs,
                "cookie_status": cookie_h,
                "reverse_worker_active": False
            }), 400

        job_id = uuid.uuid4().hex[:12]
        b_dir = _get_bridge_dir()
        job_file = b_dir / f"{job_id}.json"
        meta = get_worker_meta()
        worker_id = meta.get("worker_id") or "Connected"

        job_payload = {
            "job_id": job_id,
            "type": "info",
            "url": url,
            "status": "pending",
            "created_at": time.time()
        }
        job_file.write_text(json.dumps(job_payload), encoding="utf-8")
        logs.append(f"[{_now_ts()}] ⚡ [Engine: Home Bridge] Forwarded task ({job_id}) to Home PC Worker ({worker_id})...")

        return jsonify({
            "async_bridge": True,
            "job_id": job_id,
            "logs": logs,
            "engine": "bridge",
            "cookie_status": cookie_h,
            "reverse_worker_active": True
        })

    # 2. SERVER PO-TOKEN ENGINE
    logs.append(f"[{_now_ts()}] 🛡️ [Engine: Server PO-Token] Extracting directly on Render Cloud...")
    info, _, err = _extract_info_direct(
        url,
        extra_opts={"format": "bestvideo+bestaudio/best"},
        client_potoken=client_potoken,
        log_list=logs
    )

    if info is None:
        err_msg = f"Server resolution failed: {err}"
        logs.append(f"[{_now_ts()}] 💡 [Tip] YouTube BotGuard challenged Server IP. Click '⚡ Fetch via Home Bridge' with bridge_worker.py running on your PC.")
        return jsonify({
            "error": err_msg,
            "logs": logs,
            "engine": "potoken",
            "cookie_status": cookie_h,
            "reverse_worker_active": False,
            "troubleshoot": {
                "where": "Server Cloud Engine",
                "what": "YouTube Datacenter BotGuard Challenge",
                "why": "Render Datacenter IP blocked by YouTube.",
                "how": "Click '⚡ Fetch via Home Bridge' for instant residential extraction."
            }
        }), 400

    details = _build_full_details(info)
    details["formats"] = _build_formats(info)
    details["logs"] = logs
    details["engine"] = "potoken"
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
                "percent": round(downloaded * 100 / total, 1) if total else 50,
                "speed": speed,
                "speed_str": (_fmt_size(speed) + "/s") if speed else None,
                "eta": eta,
            })
        elif status == "finished":
            job["status"] = "processing"
            job["stage"] = "merge"
            job_logs.append(f"[{_now_ts()}] 🔀 [Cloud Merger] Packaging video & audio tracks losslessly via FFmpeg...")

    extra_opts = {
        "format": format_id or "best",
        "outtmpl": str(DL_DIR / f"{dl_id}_%(title).60s.%(ext)s"),
        "progress_hooks": [hook],
        "merge_output_format": "mp4",
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "mweb"]
            }
        }
    }

    info, ydl, err = _extract_info_direct(
        url,
        extra_opts=extra_opts,
        download=True,
        client_potoken=client_potoken,
        log_list=job_logs
    )

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
def api_downloader_start():
    data = request.json or {}
    url = (data.get("url") or "").strip()
    format_id = (data.get("format_id") or "").strip()
    stream_url = (data.get("stream_url") or "").strip()

    if not url:
        return jsonify({"error": "No URL given"}), 400

    # If direct stream URL is present and user wants direct stream download
    if stream_url and stream_url.startswith("http"):
        return jsonify({"direct_stream": True, "stream_url": stream_url})

    dl_id = uuid.uuid4().hex[:10]
    logs = [f"[{_now_ts()}] 🚀 [Cloud Stream Pipe] Initiating task ({dl_id}) for format [{format_id or 'best'}]"]
    DL_JOBS[dl_id] = {
        "status": "starting", "stage": "connect", "percent": 0,
        "downloaded": 0, "total": None, "speed": None, "eta": None,
        "filename": None, "error": None, "url": url,
        "auth_logs": logs
    }
    threading.Thread(target=_run_download_job, args=(dl_id, url, format_id), daemon=True).start()
    return jsonify({"dl_id": dl_id})


@downloader2_bp.route("/api/downloader/progress/<dl_id>")
def api_downloader_progress(dl_id):
    job = DL_JOBS.get(dl_id)
    if not job:
        return jsonify({"error": "Unknown download job"}), 404
    return jsonify(job)


@downloader2_bp.route("/api/downloader/file/<dl_id>")
def api_downloader_file(dl_id):
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
  <title>AutoShortAi — Dual Hybrid Video Downloader</title>
  <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg: #070b14;
      --card-bg: #0f172a;
      --accent: #6366f1;
      --accent-hover: #4f46e5;
      --accent-glow: rgba(99, 102, 241, 0.35);
      --cyan: #06b6d4;
      --cyan-hover: #0891b2;
      --purple: #8b5cf6;
      --purple-hover: #7c3aed;
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
    
    .input-wrap { display: flex; flex-direction: column; gap: 12px; }
    input[type="text"] { width: 100%; padding: 14px 18px; background: #060911; border: 1px solid var(--border-light); border-radius: 10px; color: var(--text); font-size: 15px; outline: none; transition: 0.2s; }
    input[type="text"]:focus { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-glow); }
    
    .btn-group { display: flex; gap: 10px; flex-wrap: wrap; }
    .btn { flex: 1; min-width: 200px; padding: 14px 20px; border: none; border-radius: 10px; font-weight: 700; cursor: pointer; transition: 0.2s; display: inline-flex; align-items: center; justify-content: center; gap: 8px; font-size: 14px; }
    .btn:disabled { opacity: 0.6; cursor: not-allowed; transform: none; }
    
    .btn-bridge { background: linear-gradient(135deg, #0284c7, #06b6d4); color: white; box-shadow: 0 4px 14px rgba(6, 182, 212, 0.25); }
    .btn-bridge:hover { background: linear-gradient(135deg, #0369a1, #0891b2); transform: translateY(-1px); }
    
    .btn-potoken { background: linear-gradient(135deg, #6d28d9, #8b5cf6); color: white; box-shadow: 0 4px 14px rgba(139, 92, 246, 0.25); }
    .btn-potoken:hover { background: linear-gradient(135deg, #5b21b6, #7c3aed); transform: translateY(-1px); }
    
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
    .fmt-btn { padding: 8px 16px; font-size: 13px; border-radius: 8px; background: linear-gradient(135deg, #4f46e5, #6366f1); border: none; color: white; cursor: pointer; font-weight: 700; transition: 0.2s; }
    .fmt-btn:hover { transform: translateY(-1px); box-shadow: 0 4px 12px rgba(99, 102, 241, 0.4); }
    .hidden { display: none !important; }
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <h1>Universal Video Downloader</h1>
      <p>Dual Ingestion Engine: Residential Stream Bridge + Server PO-Token Fallback</p>
      
      <div class="status-bar" id="statusBar">
        <div class="status-pill" id="cookiePill">🍪 Checking cookies.txt...</div>
        <div class="status-pill" id="workerPill">🌉 Home Bridge: Checking...</div>
      </div>
    </div>

    <div class="card">
      <div class="input-wrap">
        <input type="text" id="videoUrl" placeholder="Paste YouTube, Shorts, Twitter, Instagram URL..." value="https://www.youtube.com/watch?v=re0WlNMOfFU">
        <div class="btn-group">
          <button class="btn btn-bridge" id="fetchBridgeBtn" onclick="fetchVideoInfo('bridge')">
            <span>⚡ Fetch via Home Bridge</span>
          </button>
          <button class="btn btn-potoken" id="fetchPoBtn" onclick="fetchVideoInfo('potoken')">
            <span>🛡️ Fetch via Server PO-Token</span>
          </button>
        </div>
      </div>

      <!-- Live Diagnostics Pipeline Box -->
      <div class="live-status-box" id="liveStatusBox">
        <div class="live-status-head">
          <span>📡 Live Diagnostics & Stream Pipeline (Why / Where / What / How)</span>
          <button onclick="clearLiveLogs()" style="background:none; border:none; color:var(--dim); cursor:pointer; font-size:11px;">🧹 Clear</button>
        </div>
        <div class="live-stream" id="liveStream">
          <div class="stream-line stream-info">[Ready] Select an extraction engine above to resolve video streams.</div>
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
        <span id="resolverBadge" style="font-size:12px; padding:4px 10px; background:rgba(99,102,241,0.2); color:#c7d2fe; border-radius:6px; font-weight:600;">Resolved</span>
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

    async function fetchVideoInfo(engine='bridge'){
      const url = document.getElementById('videoUrl').value.trim();
      if(!url){ alert('Paste a URL first'); return; }
      
      const bBtn = document.getElementById('fetchBridgeBtn');
      const pBtn = document.getElementById('fetchPoBtn');
      bBtn.disabled = true; pBtn.disabled = true;

      const engineName = engine === 'bridge' ? 'Home Residential Bridge' : 'Server PO-Token Engine';
      addLiveLog(`[Fetch Request] Starting extraction via ${engineName} for: ${url}`, 'info');

      let res, data;
      try {
        res = await fetch('/api/downloader/info', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({url, engine})
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

        while(!finished && attempts < 45){
          await new Promise(r => setTimeout(r, 700));
          attempts++;
          try {
            const pollRes = await fetch(`/api/downloader/job_result/${jobId}`);
            const pollData = await pollRes.json();

            if(pollData.status === 'done' && pollData.formats && pollData.formats.length){
              finished = true;
              bBtn.disabled = false; pBtn.disabled = false;
              currentVideoData = pollData;
              document.getElementById('resolverBadge').textContent = '⚡ Resolved via Home Bridge';
              addLiveLog(`✅ [Home Bridge] Resolved "${pollData.title}" (${pollData.formats.length} formats) in ${attempts * 0.7}s!`, 'ok');
              renderDetails(pollData);
              return;
            } else if(pollData.status === 'failed'){
              finished = true;
              bBtn.disabled = false; pBtn.disabled = false;
              addLiveLog(`❌ [Home Bridge Error] ${pollData.error || 'Extraction failed'}`, 'err');
              return;
            }
          } catch(e){}
        }

        if(!finished){
          bBtn.disabled = false; pBtn.disabled = false;
          addLiveLog(`⚠️ [Home Bridge] Home worker did not submit result in time. Please check your PC terminal.`, 'warn');
          return;
        }
      }

      bBtn.disabled = false; pBtn.disabled = false;

      if(data && !data.error && data.formats && data.formats.length){
        currentVideoData = data;
        document.getElementById('resolverBadge').textContent = '🛡️ Resolved via Server PO-Token';
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
            <button class="fmt-btn" onclick="startDownload('${f.format_id}', '${f.stream_url || ''}')">⬇️ Download</button>
          </td>
        `;
        tbody.appendChild(tr);
      });
    }

    async function startDownload(formatId, streamUrl=''){
      const url = document.getElementById('videoUrl').value.trim();
      addLiveLog(`[Download Triggered] Initiating stream download for format [${formatId}]...`, 'info');

      const wrap = document.getElementById('progWrap');
      const bar = document.getElementById('progBar');
      const txt = document.getElementById('progText');
      const statusSpan = document.getElementById('progStatus');
      const metricsSpan = document.getElementById('progMetrics');
      
      wrap.style.display = 'block';
      txt.classList.remove('hidden');
      bar.style.width = '20%';
      statusSpan.textContent = 'Preparing download stream...';

      let res, data;
      try {
        res = await fetch('/api/downloader/start', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({url, format_id: formatId, stream_url: streamUrl})
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

      if(data.direct_stream && data.stream_url){
        bar.style.width = '100%';
        statusSpan.textContent = '✅ Direct Stream Link Ready!';
        addLiveLog(`🚀 [Direct Stream] Opening direct high-speed Google CDN stream...`, 'ok');
        window.open(data.stream_url, '_blank');
        return;
      }

      const dlId = data.dl_id;
      addLiveLog(`📥 [Cloud Task Created] ID: ${dlId}. Merging and preparing stream...`, 'ok');

      let done = false;
      while(!done){
        await new Promise(r => setTimeout(r, 1000));
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
          bar.style.width = `${Math.max(20, sd.percent)}%`;
          statusSpan.textContent = sd.stage === 'merge' ? 'Packaging video & audio tracks...' : `Processing (${sd.percent}%)`;
          metricsSpan.textContent = `${sd.downloaded_str || ''} / ${sd.total_str || ''} • ${sd.speed_str || ''}`;
        }

        if(sd.status === 'done'){
          done = true;
          bar.style.width = '100%';
          statusSpan.textContent = '✅ Complete! Delivering file...';
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
