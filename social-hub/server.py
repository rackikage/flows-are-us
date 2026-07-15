#!/usr/bin/env python3
"""server.py — FLOWS social hub server.

FastAPI app over the Composio clean pipe (composio_pipe.ComposioPipe).
Serves the Spotify-style dark UI from ./static and exposes:

  GET  /api/accounts   — wired account config
  GET  /api/overview   — profiles + recent media + quota for every account
  GET  /api/insights   — 7-day IG insights per account (best effort)
  GET  /api/scheduled  — scheduled FB page posts
  POST /api/post       — publish (or schedule, FB only) to selected accounts

Run:  python3 social-hub/server.py   →  http://127.0.0.1:8787
"""

import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

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
    image_url: str | None = None
    schedule_time: int | None = None  # unix epoch, FB only


def publish_instagram(acct, req: PostRequest):
    container = pipe.execute(
        "INSTAGRAM_POST_IG_USER_MEDIA",
        {"ig_user_id": "me", "caption": req.caption,
         "image_url": req.image_url},
        account=acct["account"])
    creation_id = (container.get("id")
                   or (container.get("data") or {}).get("id"))
    if not creation_id:
        raise PipeError(f"No container id in response: {str(container)[:200]}")
    published = pipe.execute(
        "INSTAGRAM_POST_IG_USER_MEDIA_PUBLISH",
        {"ig_user_id": "me", "creation_id": str(creation_id),
         "max_wait_seconds": 90},
        account=acct["account"])
    media_id = (published.get("id")
                or (published.get("data") or {}).get("id"))
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


def publish_facebook(acct, req: PostRequest):
    if req.image_url:
        args = {"page_id": acct["entity_id"], "message": req.caption,
                "url": req.image_url}
        if req.schedule_time:
            args.update({"published": False,
                         "scheduled_publish_time": req.schedule_time})
        data = pipe.execute("FACEBOOK_CREATE_PHOTO_POST", args,
                            account=acct["account"])
    else:
        args = {"page_id": acct["entity_id"], "message": req.caption}
        if req.schedule_time:
            args.update({"published": False,
                         "scheduled_publish_time": req.schedule_time})
        data = pipe.execute("FACEBOOK_CREATE_POST", args,
                            account=acct["account"])
    resp = data.get("response_data") if isinstance(data, dict) else None
    resp = resp if isinstance(resp, dict) else data
    post_id = resp.get("post_id") or resp.get("id") or data.get("id")
    permalink = None
    if post_id and not req.schedule_time:
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


@app.post("/api/post")
def api_post(req: PostRequest):
    if not req.targets:
        return JSONResponse(status_code=400,
                            content={"error": "No target accounts selected"})
    unknown = [t for t in req.targets if t not in BY_KEY]
    if unknown:
        return JSONResponse(status_code=400,
                            content={"error": f"Unknown targets: {unknown}"})
    ig_targets = [t for t in req.targets
                  if BY_KEY[t]["platform"] == "instagram"]
    if ig_targets and not req.image_url:
        return JSONResponse(status_code=400, content={
            "error": "Instagram requires a public image URL (JPEG, "
                     "no query params)"})
    if ig_targets and req.schedule_time:
        return JSONResponse(status_code=400, content={
            "error": "Scheduling is Facebook-only; Instagram publishes "
                     "immediately. Deselect IG targets or clear the schedule."})
    if not req.caption.strip() and not req.image_url:
        return JSONResponse(status_code=400,
                            content={"error": "Post needs a caption or image"})
    if req.schedule_time and req.schedule_time < int(time.time()) + 600:
        return JSONResponse(status_code=400, content={
            "error": "Schedule time must be at least 10 minutes from now"})

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


# ── Post detail: performance, comments, reactions ──────────────────────────

@app.get("/api/post-details")
def api_post_details(key: str, post_id: str):
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
        out = {"metrics": {}, "comments": [], "reactions": None, "notes": []}
        for kind, res in zip(kinds, results):
            if not res["successful"]:
                out["notes"].append({kind: str(res["error"])[:250]})
                continue
            data = scrub(res["data"] or {})
            if kind == "insights":
                for m in unwrap_list(data):
                    name = m.get("name")
                    vals = m.get("values") or []
                    val = vals[0].get("value") if vals else m.get("value")
                    if isinstance(val, (int, float)):
                        out["metrics"][name] = val
            elif kind == "comments":
                out["comments"] = unwrap_list(data)
            elif kind == "reactions":
                summary = (data.get("summary")
                           or (data.get("data") or {}).get("summary")
                           if isinstance(data.get("data"), dict) else
                           data.get("summary")) or {}
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
    {"platform": "Instagram", "area": "Publishing capacity",
     "slug": "INSTAGRAM_GET_IG_USER_CONTENT_PUBLISHING_LIMIT",
     "what": "Live usage of the 25-posts-per-day publishing allowance"},
    {"platform": "Instagram", "area": "Publishing",
     "slug": "INSTAGRAM_POST_IG_USER_MEDIA",
     "what": "Stage a new post (image, reel or carousel draft)"},
    {"platform": "Instagram", "area": "Publishing",
     "slug": "INSTAGRAM_POST_IG_USER_MEDIA_PUBLISH",
     "what": "Push a staged post live"},
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
    {"platform": "Facebook", "area": "Reactions",
     "slug": "FACEBOOK_GET_POST_REACTIONS",
     "what": "Reaction totals and recent reaction types"},
    {"platform": "Facebook", "area": "Publishing",
     "slug": "FACEBOOK_CREATE_POST",
     "what": "Publish or schedule a text / link post"},
    {"platform": "Facebook", "area": "Publishing",
     "slug": "FACEBOOK_CREATE_PHOTO_POST",
     "what": "Publish or schedule a photo post"},
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
    uvicorn.run(app, host="127.0.0.1", port=8787)
