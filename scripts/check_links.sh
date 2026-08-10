#!/usr/bin/env bash
# Crawls frontend/*.html for internal <a href="..."> links and flags any pointing to a
# page/file that doesn't exist under frontend/. Companion to check.sh's CSS/HTML
# structural checks, same grep-based no-dependency style so it's cheap to run in CI.
#
# Scope, deliberately narrow:
#   - Only <a href="...">, not <link>/<script src>/<img src> — those aren't "page
#     links" in the sense a 404 nav bug matters for, and check.sh already audits CSS.
#   - External links (http(s)://, //, mailto:, tel:, javascript:) and pure #-anchors
#     are skipped — not this checker's concern (external liveness is flaky/slow), and
#     an in-page #anchor isn't a "different page" to 404 on.
#   - hrefs built entirely from a JS template-literal expression (href="${...}", used
#     in innerHTML-rendered rows for things like an admin backup filename or a Discord
#     URL from settings) are skipped — there's no static target to check. hrefs that
#     are static except for a `?query=${...}` or `#hash` tail (e.g.
#     href="/user.html?u=${encodeURIComponent(name)}") ARE checked, against the static
#     path portion only.
#   - /api/* and other backend-proxied routes (/rss.xml, /sitemap.xml — see
#     nginx/nginx.conf's `location =` blocks) are skipped: they're dynamic endpoints,
#     not static files under frontend/, so "does this file exist" doesn't apply.
set -u
cd "$(dirname "$0")/.." || exit 2
fail=0

for f in frontend/*.html; do
  [ -e "$f" ] || continue
  hrefs=$(grep -oE '<a\b[^>]*\shref="[^"]*"' "$f" | grep -oE 'href="[^"]*"' | sed -E 's/^href="//; s/"$//')
  while IFS= read -r href; do
    [ -z "$href" ] && continue
    case "$href" in
      http://*|https://*|//*|mailto:*|tel:*|javascript:*|'#'*) continue ;;
      '${'*) continue ;;  # fully dynamic, e.g. href="${API}/admin/backups/${...}"
    esac

    # Strip a #fragment then a ?query, leaving just the path portion to resolve.
    path="${href%%#*}"
    path="${path%%\?*}"

    case "$path" in
      *'${'*) continue ;;               # dynamic segment mid-path — can't resolve statically
      /api/*|/rss.xml|/sitemap.xml) continue ;;  # backend-proxied, not a static frontend/ file
      "") continue ;;                   # e.g. href="#foo" already caught above, or "?x" with no path
      /) target="frontend/index.html" ;;
      /*) target="frontend${path}" ;;
      *) target="frontend/$path" ;;
    esac

    if [ ! -e "$target" ]; then
      echo "$f : broken link -> $href (missing $target)"
      fail=1
    fi
  done <<< "$hrefs"
done

if [ "$fail" = 0 ]; then
  echo "OK"
else
  echo "FAILED"
fi
exit $fail
