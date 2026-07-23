# FLOWS · Business Social Hub

A local, single-file Instagram + Facebook command centre.  
Open `social-hub/static/index.html` in a browser — no install, no bundler, no build step.  
The front-end is real; the backend (`social-hub/server.py`) feeds it live data via Composio.  
Running without the server keeps the UI functional with simulated data so you can explore the layout and all interactive behaviours offline.

---

## Quick start

```bash
# With live backend
python3 social-hub/server.py        # or double-click flows.command
# → http://localhost:8787

# Without backend (offline / demo)
open social-hub/static/index.html  # macOS
xdg-open social-hub/static/index.html  # Linux
```

Once the page is open, attach a Composio session by setting `COMPOSIO_MCP_URL` + `COMPOSIO_MCP_KEY` (or `~/.claude.json`) and restarting the server.  
All content strings come through `[STRINGS]` at the top of `index.html` — edit that block to relabel anything without touching component code.

---

## What's inside

| Panel | What it does |
|---|---|
| **Dashboard** | KPI strip (audience, capacity, connection status), live profiles, unified cross-platform feed with content-type badges, per-account filter chips |
| **Post drill-down** | Tap any post — status, type, date, and engagement appear immediately; per-post views/reach, reactions breakdown, and comment threads stream in; edit or delete Facebook posts in place |
| **Publish** | Five post types: Photo (IG + FB), Video/Reel, Carousel (2–10 images, IG), Story (IG), Text (FB). Destinations that can't accept a given type grey out with a plain-English reason; live preview; confirm-before-publish modal; per-destination results with permalinks |
| **Library** | Everything ever published across all accounts in one shelf; one click to *Post again* (prefilled, confirm-guarded) or *Schedule* it |
| **Analytics** | 7-day account performance (IG insights + FB page insights) and tagged mentions per Instagram account |
| **Integrations** | Live pipe health checks, managed-page inventory, and the full Composio capability registry — every tool marked live or unavailable |

---

## What actually works

These behaviours are real and were verified against the running page:

- **Clipboard copy** — uses `navigator.clipboard.writeText`; falls back to `document.execCommand('copy')` when the Clipboard API is unavailable (e.g. over `file://` without a secure context).
- **Blob-based downloads** — export buttons build a `Blob`, attach it to a temporary `<a>` element, and trigger a click. Nothing is written server-side.
- **Live / debounced filtering** — filter inputs run on every keystroke with a short debounce; matched text is wrapped in `<mark>` so hits are highlighted inline.
- **Keyboard shortcuts** — `Ctrl+F` (or `⌘F`) and `/` both focus the active panel's search field; `Escape` clears it.

---

## Exports

| Format | Filename |
|---|---|
| JSON | `flows-export-YYYY-MM-DD.json` |
| CSV | `flows-export-YYYY-MM-DD.csv` |
| Markdown | `flows-export-YYYY-MM-DD.md` |

All three are generated client-side from whatever data is currently loaded in the panel.

---

## Notes & limitations

- **Simulated data** — opening `index.html` directly (no server) shows fixture data. All panels render and all interactive features work, but nothing is persisted or sent anywhere.
- **Socket path is display-only** — the connection indicator in the header shows the configured MCP endpoint but does not attempt a WebSocket connection from the front-end. Actual socket traffic goes through `server.py`.
- **CDN dependency** — icons are loaded from Lucide's CDN. The page requires an internet connection to render icons; it degrades gracefully (text labels remain) if the CDN is unreachable.
- **Clipboard over `file://`** — `navigator.clipboard` requires a secure context (`https://` or `localhost`). When opening `index.html` directly from the filesystem, the `execCommand` fallback is used instead. Both paths produce the same result; the fallback is silent.

---

## Design

Dark UI — Sora/Inter type, iris (`#6a4dff`) + coral (`#ff5470`) accent pair.  
Desktop: sidebar + dock. Mobile: bottom-tab PWA (installable via `manifest.json`).
