#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Standalone YouTube Stream Resolver & Downloader Tester
------------------------------------------------------
Yeh script completely independent hai — aapke existing project files ko bilkul touch nahi karta.
Isko run karke aap directly test kar sakte hain:
1. Video Metadata & Direct CDN Streams (Video + Audio) resolution.
2. Direct FFmpeg streaming & cutting bina kisi 403 block ke.

Run command:
    python standalone_test_stream.py
"""

import os
import sys
import re
import time
import json
import subprocess
from pathlib import Path

try:
    import yt_dlp
except ImportError:
    print("[ERROR] yt-dlp installed nahi hai. Run: pip install yt-dlp")
    sys.exit(1)

try:
    import imageio_ffmpeg
    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    FFMPEG = "ffmpeg"


def resolve_youtube_stream(url, target_quality="best"):
    """
    YouTube se full 4K, 1080p, 720p adaptive streams aur poori details nikalta hai.
    """
    print(f"\n[1/3] Resolving YouTube URL: {url}")
    
    # Modern player client priority chain for highest 1080p/4K resolutions
    clients_to_try = [
        ("web_safari_highres", ["web_safari", "web_embedded", "web"]),
        ("tv_embedded_highres", ["tv_embedded", "tv"]),
        ("android_client", ["android", "ios", "mweb"])
    ]
    
    info = None
    successful_mode = None
    
    for mode_name, client_list in clients_to_try:
        print(f"  -> Testing extraction via mode: '{mode_name}'...")
        opts = {
            'quiet': True,
            'no_warnings': True,
            'noplaylist': True,
            'socket_timeout': 15,
            'retries': 3,
            'geo_bypass': True,
            'ffmpeg_location': FFMPEG,
            'extractor_args': {
                'youtube': {
                    'player_client': client_list
                }
            }
        }
        
        # Check if local cookies.txt exists in current directory
        if os.path.exists("cookies.txt"):
            opts['cookiefile'] = "cookies.txt"
            
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                extracted = ydl.extract_info(url, download=False)
            if extracted and extracted.get("formats"):
                info = extracted
                successful_mode = mode_name
                print(f"  [SUCCESS] Mode '{mode_name}' worked successfully!")
                break
        except Exception as e:
            print(f"  [FAIL] Mode '{mode_name}' failed: {e}")
            continue

    if not info:
        print("\n[ERROR] All extraction modes failed for this URL.")
        return None

    # Filter video and audio formats
    formats = info.get("formats", [])
    
    video_formats = [
        f for f in formats 
        if f.get("url") and f.get("vcodec") not in (None, "none")
    ]
    audio_formats = [
        f for f in formats 
        if f.get("url") and f.get("acodec") not in (None, "none")
    ]
    
    # Build complete formats table
    available_resolutions = []
    seen_res = set()
    for vf in video_formats:
        h = vf.get("height")
        if h and h not in seen_res:
            seen_res.add(h)
            available_resolutions.append({
                "height": h,
                "width": vf.get("width"),
                "fps": vf.get("fps"),
                "format_id": vf.get("format_id"),
                "vcodec": (vf.get("vcodec") or "").split(".")[0],
                "ext": vf.get("ext"),
                "url": vf.get("url"),
                "headers": vf.get("http_headers", {})
            })
            
    available_resolutions.sort(key=lambda x: x["height"], reverse=True)
    
    # Sort for highest quality video (1080p, 4K, etc.) preferring H.264 (avc1) for best compatibility
    video_formats.sort(
        key=lambda f: (
            f.get("height") or 0,
            1 if (f.get("vcodec") or "").startswith("avc1") else 0,
            f.get("fps") or 0,
            f.get("tbr") or 0
        ), 
        reverse=True
    )
    
    # Sort for best audio (prefer m4a/mp4a then opus)
    audio_formats.sort(
        key=lambda f: (
            1 if (f.get("acodec") or "").startswith("mp4a") else 0,
            f.get("abr") or f.get("tbr") or 0
        ), 
        reverse=True
    )
    
    # Pick target quality
    selected_video = video_formats[0] if video_formats else None
    if target_quality != "best":
        try:
            target_h = int(target_quality.replace("p", ""))
            matched = [f for f in video_formats if (f.get("height") or 0) <= target_h]
            if matched:
                selected_video = matched[0]
        except Exception:
            pass

    selected_audio = audio_formats[0] if audio_formats else None
    
    subtitles = list((info.get("subtitles") or {}).keys())
    auto_captions = list((info.get("automatic_captions") or {}).keys())
    
    result = {
        "id": info.get("id"),
        "title": info.get("title"),
        "uploader": info.get("uploader") or info.get("channel"),
        "channel_url": info.get("channel_url"),
        "duration": info.get("duration"),
        "view_count": info.get("view_count"),
        "like_count": info.get("like_count"),
        "upload_date": info.get("upload_date"),
        "thumbnail": info.get("thumbnail"),
        "tags": (info.get("tags") or [])[:10],
        "subtitles_count": len(subtitles),
        "auto_captions_count": len(auto_captions),
        "mode_used": successful_mode,
        "available_resolutions": available_resolutions,
        "video_url": selected_video.get("url") if selected_video else None,
        "video_res": f"{selected_video.get('width')}x{selected_video.get('height')} ({selected_video.get('height')}p)" if selected_video else "N/A",
        "video_height": selected_video.get("height"),
        "video_codec": selected_video.get("vcodec"),
        "video_fps": selected_video.get("fps"),
        "video_headers": selected_video.get("http_headers", {}) if selected_video else {},
        "audio_url": selected_audio.get("url") if selected_audio else None,
        "audio_bitrate": f"{selected_audio.get('abr') or selected_audio.get('tbr')} kbps" if selected_audio else "N/A",
        "audio_codec": selected_audio.get("acodec"),
        "audio_headers": selected_audio.get("http_headers", {}) if selected_audio else {},
    }
    
    return result


def test_stream_cutting(result, output_filename="test_output_clip.mp4"):
    """
    Direct stream URL se 5-second sample clip cut karke test karta hai (FFmpeg ke through).
    Bina full video download kiye direct stream slice in Full High Quality.
    """
    print(f"\n[2/3] Testing Direct Stream Slicing via FFmpeg (5s Preview Clip in {result.get('video_res')})...")
    
    video_url = result.get("video_url")
    audio_url = result.get("audio_url")
    
    if not video_url:
        print("[ERROR] No direct video URL found.")
        return False
        
    cmd = [
        FFMPEG, "-y",
        "-ss", "00:00:05", "-t", "5",  # 5th second se 10th second tak (5 seconds)
        "-i", video_url,
    ]
    
    if audio_url and audio_url != video_url:
        cmd.extend(["-ss", "00:00:05", "-t", "5", "-i", audio_url])
        cmd.extend(["-map", "0:v:0", "-map", "1:a:0?"])
    else:
        cmd.extend(["-map", "0:v:0", "-map", "0:a:0?"])
        
    cmd.extend([
        "-c:v", "libx264", "-preset", "ultrafast",
        "-c:a", "aac",
        output_filename
    ])
    
    print(f"  -> Running FFmpeg process to generate '{output_filename}'...")
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    
    if proc.returncode == 0 and os.path.exists(output_filename):
        file_size = os.path.getsize(output_filename) / (1024 * 1024)
        print(f"  [SUCCESS] High Quality clip successfully generated!")
        print(f"  -> Saved file: {os.path.abspath(output_filename)} ({file_size:.2f} MB)")
        return True
    else:
        print(f"  [ERROR] FFmpeg failed with error:")
        print(proc.stderr[-500:])
        return False


def main():
    print("=" * 70)
    print("       YouTube High-Quality Stream Resolver & Cutter Test")
    print("=" * 70)
    
    default_url = "https://youtu.be/13jCDIHml7w?si=0hXFHHVduxglTY1m"
    
    if len(sys.argv) > 1:
        test_url = sys.argv[1]
    else:
        print(f"Default URL: {default_url}")
        user_input = input("Enter custom YouTube URL (press Enter to use default): ").strip()
        test_url = user_input if user_input else default_url

    start_time = time.time()
    result = resolve_youtube_stream(test_url, target_quality="best")
    
    if not result:
        print("\n[FAIL] Stream resolve nahi ho saki.")
        return
        
    print("\n" + "=" * 70)
    print("                FULL VIDEO DETAILS & METADATA")
    print("=" * 70)
    print(f"  Video ID     : {result['id']}")
    print(f"  Title        : {result['title']}")
    print(f"  Channel      : {result['uploader']}")
    print(f"  Duration     : {result['duration']} seconds ({result['duration'] // 60}m {result['duration'] % 60}s)")
    print(f"  Views        : {result['view_count']:,}" if result['view_count'] else "  Views        : N/A")
    print(f"  Likes        : {result['like_count']:,}" if result['like_count'] else "  Likes        : N/A")
    print(f"  Upload Date  : {result['upload_date']}")
    print(f"  Thumbnail    : {result['thumbnail']}")
    print(f"  Subtitles    : {result['subtitles_count']} manual, {result['auto_captions_count']} auto-generated")
    print(f"  Top Tags     : {', '.join(result['tags']) if result['tags'] else 'None'}")
    print(f"  Auth Mode    : {result['mode_used']}")

    print("\n" + "-" * 70)
    print("           AVAILABLE QUALITIES DETECTED ON YOUTUBE")
    print("-" * 70)
    for res in result['available_resolutions']:
        print(f"   * {res['height']}p  ({res['width']}x{res['height']})  FPS: {res['fps']}  Codec: {res['vcodec']} ({res['ext']})")
    
    print("-" * 70)
    print(f"  SELECTED STREAM -> Video: {result['video_res']} ({result['video_codec']}) | Audio: {result['audio_bitrate']} ({result['audio_codec']})")
    print(f"  Video CDN URL   -> {result['video_url'][:70]}...")
    print(f"  Audio CDN URL   -> {result['audio_url'][:70]}...")

    # Run cutting test with selected high quality stream
    ok = test_stream_cutting(result)
    
    total_time = time.time() - start_time
    print("\n" + "=" * 70)
    if ok:
        print(f"[SUCCESS] ALL TESTS PASSED SUCCESSFULLY in {total_time:.2f}s!")
        print(f"High Quality ({result['video_res']}) stream resolved & sliced perfectly!")
    else:
        print(f"[FAIL] Slicing test failed in {total_time:.2f}s.")
    print("=" * 70)


if __name__ == "__main__":
    main()
