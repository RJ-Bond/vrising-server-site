🇷🇺 [Русская версия](README.md)

# ⚔ V Rising — Game Server Website

A website for **V Rising** game servers: real-time server monitoring, a news feed with comments/reactions/polls, a player leaderboard (by playtime and by points), clans (synced directly from the game), bans and ban appeals, events/tournaments, a points-based shop, private messages and notifications, and a full 4-tier admin panel. A separate part of the system is the game-server integration via a companion BepInEx plugin (in-game registration, moderation, scheduled restarts, clan sync). Deploys with a single command on a clean Debian 13 box, updates with the `js` (or `vrising`) command.

---

## Tech Stack

| Layer | Technology |
|------|-----------|
| Backend | Python 3.12, FastAPI (async) |
| Database | SQLite (aiosqlite), SQLAlchemy 2.0 (async ORM) |
| Auth | JWT (python-jose) + bcrypt (passlib), optional 2FA/TOTP (pyotp) |
| Rate limiting | slowapi |
| Email (password reset, etc.) | aiosmtplib (optional, via SMTP) |
| Frontend | HTML5, Tailwind CSS (local static build), vanilla JS, Canvas 2D (charts) |
| AI chat | Anthropic API ("Castle Overseer", optional) |
| Game integration | Companion BepInEx plugin (`vrising-bepinex-plugin`) — X-Plugin-Key HTTP API |
| Server monitoring | Steam A2S_INFO (UDP) |
| Reverse proxy | Nginx (+ optional HTTPS via Let's Encrypt) |
| Containerization | Docker + Docker Compose |
| Server OS | Debian 13 (Trixie) |

---

## Features

### Home page (`index.html`)
- Real-time monitoring widget for both game servers (online/offline, player count, map, version), with a connect button via `steam://rungameid/...`
- Wipe countdown/history
- News feed: pagination, tags, pinned posts, reactions, comments (with threaded replies and reactions on comments), polls attached to a news post, a modal with the full article text
- "Who's online right now" presence indicator (`/api/online`)
- Notification bell (comment replies, mentions) and a private-messages widget — for logged-in users
- Optional AI chat "Castle Overseer" (Anthropic API)
- Sidebar navigation to every section of the site

### Servers (`servers.html`)
- Detailed monitoring for each server: status, list of players online
- Interactive online-history charts (Canvas, smoothed curves) with a period switcher
- Hourly activity heatmap, tied to the selected period
- Wipe history and statistics

### Players (`leaderboard.html`)
- Two leaderboard modes: total playtime and points balance (a "⏱ Time / 💎 Points" toggle)
- Per-server; period switcher: all-time / month / week (playtime mode)
- Rank-change indicator (▲/▼) versus yesterday, based on nightly rank snapshots
- Player search by name (debounced)
- Highlight + "📍 Find me" button — jumps to your own row in the ranking
- "Online now" indicator for currently connected players, last-session duration
- Player avatars (pulled from the linked in-game account), a top-3 podium

### Clans (`clans.html`)
- Clan rosters are synced directly from the game by the plugin (`POST /api/plugin/clans/sync`) — this section is entirely read-only on the website; there is no manual clan creation/editing through the web UI
- Clan cards with member count, motto, and search by name
- A detail modal with the full roster: member roles (leader/officer/member), avatars, links to linked players' profiles
- Summary stats: number of clans, total member count

### Map (`map.html`)
- An informational overview of Vardoran's regions (Farbane Woods, Dunley Farmlands, Silverlight Hills, Cursed Forest, Hallowed Mountains, Gloomrot, Brighthaven, dungeons) with each region's danger level

### Bans (`bans.html`)
- List of active bans issued via the in-game `.ban` command (publicly visible: character name, server, remaining time, issuing admin, reason)
- Search by name, server filter, a "new" badge for bans under 24 hours old
- For staff (`admin`+ role), the table upgrades in place with actions ("Unban"), a link to that player's moderation history, and a resolved-bans history table
- Ban appeals section (`admin`+): review submissions from `appeal.html`; approving an appeal automatically lifts the ban
- Link to submit an appeal (`/appeal.html`)

### Ban appeal (`appeal.html`)
- A public form to appeal an active ban — no site login required (a banned player typically has neither in-game nor site access under their account): SteamID/character name + message (`POST /api/appeals`)

### Events & tournaments (`events.html`)
- A list of site events with types (pvp / pve / social / other) and statuses (upcoming / active / ended / cancelled)
- Join/leave an event for logged-in users, with a participant limit
- Event management (create/edit/delete, participant list) — for staff

### Shop (`shop.html`)
- Redeem points (earned from playtime and from a daily-connect streak) for items from a catalog
- Available to logged-in users only — the balance is tied to the site account
- A redemption request deducts points immediately; item delivery is manual, handled in-game by staff (`status`: pending/fulfilled/cancelled)
- A feed of the player's own redemption requests and their statuses

### FAQ (`faq.html`)
- An accordion of answers to common questions: connecting via Steam, wipes, PvE rules, registration and linking your in-game name, password recovery, clans, server online counts, reporting violations

### Auth & profile
- Registration and login by username/password, JWT tokens (in an httpOnly cookie), passwords hashed with bcrypt
- "Remember me", email-based password recovery (forgot/reset password, emails sent via SMTP)
- Two-factor authentication (2FA/TOTP) — enable/disable from the profile page
- Change email and password
- Personal profile (`profile.html`): bio, cover image and avatar, linking an in-game nickname, points history and shop-request history, notification feed, private messages, and — for staff — customizable admin title and badge
- Public player profiles (`user.html`) — stats, clan, activity, avatar
- Initial-setup wizard (`setup.html`) on first run — creates the first administrator

### Admin panel (`admin.html`)
Access and which sidebar sections are visible depend on role (see below). Main sections:
- Dashboard with summary stats, traffic analytics
- News management: create, edit, delete, drafts, scheduled publishing, pinning, tags, templates, image uploads, polls attached to a post
- Comment and report moderation
- Event/tournament management
- User management: roles (including assigning moderator/admin/superadmin), ban/unban site accounts, unlinking a linked Steam account, forced logout (session revocation), list of linked accounts
- Leaderboard management (delete entries) and clans (view-only — data comes from the game)
- Server wipe management
- Points shop: item catalog, fulfillment queue for redemption requests, manual point grants to players
- "Plugins" section — game-server integration: per-server heartbeat status, connect/disconnect message templates, scheduled and recurring in-game chat announcements, one-off and daily scheduled server restarts (with a countdown), an RCON console (superadmin only), the plugin API key
- Site settings: title, background, timezone and date/time format, nav-item visibility, server IP/port, SMTP, SSL certificate installation, and updating the site from GitHub via the UI (superadmin only)
- File manager for uploads, and a media library
- Admin action audit log, a unified moderation log (bans/warnings/appeals/restarts in one feed), an error log
- Password-reset request management
- Database backups: list, download, create on demand (superadmin only)

### Game integration (BepInEx plugin)
The companion mod `vrising-bepinex-plugin` for the V Rising server exchanges data with the website via an HTTP API protected by an `X-Plugin-Key` header (`backend/routers/plugin_integration.py`, with some moderation endpoints living in `backend/routers/moderation.py`). Capabilities:
- Site registration and login straight from the in-game chat (`.register`/`.login`), linking a SteamID to a site account
- Heartbeat and playtime tracking, feeding the leaderboard and point grants
- Daily-connect streak tracking, with bonus points awarded
- A welcome prompt to accept the server rules (`.accept`) on first connect
- In-game moderation: `.warn` (warnings), `.ban`/`.unban` (temporary and permanent bans), synced with the site's public ban list and ban appeals
- Scheduled and recurring server restarts (one-off and daily by time), with in-chat warning broadcasts
- Syncing the in-game clan roster to the website
- Broadcasting scheduled in-game chat announcements configured from the admin panel
- Per-server custom connect/disconnect chat message templates

---

## Roles & permissions

A 4-tier hierarchy (`backend/auth.py`, `ROLE_LEVELS`), each tier including everything below it:

| Role | Level | Permissions |
|------|---------|-------|
| `user` | 0 | Regular player: profile, comments, reactions, event participation, shop, messages |
| `moderator` | 1 | + comment/report moderation, user management (roles below their own, banning), leaderboard |
| `admin` | 2 | + news, events, site settings, clans (view), bans/appeals, shop, plugin integration, files, analytics, logs |
| `superadmin` | 3 | + managing admin roles, backups, SSL setup, updating the site from the UI, RCON |

`admin.html` hides sidebar sections according to this same hierarchy (`SECTION_MIN_ROLE` in JS), and the backend enforces access via `role_level()`/`is_at_least()` — never by comparing directly against the literal string `"admin"`.

---

## Quick start — Debian 13

### Automatic install (recommended)

```bash
git clone https://github.com/RJ-Bond/vrising-server-site.git
cd vrising-server-site
sudo bash install.sh
```

The script will automatically:
1. Update system packages
2. Install Docker and Docker Compose (official repository)
3. Configure the UFW firewall (ports 80, 443, SSH)
4. Build and start the containers
5. Create the administrator account and the database
6. Register the system commands `js`/`vrising` (update) and `vrising-https` (issue SSL) — both are symlinks pointing to the same `install.sh`

Once finished, the terminal will print the site's address and the login credentials.

### Updating

On a server where the site is already installed (`/opt/vrising-site`), updating to the latest version from the repository just takes:

```bash
sudo js
```

(equivalent to `sudo vrising` — both commands are identical). This pulls changes from GitHub, then rebuilds and restarts the containers.

### HTTPS

```bash
sudo vrising-https domain.com admin@email.com
```

Issues and installs a Let's Encrypt SSL certificate for the given domain.

---

### Manual install

**1. Install Docker**

```bash
curl -fsSL https://get.docker.com | sh
```

**2. Clone the repository**

```bash
git clone https://github.com/RJ-Bond/vrising-server-site.git
cd vrising-server-site
```

**3. Create the `.env` file**

```bash
cp .env.example .env
```

Edit `.env` (baseline set — see `.env.example`):

```env
SECRET_KEY=replace_with_a_random_32_char_string
DATABASE_URL=sqlite+aiosqlite:////data/vrising.db
VRISING_SERVER_IP=127.0.0.1
VRISING_SERVER_PORT=27016
ANTHROPIC_API_KEY=optional_for_the_castle_overseer_chat
```

`docker-compose.yml` additionally supports (all optional, with sane defaults): `ALLOWED_ORIGINS` (CORS), `SMTP_HOST`/`SMTP_PORT`/`SMTP_USER`/`SMTP_PASS`/`SMTP_FROM` (for password-recovery emails).

**4. Start the project**

```bash
docker compose up -d --build
```

The site will be available on port `80`.

---

## Project structure

```
vrising-server-site/
├── Dockerfile                 # Builds the Python image
├── docker-compose.yml         # Container orchestration (web + nginx)
├── requirements.txt           # Python dependencies (production)
├── requirements-dev.txt       # + pytest/pytest-asyncio for tests
├── install.sh                 # Auto-install/update script for Debian 13 (js/vrising)
├── enable-https.sh            # SSL certificate issuance script (vrising-https)
├── VERSION                    # Version string, served via GET /api/version
├── .env.example                # Environment variable template
│
├── backend/
│   ├── main.py                # Domains not yet split into routers: version/SEO
│   │                           # (sitemap/rss/news-embed), setup, AI chat, presence (online),
│   │                           # announcements, server monitoring (A2S), background tasks
│   │                           # (scheduled publish, auto-backup, cleanup, leaderboard
│   │                           # snapshots, event status updates)
│   ├── models.py               # DB models: User, News, Comment, Setting, PlayerRecord,
│   │                           # Wipe, GameClan(+Member), Event, Ban/BanAppeal/Warning,
│   │                           # PointsTransaction/ShopItem/ShopRedemption, etc.
│   ├── database.py             # Async SQLite engine
│   ├── auth.py                 # JWT + bcrypt + 2FA/TOTP, ROLE_LEVELS/role_level()/is_at_least()
│   ├── monitor.py              # A2S_INFO UDP server monitoring
│   ├── schemas.py              # Pydantic request/response schemas
│   ├── helpers.py              # Shared helpers used by several routers
│   ├── rate_limit.py           # slowapi configuration
│   ├── routers/                # FastAPI routers, wired up via app.include_router()
│   │   ├── auth.py              #   /api/auth/* — register/login/2FA/password-email change
│   │   ├── profile.py            #   /api/profile/* — bio, cover, badge, /api/team
│   │   ├── users.py              #   /api/users/*, /api/admin/users/* — profiles & admin user mgmt
│   │   ├── clans.py              #   /api/clans* — in-game clans (read-only)
│   │   ├── leaderboard.py        #   /api/leaderboard*
│   │   ├── news.py               #   /api/news*, /api/comments*, /api/admin/news*
│   │   ├── wipes.py              #   /api/wipes, /api/admin/wipes
│   │   ├── events.py             #   /api/events*, /api/admin/events*
│   │   ├── polls.py              #   /api/news/{slug}/poll*
│   │   ├── notifications.py      #   /api/notifications*
│   │   ├── messages.py           #   /api/messages* (private messages)
│   │   ├── reports.py            #   /api/reports, /api/admin/reports*
│   │   ├── points_shop.py        #   /api/shop/*, /api/admin/shop/*, /api/admin/points/*
│   │   ├── moderation.py         #   /api/bans, /api/appeals, /api/admin/bans|appeals|moderation-log,
│   │   │                          #   /api/plugin/warn|ban|unban|due-unbans|ban-status|log-action
│   │   ├── plugin_integration.py #   /api/plugin/* — registration/heartbeat/playtime/restarts/clan sync
│   │   ├── server_admin.py       #   /api/admin/servers/*, /api/admin/message-templates,
│   │   │                          #   /api/admin/server-api-key
│   │   ├── admin_settings.py     #   /api/settings/public, /api/admin/settings*, /api/admin/maintenance/*
│   │   ├── admin_system.py       #   /api/admin/upload|uploads|media|backup(s)|ssl|update|rcon
│   │   └── admin_misc.py         #   /api/admin/stats|comments|audit-log|analytics|export/*|errors
│   └── tests/                    # pytest suite (backend/tests/test_*.py + conftest.py)
│
├── frontend/                     # No build step — served by nginx as-is
│   ├── index.html                 # Home: monitoring + news + chat + presence
│   ├── servers.html               # Detailed server monitoring and charts
│   ├── leaderboard.html           # Leaderboard (playtime / points)
│   ├── clans.html                 # Clans (synced from the game)
│   ├── map.html                    # World map overview
│   ├── bans.html                    # Bans + appeals (admin)
│   ├── appeal.html                  # Public ban-appeal form
│   ├── events.html                  # Events & tournaments
│   ├── shop.html                     # Points shop
│   ├── faq.html                       # FAQ
│   ├── login.html                      # Login and registration
│   ├── setup.html                       # Initial site setup
│   ├── profile.html                      # Personal profile
│   ├── user.html                          # Public player profile
│   ├── reset.html                          # Password recovery
│   ├── admin.html                           # Admin panel
│   ├── maintenance.html                      # Maintenance-mode page (503)
│   ├── offline.html                           # Service worker's offline page
│   ├── 404.html                                # Not-found page
│   ├── theme.css / components.css / index.css  # Design system (tokens/shared components/home)
│   ├── common.js / index.js / sw.js              # Shared JS / home-page JS / service worker
│   └── tailwind.min.css, quill*, purify.min.js    # Locally vendored third-party libraries
│
├── nginx/
│   ├── nginx.conf              # /api/ → FastAPI, / → static, maintenance mode, SEO prerender for bots
│   └── nginx-ssl.conf          # HTTPS variant of the config (used by docker-compose)
│
└── scripts/                     # Development tooling (see CLAUDE.md)
    ├── check.sh                  # Validates frontend HTML/CSS
    ├── check_backend.sh          # Imports every backend module via uv (catches syntax/import errors)
    ├── test_backend.sh           # Runs the pytest suite
    ├── preview.sh / preview-admin.sh / preview-mock.sh  # Headless page screenshots
    ├── admin-mock-fetch.js / public-mock-fetch.js        # API mocks for the preview scripts
    └── serve.ps1                                          # Static server for frontend/ previews
```

---

## API

Swagger docs are available once the app is running, at:
`http://<server-IP>/api/docs`

Below is a representative cross-section (the project has 195+ routes); see Swagger or the corresponding `backend/routers/*.py` / `backend/main.py` files for the full list.

| Area | Example routes | Access |
|--------|------------------|--------|
| Auth | `POST /api/auth/register`, `/login`, `/logout`, `GET /auth/me`, `POST /auth/change-password`, `/change-email`, `/auth/2fa/setup`\|`enable`\|`disable`, `/auth/forgot-password`, `/auth/reset-password/{token}`, `/auth/avatar` | Public / User |
| Initial setup | `GET /api/setup/status`, `POST /api/setup/complete` | Public (until first setup) |
| Server monitoring | `GET /api/monitor/status[2]`, `/monitor/history[2]`, `/monitor/snapshots`, `/monitor/stats`, `/monitor/status/stream` (SSE) | Public |
| Presence | `POST /api/online/ping`, `GET /api/online`, `/online/stream` (SSE) | Public |
| News | `GET /api/news`, `/news/{slug}`, `/news/tags`, `POST /news/{slug}/react`, `/news/{slug}/comments`, `/news/{slug}/poll`, `/poll/vote` | Public / User |
| Wipes | `GET /api/wipes`, `POST/DELETE /api/admin/wipes` | Public / Admin |
| Leaderboard | `GET /api/leaderboard`, `/leaderboard/points`, `DELETE /api/admin/leaderboard/{id}` | Public / Admin |
| Clans (in-game) | `GET /api/clans`, `/clans/{id}` | Public |
| Events | `GET /api/events`, `/events/{id}`, `POST /events/{id}/join`, `DELETE /events/{id}/leave`, `POST/PUT/DELETE /api/admin/events` | Public / User / Admin |
| Points shop | `GET /api/shop/items`, `POST /shop/redeem`, `GET /shop/redemptions/me`, `/points/transactions/me`, `POST/PUT/DELETE /api/admin/shop/items`, `/admin/shop/redemptions/{id}/fulfill`, `POST /admin/points/grant` | Public / User / Admin |
| Bans & appeals | `GET /api/bans`, `POST /api/appeals`, `GET/POST /api/admin/bans`, `/admin/bans/{id}/unban`, `/admin/appeals`, `/admin/appeals/{id}/resolve`, `/admin/moderation-log` | Public / Admin |
| Profiles | `GET /api/users/{username}`, `/users/{username}/activity`, `POST /api/profile/bio`\|`cover`\|`badge-icon`, `GET /api/team` | Public / User |
| Notifications & messages | `GET /api/notifications`, `POST /notifications/read-all`, `POST /api/messages`, `GET /messages/inbox`, `/messages/with/{username}` | User |
| Reports | `POST /api/reports`, `GET/PATCH /api/admin/reports` | User / Moderator |
| AI chat | `POST /api/chat` | User |
| Game plugin (X-Plugin-Key) | `GET /api/plugin/status`, `POST /plugin/register`\|`login`\|`heartbeat`\|`sessions`\|`connect-streak`, `GET /plugin/wipe-info`\|`playtime`\|`restart-status`, `POST /plugin/warn`\|`ban`\|`unban`\|`clans/sync`\|`schedule-restart` | Plugin (key) |
| Admin: content | `GET/POST/PUT/DELETE /api/admin/news`, `/admin/comments`, `/admin/upload`, `/admin/uploads`, `/admin/media` | Admin |
| Admin: users | `GET /api/admin/users`, `PUT /api/admin/users/{id}/role`, `/toggle-active`, `/revoke-sessions`, `/unlink-steam`, `DELETE` | Moderator / Admin |
| Admin: settings | `GET/PUT /api/admin/settings`, `/admin/settings/import`, `/admin/maintenance/status`, `/admin/server-api-key`, `/admin/message-templates` | Admin |
| Admin: restarts & announcements | `GET/POST/DELETE /api/admin/servers/{n}/restart`, `/daily-restart`, `GET/POST/PUT/DELETE /api/admin/announcements`, `/plugin-status` | Admin |
| Admin: misc | `GET /api/admin/stats`, `/admin/audit-log`, `/admin/analytics`, `/admin/errors`, `/admin/password-resets`, `/admin/export/*` | Admin |
| Admin: superadmin | `POST /api/admin/ssl/install`, `/admin/update`, `/admin/rcon`, `GET /admin/backup`, `/admin/backups`, `POST /admin/backups/create` | Superadmin |
| Misc/SEO | `GET /api/version`, `/api/sitemap.xml`, `/api/rss.xml`, `/api/news-embed` | Public |

---

## Container management

```bash
# View logs
docker compose logs -f

# Restart
docker compose restart

# Stop
docker compose down

# Rebuild after changes
docker compose up -d --build

# Update from GitHub (on the server)
sudo js
```

---

## Default credentials

Created automatically on first run:

| Field | Value |
|----------|---------|
| Username | `admin` |
| Password | `supersecretpassword` |

> **Change the administrator password immediately after your first login.**

---

## License

MIT
