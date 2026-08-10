"""
creator_tracker.py
===================

STANDALONE MODULE — NOT wired into app.py yet. Review/test isolately pehle,
phir bottom mein "HOW TO MOUNT LATER" section ke hisaab se app.py mein jodna.

Kya karta hai:
  - Har user apni categories + language + country + content-format preference
    set karta hai (onboarding).
  - User channels search/add karta hai -> per-user category assign hota hai.
  - Channel/video data GLOBAL collections mein store hota hai (shared across
    all users) — same channel 500 users track karein to bhi ek hi jagah
    check hota hai, redundant kaam nahi hota.
  - Naye users ko recommendation milta hai unki preference (category+language
    +country) se match karti channels ka, jo already kisi aur user ne track
    ki hain (organic pool, "wisdom of crowd" tagging) — koi ML/AI nahi chahiye.
  - Naye video ka detection YouTube ke FREE PUBLIC RSS FEED se hota hai
    (https://www.youtube.com/feeds/videos.xml?channel_id=...) — NO API quota,
    NO callback URL, NO public server, NO signature, NO renewal. Bas har
    5-10 min ek chhota background job feed check karta hai. Yeh WebSub se
    kaafi zyada simple hai — same code local aur production dono jagah
    bina kisi extra config ke chalta hai.
  - Jab naya video milta hai, jitne users ne wo channel track kiya hai unke
    liye ek notification ban jaati hai.

================================================================================
SETUP (manually karna hai) — bas itna hi chahiye:
================================================================================
1. requirements.txt mein add karo (agar already nahi hai):
       apscheduler==3.10.4

2. .env mein sirf yeh chahiye (dono already tumhare .env mein hain):
       GOOGLE_API_KEY=...        <- channel resolve + video stats ke liye
       MONGODB_URI=...           <- already hai

   Koi WEBSUB_SECRET, APP_BASE_URL, callback route, ngrok — KUCH NAHI CHAHIYE.

   NOTE: yeh module project ke db.py wale MongoDB connection ko REUSE karta
   hai (apna alag connection nahi banata — do connections khulne se Atlas
   pe extra load padta hai aur flaky ho sakta hai). Isliye standalone test
   (`python creator_tracker.py`) bhi isi project folder ke andar se hi
   chalana hai, taaki `import db` resolve ho sake.

3. Isse app.py se jodne ka tarika (abhi mat karo, pehle standalone test karo):

       from creator_tracker import init_creator_tracker
       from services.scheduler import start_scheduler

       sched = start_scheduler()              # tumhara existing scheduler
       init_creator_tracker(app, scheduler=sched)   # isi scheduler mein naya job add karega

   Agar scheduler pass nahi karoge to yeh apna khud ka BackgroundScheduler
   bana lega — dono chalega, par ek hi scheduler reuse karna cleaner hai.

4. Test karne ka sabse aasan tarika (bina Flask/app.py chhue):
       python creator_tracker.py
   Yeh MongoDB connect karega, ek real channel resolve karega (Google
   Developers), uske latest videos RSS feed se fetch karke dikhayega.
================================================================================
"""

import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from functools import wraps

import requests
from bson import ObjectId
from flask import Blueprint, jsonify, redirect, render_template_string, request, session, url_for
from pymongo import ASCENDING, MongoClient
from pymongo.errors import PyMongoError

from config import Config

# ============================================================================
# CONSTANTS — apni khud ki taxonomy, YouTube ke categoryId pe depend nahi
# karte (woh rigid/limited hai). Naya category add karna ho to bas yahan
# ek string add karo, code kahin aur change nahi karna padega.
# ============================================================================
CATEGORIES = [
    "Tech", "Gaming", "Finance", "Education", "Vlogs", "Music",
    "Comedy", "News", "Sports", "Cooking", "Movies", "Fashion",
    "Fitness", "Science", "Travel", "Kids", "Health", "Beauty",
    "DIY & Crafts", "Business", "Motivation", "Automobile", "Anime",
    "Podcast", "Reviews", "Spirituality", "Photography", "Art & Design",
    "History", "Agriculture",
]

LANGUAGES = ["Hindi", "English", "Hinglish", "Tamil", "Telugu", "Bengali", "Marathi", "Punjabi", "Gujarati", "Kannada", "Malayalam", "Urdu", "Other"]

# YouTube API ke "relevanceLanguage" param ke liye ISO code — auto-seeding
# search ke liye chahiye
LANGUAGE_ISO = {
    "Hindi": "hi", "English": "en", "Hinglish": "hi", "Tamil": "ta",
    "Telugu": "te", "Bengali": "bn", "Marathi": "mr", "Punjabi": "pa",
    "Gujarati": "gu", "Kannada": "kn", "Malayalam": "ml", "Urdu": "ur",
}

# (code, display name) — /meta route se frontend ko milta hai, dropdown
# yahi se banta hai (pehle sirf 4 hardcoded options the HTML mein)
COUNTRIES = [
    ("IN", "India"), ("US", "United States"), ("GB", "United Kingdom"),
    ("CA", "Canada"), ("AU", "Australia"), ("PK", "Pakistan"),
    ("BD", "Bangladesh"), ("NP", "Nepal"), ("LK", "Sri Lanka"),
    ("AE", "UAE"), ("SA", "Saudi Arabia"), ("SG", "Singapore"),
    ("MY", "Malaysia"), ("DE", "Germany"), ("FR", "France"),
    ("BR", "Brazil"), ("JP", "Japan"), ("KR", "South Korea"),
    ("ZA", "South Africa"), ("NG", "Nigeria"), ("OT", "Global / Other"),
]
COUNTRY_CODES = {c for c, _ in COUNTRIES}

CONTENT_FORMATS = ["both", "shorts", "long_form"]

# Channel size — ab hardcoded nahi, user ki apni preference hai. Seed pool
# mein SAB channels store hote hain (bilkul spam/empty accounts ke alawa,
# dekho SEED_ABSOLUTE_MIN_SUBSCRIBERS), aur filtering sirf QUERY TIME pe
# hoti hai — isse ek user "established" chahe aur doosra "emerging", dono ko
# sahi results milte hain, bina pool dobara banaye.
CHANNEL_SIZES = ["established", "any", "emerging"]
ESTABLISHED_THRESHOLD = 10_000          # "established" filter ka minimum
SEED_ABSOLUTE_MIN_SUBSCRIBERS = 500     # bilkul spam/dead channels hi discard hote hain seed time pe

YT_API_BASE = "https://www.googleapis.com/youtube/v3"
YT_RSS_FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"

# Har (category, language, country) combo ko dobara seed karne se pehle
# itna wait karo — isse quota bachti hai, ek combo baar baar search nahi hota
SEED_STALE_AFTER = timedelta(days=14)
SEED_RESULTS_PER_QUERY = 15  # ek seed search se itne channels utthaye jaate hain

# Kitni der se check nahi hua us channel ko poll karo. Isse poori pool baar
# baar nahi, sirf "due" channels hi check hote hain.
POLL_INTERVAL_MINUTES = 7
POLL_STALE_AFTER = timedelta(minutes=POLL_INTERVAL_MINUTES)
POLL_BATCH_SIZE = 300   # ek run mein max itne channels — bade user-base pe bhi ek job kabhi lamba nahi chalega

_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}


# ============================================================================
# DB — project ke MOJUDA db.py wale connection ko REUSE karta hai. Alag
# MongoClient KHOLNA JAAN-BUJH KAR YAHAN NAHI KIYA — do separate connections
# (ek db.py ka, ek yahan ka) same Atlas cluster ke liye do baar DNS/TLS
# handshake karte the, jo Windows/Atlas ke flaky network pe extra load daal
# raha tha aur is module ki calls timeout kar rahi thi jabki db.py wali
# calls (Discover/Add, My Channels) stable rehti thi (unka connection pehle
# se warm tha). Isliye ab db.py ke get_db() ko hi call karte hain — poore
# app mein sirf EK hi MongoClient rehta hai.
# ============================================================================
import db as _project_db  # project ka existing db.py

_indexes_ensured = False


def get_db():
    global _indexes_ensured
    database = _project_db.get_db()  # <-- same shared connection jo baaki poora app use karta hai
    if not _indexes_ensured:
        _ensure_indexes(database)
        _indexes_ensured = True
    return database


def _ensure_indexes(db):
    db.ct_user_preferences.create_index([("user_id", ASCENDING)], unique=True)
    db.ct_categories.create_index([("user_id", ASCENDING), ("name", ASCENDING)], unique=True)
    db.ct_channels.create_index([("yt_channel_id", ASCENDING)], unique=True)
    db.ct_channels.create_index([("categories", ASCENDING), ("language", ASCENDING), ("country", ASCENDING)])
    db.ct_videos.create_index([("yt_video_id", ASCENDING)], unique=True)
    db.ct_videos.create_index([("channel_id", ASCENDING), ("published_at", ASCENDING)])
    db.ct_subscriptions.create_index([("user_id", ASCENDING), ("channel_id", ASCENDING)], unique=True)
    db.ct_subscriptions.create_index([("channel_id", ASCENDING)])
    db.ct_notifications.create_index([("user_id", ASCENDING), ("seen", ASCENDING), ("created_at", ASCENDING)])
    db.ct_seed_log.create_index(
        [("category", ASCENDING), ("language", ASCENDING), ("country", ASCENDING)], unique=True
    )


def _now():
    # Mongo/PyMongo datetimes ko NAIVE (bina tzinfo) laut ke deta hai by
    # default — isliye yahan bhi naive UTC use karte hain, taaki DB se aayi
    # kisi bhi datetime ke saath direct comparison kabhi crash na ho
    # ("can't compare offset-naive and offset-aware datetimes").
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ============================================================================
# YOUTUBE DATA API HELPERS — sirf channel resolve + stats ke liye (quota
# lagti hai yahan, par yeh sirf tab call hota hai jab user naya channel add
# kare, ya jab genuinely naya video mile — bahut kam frequency).
# ============================================================================
def _yt_get(path, params):
    params = {**params, "key": Config.GOOGLE_API_KEY}
    resp = requests.get(f"{YT_API_BASE}/{path}", params=params, timeout=15).json()
    if "error" in resp:
        raise RuntimeError(resp["error"].get("message", f"YouTube {path} error"))
    return resp


def _parse_channel_item(c: dict) -> dict:
    """Ek YouTube channels.list API item ko hamare internal dict format mein badalta hai (reusable)."""
    snip = c.get("snippet", {})
    stats = c.get("statistics", {})
    branding = c.get("brandingSettings", {}).get("channel", {})
    return {
        "yt_channel_id": c["id"],
        "title": snip.get("title"),
        "handle": snip.get("customUrl"),
        "thumbnail": snip.get("thumbnails", {}).get("medium", {}).get("url"),
        "subscriber_count": int(stats.get("subscriberCount", 0)) if stats.get("subscriberCount") else 0,
        "language": snip.get("defaultLanguage"),
        "country": snip.get("country") or branding.get("country"),
    }


def resolve_channel_from_query(query: str) -> dict | None:
    """
    User ne jo bhi diya ho — @handle, channel URL, channel ID (UC...), ya
    plain naam — usse actual channel resolve karta hai.
    """
    query = query.strip()
    channel_id = None
    handle = None

    m = re.search(r"youtube\.com/channel/(UC[\w-]+)", query)
    if m:
        channel_id = m.group(1)
    elif query.startswith("UC") and " " not in query:
        channel_id = query
    else:
        m = re.search(r"youtube\.com/@([\w.-]+)", query)
        if m:
            handle = m.group(1)
        elif query.startswith("@"):
            handle = query[1:]

    if channel_id:
        resp = _yt_get("channels", {"id": channel_id, "part": "snippet,statistics,contentDetails,brandingSettings"})
    elif handle:
        resp = _yt_get("channels", {"forHandle": handle, "part": "snippet,statistics,contentDetails,brandingSettings"})
    else:
        # plain text search -> sirf tab lagta hai jab user pehli baar
        # channel add kar raha ho, baar baar nahi chalta
        search_resp = _yt_get("search", {"q": query, "type": "channel", "part": "snippet", "maxResults": 1})
        items = search_resp.get("items", [])
        if not items:
            return None
        found_id = items[0]["id"]["channelId"]
        resp = _yt_get("channels", {"id": found_id, "part": "snippet,statistics,contentDetails,brandingSettings"})

    items = resp.get("items", [])
    if not items:
        return None

    return _parse_channel_item(items[0])


def fetch_video_stats(video_ids: list[str]) -> dict:
    """Batched — up to 50 IDs per call, 1 quota unit total regardless of batch size."""
    stats_map = {}
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i + 50]
        resp = _yt_get("videos", {"id": ",".join(batch), "part": "statistics"})
        for item in resp.get("items", []):
            s = item.get("statistics", {})
            stats_map[item["id"]] = {
                "views": int(s.get("viewCount", 0)) if s.get("viewCount") else 0,
                "likes": int(s.get("likeCount", 0)) if s.get("likeCount") else 0,
                "comments": int(s.get("commentCount", 0)) if s.get("commentCount") else 0,
            }
    return stats_map


# ============================================================================
# FREE RSS FEED — naya video detection ka main engine. NO API key, NO quota,
# NO callback URL. Bas ek GET request.
# ============================================================================
def fetch_channel_feed(yt_channel_id: str) -> list[dict]:
    """
    Har channel ke latest ~15 videos deta hai (naya-se-purana order mein).
    Yeh YouTube ka public feed hai — koi auth/quota nahi lagti.
    """
    url = YT_RSS_FEED_URL.format(channel_id=yt_channel_id)
    resp = requests.get(url, timeout=15)
    if resp.status_code != 200:
        raise RuntimeError(f"RSS feed fetch failed ({resp.status_code}) for {yt_channel_id}")

    root = ET.fromstring(resp.content)
    out = []
    for entry in root.findall("atom:entry", _ATOM_NS):
        video_id_el = entry.find("yt:videoId", _ATOM_NS)
        title_el = entry.find("atom:title", _ATOM_NS)
        published_el = entry.find("atom:published", _ATOM_NS)
        if video_id_el is None or not video_id_el.text:
            continue

        published = None
        if published_el is not None and published_el.text:
            published = datetime.fromisoformat(published_el.text.replace("Z", "+00:00"))

        out.append({
            "yt_video_id": video_id_el.text,
            "title": title_el.text if title_el is not None else "",
            "thumbnail": f"https://i.ytimg.com/vi/{video_id_el.text}/mqdefault.jpg",
            "published_at": published or _now(),
        })
    return out


# ============================================================================
# PREFERENCES (onboarding: category + language + country + format)
# ============================================================================
def save_user_preferences(user_id, categories: list[str], languages: list[str],
                           country: str, content_format: str = "both", channel_size: str = "established"):
    db = get_db()
    country_code = (country or "").upper()[:2]
    if country_code not in COUNTRY_CODES:
        country_code = "OT"
    doc = {
        "user_id": ObjectId(user_id),
        "categories": [c for c in categories if c in CATEGORIES],
        "languages": [l for l in languages if l in LANGUAGES],
        "country": country_code,
        "content_format": content_format if content_format in CONTENT_FORMATS else "both",
        "channel_size": channel_size if channel_size in CHANNEL_SIZES else "established",
        "updated_at": _now(),
    }
    db.ct_user_preferences.update_one({"user_id": ObjectId(user_id)}, {"$set": doc}, upsert=True)
    return doc


def get_user_preferences(user_id) -> dict | None:
    return get_db().ct_user_preferences.find_one({"user_id": ObjectId(user_id)})


# ============================================================================
# USER CATEGORIES (per-user organizing folders — jaan-bujh kar
# ct_user_preferences.categories se alag hai: preference "kya recommend
# karna hai" decide karti hai, yeh "dashboard kaise organize hai" decide
# karti hai — dono ko merge mat karna, purpose alag hai)
# ============================================================================
def create_user_category(user_id, name: str) -> dict:
    db = get_db()
    doc = {"user_id": ObjectId(user_id), "name": name.strip(), "created_at": _now()}
    existing = db.ct_categories.find_one({"user_id": ObjectId(user_id), "name": doc["name"]})
    if existing:
        return existing
    result = db.ct_categories.insert_one(doc)
    doc["_id"] = result.inserted_id
    return doc


def list_user_categories(user_id) -> list[dict]:
    return list(get_db().ct_categories.find({"user_id": ObjectId(user_id)}).sort("name", 1))


# ============================================================================
# CHANNELS — global pool, get-or-create
# ============================================================================
def get_or_create_channel(query: str, tag_category: str | None = None,
                           tag_language: str | None = None, tag_country: str | None = None) -> dict | None:
    db = get_db()
    resolved = resolve_channel_from_query(query)
    if not resolved:
        return None

    existing = db.ct_channels.find_one({"yt_channel_id": resolved["yt_channel_id"]})
    if existing:
        # union-tag: agar user ne naya category/language bataya jo pehle se
        # nahi hai, add kar do — "wisdom of crowd" tagging, koi manual
        # admin curation nahi chahiye
        update = {}
        if tag_category and tag_category not in existing.get("categories", []):
            update["categories"] = existing.get("categories", []) + [tag_category]
        if tag_language and not existing.get("language"):
            update["language"] = tag_language
        if tag_country and not existing.get("country"):
            update["country"] = tag_country
        if update:
            db.ct_channels.update_one({"_id": existing["_id"]}, {"$set": update})
            existing.update(update)
        return existing

    doc = {
        "yt_channel_id": resolved["yt_channel_id"],
        "title": resolved["title"],
        "handle": resolved["handle"],
        "thumbnail": resolved["thumbnail"],
        "subscriber_count": resolved["subscriber_count"],
        "categories": [tag_category] if tag_category else [],
        "language": tag_language or resolved.get("language"),
        "country": tag_country or resolved.get("country"),
        "is_seeded": False,
        "added_count": 0,
        "last_checked_at": None,
        "created_at": _now(),
    }
    result = db.ct_channels.insert_one(doc)
    doc["_id"] = result.inserted_id
    return doc


# ============================================================================
# AUTO-SEEDING — "cold start" problem solve karta hai. Naye project mein
# ct_channels pool khaali hota hai, isliye Discover tab khaali dikhta tha
# jab tak users organically channels add na karein. Ab jaise hi koi user
# apni preferences (category+language+country) save karta hai — ya jab bhi
# recommend_channels() chalta hai — system khud YouTube se us combo ke
# relevant channels utha ke pool mein daal deta hai. Har (category,
# language, country) combo sirf EK BAAR har 14 din mein search hoti hai
# (ct_seed_log se track), isliye quota control mein rehta hai chahe kitne
# bhi users same combo pick karein.
# ============================================================================
def ensure_seeded(category: str, language: str | None, country: str | None) -> int | None:
    """Returns None agar skip hua (recently seeded, cache se), warna kitne naye channels add hue."""
    db = get_db()
    seed_filter = {"category": category, "language": language, "country": country}
    existing = db.ct_seed_log.find_one(seed_filter)
    if existing and existing.get("seeded_at") and existing["seeded_at"] > _now() - SEED_STALE_AFTER:
        return None  # is combo ke liye recently hi seed ho chuka hai, dobara API call nahi

    lang_code = LANGUAGE_ISO.get(language) if language else None
    search_params = {
        "q": f"{category} channel",
        "type": "channel",
        "part": "snippet",
        "maxResults": SEED_RESULTS_PER_QUERY,
        "order": "relevance",
    }
    if country:
        search_params["regionCode"] = country
    if lang_code:
        search_params["relevanceLanguage"] = lang_code

    try:
        search_resp = _yt_get("search", search_params)
    except Exception as e:
        print(f"[creator_tracker] seed search failed for {category}/{language}/{country}: {e}")
        return 0

    channel_ids = [
        item["id"]["channelId"] for item in search_resp.get("items", [])
        if item.get("id", {}).get("channelId")
    ]
    added = 0
    if channel_ids:
        try:
            details_resp = _yt_get("channels", {
                "id": ",".join(channel_ids[:50]),
                "part": "snippet,statistics,contentDetails,brandingSettings",
            })
        except Exception as e:
            print(f"[creator_tracker] seed channel-details fetch failed: {e}")
            details_resp = {"items": []}

        for c in details_resp.get("items", []):
            parsed = _parse_channel_item(c)

            # Bilkul spam/dead channels hi discard karte hain seed time pe —
            # baaki size-based filtering query time pe hoti hai (user ki
            # channel_size preference ke hisaab se), taaki emerging-creator
            # chahne wale users ke liye bhi data available rahe.
            if parsed["subscriber_count"] < SEED_ABSOLUTE_MIN_SUBSCRIBERS:
                continue

            existing_channel = db.ct_channels.find_one({"yt_channel_id": parsed["yt_channel_id"]})
            if existing_channel:
                if category not in existing_channel.get("categories", []):
                    db.ct_channels.update_one(
                        {"_id": existing_channel["_id"]},
                        {"$addToSet": {"categories": category}},
                    )
                continue

            db.ct_channels.insert_one({
                "yt_channel_id": parsed["yt_channel_id"],
                "title": parsed["title"],
                "handle": parsed["handle"],
                "thumbnail": parsed["thumbnail"],
                "subscriber_count": parsed["subscriber_count"],
                "categories": [category],
                "language": language or parsed.get("language"),
                "country": country or parsed.get("country"),
                "is_seeded": True,
                "added_count": 0,
                "last_checked_at": None,
                "created_at": _now(),
            })
            added += 1

    db.ct_seed_log.update_one(seed_filter, {"$set": {"seeded_at": _now()}}, upsert=True)
    if added:
        print(f"[creator_tracker] seeded {added} new channel(s) for {category}/{language}/{country}")
    return added


# ============================================================================
# RECOMMENDATIONS — sirf user ki apni preference (category+language+country)
# se match karti channels dikhate hain. Koi cross-category noise nahi.
# ============================================================================
def get_seed_pool_health() -> list[dict]:
    """Har category mein kitne channels pool mein hain — Discover tab ke health panel ke liye."""
    db = get_db()
    pipeline = [
        {"$unwind": "$categories"},
        {"$group": {"_id": "$categories", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
    ]
    counts = {row["_id"]: row["count"] for row in db.ct_channels.aggregate(pipeline)}
    return [{"category": c, "count": counts.get(c, 0)} for c in CATEGORIES]


def recommend_channels(user_id, limit: int = 30) -> list[dict]:
    db = get_db()
    prefs = get_user_preferences(user_id)
    if not prefs or not prefs.get("categories"):
        return []

    # Auto-seed: har category ke liye pool ko taaza rakho (cached hai, isliye
    # baar baar call hone pe bhi quota safe hai — dekho ensure_seeded() docstring).
    # Ek hi request mein max 6 naye (uncached) combos try karte hain — taaki
    # pehli baar mein page slow na ho aur quota burst na ho. Baaki combos
    # agli visit pe apne aap seed ho jaate hain.
    languages_to_try = prefs.get("languages") or [None]
    seed_attempts = 0
    for category in prefs["categories"]:
        for language in languages_to_try:
            if seed_attempts >= 6:
                break
            try:
                result = ensure_seeded(category, language, prefs.get("country"))
                if result is not None:   # genuine API call hui (cache-skip nahi tha)
                    seed_attempts += 1
            except Exception as e:
                print(f"[creator_tracker] seeding skipped due to error: {e}")
        if seed_attempts >= 6:
            break

    already_tracked = {
        s["channel_id"] for s in db.ct_subscriptions.find({"user_id": ObjectId(user_id)}, {"channel_id": 1})
    }

    size_filter = _channel_size_filter(prefs.get("channel_size", "established"))
    sort_order = [("added_count", -1), ("last_video_published_at", -1), ("subscriber_count", -1)]

    query = {
        "categories": {"$in": prefs["categories"]},
        "_id": {"$nin": list(already_tracked)},
        **size_filter,
    }
    if prefs.get("languages"):
        query["language"] = {"$in": prefs["languages"]}

    results = list(db.ct_channels.find(query).sort(sort_order).limit(limit))

    # fallback: country/language match kam pada to sirf category pe relax kar
    # do, taaki naye/rare combos ke liye khaali screen kabhi na aaye
    # (channel-size filter yahan bhi rakha hai — yeh koi match-preference
    # nahi, ek quality gate hai, isliye relax nahi karte)
    if len(results) < min(5, limit):
        loose_query = {
            "categories": {"$in": prefs["categories"]},
            "_id": {"$nin": list(already_tracked)},
            **size_filter,
        }
        results = list(db.ct_channels.find(loose_query).sort(sort_order).limit(limit))

    return results


def _channel_size_filter(channel_size: str) -> dict:
    """User ki 'channel size' preference ko Mongo query filter mein badalta hai."""
    if channel_size == "any":
        return {}
    if channel_size == "emerging":
        return {"subscriber_count": {"$lt": ESTABLISHED_THRESHOLD}}
    return {"subscriber_count": {"$gte": ESTABLISHED_THRESHOLD}}  # default "established"


# ============================================================================
# SUBSCRIPTIONS — user <-> channel link, with the user's own category
# ============================================================================
def subscribe_user_to_channel(user_id, channel_mongo_id, user_category_id=None) -> dict:
    db = get_db()
    doc = {
        "user_id": ObjectId(user_id),
        "channel_id": ObjectId(channel_mongo_id),
        "category_id": ObjectId(user_category_id) if user_category_id else None,
        "notify": True,
        "added_at": _now(),
    }
    db.ct_subscriptions.update_one(
        {"user_id": ObjectId(user_id), "channel_id": ObjectId(channel_mongo_id)},
        {"$setOnInsert": doc},
        upsert=True,
    )
    db.ct_channels.update_one({"_id": ObjectId(channel_mongo_id)}, {"$inc": {"added_count": 1}})

    # turant last-few videos bhi le aao (RSS feed se, free), taaki user ko
    # khaali dashboard na dikhe
    channel = db.ct_channels.find_one({"_id": ObjectId(channel_mongo_id)})
    if channel:
        try:
            check_channel_for_new_videos(channel)
        except Exception as e:
            print(f"initial video sync failed for {channel['yt_channel_id']}: {e}")

    return doc


def unsubscribe_user_from_channel(user_id, channel_mongo_id):
    db = get_db()
    db.ct_subscriptions.delete_one({"user_id": ObjectId(user_id), "channel_id": ObjectId(channel_mongo_id)})
    db.ct_channels.update_one({"_id": ObjectId(channel_mongo_id)}, {"$inc": {"added_count": -1}})


def reassign_channel_category(user_id, channel_mongo_id, user_category_id=None):
    """User ke apne dashboard-folder (category_id) ko badalta hai kisi tracked channel ke liye."""
    db = get_db()
    db.ct_subscriptions.update_one(
        {"user_id": ObjectId(user_id), "channel_id": ObjectId(channel_mongo_id)},
        {"$set": {"category_id": ObjectId(user_category_id) if user_category_id else None}},
    )


def is_channel_tracked_by_user(user_id, yt_channel_id: str) -> tuple[bool, str | None]:
    """Returns (already_tracked, channel_mongo_id_as_str). Manual search/Discover ke liye use hota hai."""
    db = get_db()
    channel = db.ct_channels.find_one({"yt_channel_id": yt_channel_id})
    if not channel:
        return False, None
    sub = db.ct_subscriptions.find_one({"user_id": ObjectId(user_id), "channel_id": channel["_id"]})
    return (sub is not None), str(channel["_id"])


def list_user_channels(user_id) -> list[dict]:
    db = get_db()
    subs = list(db.ct_subscriptions.find({"user_id": ObjectId(user_id)}))
    channel_ids = [s["channel_id"] for s in subs]
    channels = {c["_id"]: c for c in db.ct_channels.find({"_id": {"$in": channel_ids}})}

    out = []
    for s in subs:
        channel = channels.get(s["channel_id"])
        if not channel:
            continue
        latest_videos = list(
            db.ct_videos.find({"channel_id": channel["_id"]}).sort("published_at", -1).limit(5)
        )
        # Sort-key ke liye: is channel ka sabse naya video kab aaya
        most_recent = latest_videos[0]["published_at"] if latest_videos else datetime.min
        out.append({"subscription": s, "channel": channel, "latest_videos": latest_videos, "_sort_key": most_recent})

    # Jis channel ne sabse RECENTLY naya video daala ho, wahi sabse upar —
    # taaki active creators dikhein, purane/inactive neeche chale jaayein
    out.sort(key=lambda row: row["_sort_key"], reverse=True)
    for row in out:
        row.pop("_sort_key", None)
    return out


# ============================================================================
# VIDEO INGESTION + DETECTION — yahi asli "naya video mila" ka core hai.
# ============================================================================
def ingest_new_video(channel: dict, yt_video_id: str, title: str, thumbnail: str, published_at) -> bool:
    """Returns True agar yeh genuinely naya video tha (already-seen ho to False)."""
    db = get_db()
    if db.ct_videos.find_one({"yt_video_id": yt_video_id}):
        return False

    # RSS feed se aane wala published_at timezone-AWARE hota hai — Mongo se
    # wapas aane wali baaki saari datetimes ki tarah naive bana dete hain,
    # taaki koi bhi Python-side comparison kabhi crash na ho.
    if hasattr(published_at, "tzinfo") and published_at.tzinfo is not None:
        published_at = published_at.replace(tzinfo=None)

    stats = {"views": 0, "likes": 0, "comments": 0}
    try:
        stats = fetch_video_stats([yt_video_id]).get(yt_video_id, stats)
    except Exception as e:
        print(f"stats fetch failed for {yt_video_id}, saving with 0s: {e}")

    video_doc = {
        "channel_id": channel["_id"],
        "yt_video_id": yt_video_id,
        "title": title,
        "thumbnail": thumbnail,
        "published_at": published_at,
        "url": f"https://www.youtube.com/watch?v={yt_video_id}",
        "stats": stats,
        "stats_updated_at": _now(),
        "created_at": _now(),
    }
    result = db.ct_videos.insert_one(video_doc)
    video_doc["_id"] = result.inserted_id

    # Trending/activity signal — channel ka "last posted" time update karo.
    # Recommendations aur My Channels dono isse istemal karte hain, taaki
    # sirf bade hi nahi, balki abhi-active channels bhi upar dikhein.
    current = channel.get("last_video_published_at")
    if not current or published_at > current:
        db.ct_channels.update_one(
            {"_id": channel["_id"]}, {"$set": {"last_video_published_at": published_at}}
        )

    # fanout: is channel ko track karne wale sab users ko notify karo
    subscribers = db.ct_subscriptions.find({"channel_id": channel["_id"], "notify": True})
    notif_docs = [{
        "user_id": sub["user_id"],
        "video_id": video_doc["_id"],
        "channel_id": channel["_id"],
        "category_id": sub.get("category_id"),
        "seen": False,
        "created_at": _now(),
    } for sub in subscribers]
    if notif_docs:
        db.ct_notifications.insert_many(notif_docs)

    return True


def check_channel_for_new_videos(channel: dict) -> int:
    """
    Ek channel ka RSS feed check karta hai, jo bhi videos already DB mein
    nahi hain unhe ingest karta hai. Returns kitne naye videos mile.
    """
    db = get_db()
    feed_items = fetch_channel_feed(channel["yt_channel_id"])
    new_count = 0
    for item in feed_items:
        was_new = ingest_new_video(
            channel, item["yt_video_id"], item["title"], item["thumbnail"], item["published_at"]
        )
        if was_new:
            new_count += 1
    db.ct_channels.update_one({"_id": channel["_id"]}, {"$set": {"last_checked_at": _now()}})
    return new_count


def poll_all_tracked_channels():
    """
    SCHEDULED JOB — har POLL_INTERVAL_MINUTES pe chalta hai. Sirf un channels
    ko check karta hai jinke "due" hone ka time ho gaya hai (last_checked_at
    purana ho gaya), taaki ek run mein sab kuch ek saath check na ho. Isse
    load spread out rehta hai chahe 10 channels ho ya 10,000.
    """
    db = get_db()
    stale_before = _now() - POLL_STALE_AFTER
    due_channels = list(db.ct_channels.find({
        "added_count": {"$gt": 0},   # sirf active-tracked channels
        "$or": [{"last_checked_at": None}, {"last_checked_at": {"$lte": stale_before}}],
    }).limit(POLL_BATCH_SIZE))

    total_new = 0
    for channel in due_channels:
        try:
            total_new += check_channel_for_new_videos(channel)
        except Exception as e:
            print(f"poll failed for {channel.get('yt_channel_id')}: {e}")

    if due_channels:
        print(f"[creator_tracker] polled {len(due_channels)} channels, found {total_new} new videos")


# ============================================================================
# NOTIFICATIONS
# ============================================================================
def get_user_notifications(user_id, unseen_only: bool = False, limit: int = 50) -> list[dict]:
    db = get_db()
    query = {"user_id": ObjectId(user_id)}
    if unseen_only:
        query["seen"] = False

    notifs = list(db.ct_notifications.find(query).sort("created_at", -1).limit(limit))
    video_ids = [n["video_id"] for n in notifs]
    channel_ids = [n["channel_id"] for n in notifs]
    videos = {v["_id"]: v for v in db.ct_videos.find({"_id": {"$in": video_ids}})}
    channels = {c["_id"]: c for c in db.ct_channels.find({"_id": {"$in": channel_ids}})}

    return [{
        "notification": n,
        "video": videos.get(n["video_id"]),
        "channel": channels.get(n["channel_id"]),
    } for n in notifs]


def mark_notification_seen(notification_id):
    get_db().ct_notifications.update_one({"_id": ObjectId(notification_id)}, {"$set": {"seen": True}})


# ============================================================================
# FLASK BLUEPRINT
# ============================================================================
creator_tracker_bp = Blueprint("creator_tracker", __name__, url_prefix="/creator-tracker")


# ============================================================================
# DB ERROR HANDLING — agar Mongo us waqt reachable na ho (flaky network/DNS,
# jo Windows par ho sakta hai), koi bhi ek route fail ho to POORA APP CRASH
# NAHI hona chahiye. Yeh decorator har DB-touching route pe lagta hai: error
# ko pakadta hai, TERMINAL/CMD mein clearly print karta hai (taaki tumhe pata
# chale connection ka status kya hai), aur user ko ek clean JSON error deta
# hai — raw Python traceback browser mein kabhi nahi jaata.
# ============================================================================
def _handle_db_errors(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except PyMongoError as e:
            print(f"[creator_tracker] DB UNREACHABLE in '{fn.__name__}': {e}")
            return jsonify({"error": "Database temporarily unreachable. Please try again in a moment."}), 503
    return wrapper


@creator_tracker_bp.route("/health", methods=["GET"])
def health_check():
    """
    Isse hit karke terminal/browser dono jagah pata chal sakta hai ki DB
    connect hai ya nahi — bilkul tumhare project ke existing /health/db
    jaisa hi, isi module ke liye.
    """
    try:
        get_db().client.admin.command("ping")
        print("[creator_tracker] health check: MongoDB CONNECTED")
        return jsonify({"connected": True})
    except Exception as e:
        print(f"[creator_tracker] health check: MongoDB NOT reachable — {e}")
        return jsonify({"connected": False, "error": str(e)}), 503


def _current_user_id():
    return session.get("user_id")


def _require_login():
    uid = _current_user_id()
    if not uid:
        return None, (jsonify({"error": "login required"}), 401)
    return uid, None


@creator_tracker_bp.route("/meta", methods=["GET"])
def meta():
    """Onboarding form ke liye — categories/languages/countries/formats list."""
    return jsonify({
        "categories": CATEGORIES,
        "languages": LANGUAGES,
        "countries": [{"code": code, "name": name} for code, name in COUNTRIES],
        "content_formats": CONTENT_FORMATS,
        "channel_sizes": [
            {"value": "established", "label": "Established (10k+ subscribers)"},
            {"value": "any", "label": "Any size"},
            {"value": "emerging", "label": "Emerging creators (under 10k)"},
        ],
    })


@creator_tracker_bp.route("/preferences", methods=["GET", "POST"])
@_handle_db_errors
def preferences():
    uid, err = _require_login()
    if err:
        return err

    if request.method == "GET":
        prefs = get_user_preferences(uid)
        return jsonify(_serialize_preferences(prefs))

    body = request.get_json(force=True, silent=True) or {}
    doc = save_user_preferences(
        uid,
        categories=body.get("categories", []),
        languages=body.get("languages", []),
        country=body.get("country", ""),
        content_format=body.get("content_format", "both"),
        channel_size=body.get("channel_size", "established"),
    )
    return jsonify(_serialize_preferences(doc))


@creator_tracker_bp.route("/categories", methods=["GET", "POST"])
@_handle_db_errors
def categories():
    uid, err = _require_login()
    if err:
        return err

    if request.method == "POST":
        body = request.get_json(force=True, silent=True) or {}
        name = (body.get("name") or "").strip()
        if not name:
            return jsonify({"error": "name is required"}), 400
        cat = create_user_category(uid, name)
        return jsonify({"_id": str(cat["_id"]), "name": cat["name"]})

    cats = list_user_categories(uid)
    return jsonify([{"_id": str(c["_id"]), "name": c["name"]} for c in cats])


@creator_tracker_bp.route("/recommendations", methods=["GET"])
@_handle_db_errors
def recommendations():
    uid, err = _require_login()
    if err:
        return err
    return jsonify([_serialize_channel(c) for c in recommend_channels(uid)])


@creator_tracker_bp.route("/seed/plan", methods=["GET"])
@_handle_db_errors
def seed_plan():
    """
    User ki preferences ke hisaab se batata hai kaunse (category, language)
    combos already "warm" hain (pehle se seed ho chuke, turant load hoga) aur
    kaunse "cold" hain (naye, inhe seed karna padega). Frontend isse real
    progress bar banata hai — koi fake/guessed progress nahi.
    """
    uid, err = _require_login()
    if err:
        return err

    prefs = get_user_preferences(uid)
    if not prefs or not prefs.get("categories"):
        return jsonify({"combos": []})

    db = get_db()
    country = prefs.get("country")
    languages = prefs.get("languages") or [None]

    combos = []
    for category in prefs["categories"]:
        for language in languages:
            seed_filter = {"category": category, "language": language, "country": country}
            existing = db.ct_seed_log.find_one(seed_filter)
            warm = bool(
                existing and existing.get("seeded_at")
                and existing["seeded_at"] > _now() - SEED_STALE_AFTER
            )
            combos.append({"category": category, "language": language, "warm": warm})

    return jsonify({"combos": combos})


@creator_tracker_bp.route("/seed/run", methods=["POST"])
@_handle_db_errors
def seed_run():
    """Ek (category, language) combo ko actually seed karta hai — frontend inhe ek-ek karke call karta hai, progress bar update karte hue."""
    uid, err = _require_login()
    if err:
        return err

    body = request.get_json(force=True, silent=True) or {}
    category = body.get("category")
    language = body.get("language")
    if not category:
        return jsonify({"error": "category is required"}), 400

    prefs = get_user_preferences(uid)
    country = (prefs or {}).get("country")

    added = ensure_seeded(category, language, country)
    return jsonify({"category": category, "language": language, "added": added or 0})


@creator_tracker_bp.route("/channels/search", methods=["GET"])
@_handle_db_errors
def channels_search():
    """Naya channel add karne se pehle preview — query se resolve karta hai."""
    uid, err = _require_login()
    if err:
        return err
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "q is required"}), 400
    resolved = resolve_channel_from_query(query)
    if not resolved:
        return jsonify({"found": False})

    already_tracked, _ = is_channel_tracked_by_user(uid, resolved["yt_channel_id"])
    return jsonify({"found": True, "channel": resolved, "already_tracked": already_tracked})


@creator_tracker_bp.route("/channels/subscribe", methods=["POST"])
@_handle_db_errors
def channels_subscribe():
    uid, err = _require_login()
    if err:
        return err
    body = request.get_json(force=True, silent=True) or {}
    query = body.get("query", "").strip()
    user_category_id = body.get("category_id")   # user's own dashboard folder
    tag_category = body.get("tag_category")       # recommendation taxonomy tag
    if not query:
        return jsonify({"error": "query is required"}), 400

    prefs = get_user_preferences(uid)
    channel = get_or_create_channel(
        query,
        tag_category=tag_category,
        tag_language=(prefs or {}).get("languages", [None])[0] if prefs else None,
        tag_country=(prefs or {}).get("country") if prefs else None,
    )
    if not channel:
        return jsonify({"error": "channel not found"}), 404

    subscribe_user_to_channel(uid, channel["_id"], user_category_id)
    return jsonify({"ok": True, "channel": _serialize_channel(channel)})


@creator_tracker_bp.route("/channels/unsubscribe", methods=["POST"])
@_handle_db_errors
def channels_unsubscribe():
    uid, err = _require_login()
    if err:
        return err
    body = request.get_json(force=True, silent=True) or {}
    channel_id = body.get("channel_id")
    if not channel_id:
        return jsonify({"error": "channel_id is required"}), 400
    unsubscribe_user_from_channel(uid, channel_id)
    return jsonify({"ok": True})


@creator_tracker_bp.route("/channels/category", methods=["POST"])
@_handle_db_errors
def channels_reassign_category():
    """User apne kisi tracked channel ka dashboard-folder (category) badalta hai."""
    uid, err = _require_login()
    if err:
        return err
    body = request.get_json(force=True, silent=True) or {}
    channel_id = body.get("channel_id")
    category_id = body.get("category_id")   # None/empty = "Uncategorized"
    if not channel_id:
        return jsonify({"error": "channel_id is required"}), 400
    reassign_channel_category(uid, channel_id, category_id)
    return jsonify({"ok": True})


@creator_tracker_bp.route("/seed/health", methods=["GET"])
@_handle_db_errors
def seed_health():
    """Har category mein pool mein kitne channels hain — Discover tab ke transparency panel ke liye."""
    return jsonify({"categories": get_seed_pool_health()})


@creator_tracker_bp.route("/channels", methods=["GET"])
@_handle_db_errors
def channels_list():
    uid, err = _require_login()
    if err:
        return err
    out = []
    for r in list_user_channels(uid):
        out.append({
            "channel": _serialize_channel(r["channel"]),
            "category_id": str(r["subscription"]["category_id"]) if r["subscription"].get("category_id") else None,
            "latest_videos": [_serialize_video(v) for v in r["latest_videos"]],
        })
    return jsonify(out)


@creator_tracker_bp.route("/notifications", methods=["GET"])
@_handle_db_errors
def notifications_list():
    uid, err = _require_login()
    if err:
        return err
    unseen_only = request.args.get("unseen_only") == "1"
    out = []
    for r in get_user_notifications(uid, unseen_only=unseen_only):
        out.append({
            "id": str(r["notification"]["_id"]),
            "seen": r["notification"]["seen"],
            "created_at": r["notification"]["created_at"].isoformat(),
            "video": _serialize_video(r["video"]) if r["video"] else None,
            "channel": _serialize_channel(r["channel"]) if r["channel"] else None,
        })
    return jsonify(out)


@creator_tracker_bp.route("/notifications/<notification_id>/seen", methods=["POST"])
@_handle_db_errors
def notifications_mark_seen(notification_id):
    uid, err = _require_login()
    if err:
        return err
    mark_notification_seen(notification_id)
    return jsonify({"ok": True})


# ============================================================================
# PAGE — single self-contained HTML page (no separate template file needed).
# Vanilla JS, fetch() the JSON routes above. Uses the SAME session cookie as
# the rest of the app (login()/session["user_id"] in app.py), so agar user
# already logged in hai to yeh page seedha kaam karega.
# ============================================================================
@creator_tracker_bp.route("/", methods=["GET"])
def dashboard_page():
    if not _current_user_id():
        return redirect(url_for("login"))
    return render_template_string(_PAGE_HTML)


_PAGE_HTML = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Creator Tracker</title>
<style>
  :root {
    --ink:#101114; --surface:#1A1C21; --surface-2:#20232A; --border:#2A2D35;
    --text:#EDEDEF; --text-dim:#9A9DA6; --accent:#F2A93B; --accent-text:#1A1206;
    --ok:#3DD68C; --fail:#FF6259;
  }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--ink); color:var(--text); font-family: Inter, system-ui, sans-serif; }
  .wrap { max-width: 1000px; margin: 0 auto; padding: 28px 20px 80px; }
  h1 { font-size: 22px; margin-bottom: 4px; }
  h2 { font-size: 15px; color: var(--text-dim); text-transform: uppercase; letter-spacing: .04em; margin: 32px 0 12px; }
  .card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 16px; }
  .row { display:flex; gap:8px; flex-wrap:wrap; }
  .chip { padding:6px 12px; border-radius:20px; border:1px solid var(--border); background:var(--surface-2);
          color:var(--text-dim); font-size:13px; cursor:pointer; user-select:none; }
  .chip.selected { background:var(--accent); color:var(--accent-text); border-color:var(--accent); }
  select, input[type=text] { background:var(--surface-2); border:1px solid var(--border); color:var(--text);
          border-radius:8px; padding:8px 10px; font-size:14px; }
  button { background:var(--accent); color:var(--accent-text); border:none; border-radius:8px;
          padding:8px 16px; font-size:14px; font-weight:600; cursor:pointer; display:inline-flex; align-items:center; gap:8px; }
  button.secondary { background:var(--surface-2); color:var(--text); border:1px solid var(--border); }
  button.tracked { background: var(--surface-2); color: var(--ok); border: 1px solid var(--ok); cursor:default; }
  button:disabled { opacity:.6; cursor:default; }
  .grid { display:grid; grid-template-columns: repeat(auto-fill, minmax(260px,1fr)); gap:12px; }
  .ch-card { background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:12px; }
  .ch-head { display:flex; gap:10px; align-items:center; }
  .ch-head img { width:44px; height:44px; border-radius:50%; object-fit:cover; background:var(--surface-2); }
  .ch-title { font-weight:600; font-size:14px; }
  .ch-sub { font-size:12px; color:var(--text-dim); }
  .video-row { display:flex; gap:8px; margin-top:10px; padding-top:10px; border-top:1px solid var(--border); }
  .video-row img { width:80px; height:45px; border-radius:6px; object-fit:cover; background:var(--surface-2); flex-shrink:0; }
  .video-title { font-size:13px; line-height:1.3; }
  .video-stats { font-size:11px; color:var(--text-dim); margin-top:3px; }
  .video-title a { color:var(--text); text-decoration:none; }
  .video-title a:hover { color:var(--accent); }
  .notif-item { display:flex; gap:10px; padding:10px 0; border-bottom:1px solid var(--border); align-items:center; }
  .notif-item img { width:56px; height:31px; border-radius:6px; object-fit:cover; }
  .dot { width:8px; height:8px; border-radius:50%; background:var(--accent); flex-shrink:0; }
  .muted { color:var(--text-dim); font-size:13px; }
  .tabs { display:flex; gap:4px; margin-bottom:16px; }
  .tab-btn { padding:8px 14px; border-radius:8px; background:transparent; color:var(--text-dim); border:1px solid var(--border); }
  .tab-btn.active { background:var(--surface-2); color:var(--text); border-color:var(--accent); }
  .tab-panel { display:none; }
  .tab-panel.active { display:block; }
  .save-bar { margin-top:14px; display:flex; align-items:center; gap:10px; }

  .spinner { width:14px; height:14px; border-radius:50%; border:2px solid rgba(0,0,0,.25);
             border-top-color: var(--accent-text); animation: spin .7s linear infinite; flex-shrink:0; }
  .spinner.light { border-color: rgba(255,255,255,.25); border-top-color: var(--text); }
  @keyframes spin { to { transform: rotate(360deg); } }
  .save-status { font-size:13px; }
  .save-status.ok { color: var(--ok); }
  .save-status.fail { color: var(--fail); }

  .progress-wrap { margin: 8px 0 20px; }
  .progress-track { width:100%; height:8px; border-radius:6px; background: var(--surface-2); overflow:hidden; }
  .progress-fill { height:100%; background: var(--accent); width:0%; transition: width .25s ease; }
  .progress-label { font-size:12px; color:var(--text-dim); margin-top:6px; }

  .pool-health { display:flex; flex-wrap:wrap; gap:6px; margin: 6px 0 4px; }
  .pool-pill { font-size:11px; color:var(--text-dim); background:var(--surface-2); border:1px solid var(--border);
               border-radius:12px; padding:3px 9px; }
  .pool-toggle { font-size:12px; color: var(--text-dim); cursor:pointer; text-decoration: underline; margin-bottom: 8px; display:inline-block; }
  .cat-select-inline { font-size:12px; padding:4px 6px; margin-top:8px; width:100%; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Creator Tracker</h1>
  <div class="muted" id="statusLine"></div>

  <div class="tabs">
    <button class="tab-btn active" data-tab="pref">Preferences</button>
    <button class="tab-btn" data-tab="discover">Discover / Add</button>
    <button class="tab-btn" data-tab="channels">My Channels</button>
    <button class="tab-btn" data-tab="notifs">Notifications <span id="notifBadge"></span></button>
  </div>

  <!-- PREFERENCES TAB -->
  <div class="tab-panel active" id="tab-pref">
    <div class="card">
      <h2 style="margin-top:0">Categories</h2>
      <div class="row" id="categoryChips"></div>

      <h2>Languages</h2>
      <div class="row" id="languageChips"></div>

      <h2>Country</h2>
      <select id="countrySelect"></select>

      <h2>Content format</h2>
      <select id="formatSelect">
        <option value="both">Both</option>
        <option value="shorts">Shorts only</option>
        <option value="long_form">Long-form only</option>
      </select>

      <h2>Channel size</h2>
      <select id="channelSizeSelect"></select>

      <div class="save-bar">
        <button id="saveBtn" onclick="savePreferences()">Save preferences</button>
        <span class="save-status" id="saveStatus"></span>
      </div>
    </div>
  </div>

  <!-- DISCOVER TAB -->
  <div class="tab-panel" id="tab-discover">
    <div class="card">
      <h2 style="margin-top:0">Add a channel</h2>
      <div class="row">
        <input type="text" id="searchInput" placeholder="@handle, channel URL, or channel name" style="flex:1; min-width:220px">
        <button onclick="searchChannel()">Search</button>
      </div>
      <div id="searchResult" style="margin-top:12px"></div>
    </div>

    <h2>Recommended for you</h2>
    <span class="pool-toggle" id="poolToggle" onclick="togglePoolHealth()" style="display:none">Show channel pool stats</span>
    <div class="pool-health" id="poolHealth" style="display:none"></div>
    <div class="progress-wrap" id="seedProgressWrap" style="display:none">
      <div class="progress-track"><div class="progress-fill" id="seedProgressFill"></div></div>
      <div class="progress-label" id="seedProgressLabel">Starting...</div>
    </div>
    <div class="grid" id="recommendGrid"><div class="muted">Set your preferences first.</div></div>
  </div>

  <!-- MY CHANNELS TAB -->
  <div class="tab-panel" id="tab-channels">
    <div class="grid" id="myChannelsGrid"><div class="muted">No channels tracked yet.</div></div>
  </div>

  <!-- NOTIFICATIONS TAB -->
  <div class="tab-panel" id="tab-notifs">
    <div class="card" id="notifList"><div class="muted">No notifications yet.</div></div>
  </div>
</div>

<script>
const META_URL = "/creator-tracker/meta";
const PREFS_URL = "/creator-tracker/preferences";
const SEED_PLAN_URL = "/creator-tracker/seed/plan";
const SEED_RUN_URL = "/creator-tracker/seed/run";
const SEED_HEALTH_URL = "/creator-tracker/seed/health";
const RECS_URL = "/creator-tracker/recommendations";
const SEARCH_URL = "/creator-tracker/channels/search";
const SUB_URL = "/creator-tracker/channels/subscribe";
const UNSUB_URL = "/creator-tracker/channels/unsubscribe";
const CATEGORY_REASSIGN_URL = "/creator-tracker/channels/category";
const CHANNELS_URL = "/creator-tracker/channels";
const CATEGORIES_URL = "/creator-tracker/categories";
const NOTIFS_URL = "/creator-tracker/notifications";

let selectedCategories = new Set();
let selectedLanguages = new Set();
let userCategories = [];   // user ke apne dashboard-folders (My Channels ke liye)

document.querySelectorAll(".tab-btn").forEach(btn => {
  btn.addEventListener("click", () => switchTab(btn.dataset.tab));
});

function switchTab(tabName) {
  document.querySelectorAll(".tab-btn").forEach(b => b.classList.toggle("active", b.dataset.tab === tabName));
  document.querySelectorAll(".tab-panel").forEach(p => p.classList.remove("active"));
  document.getElementById("tab-" + tabName).classList.add("active");
  if (tabName === "discover") loadRecommendationsWithProgress();
  if (tabName === "channels") loadMyChannels();
  if (tabName === "notifs") loadNotifications();
}

async function api(url, opts) {
  const resp = await fetch(url, opts);
  if (resp.status === 401) {
    document.getElementById("statusLine").textContent = "Please log in first.";
    throw new Error("not logged in");
  }
  if (!resp.ok) {
    let msg = "Something went wrong (status " + resp.status + ").";
    try { const body = await resp.json(); if (body.error) msg = body.error; } catch (e) {}
    document.getElementById("statusLine").textContent = msg;
    throw new Error(msg);
  }
  return resp.json();
}

async function init() {
  const meta = await api(META_URL);

  document.getElementById("categoryChips").innerHTML = meta.categories.map(c =>
    `<span class="chip" data-val="${c}" onclick="toggleChip(this, selectedCategories)">${c}</span>`).join("");
  document.getElementById("languageChips").innerHTML = meta.languages.map(l =>
    `<span class="chip" data-val="${l}" onclick="toggleChip(this, selectedLanguages)">${l}</span>`).join("");
  document.getElementById("countrySelect").innerHTML = meta.countries.map(c =>
    `<option value="${c.code}">${c.name}</option>`).join("");
  document.getElementById("channelSizeSelect").innerHTML = meta.channel_sizes.map(s =>
    `<option value="${s.value}">${s.label}</option>`).join("");

  const prefs = await api(PREFS_URL);
  if (prefs && prefs.categories) {
    prefs.categories.forEach(c => {
      selectedCategories.add(c);
      const el = document.querySelector(`#categoryChips [data-val="${c}"]`);
      if (el) el.classList.add("selected");
    });
    (prefs.languages || []).forEach(l => {
      selectedLanguages.add(l);
      const el = document.querySelector(`#languageChips [data-val="${l}"]`);
      if (el) el.classList.add("selected");
    });
    if (prefs.country) document.getElementById("countrySelect").value = prefs.country;
    if (prefs.content_format) document.getElementById("formatSelect").value = prefs.content_format;
    if (prefs.channel_size) document.getElementById("channelSizeSelect").value = prefs.channel_size;
  }

  try {
    userCategories = await api(CATEGORIES_URL);
  } catch (e) { userCategories = []; }

  loadMyChannels();
  loadNotifications();
}

function toggleChip(el, set) {
  const val = el.dataset.val;
  if (set.has(val)) { set.delete(val); el.classList.remove("selected"); }
  else { set.add(val); el.classList.add("selected"); }
}

async function savePreferences() {
  const btn = document.getElementById("saveBtn");
  const status = document.getElementById("saveStatus");

  if (!selectedCategories.size) {
    status.className = "save-status fail";
    status.textContent = "Pick at least one category first.";
    return;
  }

  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Saving...';
  status.className = "save-status";
  status.textContent = "";

  try {
    const body = {
      categories: Array.from(selectedCategories),
      languages: Array.from(selectedLanguages),
      country: document.getElementById("countrySelect").value,
      content_format: document.getElementById("formatSelect").value,
      channel_size: document.getElementById("channelSizeSelect").value,
    };
    await api(PREFS_URL, { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body) });

    btn.innerHTML = "&#10003; Saved";
    status.className = "save-status ok";
    status.textContent = "Preferences saved — taking you to Discover...";

    setTimeout(() => {
      btn.disabled = false;
      btn.textContent = "Save preferences";
      status.textContent = "";
      switchTab("discover");
    }, 900);
  } catch (e) {
    btn.disabled = false;
    btn.textContent = "Save preferences";
    status.className = "save-status fail";
    status.textContent = "Could not save — please retry.";
  }
}

async function searchChannel() {
  const q = document.getElementById("searchInput").value.trim();
  if (!q) return;
  const box = document.getElementById("searchResult");
  box.innerHTML = '<span class="muted">Searching...</span>';
  const result = await api(SEARCH_URL + "?q=" + encodeURIComponent(q));
  if (!result.found) { box.innerHTML = '<span class="muted">Channel not found.</span>'; return; }
  const c = result.channel;
  const btnHtml = result.already_tracked
    ? `<button class="tracked" disabled>&#10003; Already tracked</button>`
    : `<button id="trackBtn_manual" onclick="addChannel('${q.replace(/'/g,"\\'")}', 'trackBtn_manual')">Track this channel</button>`;
  box.innerHTML = `
    <div class="ch-card">
      <div class="ch-head">
        <img src="${c.thumbnail || ''}">
        <div><div class="ch-title">${c.title}</div><div class="ch-sub">${(c.subscriber_count||0).toLocaleString()} subscribers</div></div>
      </div>
      <div class="row" style="margin-top:10px">${btnHtml}</div>
    </div>`;
}

async function addChannel(query, buttonId) {
  const btn = buttonId ? document.getElementById(buttonId) : null;
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner light"></span> Adding...';
  }
  try {
    const tagCategory = selectedCategories.size ? Array.from(selectedCategories)[0] : null;
    await api(SUB_URL, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ query, tag_category: tagCategory }),
    });
    if (btn) {
      btn.className = "tracked";
      btn.innerHTML = "&#10003; Tracked";
      btn.disabled = true;
    }
    loadMyChannels();
  } catch (e) {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "Track this channel";
    }
  }
}

function togglePoolHealth() {
  const panel = document.getElementById("poolHealth");
  const isHidden = panel.style.display === "none";
  panel.style.display = isHidden ? "flex" : "none";
  document.getElementById("poolToggle").textContent = isHidden ? "Hide channel pool stats" : "Show channel pool stats";
}

async function loadPoolHealth() {
  try {
    const health = await api(SEED_HEALTH_URL);
    const nonZero = health.categories.filter(c => c.count > 0);
    if (!nonZero.length) return;
    document.getElementById("poolToggle").style.display = "inline-block";
    document.getElementById("poolHealth").innerHTML = nonZero
      .map(c => `<span class="pool-pill">${c.category}: ${c.count}</span>`).join("");
  } catch (e) { /* non-critical, skip silently */ }
}

// ---- Discover tab: REAL progress (not a fake animation) ----
async function loadRecommendationsWithProgress() {
  const grid = document.getElementById("recommendGrid");
  const wrap = document.getElementById("seedProgressWrap");
  const fill = document.getElementById("seedProgressFill");
  const label = document.getElementById("seedProgressLabel");

  loadPoolHealth();

  let plan;
  try {
    plan = await api(SEED_PLAN_URL);
  } catch (e) {
    return;
  }

  const combos = plan.combos || [];
  const coldCombos = combos.filter(c => !c.warm);

  if (!combos.length) {
    grid.innerHTML = '<div class="muted">Set your preferences first to see recommendations.</div>';
    return;
  }

  if (coldCombos.length === 0) {
    wrap.style.display = "none";
    return loadRecommendations();
  }

  wrap.style.display = "block";
  grid.innerHTML = "";
  let done = 0;
  const total = coldCombos.length;

  for (const combo of coldCombos) {
    const langLabel = combo.language ? ` (${combo.language})` : "";
    label.textContent = `Finding ${combo.category}${langLabel} channels for you... (${done + 1}/${total})`;
    try {
      await api(SEED_RUN_URL, {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ category: combo.category, language: combo.language }),
      });
    } catch (e) {
      // ek combo fail ho to baaki continue karte hain
    }
    done += 1;
    fill.style.width = Math.round((done / total) * 100) + "%";
  }

  label.textContent = "Ranking your recommendations...";
  await loadRecommendations();
  loadPoolHealth();
  wrap.style.display = "none";
}

async function loadRecommendations() {
  const grid = document.getElementById("recommendGrid");
  const recs = await api(RECS_URL);
  // Backend already excludes channels the user is tracking — koi bhi
  // already-tracked channel yahan dikhega hi nahi.
  if (!recs.length) { grid.innerHTML = '<div class="muted">No matches yet — try adding a channel manually above, or widen your language/category/channel-size choices in Preferences.</div>'; return; }
  grid.innerHTML = recs.map((c, i) => `
    <div class="ch-card">
      <div class="ch-head">
        <img src="${c.thumbnail || ''}">
        <div><div class="ch-title">${c.title}</div><div class="ch-sub">${(c.subscriber_count||0).toLocaleString()} subs · ${c.language||''}</div></div>
      </div>
      <div class="row" style="margin-top:10px">
        <button id="trackBtn_rec_${i}" onclick="addChannel('${c.yt_channel_id}', 'trackBtn_rec_${i}')">Track</button>
      </div>
    </div>`).join("");
}

async function loadMyChannels() {
  const grid = document.getElementById("myChannelsGrid");
  const rows = await api(CHANNELS_URL);
  if (!rows.length) { grid.innerHTML = '<div class="muted">No channels tracked yet — add one from the Discover tab.</div>'; return; }

  const categoryOptions = `<option value="">Uncategorized</option>` +
    userCategories.map(c => `<option value="${c._id}">${c.name}</option>`).join("");

  // Backend already sorts by most-recent-video-first — hum yahan waisa hi render karte hain
  grid.innerHTML = rows.map(r => {
    const c = r.channel;
    const videos = r.latest_videos.map(v => `
      <div class="video-row">
        <img src="${v.thumbnail || ''}">
        <div>
          <div class="video-title"><a href="${v.url}" target="_blank">${v.title}</a></div>
          <div class="video-stats">${(v.stats.views||0).toLocaleString()} views · ${(v.stats.likes||0).toLocaleString()} likes</div>
        </div>
      </div>`).join("") || '<div class="muted" style="margin-top:8px">No videos synced yet.</div>';

    const selected = r.category_id || "";
    const catSelectHtml = userCategories.length
      ? `<select class="cat-select-inline" onchange="reassignCategory('${c.id}', this.value)">
           ${categoryOptions.replace(`value="${selected}"`, `value="${selected}" selected`)}
         </select>`
      : "";

    return `
      <div class="ch-card">
        <div class="ch-head">
          <img src="${c.thumbnail || ''}">
          <div style="flex:1"><div class="ch-title">${c.title}</div><div class="ch-sub">${(c.subscriber_count||0).toLocaleString()} subscribers</div></div>
          <button class="secondary" onclick="unsubscribe('${c.id}')">Remove</button>
        </div>
        ${catSelectHtml}
        ${videos}
      </div>`;
  }).join("");
}

async function unsubscribe(channelId) {
  await api(UNSUB_URL, { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ channel_id: channelId }) });
  loadMyChannels();
}

async function reassignCategory(channelId, categoryId) {
  await api(CATEGORY_REASSIGN_URL, {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ channel_id: channelId, category_id: categoryId || null }),
  });
}

async function loadNotifications() {
  const list = document.getElementById("notifList");
  const rows = await api(NOTIFS_URL);
  const unseen = rows.filter(r => !r.seen).length;
  document.getElementById("notifBadge").textContent = unseen ? `(${unseen})` : "";
  if (!rows.length) { list.innerHTML = '<div class="muted">No notifications yet.</div>'; return; }
  list.innerHTML = rows.map(r => `
    <div class="notif-item">
      ${!r.seen ? '<span class="dot"></span>' : '<span style="width:8px;display:inline-block"></span>'}
      <img src="${r.video ? r.video.thumbnail : ''}">
      <div style="flex:1">
        <div style="font-size:13px">${r.channel ? r.channel.title : ''} posted a new video</div>
        <div class="video-title"><a href="${r.video ? r.video.url : '#'}" target="_blank">${r.video ? r.video.title : ''}</a></div>
      </div>
      ${!r.seen ? `<button class="secondary" onclick="markSeen('${r.id}')">Mark seen</button>` : ''}
    </div>`).join("");
}

async function markSeen(id) {
  await api(NOTIFS_URL + "/" + id + "/seen", { method: "POST" });
  loadNotifications();
}

init();
</script>
</body>
</html>
"""


# ============================================================================
# SERIALIZATION HELPERS (Mongo ObjectId/datetime -> JSON-safe)
# ============================================================================
def _serialize_preferences(p: dict | None) -> dict:
    if not p:
        return {}
    return {
        "categories": p.get("categories", []),
        "languages": p.get("languages", []),
        "country": p.get("country", ""),
        "content_format": p.get("content_format", "both"),
        "channel_size": p.get("channel_size", "established"),
        "updated_at": p["updated_at"].isoformat() if p.get("updated_at") else None,
    }


def _serialize_channel(c: dict) -> dict:
    return {
        "id": str(c["_id"]),
        "yt_channel_id": c.get("yt_channel_id"),
        "title": c.get("title"),
        "handle": c.get("handle"),
        "thumbnail": c.get("thumbnail"),
        "subscriber_count": c.get("subscriber_count", 0),
        "categories": c.get("categories", []),
        "language": c.get("language"),
        "country": c.get("country"),
        "channel_url": f"https://www.youtube.com/channel/{c.get('yt_channel_id')}",
    }


def _serialize_video(v: dict) -> dict:
    published_at = v.get("published_at")
    return {
        "id": str(v["_id"]),
        "yt_video_id": v.get("yt_video_id"),
        "title": v.get("title"),
        "thumbnail": v.get("thumbnail"),
        "url": v.get("url"),
        "published_at": published_at.isoformat() if hasattr(published_at, "isoformat") else published_at,
        "stats": v.get("stats", {}),
    }


# ============================================================================
# INIT — registers the blueprint + the background polling job.
# ============================================================================
def init_creator_tracker(app, scheduler=None):
    """
    app.py mein add karna hoga (abhi mat karo, pehle standalone review karo):

        from creator_tracker import init_creator_tracker
        init_creator_tracker(app)

    Agar apna khud ka APScheduler instance already chala rahe ho
    (services/scheduler.py wala), usko `scheduler=` param mein pass kar
    dena — do alag BackgroundScheduler chalane ki zaroorat nahi:

        from services.scheduler import start_scheduler
        sched = start_scheduler()
        init_creator_tracker(app, scheduler=sched)
    """
    app.register_blueprint(creator_tracker_bp)
    # NOTE: get_db() yahan JAAN-BUJH KAR call nahi kiya. Pehle yahan startup
    # pe hi ek turant DB connection try hoti thi — agar us exact waqt Atlas
    # se connection na bane (network blip), to POORA app crash ho jaata tha,
    # chahe baaki features (login, dashboard) ka isse koi lena-dena na ho.
    # Ab connection sirf tab banega jab koi creator-tracker route/job pehli
    # baar genuinely chalega (get_db() ke andar wala _indexes_ensured flag
    # yeh lazy-init khud sambhal leta hai) — app hamesha start hoga, chahe
    # us waqt Mongo thodi der ke liye unreachable ho.

    if scheduler is None:
        from apscheduler.schedulers.background import BackgroundScheduler
        scheduler = BackgroundScheduler()
        scheduler.start()

    scheduler.add_job(
        poll_all_tracked_channels, "interval",
        minutes=POLL_INTERVAL_MINUTES, id="ct_poll_channels", replace_existing=True,
    )
    return scheduler


# ============================================================================
# STANDALONE SELF-TEST — `python creator_tracker.py` chala ke dekh sakte ho
# ki Mongo connect ho raha hai aur RSS feed se video mil rahe hain, bina
# Flask app chalaye aur bina kisi API quota use kiye (channel resolve ke
# alawa, jo sirf ek baar hota hai).
# ============================================================================
if __name__ == "__main__":
    print("Checking MongoDB connection...")
    db = get_db()
    print(f"Connected. Collections so far: {db.list_collection_names()}")

    print("\nResolving a test channel (Google Developers)...")
    result = resolve_channel_from_query("@GoogleDevelopers")
    if not result:
        print("Not found — check GOOGLE_API_KEY in your .env")
    else:
        print(f"Found: {result['title']} ({result['yt_channel_id']}) — {result['subscriber_count']} subs")

        print("\nFetching latest videos from FREE RSS feed (no API quota used)...")
        feed_items = fetch_channel_feed(result["yt_channel_id"])
        for v in feed_items[:5]:
            print(f"  - {v['title']}  ({v['published_at']})")

        print("\nSimulating full ingest for one video (this DOES use 1 quota unit for stats)...")
        channel_doc = get_or_create_channel("@GoogleDevelopers", tag_category="Tech", tag_language="English")
        new_count = check_channel_for_new_videos(channel_doc)
        print(f"Ingested {new_count} new video(s) into ct_videos collection.")