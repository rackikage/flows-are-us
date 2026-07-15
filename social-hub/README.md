# FLOWS · Social Hub

Live all-accounts hub for Instagram + Facebook — Spotify-style dark UI, wired
**exclusively through the Composio clean pipe** (strict rule: no hand-made Graph
API calls anywhere).

![stack](https://img.shields.io/badge/pipe-Composio%20MCP-1db954) ![ui](https://img.shields.io/badge/ui-vibrant%20dark-7c3aed)

## Run it

```bash
python3 social-hub/server.py
# → http://127.0.0.1:8787
```

CLI dashboard (same pipe):

```bash
python3 social-hub/hub.py
```

## Architecture

```
static/index.html   Spotify-style UI (sidebar / hero / cards / compose dock)
        │  fetch /api/*
server.py           FastAPI — overview, insights, scheduled, post
        │  ComposioPipe.execute / execute_batch
composio_pipe.py    MCP streamable-HTTP client → COMPOSIO_MULTI_EXECUTE_TOOL
        │
Composio (connect.composio.dev) → Instagram / Facebook Graph
```

Credentials: read at startup from `~/.claude.json` (`mcpServers.composio`) or
`COMPOSIO_MCP_URL` + `COMPOSIO_MCP_KEY` env vars. Nothing secret lives in this
repo. **Note:** the old `composio-core` Python SDK is dead (v1/v2 API sunset,
HTTP 410) — everything now rides the MCP endpoint.

## Accounts wired

| Platform  | Handle / Page      | Composio account         | Entity ID           |
|-----------|--------------------|--------------------------|---------------------|
| Instagram | @rossmorebuilding  | `instagram_okie-ceile`   | 27412531841701728   |
| Instagram | @human247.365      | `instagram_anode-conch`  | 27330220746665792   |
| Facebook  | Human 247/365      | `facebook_uberty-minor`  | 1154954434369587    |

Add/remove accounts in `ACCOUNTS` at the top of `server.py`.

## Features

- **Dashboard** — KPI strip (audience, capacity, connection status), live
  profiles, unified cross-platform feed, per-account filter chips
- **Post drill-down** — click any post: per-post views/reach, reactions
  breakdown, full comment threads; edit or remove Facebook posts in place
- **Publish** — one message + image to any mix of accounts; live preview;
  confirm-before-publish modal; per-destination results with permalinks
- **Analytics** — 7-day account performance (IG insights + FB page insights)
  and tagged mentions per Instagram account
- **Scheduled** — the Facebook publish queue with move/remove controls
  (scheduling is FB-only; the IG Graph API has no native scheduling)
- **Integrations** — live pipe health checks, managed-page inventory, and the
  full capability registry (all 24 Composio tools, each marked live)

## Composio tools used

| Purpose | Tool slug |
|---------|-----------|
| IG profile | `INSTAGRAM_GET_USER_INFO` |
| IG media | `INSTAGRAM_GET_IG_USER_MEDIA` |
| IG 7-day insights | `INSTAGRAM_GET_USER_INSIGHTS` |
| IG publish quota | `INSTAGRAM_GET_IG_USER_CONTENT_PUBLISHING_LIMIT` |
| IG create container | `INSTAGRAM_POST_IG_USER_MEDIA` |
| IG publish | `INSTAGRAM_POST_IG_USER_MEDIA_PUBLISH` |
| IG permalink | `INSTAGRAM_GET_IG_MEDIA` |
| FB page details | `FACEBOOK_GET_PAGE_DETAILS` |
| FB feed | `FACEBOOK_GET_PAGE_POSTS` |
| FB text/link post (+schedule) | `FACEBOOK_CREATE_POST` |
| FB photo post (+schedule) | `FACEBOOK_CREATE_PHOTO_POST` |
| FB scheduled queue | `FACEBOOK_GET_SCHEDULED_POSTS` |
| FB permalink | `FACEBOOK_GET_POST` |
| IG post insights | `INSTAGRAM_GET_IG_MEDIA_INSIGHTS` |
| IG comments | `INSTAGRAM_GET_IG_MEDIA_COMMENTS` |
| IG tagged mentions | `INSTAGRAM_GET_IG_USER_TAGS` |
| FB post insights | `FACEBOOK_GET_POST_INSIGHTS` |
| FB comments | `FACEBOOK_GET_COMMENTS` |
| FB reactions | `FACEBOOK_GET_POST_REACTIONS` |
| FB page insights | `FACEBOOK_GET_PAGE_INSIGHTS` |
| FB pages inventory | `FACEBOOK_LIST_MANAGED_PAGES` |
| FB edit post | `FACEBOOK_UPDATE_POST` |
| FB delete post | `FACEBOOK_DELETE_POST` |
| FB reschedule | `FACEBOOK_RESCHEDULE_POST` |

## Key pitfalls (learned the hard way)

- IG posting is two-step: container → publish; container IDs are single-use
- IG `image_url` must be public HTTPS **JPEG with no query params** (signed
  S3 URLs are rejected)
- IG API limit: 25 published posts per rolling 24h window (shown in the UI)
- FB scheduling needs `published=false` + `scheduled_publish_time` ≥ 10 min out
- FB engagement lives at `.summary.total_count`; list payloads nest `data.data`
- Page `access_token` fields are scrubbed server-side before reaching the UI
- MCP SSE responses must be decoded as UTF-8 explicitly (requests guesses
  Latin-1 → emoji mojibake)
- Meta CDN images 403 with a referrer — the UI sets `<meta name="referrer"
  content="no-referrer">`
