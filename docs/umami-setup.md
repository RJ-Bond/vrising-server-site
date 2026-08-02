# Umami analytics — setup

Self-hosted [Umami](https://umami.is) (open-source, cookie-free, privacy-friendly
analytics with its own dashboard: page views, referrers, devices, countries,
real-time). This is an **addition**, not a replacement — the existing hand-rolled
tracker (`PageViewMiddleware` in `backend/main.py`, surfaced via
`GET /api/admin/analytics`) is untouched and keeps running side by side. Umami
gives the maintainer a nicer, more detailed dashboard; the existing tracker keeps
feeding the admin panel's built-in stats. Neither depends on the other.

## What was added

- `docker-compose.yml`: two new services —
  - `umami-db` — Postgres 16, holds Umami's own data. Umami does **not** support
    SQLite, so this can't reuse the `web` container's sqlite file the way the rest
    of the site does.
  - `umami` — the Umami app itself (`ghcr.io/umami-software/umami:postgresql-latest`).
- `nginx/nginx.conf` + `nginx/nginx-ssl.conf`: a `location ^~ /analytics/` block
  reverse-proxying to the `umami` container, at the **same origin** as the main
  site (`https://v.just-skill.ru/analytics/...`).
- `frontend/common.js`: an inert-by-default tracker snippet, gated behind
  `UMAMI_WEBSITE_ID` (empty string = no-op, same pattern as the existing
  `SENTRY_DSN` flag right above it in the same file).
- `.env.example`: `UMAMI_APP_SECRET` and `UMAMI_DB_PASSWORD` placeholders.

## Why same-origin path (`/analytics/`) instead of a subdomain

Two ways to expose Umami were considered:

1. **A subdomain** (e.g. `stats.v.just-skill.ru`) — the "textbook" way most Umami
   guides show it. Requires a new DNS A/AAAA record pointing at the server, and
   either a new certbot cert for that subdomain or reissuing the existing one as a
   SAN/wildcard cert. Both are manual steps outside this repo that only the
   maintainer with DNS/server access can do, and neither can be tested from here.
2. **A path on the existing origin** (`/analytics/`), proxied by the *same* nginx
   container that already proxies `/api/` to the `web` container — chosen here.
   No new DNS record, no new cert, no new open port. It's also the more
   consistent choice: this repo already has exactly one pattern for "reach an
   internal container from the public site" (the `location ^~ /api/` block), and
   `/analytics/` just follows it.

This only works because Umami has a documented `BASE_PATH` env var specifically
for reverse-proxy-under-a-subpath deployments (set to `/analytics` on the `umami`
service in `docker-compose.yml`) — without it, the app's own HTML/JS asset URLs
and API calls are absolute at `/`, and a plain path-prefixed proxy would 404 on
every asset. **This detail could not be tested from this sandbox** (no server to
actually deploy to) — verify after the first real deploy; see "Known unknowns"
below.

Directly exposing Umami's port (3000) on the host was ruled out: it's strictly
more exposure than the path-proxy option above for no benefit, since the path
option was just as easy to write and needs no extra hardening follow-up.

## Manual steps required after deploy (cannot be done from here)

None of this can be completed without a live server, so after `sudo js` deploys
this compose change, the maintainer needs to:

1. **Set real secrets** before first boot — put actual values in the server's
   `.env` (not committed) for:
   - `UMAMI_APP_SECRET` — random string, 32+ chars. Generate with e.g.
     `openssl rand -hex 32`.
   - `UMAMI_DB_PASSWORD` — random Postgres password.

   The compose defaults (`changeme_generate_random_32chars` /
   `changeme_generate_random_password`) are placeholders only — fine for a
   throwaway local test, not for the real deploy.

2. **First login + change the default password.** Umami creates a default
   `admin` / `umami` account on first boot. Go to
   `https://v.just-skill.ru/analytics/`, log in with that, and **change the
   password immediately** — this is the single most important manual step,
   the default credentials are publicly documented by Umami itself.

3. **Add this site as a website inside Umami's dashboard.** This generates a
   Website ID (a UUID).

4. **Paste that Website ID into `frontend/common.js`.** Set
   `const UMAMI_WEBSITE_ID = '...'` (currently left empty/commented — see the
   block right after the Sentry one in that file). Until this is done, the
   tracker script is never injected and Umami collects nothing.

5. **Bump the cache-busting `?v=` param** wherever `common.js` is referenced
   (`<script src="/common.js?v=N"></script>` — currently `v=10` across all
   `frontend/*.html` pages) so nginx's `immutable` cache header doesn't keep
   serving the old copy to returning visitors. Grep for `common.js?v=` to find
   every page that needs the bump.

6. Redeploy (`sudo js`) once more after steps 3–5 so the real Website ID goes live.

## Known unknowns / things to verify at deploy time

Since there's no server access from this environment, a few things are
documented as the best-available answer but not actually verified live:

- **`BASE_PATH=/analytics` behavior** — assumed to prefix *every* app route
  (dashboard pages, `/api/send`, `/api/heartbeat`, the tracker script itself)
  uniformly, per Umami's own docs. If the container comes up unhealthy or
  `/analytics/` 404s after deploy, check `docker logs vrising_umami` — the
  healthcheck path (`/analytics/api/heartbeat`) is the most likely thing to need
  adjusting.
- **Image tag** — `ghcr.io/umami-software/umami:postgresql-latest` is a floating
  tag. Once this has been deployed once and confirmed working, pin it to a
  specific release (see the image's tag list at
  `https://github.com/umami-software/umami/pkgs/container/umami`) instead of
  tracking `latest` forever.
- **Postgres version** — pinned to `postgres:16-alpine`; Umami v3 requires
  Postgres 12.14+, so this is comfortably above the floor, but wasn't tested
  against the actual Umami release that ends up deployed.

## CSP

No Content-Security-Policy change was needed. Umami's tracker is proxied through
the same nginx origin as the rest of the site, so both the script fetch
(`script-src 'self'`) and its data-collection POSTs (`connect-src 'self'`) are
already covered by the existing `'self'` directives in
`nginx/nginx.conf`/`nginx/nginx-ssl.conf` — see the comment block above the
`Content-Security-Policy` header in both files (right next to the existing
Sentry CSP note, which *does* need a manual domain added since Sentry is
necessarily cross-origin).
