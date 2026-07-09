# CLAUDE.md — working notes for agents

Operational map for this repo. Complements `README.md` (which covers the
feature set / stack). Read this first; it captures the things that are easy to
re-derive the hard way.

## What this is
V Rising community site. **Backend:** FastAPI + SQLAlchemy 2.0 async + SQLite
(`backend/main.py`, `models.py`, `schemas.py`, `monitor.py`). **Frontend:**
vanilla JS + inline/extracted CSS, **no build step** — files in `frontend/`
are served as-is by nginx. Tailwind is a static `frontend/tailwind.min.css`.
CSP blocks external CDNs, so third-party libs are vendored locally
(`purify.min.js`, `quill*`, `tailwind.min.css`).

## Deploy (IMPORTANT)
Changes go live only when the maintainer runs the server-side deploy. After you
commit **and push to `master`**, always tell the user: **«На сервере: `sudo js`»**.
You cannot deploy yourself. Work on `master` is normal here.

## Frontend design system
Three layers, loaded in this order (page inline `<style>` wins last):
1. **`frontend/theme.css`** — design tokens (`--bg --card --crimson --purple
   --gold --text --muted …`). Single source for brand colours: change the
   accent here, not in 12 files.
2. **`frontend/components.css`** — shared nav / mobile drawer / tooltips /
   scrollbar / `fade-up` for the content pages (servers, leaderboard, clans,
   bans, faq, map, events).
3. Each page keeps in its inline `<style>` only the tokens/rules it **overrides**
   (e.g. servers/leaderboard/faq/map use a lighter `--text`/`--card`).
- `index.html` was split: CSS → `frontend/index.css`, app JS → `frontend/index.js`
  (small tail scripts stay inline). `common.js` is shared across all pages.
- Shared CSS/JS links carry `?v=N` — **bump it when you edit that file** (nginx
  serves css/js as `immutable`, so without the bump browsers keep the old copy).

## Tooling (scripts/)
- `bash scripts/check.sh` — validates every `frontend/*.html` + the extracted
  CSS: `<style>` balance and undefined `var(--x)`. Run before pushing CSS work.
- `bash scripts/preview.sh <page.html> [mobile|desktop]` — one-command headless
  screenshot to `.shots/`; auto-starts the static server. Use it to self-verify
  layout. **Mobile MUST use `mobile`** (renders inside a 390px iframe) — a direct
  `--window-size=390` shot renders too wide because desktop Chrome ignores the
  viewport meta.
- `scripts/serve.ps1` — tiny PowerShell static server for `frontend/`
  (no node/python available in this sandbox; **Chrome is** at
  `C:\Program Files\Google\Chrome\Application\chrome.exe`).
- Preview has **no backend**, so data regions show loading/empty/error states.
  It's accurate for layout/nav/forms, not for real data.

## Gotchas (bitten by these)
- **Service worker** (`frontend/sw.js`): never intercept image requests — proxying
  them through `fetch()` breaks `background-image` on normal reload (works on
  Ctrl+Shift+R, not F5). Bump `CACHE_NAME` when changing sw.js.
- **SQLite datetimes come back naive.** Calling `.timestamp()` / `.hour` /
  comparing with `datetime.now(timezone.utc)` assumes the host's local zone
  (Europe/Moscow) → 3h skew / TypeError. Normalize to UTC first (see
  `_utc_ts()` / `_fmt_dt()` in `main.py`).
- **Settings** are key/value in the DB. A new setting needs: add to
  `ALLOWED_SETTING_KEYS` (save allow-list) **and** the `/api/settings/public`
  keys list in `main.py`, plus admin `SETTINGS_FIELD_KEYS` in `admin.html`.
- **FOUC:** don't hard-code placeholder text (e.g. "V RISING") that JS overwrites
  from settings — it flashes on refresh. Leave it empty; JS fills it.
- Line endings handled by `.gitattributes` (LF). `.shots/` is gitignored.
