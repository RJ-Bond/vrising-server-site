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
- `scripts/serve.ps1` — tiny PowerShell static server for `frontend/` (no
  node and no *system* python in this sandbox — but see the `uv` entry below
  for backend work; **Chrome is** at
  `C:\Program Files\Google\Chrome\Application\chrome.exe`).
- Preview has **no backend**, so data regions show loading/empty/error states.
  It's accurate for layout/nav/forms, not for real data.
- `bash scripts/preview-admin.sh [mobile|desktop] [width] [height]` — like
  `preview.sh` but for **`admin.html` specifically**: it's auth-gated (redirects
  to `/login.html` without a session) and its dashboard fetches live data, so
  plain `preview.sh` only ever shows the login screen. This builds a throwaway
  copy of `admin.html` with `scripts/admin-mock-fetch.js` injected as the first
  `<script>` — it seeds a fake admin session in `localStorage` and monkey-patches
  `window.fetch` with canned JSON matching the real backend response shapes for
  the endpoints the dashboard hits on load (`/api/auth/me`, `/api/admin/stats`,
  `/api/monitor/status(2)`, etc.) — so the sidebar/dashboard actually render with
  realistic data instead of stopping at the login form. Use this (not blind CSS
  reasoning) when touching `admin.html` layout — a past round of admin mobile
  fixes went through 3 blind iterations before this existed. If you add a new
  section's fetch calls to the mock, keep field shapes in sync with
  `backend/schemas.py`.
- `bash scripts/preview-mock.sh <page.html> [mobile|desktop] [width] [height]` —
  like `preview-admin.sh` but for **public pages** (clans/events/leaderboard/
  servers/bans/…): injects `scripts/public-mock-fetch.js` so `/api/clans`,
  `/api/events`, `/api/leaderboard`, `/api/monitor/status(2)`, `/api/wipes`,
  `/api/bans`, etc. resolve with realistic canned data (anonymous visitor, no
  session) instead of the loading/empty/error states plain `preview.sh` shows.
  Use this to actually see card/list layouts filled with content — that's how
  a missing `clan.description` on the clans-page cards got caught. Keep field
  shapes in sync with `backend/schemas.py` / the route handlers in `main.py`
  when you add a new page's endpoints to the mock.
- **Backend verification — `uv` is on PATH and can provision a real Python on
  demand** (this sandbox's own `python`/`py` are non-functional Windows Store
  stubs; don't trust them). Use it instead of reading diffs and hoping:
  - `bash scripts/check_backend.sh` — imports every `backend/*.py` module for
    real (via `uv run --python 3.12 --with-requirements requirements.txt`).
    Catches syntax errors, bad imports, undefined names. Run before pushing
    ANY backend change — cheap, seconds to run.
  - `bash scripts/test_backend.sh` — runs the pytest suite in `backend/tests/`
    (via `requirements-dev.txt`, adds pytest + pytest-asyncio on top of prod
    deps). `backend/tests/conftest.py` gives a fresh file-based sqlite DB per
    test (monkeypatches `backend.database.engine`/`AsyncSessionLocal` *and*
    `backend.main.engine`, since main.py imports `engine` by name for its own
    background tasks) plus an `httpx.ASGITransport` client fixture — add new
    test files here for any backend logic worth protecting from regressions.
  - Both exist because the leaderboard rank-delta feature shipped after 3
    blind CSS-reasoning-only mobile fixes went wrong this same session —
    don't repeat that pattern for backend code, where a mistake means a 500
    or a broken deploy, not just an ugly screenshot.

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
