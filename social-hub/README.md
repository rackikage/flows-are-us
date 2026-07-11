# Social Media Hub

All-accounts dashboard for Instagram & Facebook via Composio connectors.

## Accounts wired

| Platform  | Handle / Page      | Composio Alias           | Account ID          |
|-----------|--------------------|--------------------------|---------------------|
| Instagram | @rossmorebuilding  | `instagram_okie-ceile`   | 27412531841701728   |
| Instagram | @human247.365      | `instagram_anode-conch`  | 27330220746665792   |
| Facebook  | Human 247/365      | `facebook_uberty-minor`  | 1154954434369587    |

## APIs used

### Instagram Graph API (v21.0)
| Tool | Endpoint | Data |
|------|----------|------|
| `INSTAGRAM_GET_USER_INFO` | `/me` | Profile, bio, follower counts |
| `INSTAGRAM_GET_IG_USER_MEDIA` | `/me/media` | Posts, reels, carousels |
| `INSTAGRAM_GET_USER_INSIGHTS` | `/me/insights` | Reach, engagement, interactions |
| `INSTAGRAM_GET_IG_MEDIA_INSIGHTS` | `/{media_id}/insights` | Per-post reach, likes, saves |
| `INSTAGRAM_GET_IG_MEDIA_COMMENTS` | `/{media_id}/comments` | Comments + replies |
| `INSTAGRAM_GET_IG_USER_TAGS` | `/me/tags` | Tagged media |

### Facebook Graph API (v23.0)
| Tool | Endpoint | Data |
|------|----------|------|
| `FACEBOOK_LIST_MANAGED_PAGES` | `/me/accounts` | All pages you manage |
| `FACEBOOK_GET_PAGE_DETAILS` | `/{page_id}` | Page metadata |
| `FACEBOOK_GET_PAGE_POSTS` | `/{page_id}/feed` | All timeline posts |
| `FACEBOOK_GET_PAGE_INSIGHTS` | `/{page_id}/insights` | Page impressions, reach |
| `FACEBOOK_GET_POST_INSIGHTS` | `/{post_id}/insights` | Per-post analytics |
| `FACEBOOK_GET_COMMENTS` | `/{post_id}/comments` | Comments |
| `FACEBOOK_GET_POST_REACTIONS` | `/{post_id}/reactions` | Reaction breakdown |

## Setup

```bash
pip install composio-core rich
```

Ensure your Composio API key is set:
```bash
export COMPOSIO_API_KEY=your_key_here
```

## Run

```bash
python social-hub/hub.py
```

## Pagination

Both Instagram and Facebook paginate via cursor. Use `paging.cursors.after` from each response
and pass it as the `after` param on the next call until no `paging.next` is returned.

## Key pitfalls

- Instagram insights silently omit metrics with no data — guard missing keys
- Facebook engagement totals live at `.summary.total_count`, shares at `.count`
- Instagram media double-nests under `data.data` — always parse defensively
- Access tokens from `FACEBOOK_LIST_MANAGED_PAGES` are secrets — never log or return them
