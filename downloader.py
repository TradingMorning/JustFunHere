#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AutoShortAi — Downloader module
────────────────────────────────
A fully self-contained "download any video" feature (YouTube, Instagram,
TikTok, X/Twitter, Facebook, Vimeo & 1000+ more sites via yt-dlp), shipped
as its OWN file and wired into the main app as a Flask Blueprint — so it
runs in the exact same process, on the exact same port. Nothing extra to
start, nothing to configure separately.

Wire it into the main app with:

    from downloader import downloader_bp, init_downloader
    init_downloader(BASE, FFMPEG)
    app.register_blueprint(downloader_bp)

Routes exposed (all under /api/downloader/...):
    POST /api/downloader/info               -> full metadata + formats (no download)
    POST /api/downloader/start               -> kicks off a background download
    GET  /api/downloader/progress/<dl_id>    -> live percent/speed/eta/size
    GET  /api/downloader/file/<dl_id>        -> serves the finished file

SPEED FIX — smart auth caching:
    Resolving a video normally means trying several auth methods in order
    (a cookies.txt file, then each installed browser's cookies, then plain,
    then a bypass mode) until one works. Doing that fresh on EVERY request
    (once for "fetch details", again for "download") is slow and is a bad
    experience. This module remembers, per process, which mode last
    succeeded and tries THAT one first on every subsequent call — so the
    "Fetch Details" step and the "Download" step that follows it are both
    fast, and only a first cold call (or a mode that stops working) pays
    the full fallback cost.

WINDOWS "UGLY CMD WINDOW" FIX:
    Every ffmpeg/yt-dlp subprocess call made from here passes
    CREATE_NO_WINDOW on Windows, so no flashing black console window pops
    up mid-download — a small but very real "this app looks unprofessional"
    fix for non-technical users.
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

# dl_id -> {status, percent, downloaded, total, speed, eta, filename, error, url, stage}
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

    # Cloud secret files check
    if not COOKIES_FILE.exists():
        for _sec in (Path("/etc/secrets/cookies.txt"), base_dir.parent / "cookies.txt"):
            if _sec.exists():
                COOKIES_FILE = _sec
                break

    # Auto-load cookies from environment variable if provided
    _env_cookies = os.environ.get("YOUTUBE_COOKIES") or os.environ.get("COOKIES_TEXT") or os.environ.get("YTDLP_COOKIES")
    if _env_cookies and not COOKIES_FILE.exists():
        try:
            (base_dir / "cookies.txt").write_text(_env_cookies, encoding="utf-8")
            COOKIES_FILE = base_dir / "cookies.txt"
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

    # OAuth2 Token file check
    OAUTH2_TOKEN_FILE = base_dir / "yt-dlp-oauth2.token"
    if not OAUTH2_TOKEN_FILE.exists():
        for _sec in (Path("/etc/secrets/yt-dlp-oauth2.token"), base_dir / "token.json", Path("/etc/secrets/token.json")):
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
    attempts = []
    if _RESOLVED_MODE["mode"]:
        attempts.append(_RESOLVED_MODE["mode"])
    rest = ["web_safari_highres", "tv_embedded", "ios_mobile"]
    if COOKIES_FILE and COOKIES_FILE.exists():
        rest.append("cookies_file")
    if OAUTH2_TOKEN_FILE and OAUTH2_TOKEN_FILE.exists():
        rest.append("oauth2")
    rest.extend(["bypass", "default"])
    rest.extend(COOKIE_BROWSERS)
    for m in rest:
        if m not in attempts:
            attempts.append(m)
    return attempts


def _apply_auth_mode(opts, mode):
    opts = dict(opts)
    opts.pop("cookiefile", None)
    opts.pop("cookiesfrombrowser", None)
    opts.pop("username", None)
    opts.pop("password", None)

    if mode == "web_safari_highres":
        # #1 Priority: Full 4K & 1080p DASH adaptive stream extraction
        opts["extractor_args"] = {
            "youtube": {
                "player_client": ["web_safari", "web_embedded", "web"]
            }
        }
    elif mode == "tv_embedded":
        opts["extractor_args"] = {
            "youtube": {
                "player_client": ["tv_embedded", "tv", "ios"],
                "player_skip": ["webpage", "configs"]
            }
        }
    elif mode == "ios_mobile":
        opts["extractor_args"] = {
            "youtube": {
                "player_client": ["ios", "mweb", "android"],
                "player_skip": ["webpage", "configs"]
            }
        }
    elif mode == "cookies_file" and COOKIES_FILE and COOKIES_FILE.exists():
        opts["cookiefile"] = str(COOKIES_FILE)
    elif mode == "oauth2":
        opts["username"] = "oauth2"
        opts["password"] = ""
        opts["extractor_args"] = {"youtube": {"player_client": ["tv_embedded", "tv", "android"]}}
    elif mode in COOKIE_BROWSERS:
        opts["cookiesfrombrowser"] = (mode,)
    elif mode == "default":
        opts["extractor_args"] = {"youtube": {"player_client": ["web_embedded", "android", "web"]}}
    elif mode == "bypass":
        opts["extractor_args"] = {"youtube": {"player_client": ["web_safari", "android_vr", "web"]}}
    return opts


def _base_ydl_opts():
    opts = {
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "socket_timeout": 15, "retries": 2, "extractor_retries": 1,
        "geo_bypass": True,
    }
    if FFMPEG_PATH:
        opts["ffmpeg_location"] = str(FFMPEG_PATH)
    return opts


def _extract_info_smart(url, extra_opts=None, download=False):
    """Tries the cached working auth mode first, only falling back through
    the rest of the chain if needed. Remembers whatever mode succeeds."""
    base_opts = _base_ydl_opts()
    if extra_opts:
        base_opts.update(extra_opts)

    last_err = None
    for mode in _auth_attempts():
        opts = _apply_auth_mode(base_opts, mode)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=download)
            _RESOLVED_MODE["mode"] = mode
            return info, ydl if download else None, None
        except Exception as e:
            last_err = e
            continue

    if not download:
        pub_info, _ = _resolve_via_public_api(url)
        if pub_info:
            return pub_info, None, None

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

@downloader_bp.route("/api/downloader/info", methods=["POST"])
def api_downloader_info():
    data = request.json or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "Paste a video URL first"}), 400

    info, _, err = _extract_info_smart(url, extra_opts={"format": "bestvideo+bestaudio/best"})
    if info is None:
        return jsonify({"error": f"Could not resolve video: {err}"}), 400

    details = _build_full_details(info)
    details["formats"] = _build_formats(info)
    return jsonify(details)


def _run_download_job(dl_id, url, format_id):
    job = DL_JOBS[dl_id]
    job["stage"] = "connect"

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

    if format_id:
        if "+" not in format_id and not format_id.startswith("best") and not format_id.startswith("ba"):
            # Auto-pair selected video stream with highest quality audio so user gets high quality video WITH crystal clear sound
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

    # Patch subprocess so any ffmpeg/ffprobe child process yt-dlp spawns for
    # merging/remuxing stays silent — no flashing terminal window.
    _orig_popen = subprocess.Popen
    def _quiet_popen(*args, **kwargs):
        kwargs.update(_no_console_kwargs())
        return _orig_popen(*args, **kwargs)
    subprocess.Popen = _quiet_popen
    try:
        info, ydl, err = _extract_info_smart(url, extra_opts=extra_opts, download=True)
    finally:
        subprocess.Popen = _orig_popen

    if info is None:
        job["status"] = "error"
        job["error"] = f"Download failed: {err}"
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
    except Exception as e:
        job["status"] = "error"
        job["error"] = f"Could not finalize file: {e}"


@downloader_bp.route("/api/downloader/start", methods=["POST"])
def api_downloader_start():
    data = request.json or {}
    url = (data.get("url") or "").strip()
    format_id = (data.get("format_id") or "").strip()
    if not url:
        return jsonify({"error": "No URL given"}), 400
    dl_id = uuid.uuid4().hex[:10]
    DL_JOBS[dl_id] = {
        "status": "starting", "stage": "connect", "percent": 0,
        "downloaded": 0, "total": None, "speed": None, "eta": None,
        "filename": None, "error": None, "url": url,
    }
    threading.Thread(target=_run_download_job, args=(dl_id, url, format_id), daemon=True).start()
    return jsonify({"dl_id": dl_id})


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