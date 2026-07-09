#!/usr/bin/env bash
# Screenshot a frontend page with headless Chrome (layout/CSS check).
# Dynamic data needs the backend; over file:// only the static shell renders.
# CAVEAT: absolute-path assets (/theme.css, /components.css, /tailwind.min.css,
# /common.js …) do NOT load over file:// — they resolve to the filesystem root.
# So this is accurate for pages whose CSS is INLINE (e.g. index.html); pages that
# rely on the shared external stylesheets render unstyled here (a file:// artifact,
# not a real bug). For those, serve the folder over HTTP instead.
#
# Usage: bash scripts/shot.sh [page.html] [width] [height]
#   bash scripts/shot.sh index.html 390 900   # mobile
#   bash scripts/shot.sh servers.html 1280 900 # desktop
set -u
page="${1:-index.html}"; w="${2:-390}"; h="${3:-900}"
root="$(cd "$(dirname "$0")/.." && pwd)"
CHROME="/c/Program Files/Google/Chrome/Application/chrome.exe"
[ -x "$CHROME" ] || CHROME="/c/Program Files (x86)/Google/Chrome/Application/chrome.exe"
mkdir -p "$root/.shots"
out="$root/.shots/${page%.html}_${w}x${h}.png"
# Chrome needs a Windows-style path (D:/...) in the file:// URL, not MSYS /d/...
winroot="$(cygpath -m "$root" 2>/dev/null || printf '%s' "$root" | sed -E 's#^/([a-zA-Z])/#\1:/#')"
"$CHROME" --headless=new --disable-gpu --hide-scrollbars --force-device-scale-factor=1 \
  --window-size="${w},${h}" --virtual-time-budget=3000 \
  --screenshot="$out" "file:///$winroot/frontend/$page" 2>/dev/null
if [ -f "$out" ]; then echo "$out"; else echo "FAILED: no screenshot produced" >&2; exit 1; fi
