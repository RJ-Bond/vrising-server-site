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

## Admin roles (3-tier)
`User.role` is `"user"` | `"moderator"` | `"admin"` | `"superadmin"` (ascending
capability — see `ROLE_LEVELS` in `backend/auth.py`). `get_admin_user` kept its
name but now means "admin or superadmin"; `get_moderator_user`/
`get_superadmin_user` are the other two tiers. Moderator = content/user
moderation only (comments, reports, ban toggle); superadmin = role management,
backups, deploy/SSL, RCON (full-DB/infra access). When adding a new
admin-gated endpoint or a manual `current_user.role` check, use
`role_level()`/`is_at_least()` from `backend/auth.py` — **never** compare
against the literal string `"admin"` (a `superadmin` legitimately fails that
comparison and silently loses access — this bit the migration itself twice
during development). `frontend/admin.html` mirrors the hierarchy client-side
via `SECTION_MIN_ROLE` (hides sidebar sections + gates `showSection()`) — that
map, `ROLE_LEVELS`, and `ROLE_LABELS` **must** be declared as plain top-level
`const`s, not inside a function that runs later, since `applyRoleVisibility()`
is called from the auth-gate IIFE near the top of the script; a `const`
declared further down throws "Cannot access before initialization" the
moment that early call runs (caught via a live screenshot, not by reading the
diff — `scripts/preview-admin.sh mobile 390 3000 moderator` reproduces it).
`scripts/preview-admin.sh` takes a `[role]` 4th arg (`moderator`/`admin`/
`superadmin`) for exactly this kind of verification.

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
- `bash scripts/lint_backend.sh` — ruff (`pyproject.toml` has the rule
  selection + the *reasons* for each ignore — read it before "fixing" an
  E711/E712 SQLAlchemy `== True`/`!= None` finding; that rewrite is a real
  bug in query-building code, not a style nit, which is exactly why it's
  ignored repo-wide rather than fixed). Wired into CI as its own step.
- `bash scripts/lint_frontend.sh` — ESLint (flat config: `eslint.config.js`)
  over the standalone `frontend/*.js` files only (not inline `<script>`
  blocks in HTML, not vendored `*.min.js`). Needs `npm ci` once (Node/npm are
  available in CI via `actions/setup-node`; not guaranteed in every dev
  sandbox). No stylistic rules — real-bug rules only (`no-undef`,
  `no-unused-vars` scoped to function-local vars only, since many top-level
  functions are legitimately only called from `onclick="..."` in
  server-rendered/innerHTML markup ESLint can't see).
- `.pre-commit-config.yaml` — runs the four checks above (ruff, check_backend,
  check.sh, lint_frontend) locally before a commit lands, not just in CI.
  `pip install pre-commit && pre-commit install` once per clone; opt-in, not
  enforced server-side.
- **Alembic** (`alembic/`) is set up (env.py bridges its sync migration
  context to the app's async engine via `run_sync`) with one verified initial
  migration reproducing today's 41-table schema exactly — but it is **not
  yet wired into the startup path**. `main.py`'s `lifespan()` still calls
  `Base.metadata.create_all()` on every boot (fine for fresh installs, does
  nothing for existing tables/columns on prod). Until that integration
  happens, a schema change still needs the same manual care it always has;
  don't assume `alembic upgrade head` runs anywhere in prod yet.
- **Pillow-based upload optimization** (`optimize_image_bytes()` in
  `backend/helpers.py`) downscales any upload wider than 2000px and
  re-compresses it, called from all 4 raster-upload endpoints (admin generic
  upload, avatar, profile cover, badge icon). Deliberately preserves the
  source format rather than normalizing to JPEG (several uploads — site
  logo, hero logo, favicon — need transparency JPEG would flatten), and is a
  no-op for `.gif`/`.ico` (animation / multi-res icon sets Pillow can't
  round-trip). Any decode/encode failure silently falls back to the original
  bytes — it's a size optimization, not a validation gate.
- **Sentry error monitoring** is wired but **inert by default** — nobody has
  a Sentry project yet. Backend: gated on `SENTRY_DSN` env var (`main.py`,
  right after the logging setup) — unset means `sentry_sdk.init()` never
  runs. Frontend: gated on a hardcoded empty `SENTRY_DSN` constant at the top
  of `common.js` (couldn't thread it through `/api/settings/public` without
  touching `admin_settings.py`, out of scope when this was added — revisit if
  Sentry actually gets adopted). `frontend/sentry.min.js` is a real vendored
  `@sentry/browser` bundle, only fetched when the constant is non-empty.
  Turning it on for real also needs the Sentry ingest domain added to
  `connect-src` in both `nginx/nginx.conf` and `nginx/nginx-ssl.conf` (CSP
  blocks it otherwise) — see the comment at that line in each file.
- **Dependabot** (`.github/dependabot.yml`) — weekly PRs for pip (grouped
  into one), Docker base images, and GitHub Actions versions.

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
  keys list, both in `backend/routers/admin_settings.py` (not `main.py` —
  moved there in the router split), plus admin `SETTINGS_FIELD_KEYS` in
  `admin.html`.
- **FOUC:** don't hard-code placeholder text (e.g. "V RISING") that JS overwrites
  from settings — it flashes on refresh. Leave it empty; JS fills it.
- Line endings handled by `.gitattributes` (LF). `.shots/` is gitignored.
