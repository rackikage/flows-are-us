#!/usr/bin/env python3
"""server.py — FLOWS social hub server.

FastAPI app over the Composio clean pipe (composio_pipe.ComposioPipe).
Serves the FLOWS dark UI from ./static and exposes:

  GET  /api/accounts   — wired account config
  GET  /api/overview   — profiles + recent media + quota for every account
  GET  /api/insights   — 7-day IG insights per account (best effort)
  GET  /api/scheduled  — scheduled FB page posts
  GET  /api/library    — unified content library across every account
  GET  /api/qr         — scan-to-open QR + LAN URL for phones
  POST /api/post       — publish or schedule: photo, video/reel, carousel,
                         story, text — routed per platform

Run:  python3 social-hub/server.py   →  http://<lan-ip>:8787 (all interfaces,
so iPhones and Samsung/Android phones on the same Wi-Fi can open and install
it as an app).
"""

import os
import socket
import time
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import qr
from composio_pipe import ComposioPipe, PipeError

HERE = os.path.dirname(os.path.abspath(__file__))

ACCOUNTS = [
    {
        "key": "ig_rossmore", "platform": "instagram",
        "handle": "@rossmorebuilding", "name": "Rossmore Building",
        "account": "instagram_okie-ceile", "entity_id": "27412531841701728",
    },
    {
        "key": "ig_human", "platform": "instagram",
        "handle": "@human247.365", "name": "Human 247/365",
        "account": "instagram_anode-conch", "entity_id": "27330220746665792",
    },
    {
        "key": "fb_human", "platform": "facebook",
        "handle": "Human 247/365", "name": "Human 247/365 (Page)",
        "account": "facebook_uberty-minor", "entity_id": "1154954434369587",
    },
]
BY_KEY = {a["key"]: a for a in ACCOUNTS}

IG_MEDIA_FIELDS = ("id,caption,media_type,media_url,thumbnail_url,permalink,"
                   "timestamp,like_count,comments_count,media_product_type")
FB_POST_FIELDS = ("id,message,created_time,permalink_url,full_picture,"
                  "reactions.summary(true),comments.summary(true),shares")

app = FastAPI(title="FLOWS social hub")
pipe = ComposioPipe()

_cache = {}
_cache_lock = threading.Lock()
CACHE_TTL = 90


def cached(key, fresh, builder):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and not fresh and now - hit[0] < CACHE_TTL:
            return hit[1]
    value = builder()
    with _cache_lock:
        _cache[key] = (time.time(), value)
    return value


def scrub(obj):
    """Drop token-like fields from Graph responses before they reach the UI."""
    if isinstance(obj, dict):
        return {k: scrub(v) for k, v in obj.items()
                if "access_token" not in k and "token" != k}
    if isinstance(obj, list):
        return [scrub(v) for v in obj]
    return obj


def unwrap_list(data):
    """IG/FB list payloads nest under data (sometimes data.data)."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        inner = data.get("data")
        if isinstance(inner, list):
            return inner
        if isinstance(inner, dict) and isinstance(inner.get("data"), list):
            return inner["data"]
    return []


# ── Read endpoints ─────────────────────────────────────────────────────────

@app.get("/api/accounts")
def api_accounts():
    return {"accounts": ACCOUNTS}


@app.get("/api/overview")
def api_overview(fresh: bool = False):
    def build():
        tools, index = [], []
        for a in ACCOUNTS:
            if a["platform"] == "instagram":
                tools.append({"tool_slug": "INSTAGRAM_GET_USER_INFO",
                              "arguments": {"ig_user_id": "me"},
                              "account": a["account"]})
                index.append((a["key"], "profile"))
                tools.append({"tool_slug": "INSTAGRAM_GET_IG_USER_MEDIA",
                              "arguments": {"ig_user_id": "me", "limit": 12,
                                            "fields": IG_MEDIA_FIELDS},
                              "account": a["account"]})
                index.append((a["key"], "media"))
                tools.append({"tool_slug":
                              "INSTAGRAM_GET_IG_USER_CONTENT_PUBLISHING_LIMIT",
                              "arguments": {"ig_user_id": "me"},
                              "account": a["account"]})
                index.append((a["key"], "quota"))
            else:
                tools.append({"tool_slug": "FACEBOOK_GET_PAGE_DETAILS",
                              "arguments": {"page_id": a["entity_id"],
                                            "fields": "id,name,about,category,"
                                                      "fan_count,followers_count,"
                                                      "picture,link"},
                              "account": a["account"]})
                index.append((a["key"], "profile"))
                tools.append({"tool_slug": "FACEBOOK_GET_PAGE_POSTS",
                              "arguments": {"page_id": a["entity_id"],
                                            "limit": 12,
                                            "fields": FB_POST_FIELDS},
                              "account": a["account"]})
                index.append((a["key"], "media"))
        results = pipe.execute_batch(tools, thought="hub overview refresh")
        out = {a["key"]: {**a, "profile": None, "media": [], "quota": None,
                          "errors": []} for a in ACCOUNTS}
        for (key, kind), res in zip(index, results):
            slot = out[key]
            if not res["successful"]:
                slot["errors"].append({kind: str(res["error"])[:300]})
                continue
            data = scrub(res["data"] or {})
            if kind == "profile":
                slot["profile"] = data
            elif kind == "media":
                slot["media"] = unwrap_list(data)
            elif kind == "quota":
                entries = unwrap_list(data)
                if entries:
                    slot["quota"] = {"used": entries[0].get("quota_usage"),
                                     "limit": 25}
        return {"generated_at": datetime.now(timezone.utc).isoformat(),
                "accounts": [out[a["key"]] for a in ACCOUNTS]}

    try:
        return cached("overview", fresh, build)
    except (PipeError, Exception) as e:  # surface as JSON, not a 500 page
        return JSONResponse(status_code=502, content={"error": str(e)[:400]})


# ── Avatar proxy ────────────────────────────────────────────────────────────
# IG/FB profile pictures are signed CDN URLs that expire, so the UI's avatars
# would blank out (fall back to initials) on refresh. We proxy them same-origin
# and keep the last-good bytes on disk, so a picture that ever loaded stays put
# even after its upstream URL dies or the pipe is briefly down.

AVATAR_DIR = os.path.join(HERE, ".avatar_cache")
os.makedirs(AVATAR_DIR, exist_ok=True)
_avatar_mem = {}                 # key -> (fetched_at, bytes, content_type)
_avatar_lock = threading.Lock()
AVATAR_TTL = 6 * 3600            # re-pull from upstream at most every 6h

# macOS system Python can't find CA roots on its own, so verifying IG/FB CDN
# certs fails without a bundle — use certifi's when present.
import ssl
try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _SSL_CTX = ssl.create_default_context()


def _profile_pic_url(key):
    """Current upstream profile-picture URL for an account, from the overview."""
    ov = api_overview(fresh=False)
    if isinstance(ov, JSONResponse):
        return None
    for a in ov.get("accounts", []):
        if a.get("key") == key:
            p = a.get("profile") or {}
            pic = p.get("profile_picture_url")
            if not pic:
                data = (p.get("picture") or {}).get("data") or {}
                pic = data.get("url")
            return pic
    return None


def _avatar_disk(key):
    path = os.path.join(AVATAR_DIR, key)
    if os.path.exists(path):
        try:
            with open(path, "rb") as f:
                return f.read()
        except OSError:
            return None
    return None


@app.get("/api/avatar")
def api_avatar(key: str):
    if key not in BY_KEY:
        return JSONResponse(status_code=404, content={"error": "unknown account"})

    now = time.time()
    with _avatar_lock:
        hit = _avatar_mem.get(key)
    if hit and now - hit[0] < AVATAR_TTL:
        return Response(content=hit[1], media_type=hit[2],
                        headers={"Cache-Control": "public, max-age=86400"})

    url = _profile_pic_url(key)
    if url:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "FLOWS/1.0"})
            try:
                resp = urllib.request.urlopen(req, timeout=8, context=_SSL_CTX)
            except ssl.SSLError:
                # Last-resort for hosts without a usable CA bundle — these are
                # public avatar images and we send no credentials.
                resp = urllib.request.urlopen(
                    req, timeout=8, context=ssl._create_unverified_context())
            with resp:
                data = resp.read()
                ctype = resp.headers.get("Content-Type", "image/jpeg")
            with _avatar_lock:
                _avatar_mem[key] = (now, data, ctype)
            try:
                with open(os.path.join(AVATAR_DIR, key), "wb") as f:
                    f.write(data)
            except OSError:
                pass
            return Response(content=data, media_type=ctype,
                            headers={"Cache-Control": "public, max-age=86400"})
        except Exception:
            pass  # fall through to last-good bytes

    disk = _avatar_disk(key)
    if disk:
        return Response(content=disk, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=3600"})
    # Never loaded → 404 so the UI shows initials instead.
    return JSONResponse(status_code=404, content={"error": "no avatar yet"})


@app.get("/api/insights")
def api_insights(fresh: bool = False):
    now = int(time.time())
    week_ago = now - 7 * 86400

    def build():
        tools, keys = [], []
        for a in ACCOUNTS:
            if a["platform"] != "instagram":
                continue
            tools.append({"tool_slug": "INSTAGRAM_GET_USER_INSIGHTS",
                          "arguments": {"ig_user_id": "me",
                                        "metric": ["reach", "total_interactions",
                                                   "likes", "comments",
                                                   "shares", "saves"],
                                        "period": "day",
                                        "metric_type": "total_value",
                                        "since": week_ago, "until": now},
                          "account": a["account"]})
            keys.append(a["key"])
        results = pipe.execute_batch(tools, thought="hub 7d insights")
        out = {}
        for key, res in zip(keys, results):
            metrics = {}
            if res["successful"]:
                for m in unwrap_list(res["data"] or {}):
                    name = m.get("name")
                    total = ((m.get("total_value") or {}).get("value")
                             if isinstance(m.get("total_value"), dict) else None)
                    if total is None:
                        total = sum(v.get("value", 0)
                                    for v in (m.get("values") or []))
                    metrics[name] = total
            out[key] = {"metrics": metrics,
                        "error": None if res["successful"]
                        else str(res["error"])[:300]}
        return {"window": "7d", "insights": out}

    try:
        return cached("insights", fresh, build)
    except Exception as e:
        return JSONResponse(status_code=502, content={"error": str(e)[:400]})


@app.get("/api/scheduled")
def api_scheduled(fresh: bool = False):
    def build():
        fb = [a for a in ACCOUNTS if a["platform"] == "facebook"]
        tools = [{"tool_slug": "FACEBOOK_GET_SCHEDULED_POSTS",
                  "arguments": {"page_id": a["entity_id"]},
                  "account": a["account"]} for a in fb]
        results = pipe.execute_batch(tools, thought="hub scheduled posts")
        out = {}
        for a, res in zip(fb, results):
            out[a["key"]] = {
                "posts": unwrap_list(scrub(res["data"] or {}))
                if res["successful"] else [],
                "error": None if res["successful"] else str(res["error"])[:300],
            }
        return {"scheduled": out}

    try:
        return cached("scheduled", fresh, build)
    except Exception as e:
        return JSONResponse(status_code=502, content={"error": str(e)[:400]})


# ── Publishing ─────────────────────────────────────────────────────────────

class PostRequest(BaseModel):
    targets: list[str]
    caption: str = ""
    post_type: str = "photo"  # photo | video | carousel | story | text
    image_url: str | None = None
    image_urls: list[str] | None = None  # carousel: 2-10 public JPEG URLs
    video_url: str | None = None         # video/reel: public MP4 URL
    cover_url: str | None = None         # optional reel cover image
    schedule_time: int | None = None     # unix epoch, FB only


def _container_id(resp):
    return (resp.get("id") or (resp.get("data") or {}).get("id"))


def _ig_publish_container(acct, creation_id):
    """Step 2 of Instagram publishing: push a staged container live."""
    published = pipe.execute(
        "INSTAGRAM_POST_IG_USER_MEDIA_PUBLISH",
        {"ig_user_id": "me", "creation_id": str(creation_id),
         "max_wait_seconds": 120},
        account=acct["account"])
    media_id = _container_id(published)
    permalink = None
    if media_id:
        try:
            media = pipe.execute("INSTAGRAM_GET_IG_MEDIA",
                                 {"ig_media_id": str(media_id),
                                  "fields": "id,permalink"},
                                 account=acct["account"])
            permalink = media.get("permalink")
        except PipeError:
            pass
    return {"id": media_id, "permalink": permalink}


def publish_instagram(acct, req: PostRequest):
    """Instagram publishing, all container shapes.

    photo    → image container → publish
    video    → REELS container (video_url, optional cover) → publish
    story    → STORIES container (image or video) → publish
    carousel → 2-10 child containers → parent CAROUSEL container → publish
    """
    base = {"ig_user_id": "me"}
    if req.post_type == "carousel":
        children = []
        for url in (req.image_urls or []):
            child = pipe.execute(
                "INSTAGRAM_POST_IG_USER_MEDIA",
                {**base, "image_url": url, "is_carousel_item": True},
                account=acct["account"])
            cid = _container_id(child)
            if not cid:
                raise PipeError(f"Carousel item failed: {str(child)[:200]}")
            children.append(str(cid))
        container = pipe.execute(
            "INSTAGRAM_POST_IG_USER_MEDIA",
            {**base, "caption": req.caption, "media_type": "CAROUSEL",
             "children": children},
            account=acct["account"])
    elif req.post_type == "video":
        args = {**base, "caption": req.caption, "media_type": "REELS",
                "video_url": req.video_url, "share_to_feed": True}
        if req.cover_url:
            args["cover_url"] = req.cover_url
        container = pipe.execute("INSTAGRAM_POST_IG_USER_MEDIA", args,
                                 account=acct["account"])
    elif req.post_type == "story":
        args = {**base, "media_type": "STORIES"}
        if req.video_url:
            args["video_url"] = req.video_url
        else:
            args["image_url"] = req.image_url
        container = pipe.execute("INSTAGRAM_POST_IG_USER_MEDIA", args,
                                 account=acct["account"])
    else:  # photo
        container = pipe.execute(
            "INSTAGRAM_POST_IG_USER_MEDIA",
            {**base, "caption": req.caption, "image_url": req.image_url},
            account=acct["account"])
    creation_id = _container_id(container)
    if not creation_id:
        raise PipeError(f"No container id in response: {str(container)[:200]}")
    return _ig_publish_container(acct, creation_id)


def publish_facebook(acct, req: PostRequest):
    """Facebook page publishing: text, photo or video — schedulable."""
    scheduled = {"published": False,
                 "scheduled_publish_time": req.schedule_time} \
        if req.schedule_time else {}
    if req.post_type == "video":
        args = {"page_id": acct["entity_id"], "file_url": req.video_url,
                "description": req.caption, **scheduled}
        data = pipe.execute("FACEBOOK_CREATE_VIDEO_POST", args,
                            account=acct["account"])
    elif req.image_url:
        args = {"page_id": acct["entity_id"], "message": req.caption,
                "url": req.image_url, **scheduled}
        data = pipe.execute("FACEBOOK_CREATE_PHOTO_POST", args,
                            account=acct["account"])
    else:
        args = {"page_id": acct["entity_id"], "message": req.caption,
                **scheduled}
        data = pipe.execute("FACEBOOK_CREATE_POST", args,
                            account=acct["account"])
    resp = data.get("response_data") if isinstance(data, dict) else None
    resp = resp if isinstance(resp, dict) else data
    post_id = resp.get("post_id") or resp.get("id") or data.get("id")
    permalink = None
    if post_id and not req.schedule_time and req.post_type != "video":
        try:
            post = pipe.execute("FACEBOOK_GET_POST",
                                {"post_id": str(post_id),
                                 "fields": "id,permalink_url"},
                                account=acct["account"])
            permalink = (post.get("permalink_url")
                         or (post.get("data") or {}).get("permalink_url"))
        except PipeError:
            pass
    return {"id": post_id, "permalink": permalink,
            "scheduled": bool(req.schedule_time)}


# Which platforms each post type can go to, and what media it needs.
POST_TYPES = {
    "photo":    {"platforms": {"instagram", "facebook"}},
    "video":    {"platforms": {"instagram", "facebook"}},
    "carousel": {"platforms": {"instagram"}},
    "story":    {"platforms": {"instagram"}},
    "text":     {"platforms": {"facebook"}},
}


def _validate_post(req: PostRequest):
    """Return an error string, or None when the request is publishable."""
    if req.post_type not in POST_TYPES:
        return f"Unknown post type: {req.post_type}"
    bad_platform = [BY_KEY[t]["handle"] for t in req.targets
                    if BY_KEY[t]["platform"]
                    not in POST_TYPES[req.post_type]["platforms"]]
    if bad_platform:
        kinds = " and ".join(sorted(POST_TYPES[req.post_type]["platforms"]))
        return (f"A {req.post_type} post can only go to {kinds} — "
                f"remove {', '.join(bad_platform)} from the destinations")
    if req.post_type == "photo" and not req.image_url:
        ig = any(BY_KEY[t]["platform"] == "instagram" for t in req.targets)
        if ig:
            return ("Instagram needs a public image link (a direct JPEG URL "
                    "with no query parameters)")
        if not req.caption.strip():
            return "Add a message or an image link first"
    if req.post_type == "video" and not req.video_url:
        return "Add a public video link (a direct MP4 URL) first"
    if req.post_type == "carousel":
        urls = [u for u in (req.image_urls or []) if u.strip()]
        if not 2 <= len(urls) <= 10:
            return "A carousel needs between 2 and 10 image links"
    if req.post_type == "story" and not (req.image_url or req.video_url):
        return "A story needs an image or video link"
    if req.post_type == "text" and not req.caption.strip():
        return "Write the message for your post first"
    ig_targets = [t for t in req.targets
                  if BY_KEY[t]["platform"] == "instagram"]
    if ig_targets and req.schedule_time:
        return ("Scheduling is available for Facebook pages only — "
                "Instagram publishes immediately. Remove the Instagram "
                "destinations or clear the schedule.")
    if req.schedule_time and req.schedule_time < int(time.time()) + 600:
        return "Schedule time must be at least 10 minutes from now"
    return None


@app.post("/api/post")
def api_post(req: PostRequest):
    if not req.targets:
        return JSONResponse(status_code=400,
                            content={"error": "No target accounts selected"})
    unknown = [t for t in req.targets if t not in BY_KEY]
    if unknown:
        return JSONResponse(status_code=400,
                            content={"error": f"Unknown targets: {unknown}"})
    problem = _validate_post(req)
    if problem:
        return JSONResponse(status_code=400, content={"error": problem})

    results = {}
    with ThreadPoolExecutor(max_workers=len(req.targets)) as ex:
        futures = {}
        for t in req.targets:
            acct = BY_KEY[t]
            fn = (publish_instagram if acct["platform"] == "instagram"
                  else publish_facebook)
            futures[t] = ex.submit(fn, acct, req)
        for t, fut in futures.items():
            try:
                results[t] = {"ok": True, **fut.result()}
            except Exception as e:
                results[t] = {"ok": False, "error": str(e)[:400]}

    with _cache_lock:  # posts change the feeds — drop caches
        _cache.clear()
    return {"results": results}


# ── Content library: everything published, across every account ────────────

@app.get("/api/library")
def api_library(fresh: bool = False):
    def build():
        tools, keys = [], []
        for a in ACCOUNTS:
            if a["platform"] == "instagram":
                tools.append({"tool_slug": "INSTAGRAM_GET_IG_USER_MEDIA",
                              "arguments": {"ig_user_id": "me", "limit": 36,
                                            "fields": IG_MEDIA_FIELDS},
                              "account": a["account"]})
            else:
                tools.append({"tool_slug": "FACEBOOK_GET_PAGE_POSTS",
                              "arguments": {"page_id": a["entity_id"],
                                            "limit": 36,
                                            "fields": FB_POST_FIELDS},
                              "account": a["account"]})
            keys.append(a["key"])
        results = pipe.execute_batch(tools, thought="content library pull")
        items, notes = [], []
        for key, res in zip(keys, results):
            acct = BY_KEY[key]
            if not res["successful"]:
                notes.append({key: str(res["error"])[:250]})
                continue
            for m in unwrap_list(scrub(res["data"] or {})):
                if acct["platform"] == "instagram":
                    items.append({
                        "key": key, "id": m.get("id"),
                        "ts": m.get("timestamp"),
                        "img": m.get("media_url") or m.get("thumbnail_url"),
                        "caption": m.get("caption"),
                        "media_type": m.get("media_type"),
                        "product": m.get("media_product_type"),
                        "likes": m.get("like_count"),
                        "comments": m.get("comments_count"),
                        "link": m.get("permalink"),
                    })
                else:
                    items.append({
                        "key": key, "id": m.get("id"),
                        "ts": m.get("created_time"),
                        "img": m.get("full_picture"),
                        "caption": m.get("message"),
                        "media_type": "IMAGE" if m.get("full_picture")
                                      else "TEXT",
                        "product": "PAGE",
                        "likes": ((m.get("reactions") or {}).get("summary")
                                  or {}).get("total_count"),
                        "comments": ((m.get("comments") or {}).get("summary")
                                     or {}).get("total_count"),
                        "link": m.get("permalink_url"),
                    })
        items.sort(key=lambda x: x.get("ts") or "", reverse=True)
        return {"items": items, "notes": notes,
                "generated_at": datetime.now(timezone.utc).isoformat()}

    try:
        return cached("library", fresh, build)
    except Exception as e:
        return JSONResponse(status_code=502, content={"error": str(e)[:400]})


# ── Phone access: LAN address + scan-to-open QR ─────────────────────────────

def lan_ip():
    """Best-effort LAN address — the address phones on this Wi-Fi can reach."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # no packets sent; just picks the route
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


@app.get("/api/qr")
def api_qr():
    url = f"http://{lan_ip()}:8787"
    try:
        svg = qr.render_svg(url)
    except ValueError as e:
        return JSONResponse(status_code=500, content={"error": str(e)})
    return {"url": url, "svg": svg}


# ── Stories: everything live right now ─────────────────────────────────────

@app.get("/api/stories")
def api_stories(fresh: bool = False):
    def build():
        ig = [a for a in ACCOUNTS if a["platform"] == "instagram"]
        tools = [{"tool_slug": "INSTAGRAM_GET_IG_USER_STORIES",
                  "arguments": {"ig_user_id": "me",
                                "fields": "id,media_type,media_url,"
                                          "thumbnail_url,permalink,timestamp"},
                  "account": a["account"]} for a in ig]
        results = pipe.execute_batch(tools, thought="active stories check")
        out = {}
        for a, res in zip(ig, results):
            out[a["key"]] = {
                "stories": unwrap_list(scrub(res["data"] or {}))
                if res["successful"] else [],
                "error": None if res["successful"] else str(res["error"])[:250],
            }
        return {"stories": out}

    try:
        return cached("stories", fresh, build)
    except Exception as e:
        return JSONResponse(status_code=502, content={"error": str(e)[:400]})


# ── Inbox: Instagram DMs ────────────────────────────────────────────────────

@app.get("/api/inbox")
def api_inbox(fresh: bool = False):
    def build():
        ig = [a for a in ACCOUNTS if a["platform"] == "instagram"]
        tools = [{"tool_slug": "INSTAGRAM_LIST_ALL_CONVERSATIONS",
                  "arguments": {"limit": 25},
                  "account": a["account"]} for a in ig]
        results = pipe.execute_batch(tools, thought="inbox conversations")
        out = {}
        for a, res in zip(ig, results):
            out[a["key"]] = {
                "conversations": unwrap_list(scrub(res["data"] or {}))
                if res["successful"] else [],
                "error": None if res["successful"] else str(res["error"])[:250],
            }
        return {"inbox": out}

    try:
        return cached("inbox", fresh, build)
    except Exception as e:
        return JSONResponse(status_code=502, content={"error": str(e)[:400]})


@app.get("/api/inbox/thread")
def api_inbox_thread(key: str, conversation_id: str):
    acct = BY_KEY.get(key)
    if not acct or acct["platform"] != "instagram":
        return JSONResponse(status_code=400,
                            content={"error": "Unknown Instagram account"})

    def build():
        results = pipe.execute_batch([
            {"tool_slug": "INSTAGRAM_GET_CONVERSATION",
             "arguments": {"conversation_id": conversation_id},
             "account": acct["account"]},
            {"tool_slug": "INSTAGRAM_LIST_ALL_MESSAGES",
             "arguments": {"conversation_id": conversation_id, "limit": 50},
             "account": acct["account"]},
        ], thought="inbox thread read")
        convo = scrub(results[0]["data"] or {}) if results[0]["successful"] \
            else {}
        messages = unwrap_list(scrub(results[1]["data"] or {})) \
            if results[1]["successful"] else []
        # The other participant is whoever isn't this business account.
        me = acct["entity_id"]
        participants = unwrap_list(convo.get("participants") or {}) or \
            [p for m in messages for p in unwrap_list(m.get("from") or {})]
        if isinstance(convo.get("participants"), dict):
            participants = (convo["participants"].get("data") or [])
        other = next((p for p in participants
                      if str(p.get("id")) != str(me)), None)
        notes = [str(r["error"])[:250] for r in results
                 if not r["successful"]]
        return {"messages": messages, "participants": participants,
                "other": other, "notes": notes}

    try:
        return cached(f"thread:{key}:{conversation_id}", False, build)
    except Exception as e:
        return JSONResponse(status_code=502, content={"error": str(e)[:400]})


class InboxSendRequest(BaseModel):
    key: str
    recipient_id: str
    text: str


@app.post("/api/inbox/send")
def api_inbox_send(req: InboxSendRequest):
    acct = BY_KEY.get(req.key)
    if not acct or acct["platform"] != "instagram":
        return JSONResponse(status_code=400,
                            content={"error": "Unknown Instagram account"})
    if not req.text.strip():
        return JSONResponse(status_code=400,
                            content={"error": "Write the reply first"})
    try:
        pipe.execute("INSTAGRAM_SEND_TEXT_MESSAGE",
                     {"recipient_id": req.recipient_id, "text": req.text},
                     account=acct["account"])
        with _cache_lock:
            _cache.clear()
        return {"ok": True}
    except PipeError as e:
        msg = str(e)
        if "2534022" in msg or "allowed window" in msg.lower():
            msg = ("Instagram only lets a business reply within 24 hours of "
                   "the customer's last message. This conversation is outside "
                   "that window — it reopens when they message you again.")
        return JSONResponse(status_code=502, content={"error": msg[:400]})


# ── Comment actions: reply (FB) and remove (both platforms) ────────────────

class CommentRequest(BaseModel):
    key: str
    comment_id: str | None = None
    object_id: str | None = None  # post or comment to reply under (FB)
    message: str | None = None


@app.post("/api/comment/reply")
def api_comment_reply(req: CommentRequest):
    acct = BY_KEY.get(req.key)
    if not acct:
        return JSONResponse(status_code=400, content={"error": "Unknown account"})
    if acct["platform"] != "facebook":
        return JSONResponse(status_code=400, content={
            "error": "Replying to comments is available on Facebook pages — "
                     "Instagram's API doesn't allow comment replies yet"})
    if not (req.message or "").strip() or not req.object_id:
        return JSONResponse(status_code=400,
                            content={"error": "Write the reply first"})
    try:
        data = pipe.execute("FACEBOOK_CREATE_COMMENT",
                            {"object_id": req.object_id,
                             "message": req.message},
                            account=acct["account"])
        with _cache_lock:
            _cache.clear()
        resp = data.get("response_data") if isinstance(data, dict) else None
        resp = resp if isinstance(resp, dict) else data
        return {"ok": True, "comment_id": resp.get("id")}
    except PipeError as e:
        return JSONResponse(status_code=502, content={"error": str(e)[:400]})


@app.post("/api/comment/delete")
def api_comment_delete(req: CommentRequest):
    acct = BY_KEY.get(req.key)
    if not acct:
        return JSONResponse(status_code=400, content={"error": "Unknown account"})
    if not req.comment_id:
        return JSONResponse(status_code=400,
                            content={"error": "No comment selected"})
    slug = ("INSTAGRAM_DELETE_COMMENT" if acct["platform"] == "instagram"
            else "FACEBOOK_DELETE_COMMENT")
    args = ({"ig_comment_id": req.comment_id}
            if acct["platform"] == "instagram"
            else {"comment_id": req.comment_id})
    try:
        pipe.execute(slug, args, account=acct["account"])
        with _cache_lock:
            _cache.clear()
        return {"ok": True}
    except PipeError as e:
        return JSONResponse(status_code=502, content={"error": str(e)[:400]})


# ── Post detail: performance, comments, reactions ──────────────────────────

@app.get("/api/post-details")
def api_post_details(key: str, post_id: str, carousel: bool = False):
    acct = BY_KEY.get(key)
    if not acct:
        return JSONResponse(status_code=400, content={"error": "Unknown account"})

    def build():
        if acct["platform"] == "instagram":
            tools = [
                {"tool_slug": "INSTAGRAM_GET_IG_MEDIA_INSIGHTS",
                 "arguments": {"ig_media_id": post_id,
                               "metric": ["views", "reach", "likes", "comments",
                                          "saved", "shares", "total_interactions"]},
                 "account": acct["account"]},
                {"tool_slug": "INSTAGRAM_GET_IG_MEDIA_COMMENTS",
                 "arguments": {"ig_media_id": post_id, "limit": 50,
                               "fields": "id,text,username,timestamp,like_count"},
                 "account": acct["account"]},
            ]
            kinds = ["insights", "comments"]
            if carousel:
                tools.append({"tool_slug": "INSTAGRAM_GET_IG_MEDIA_CHILDREN",
                              "arguments": {"ig_media_id": post_id},
                              "account": acct["account"]})
                kinds.append("children")
        else:
            tools = [
                {"tool_slug": "FACEBOOK_GET_POST_INSIGHTS",
                 "arguments": {"post_id": post_id, "metrics": "post_media_view"},
                 "account": acct["account"]},
                {"tool_slug": "FACEBOOK_GET_COMMENTS",
                 "arguments": {"object_id": post_id, "limit": 50,
                               "fields": "id,message,created_time,from,"
                                         "like_count,comment_count"},
                 "account": acct["account"]},
                {"tool_slug": "FACEBOOK_GET_POST_REACTIONS",
                 "arguments": {"post_id": post_id, "summary": True, "limit": 50},
                 "account": acct["account"]},
            ]
            kinds = ["insights", "comments", "reactions"]

        results = pipe.execute_batch(tools, thought="post performance detail")
        out = {"metrics": {}, "comments": [], "reactions": None,
               "children": [], "notes": []}
        for kind, res in zip(kinds, results):
            if not res["successful"]:
                out["notes"].append({kind: str(res["error"])[:250]})
                continue
            data = scrub(res["data"] or {})
            if kind == "children":
                out["children"] = unwrap_list(data)
            elif kind == "insights":
                for m in unwrap_list(data):
                    name = m.get("name")
                    vals = m.get("values") or []
                    val = vals[0].get("value") if vals else m.get("value")
                    if isinstance(val, (int, float)):
                        out["metrics"][name] = val
            elif kind == "comments":
                out["comments"] = unwrap_list(data)
            elif kind == "reactions":
                summary = data.get("summary") or {}
                if not summary and isinstance(data.get("data"), dict):
                    summary = data["data"].get("summary") or {}
                out["reactions"] = {
                    "total": summary.get("total_count"),
                    "recent": [r.get("type") for r in unwrap_list(data)][:50],
                }
        return out

    try:
        return cached(f"detail:{key}:{post_id}", False, build)
    except Exception as e:
        return JSONResponse(status_code=502, content={"error": str(e)[:400]})


# ── Analytics: account performance + mentions ──────────────────────────────

@app.get("/api/analytics")
def api_analytics(fresh: bool = False):
    now = int(time.time())
    week_ago = now - 7 * 86400

    def build():
        tools, index = [], []
        for a in ACCOUNTS:
            if a["platform"] == "instagram":
                tools.append({"tool_slug": "INSTAGRAM_GET_USER_INSIGHTS",
                              "arguments": {"ig_user_id": "me",
                                            "metric": ["reach",
                                                       "total_interactions",
                                                       "likes", "comments",
                                                       "shares", "saves"],
                                            "period": "day",
                                            "metric_type": "total_value",
                                            "since": week_ago, "until": now},
                              "account": a["account"]})
                index.append((a["key"], "metrics"))
                tools.append({"tool_slug": "INSTAGRAM_GET_IG_USER_TAGS",
                              "arguments": {"ig_user_id": "me", "limit": 12},
                              "account": a["account"]})
                index.append((a["key"], "tagged"))
            else:
                tools.append({"tool_slug": "FACEBOOK_GET_PAGE_INSIGHTS",
                              "arguments": {"page_id": a["entity_id"],
                                            "metrics": "page_follows,"
                                                       "page_daily_follows_unique,"
                                                       "page_daily_unfollows_unique,"
                                                       "page_media_view,"
                                                       "page_post_engagements,"
                                                       "page_total_actions",
                                            "period": "day",
                                            "since": str(week_ago),
                                            "until": str(now)},
                              "account": a["account"]})
                index.append((a["key"], "metrics"))
        results = pipe.execute_batch(tools, thought="7-day performance analytics")
        out = {a["key"]: {"metrics": {}, "tagged": [], "notes": []}
               for a in ACCOUNTS}
        for (key, kind), res in zip(index, results):
            slot = out[key]
            if not res["successful"]:
                slot["notes"].append({kind: str(res["error"])[:250]})
                continue
            data = scrub(res["data"] or {})
            if kind == "tagged":
                slot["tagged"] = unwrap_list(data)
                continue
            for m in unwrap_list(data):
                name = m.get("name")
                if isinstance(m.get("total_value"), dict):
                    slot["metrics"][name] = m["total_value"].get("value")
                    continue
                vals = m.get("values") or []
                if name == "page_follows":  # running total — latest wins
                    slot["metrics"][name] = (vals[-1].get("value")
                                             if vals else None)
                else:
                    slot["metrics"][name] = sum(
                        v.get("value", 0) for v in vals
                        if isinstance(v.get("value"), (int, float)))
        return {"window": "7d",
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "accounts": out}

    try:
        return cached("analytics", fresh, build)
    except Exception as e:
        return JSONResponse(status_code=502, content={"error": str(e)[:400]})


# ── Post management (Facebook pages) ───────────────────────────────────────

class ManagePostRequest(BaseModel):
    key: str
    post_id: str
    message: str | None = None
    schedule_time: int | None = None


def _require_fb(req: ManagePostRequest):
    acct = BY_KEY.get(req.key)
    if not acct:
        return None, JSONResponse(status_code=400,
                                  content={"error": "Unknown account"})
    if acct["platform"] != "facebook":
        return None, JSONResponse(status_code=400, content={
            "error": "This action is available for Facebook pages only — "
                     "Instagram posts can't be edited via the API"})
    return acct, None


@app.post("/api/post/update")
def api_post_update(req: ManagePostRequest):
    acct, err = _require_fb(req)
    if err:
        return err
    if not (req.message or "").strip():
        return JSONResponse(status_code=400,
                            content={"error": "Updated text can't be empty"})
    try:
        pipe.execute("FACEBOOK_UPDATE_POST",
                     {"post_id": req.post_id, "message": req.message},
                     account=acct["account"])
        with _cache_lock:
            _cache.clear()
        return {"ok": True}
    except PipeError as e:
        return JSONResponse(status_code=502, content={"error": str(e)[:400]})


@app.post("/api/post/delete")
def api_post_delete(req: ManagePostRequest):
    acct, err = _require_fb(req)
    if err:
        return err
    try:
        pipe.execute("FACEBOOK_DELETE_POST", {"post_id": req.post_id},
                     account=acct["account"])
        with _cache_lock:
            _cache.clear()
        return {"ok": True}
    except PipeError as e:
        return JSONResponse(status_code=502, content={"error": str(e)[:400]})


@app.post("/api/post/reschedule")
def api_post_reschedule(req: ManagePostRequest):
    acct, err = _require_fb(req)
    if err:
        return err
    if not req.schedule_time or req.schedule_time < int(time.time()) + 600:
        return JSONResponse(status_code=400, content={
            "error": "New publish time must be at least 10 minutes from now"})
    try:
        pipe.execute("FACEBOOK_RESCHEDULE_POST",
                     {"post_id": req.post_id,
                      "scheduled_publish_time": req.schedule_time},
                     account=acct["account"])
        with _cache_lock:
            _cache.clear()
        return {"ok": True}
    except PipeError as e:
        return JSONResponse(status_code=502, content={"error": str(e)[:400]})


# ── Integrations: full API coverage + pipe health ──────────────────────────

TOOL_REGISTRY = [
    {"platform": "Instagram", "area": "Profile & audience",
     "slug": "INSTAGRAM_GET_USER_INFO",
     "what": "Profile, bio, follower and post counts"},
    {"platform": "Instagram", "area": "Content library",
     "slug": "INSTAGRAM_GET_IG_USER_MEDIA",
     "what": "Every published post, reel and carousel"},
    {"platform": "Instagram", "area": "Account performance",
     "slug": "INSTAGRAM_GET_USER_INSIGHTS",
     "what": "7-day reach, interactions, likes, comments, shares, saves"},
    {"platform": "Instagram", "area": "Post performance",
     "slug": "INSTAGRAM_GET_IG_MEDIA_INSIGHTS",
     "what": "Views, reach and engagement per post"},
    {"platform": "Instagram", "area": "Comments",
     "slug": "INSTAGRAM_GET_IG_MEDIA_COMMENTS",
     "what": "Comment threads on any of your posts"},
    {"platform": "Instagram", "area": "Mentions",
     "slug": "INSTAGRAM_GET_IG_USER_TAGS",
     "what": "Posts where your account is tagged"},
    {"platform": "Instagram", "area": "Stories",
     "slug": "INSTAGRAM_GET_IG_USER_STORIES",
     "what": "Every story currently live in its 24-hour window"},
    {"platform": "Instagram", "area": "Carousel detail",
     "slug": "INSTAGRAM_GET_IG_MEDIA_CHILDREN",
     "what": "Every image inside a published carousel"},
    {"platform": "Instagram", "area": "Comment threads",
     "slug": "INSTAGRAM_GET_IG_COMMENT_REPLIES",
     "what": "Replies under any comment on your posts"},
    {"platform": "Instagram", "area": "Comment moderation",
     "slug": "INSTAGRAM_DELETE_COMMENT",
     "what": "Remove a comment from your posts"},
    {"platform": "Instagram", "area": "Inbox",
     "slug": "INSTAGRAM_LIST_ALL_CONVERSATIONS",
     "what": "Every customer DM conversation"},
    {"platform": "Instagram", "area": "Inbox",
     "slug": "INSTAGRAM_GET_CONVERSATION",
     "what": "Who's in a conversation"},
    {"platform": "Instagram", "area": "Inbox",
     "slug": "INSTAGRAM_LIST_ALL_MESSAGES",
     "what": "Full message history of a conversation"},
    {"platform": "Instagram", "area": "Inbox",
     "slug": "INSTAGRAM_SEND_TEXT_MESSAGE",
     "what": "Reply to a customer DM (within Meta's 24-hour window)"},
    {"platform": "Instagram", "area": "Inbox",
     "slug": "INSTAGRAM_MARK_SEEN",
     "what": "Mark a conversation as read"},
    {"platform": "Instagram", "area": "Publishing capacity",
     "slug": "INSTAGRAM_GET_IG_USER_CONTENT_PUBLISHING_LIMIT",
     "what": "Live usage of the 25-posts-per-day publishing allowance"},
    {"platform": "Instagram", "area": "Publishing · Photos",
     "slug": "INSTAGRAM_POST_IG_USER_MEDIA",
     "what": "Stage a photo post"},
    {"platform": "Instagram", "area": "Publishing · Reels",
     "slug": "INSTAGRAM_POST_IG_USER_MEDIA",
     "what": "Stage a video as a Reel, with optional custom cover"},
    {"platform": "Instagram", "area": "Publishing · Carousels",
     "slug": "INSTAGRAM_POST_IG_USER_MEDIA",
     "what": "Stage a 2-10 image carousel (child + parent containers)"},
    {"platform": "Instagram", "area": "Publishing · Stories",
     "slug": "INSTAGRAM_POST_IG_USER_MEDIA",
     "what": "Stage an image or video story"},
    {"platform": "Instagram", "area": "Publishing",
     "slug": "INSTAGRAM_POST_IG_USER_MEDIA_PUBLISH",
     "what": "Push any staged post live"},
    {"platform": "Instagram", "area": "Post lookup",
     "slug": "INSTAGRAM_GET_IG_MEDIA",
     "what": "Confirm a published post and fetch its link"},
    {"platform": "Facebook", "area": "Page inventory",
     "slug": "FACEBOOK_LIST_MANAGED_PAGES",
     "what": "All pages your business manages"},
    {"platform": "Facebook", "area": "Page profile",
     "slug": "FACEBOOK_GET_PAGE_DETAILS",
     "what": "Page identity, category, audience size"},
    {"platform": "Facebook", "area": "Content library",
     "slug": "FACEBOOK_GET_PAGE_POSTS",
     "what": "Full page timeline with engagement counts"},
    {"platform": "Facebook", "area": "Page performance",
     "slug": "FACEBOOK_GET_PAGE_INSIGHTS",
     "what": "Followers, content views, engagement over time"},
    {"platform": "Facebook", "area": "Post performance",
     "slug": "FACEBOOK_GET_POST_INSIGHTS",
     "what": "Views per post (Meta's current post metric)"},
    {"platform": "Facebook", "area": "Comments",
     "slug": "FACEBOOK_GET_COMMENTS",
     "what": "Comment threads on any page post"},
    {"platform": "Facebook", "area": "Comment replies",
     "slug": "FACEBOOK_CREATE_COMMENT",
     "what": "Reply to any comment as your page"},
    {"platform": "Facebook", "area": "Comment moderation",
     "slug": "FACEBOOK_UPDATE_COMMENT",
     "what": "Edit a reply your page wrote"},
    {"platform": "Facebook", "area": "Comment moderation",
     "slug": "FACEBOOK_DELETE_COMMENT",
     "what": "Remove a comment from your page's posts"},
    {"platform": "Facebook", "area": "Reactions",
     "slug": "FACEBOOK_GET_POST_REACTIONS",
     "what": "Reaction totals and recent reaction types"},
    {"platform": "Facebook", "area": "Publishing",
     "slug": "FACEBOOK_CREATE_POST",
     "what": "Publish or schedule a text / link post"},
    {"platform": "Facebook", "area": "Publishing",
     "slug": "FACEBOOK_CREATE_PHOTO_POST",
     "what": "Publish or schedule a photo post"},
    {"platform": "Facebook", "area": "Publishing",
     "slug": "FACEBOOK_CREATE_VIDEO_POST",
     "what": "Publish or schedule a video post"},
    {"platform": "Facebook", "area": "Post management",
     "slug": "FACEBOOK_UPDATE_POST",
     "what": "Edit the text of a live post"},
    {"platform": "Facebook", "area": "Post management",
     "slug": "FACEBOOK_DELETE_POST",
     "what": "Take a post down permanently"},
    {"platform": "Facebook", "area": "Scheduling",
     "slug": "FACEBOOK_GET_SCHEDULED_POSTS",
     "what": "Everything waiting in the publish queue"},
    {"platform": "Facebook", "area": "Scheduling",
     "slug": "FACEBOOK_RESCHEDULE_POST",
     "what": "Move a queued post to a new time"},
    {"platform": "Facebook", "area": "Post lookup",
     "slug": "FACEBOOK_GET_POST",
     "what": "Confirm a published post and fetch its link"},
]


@app.get("/api/integrations")
def api_integrations(fresh: bool = False):
    def build():
        checks = pipe.execute_batch([
            {"tool_slug": "FACEBOOK_LIST_MANAGED_PAGES",
             "arguments": {"user_id": "me", "limit": 5},
             "account": ACCOUNTS[-1]["account"]},
            {"tool_slug": "INSTAGRAM_GET_USER_INFO",
             "arguments": {"ig_user_id": "me"},
             "account": ACCOUNTS[0]["account"]},
        ], thought="pipe health check")
        pages = unwrap_list(scrub(checks[0]["data"] or {})) \
            if checks[0]["successful"] else []
        return {
            "pipe": "operational" if all(c["successful"] for c in checks)
                    else "degraded",
            "facebook_ok": checks[0]["successful"],
            "instagram_ok": checks[1]["successful"],
            "managed_pages": [{"id": p.get("id"), "name": p.get("name"),
                               "category": p.get("category")} for p in pages],
            "tools": TOOL_REGISTRY,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }

    try:
        return cached("integrations", fresh, build)
    except Exception as e:
        return JSONResponse(status_code=502, content={"error": str(e)[:400]})


app.mount("/", StaticFiles(directory=os.path.join(HERE, "static"), html=True),
          name="static")

if __name__ == "__main__":
    import uvicorn
    # All interfaces, so phones on the same Wi-Fi can open the hub.
    uvicorn.run(app, host="0.0.0.0", port=8787)
