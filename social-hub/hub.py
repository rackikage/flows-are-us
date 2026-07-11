#!/usr/bin/env python3
"""
social_hub.py — All-accounts Instagram + Facebook hub
Accounts:
  Instagram  rossmorebuilding  id: 27412531841701728  alias: instagram_okie-ceile
  Instagram  human247.365      id: 27330220746665792  alias: instagram_anode-conch
  Facebook   Human 247/365     id: 1154954434369587   alias: facebook_uberty-minor

Requires: composio-core, rich
  pip install composio-core rich
"""

import json
from datetime import datetime, timezone
from composio import ComposioToolSet
from rich.console import Console
from rich.table import Table
from rich import box

console = Console()
toolset = ComposioToolSet()

# ── Account config ─────────────────────────────────────────────────────────────
IG_ACCOUNTS = [
    {"alias": "instagram_okie-ceile",  "username": "rossmorebuilding", "ig_id": "27412531841701728"},
    {"alias": "instagram_anode-conch", "username": "human247.365",     "ig_id": "27330220746665792"},
]

FB_PAGES = [
    {"alias": "facebook_uberty-minor", "page_id": "1154954434369587", "name": "Human 247/365"},
]

NOW_EPOCH  = int(datetime.now(timezone.utc).timestamp())
WEEK_EPOCH = NOW_EPOCH - 7 * 86400


# ── Helpers ────────────────────────────────────────────────────────────────────
def execute(tool_slug: str, arguments: dict, account: str) -> dict:
    """Execute a Composio tool and return the data dict."""
    resp = toolset.execute_action(
        action=tool_slug,
        params=arguments,
        connected_account_id=account,
    )
    if isinstance(resp, dict):
        return resp.get("data") or resp
    return {}


def safe_list(d: dict, *keys) -> list:
    for k in keys:
        d = (d or {}).get(k) or {}
    return d if isinstance(d, list) else []


def sum_metric(insights_data: list, name: str) -> int:
    for m in insights_data:
        if m.get("name") == name:
            vals = m.get("values") or []
            return sum(v.get("value", 0) for v in vals)
    return 0


# ── Instagram ──────────────────────────────────────────────────────────────────
def fetch_ig_profile(acct: dict) -> dict:
    return execute("INSTAGRAM_GET_USER_INFO", {"ig_user_id": "me"}, acct["alias"])


def fetch_ig_media(acct: dict, limit: int = 25) -> list:
    data = execute(
        "INSTAGRAM_GET_IG_USER_MEDIA",
        {
            "ig_user_id": "me",
            "limit": limit,
            "fields": "id,caption,media_type,permalink,timestamp,like_count,comments_count,media_product_type",
        },
        acct["alias"],
    )
    return safe_list(data, "data") or safe_list({"data": data}, "data")


def fetch_ig_insights(acct: dict) -> list:
    data = execute(
        "INSTAGRAM_GET_USER_INSIGHTS",
        {
            "ig_user_id": "me",
            "metric": ["reach", "follower_count", "accounts_engaged", "total_interactions",
                        "likes", "comments", "shares", "saves"],
            "period": "day",
            "since": WEEK_EPOCH,
            "until": NOW_EPOCH,
        },
        acct["alias"],
    )
    return safe_list(data, "data") or []


def fetch_ig_media_insights(acct: dict, media_id: str) -> list:
    data = execute(
        "INSTAGRAM_GET_IG_MEDIA_INSIGHTS",
        {"ig_media_id": media_id, "metric": "reach,likes,comments,saves,shares"},
        acct["alias"],
    )
    return safe_list(data, "data") or []


# ── Facebook ───────────────────────────────────────────────────────────────────
def fetch_fb_pages(acct: dict) -> list:
    data = execute("FACEBOOK_LIST_MANAGED_PAGES", {"user_id": "me", "limit": 25}, acct["alias"])
    return safe_list(data, "data") or []


def fetch_fb_posts(acct: dict, page_id: str, limit: int = 25) -> list:
    data = execute(
        "FACEBOOK_GET_PAGE_POSTS",
        {
            "page_id": page_id,
            "limit": limit,
            "fields": "id,message,created_time,permalink_url,reactions.summary(true),comments.summary(true),shares",
        },
        acct["alias"],
    )
    return safe_list(data, "data") or []


def fetch_fb_page_insights(acct: dict, page_id: str) -> list:
    data = execute(
        "FACEBOOK_GET_PAGE_INSIGHTS",
        {"page_id": page_id, "metric": "page_impressions,page_reach,page_engaged_users",
         "period": "day", "since": str(WEEK_EPOCH), "until": str(NOW_EPOCH)},
        acct["alias"],
    )
    return safe_list(data, "data") or []


# ── Display ────────────────────────────────────────────────────────────────────
def render_ig_profile(profile: dict, acct: dict):
    console.rule(f"[bold cyan]Instagram · @{acct['username']}")
    console.print(f"  Bio        : {profile.get('biography', '')}")
    console.print(f"  Followers  : {profile.get('followers_count', 'N/A')}")
    console.print(f"  Following  : {profile.get('follows_count', 'N/A')}")
    console.print(f"  Posts      : {profile.get('media_count', 'N/A')}")
    console.print(f"  Website    : {profile.get('website', '')}")
    console.print()


def render_ig_media(media_items: list, username: str):
    if not media_items:
        console.print(f"  [dim]No media found for @{username}[/dim]\n")
        return
    t = Table(title=f"@{username} — Recent Media", box=box.SIMPLE_HEAVY, show_lines=True)
    t.add_column("Type",      style="magenta",  width=8)
    t.add_column("Date",      style="dim",       width=12)
    t.add_column("Likes",     justify="right",   width=7)
    t.add_column("Comments",  justify="right",   width=9)
    t.add_column("Caption",   no_wrap=False,     width=50)
    t.add_column("Link",      no_wrap=True,      width=40)
    for m in media_items:
        ts = m.get("timestamp", "")[:10] if m.get("timestamp") else ""
        cap = (m.get("caption") or "")[:120]
        t.add_row(
            m.get("media_product_type") or m.get("media_type", ""),
            ts,
            str(m.get("like_count") or 0),
            str(m.get("comments_count") or 0),
            cap,
            m.get("permalink", ""),
        )
    console.print(t)


def render_ig_insights(insights: list, username: str):
    console.print(f"  [bold]7-day insights · @{username}[/bold]")
    metrics = ["reach", "accounts_engaged", "total_interactions", "likes", "comments", "saves", "shares"]
    for name in metrics:
        val = sum_metric(insights, name)
        console.print(f"    {name:<24}: {val}")
    console.print()


def render_fb_posts(posts: list, page_name: str):
    if not posts:
        console.print(f"  [dim]No posts found for {page_name}[/dim]\n")
        return
    t = Table(title=f"{page_name} — Facebook Posts", box=box.SIMPLE_HEAVY, show_lines=True)
    t.add_column("Date",      width=12)
    t.add_column("Reactions", justify="right", width=10)
    t.add_column("Comments",  justify="right", width=10)
    t.add_column("Shares",    justify="right", width=8)
    t.add_column("Message",   no_wrap=False,   width=60)
    for p in posts:
        ts = (p.get("created_time") or "")[:10]
        reactions = ((p.get("reactions") or {}).get("summary") or {}).get("total_count", 0)
        comments  = ((p.get("comments")  or {}).get("summary") or {}).get("total_count", 0)
        shares    = (p.get("shares") or {}).get("count", 0)
        msg = (p.get("message") or "")[:120]
        t.add_row(ts, str(reactions), str(comments), str(shares), msg)
    console.print(t)


# ── Main hub ───────────────────────────────────────────────────────────────────
def run_hub():
    console.rule("[bold green]🚀 Social Media Hub — All Accounts", style="green")
    console.print(f"  Run time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

    # ── Instagram ──────────────────────────────────────────────────────────────
    console.rule("[bold blue]INSTAGRAM", style="blue")
    for acct in IG_ACCOUNTS:
        profile  = fetch_ig_profile(acct)
        media    = fetch_ig_media(acct)
        insights = fetch_ig_insights(acct)

        render_ig_profile(profile, acct)
        render_ig_media(media, acct["username"])
        render_ig_insights(insights, acct["username"])

    # ── Facebook ───────────────────────────────────────────────────────────────
    console.rule("[bold blue]FACEBOOK", style="blue")
    for fb_acct in FB_PAGES:
        console.print(f"\n[bold cyan]Facebook Page · {fb_acct['name']}[/bold cyan]")
        posts    = fetch_fb_posts(fb_acct, fb_acct["page_id"])
        insights = fetch_fb_page_insights(fb_acct, fb_acct["page_id"])
        render_fb_posts(posts, fb_acct["name"])

    console.rule("[bold green]✅ Hub complete", style="green")


if __name__ == "__main__":
    run_hub()
