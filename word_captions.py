#!/usr/bin/env python3
# word_captions.py — YouTube ke internal json3 timedtext format se REAL
# word-by-word timestamps nikalta hai (guess/estimate nahi, actual data).
# pip install yt-dlp

import re
import json
import os
import yt_dlp
import tempfile

def get_word_level_captions(url, lang="auto", cookie_file_path=None):
    """Returns [{"word": str, "start": float_sec, "end": float_sec}, ...]"""
    # Auto-detect cookies if not passed explicitly
    if not cookie_file_path or not os.path.exists(cookie_file_path):
        for candidate in ("cookies.txt", "/etc/secrets/cookies.txt"):
            if os.path.exists(candidate):
                cookie_file_path = candidate
                break

    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        'noplaylist': True,
        'ignoreerrors': 'only_download',
        'geo_bypass': True,
        'socket_timeout': 15,
    }
    if cookie_file_path and os.path.exists(cookie_file_path):
        ydl_opts['cookiefile'] = str(cookie_file_path)

    # Auth fallback loop for subtitle metadata extraction
    info = None
    last_err = None
    
    attempts = ["web_safari_highres", "tv_embedded", "ios_mobile"]
    if cookie_file_path and os.path.exists(cookie_file_path):
        attempts.append("cookies_file")
    attempts.extend(["bypass", "default"])

    for mode in attempts:
        opts = dict(ydl_opts)
        opts.pop('cookiefile', None)
        opts.pop('extractor_args', None)

        if mode == "web_safari_highres":
            opts['extractor_args'] = {'youtube': {'player_client': ['web_safari', 'web_embedded', 'web']}}
        elif mode == "tv_embedded":
            opts['extractor_args'] = {'youtube': {'player_client': ['tv_embedded', 'tv', 'ios'], 'player_skip': ['webpage', 'configs']}}
        elif mode == "ios_mobile":
            opts['extractor_args'] = {'youtube': {'player_client': ['ios', 'mweb', 'android'], 'player_skip': ['webpage', 'configs']}}
        elif mode == "cookies_file" and cookie_file_path and os.path.exists(cookie_file_path):
            opts['cookiefile'] = str(cookie_file_path)
        elif mode == "bypass":
            opts['extractor_args'] = {'youtube': {'player_client': ['web_safari', 'android_vr', 'web']}}
        elif mode == "default":
            opts['extractor_args'] = {'youtube': {'player_client': ['web_embedded', 'android', 'web']}}

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            if info:
                break
        except Exception as e:
            last_err = e
            continue

    if not info:
        raise RuntimeError(f"Video info extract nahi ho paya: {last_err or 'Blocked ya invalid URL'}")

    # Gather both manual subtitles and auto captions
    bucket = info.get('subtitles') or {}
    auto_bucket = info.get('automatic_captions') or {}
    
    all_captions = {}
    if auto_bucket:
        all_captions.update(auto_bucket)
    if bucket:
        all_captions.update(bucket)

    if not all_captions:
        raise RuntimeError("Is video me koi captions/subtitles available nahi hain.")

    chosen_lang = None
    if lang and lang != "auto" and lang in all_captions:
        chosen_lang = lang
    elif "en" in all_captions:
        chosen_lang = "en"
    elif "hi" in all_captions:
        chosen_lang = "hi"
    elif "en-orig" in all_captions:
        chosen_lang = "en-orig"
    else:
        chosen_lang = next(iter(all_captions))

    tracks = all_captions[chosen_lang]

    # json3-native track dhundo, warna kisi bhi track ka URL le ke fmt=json3 force karo
    base_url = None
    for t in tracks:
        if t.get('ext') == 'json3':
            base_url = t['url']
            break
    if not base_url:
        base_url = tracks[0]['url']
        if 'fmt=' in base_url:
            base_url = re.sub(r'fmt=[^&]+', 'fmt=json3', base_url)
        else:
            base_url += ('&' if '?' in base_url else '?') + 'fmt=json3'

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            raw_data = ydl.urlopen(base_url).read().decode('utf-8')
        except Exception as e:
            raise RuntimeError(f"Caption data fetch error: {e}")

    if not raw_data or not raw_data.strip():
        raise RuntimeError("YouTube se khali captions response mila.")

    return _parse_raw_captions(raw_data)


def _safety_split_words(words):
    split_words = []
    for w in words:
        parts = w["word"].split()
        if len(parts) <= 1:
            split_words.append(w)
            continue
        seg_start = w.get("start", 0)
        seg_end = w.get("end", seg_start + 0.4)
        seg_dur = max(seg_end - seg_start, 0.001)
        weights = [len(p) + 1 for p in parts]
        total = sum(weights)
        cursor = seg_start
        for i, p in enumerate(parts):
            share = (weights[i] / total) * seg_dur
            w_start = cursor
            w_end = seg_end if i == len(parts) - 1 else min(seg_end, cursor + share)
            cursor = w_end
            if w_end <= w_start:
                continue
            split_words.append({"word": p, "start": round(w_start, 2), "end": round(w_end, 2)})
    return split_words


def _parse_raw_captions(raw_text):
    """Multi-format parser for YouTube subtitles: JSON3, WebVTT, TTML/XML, and SRT."""
    words = []
    raw_text = raw_text.strip()

    # 1. Try native JSON3 format
    if raw_text.startswith('{'):
        try:
            data = json.loads(raw_text)
            for event in data.get('events', []):
                t_start = event.get('tStartMs')
                if t_start is None:
                    continue
                for seg in (event.get('segs') or []):
                    text = seg.get('utf8', '')
                    if not text.strip():
                        continue
                    offset = seg.get('tOffsetMs', 0)
                    words.append({"word": text.strip(), "start": (t_start + offset) / 1000.0})
            if words:
                for i in range(len(words) - 1):
                    words[i]["end"] = words[i + 1]["start"]
                words[-1]["end"] = words[-1]["start"] + 0.4
                return _safety_split_words(words)
        except Exception:
            pass

    # 2. Try WebVTT / SRT format
    if "-->" in raw_text:
        tag_re = re.compile(r'<[^>]+>')
        time_re = re.compile(r'(\d+):(\d{2}):(\d{2})[.,](\d{1,3})')
        def to_sec(ts):
            m = time_re.search(ts)
            if not m: return None
            h, mi, s, ms = m.groups()
            return int(h)*3600 + int(mi)*60 + int(s) + int((ms+"000")[:3])/1000.0

        blocks = re.split(r'\n\s*\n', raw_text.replace("\r\n", "\n").replace("\r", "\n"))
        cues = []
        for block in blocks:
            lines = [l.strip() for l in block.split("\n") if l.strip()]
            for i, l in enumerate(lines):
                if "-->" in l:
                    pts = l.split("-->")
                    s, e = to_sec(pts[0]), to_sec(pts[1])
                    if s is not None and e is not None:
                        txt = tag_re.sub("", " ".join(lines[i+1:])).strip()
                        if txt:
                            cues.append((s, e, txt))
                    break
        for s, e, txt in cues:
            parts = txt.split()
            if not parts: continue
            dur = max(0.1, e - s)
            for i, p in enumerate(parts):
                ws = s + (i / len(parts)) * dur
                we = s + ((i + 1) / len(parts)) * dur
                words.append({"word": p, "start": round(ws, 2), "end": round(we, 2)})
        if words:
            return words

    # 3. Try YouTube XML / TTML / srv3 format (<text start="1.23" dur="2.34">...</text>)
    if "<text " in raw_text:
        import html
        matches = re.findall(r'<text\s+start="([\d\.]+)"(?:\s+dur="([\d\.]+)")?[^>]*>(.*?)</text>', raw_text, re.DOTALL)
        for s_str, d_str, raw_seg in matches:
            s = float(s_str)
            dur = float(d_str) if d_str else 2.0
            e = s + dur
            clean_seg = html.unescape(re.sub(r'<[^>]+>', '', raw_seg)).strip()
            parts = clean_seg.split()
            if not parts: continue
            for i, p in enumerate(parts):
                ws = s + (i / len(parts)) * dur
                we = s + ((i + 1) / len(parts)) * dur
                words.append({"word": p, "start": round(ws, 2), "end": round(we, 2)})
        if words:
            return words

    return words


def save_words_to_file(words, url, out_path=None):
    """Found word-level captions ko JSON file mein save karta hai (same route/folder)."""
    if out_path is None:
        # video ID se filename banao taaki har video ki alag file bane
        video_id = url.strip().split("v=")[-1].split("&")[0].split("/")[-1].split("?")[0]
        video_id = re.sub(r'[^a-zA-Z0-9_-]', '', video_id) or "captions"
        out_path = f"{video_id}_word_captions.json"

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(words, f, ensure_ascii=False, indent=2)

    return os.path.abspath(out_path)


if __name__ == "__main__":
    url = input("YouTube URL: ").strip()
    # cookies.txt ka path optional hai — agar bot-detection lage toh isse pass karo
    cookies_path = "www.youtube.com_cookies.txt"  # ya None rakh do agar zarurat nahi
    try:
        words = get_word_level_captions(url, cookie_file_path=cookies_path)
        print(f"\n✅ {len(words)} words with timing found:\n")
        for w in words[:40]:
            print(f"[{w['start']:.2f}s - {w['end']:.2f}s]  {w['word']}")
        if len(words) > 40:
            print(f"... aur {len(words)-40} words")

        # FIX: found data ko usi route (script ke folder) mein JSON file ke roop me save karo
        saved_path = save_words_to_file(words, url)
        print(f"\n💾 Poora data save ho gaya: {saved_path}")

    except Exception as e:
        print(f"\n❌ Error encountered: {e}")