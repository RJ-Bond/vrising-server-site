#!/usr/bin/env bash
# Screenshot frontend/admin.html fully "logged in" with mock API data — no backend needed.
#   bash scripts/preview-admin.sh [mobile|desktop] [width] [height] [role]
#     role: moderator|admin|superadmin (default admin) — see scripts/admin-mock-fetch.js
#
# admin.html is auth-gated (redirects to /login.html without a real session) and most of
# its sections fetch live data, so scripts/preview.sh alone only ever shows the login
# screen. This builds a throwaway copy of admin.html with scripts/admin-mock-fetch.js
# injected as the very first <script> — it seeds a fake admin session in localStorage and
# monkey-patches window.fetch with canned JSON for the endpoints admin.html's dashboard
# hits on load — then screenshots that copy the same way preview.sh does (mobile renders
# inside a 390px iframe; headless Chrome ignores <meta viewport> otherwise).
set -u
mode="${1:-mobile}"; width="${2:-}"; height="${3:-4000}"; role="${4:-admin}"
port=8977
root="$(cd "$(dirname "$0")/.." && pwd)"
winroot="$(cygpath -m "$root" 2>/dev/null || printf '%s' "$root" | sed -E 's#^/([a-zA-Z])/#\1:/#')"
CHROME="/c/Program Files/Google/Chrome/Application/chrome.exe"
[ -x "$CHROME" ] || CHROME="/c/Program Files (x86)/Google/Chrome/Application/chrome.exe"
mkdir -p "$root/.shots"

up="$(powershell.exe -NoProfile -Command "try{(Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:$port/theme.css' -TimeoutSec 2).StatusCode}catch{0}" 2>/dev/null | tr -d '\r')"
if [ "$up" != "200" ]; then
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$winroot/scripts/serve.ps1" -Port "$port" -Root "$winroot/frontend" >/dev/null 2>&1 &
  sleep 1.6
fi

# Throwaway files served alongside the real frontend/ — cleaned up in the trap below
# regardless of how this script exits.
mock_js="$root/frontend/.admin_mock_fetch.js"
mock_html="$root/frontend/.admin_mock.html"
frame="$root/frontend/.preview_admin_frame.html"
cleanup() { rm -f "$mock_js" "$mock_html" "$frame"; }
trap cleanup EXIT

cp "$root/scripts/admin-mock-fetch.js" "$mock_js"
# Inject the shim as the first <script> right after <body ...> so it patches
# window.fetch / localStorage before purify.min.js / common.js / admin.html's own
# inline script (all later <script> tags) get a chance to run.
awk '{
  print
  if (!done && $0 ~ /<body[^>]*>/) { print "<script src=\"/.admin_mock_fetch.js\"></script>"; done=1 }
}' "$root/frontend/admin.html" > "$mock_html"

if [ "$mode" = "mobile" ]; then
  w="${width:-390}"
  printf '<!doctype html><meta charset=utf-8><body style="margin:0;background:#06000f"><iframe src="/.admin_mock.html?mockRole=%s" style="width:%spx;height:%spx;border:0;display:block"></iframe>' "$role" "$w" "$height" > "$frame"
  out="$root/.shots/admin_mock_mobile_${w}_${role}.png"
  "$CHROME" --headless=new --disable-gpu --hide-scrollbars --force-device-scale-factor=1 \
    --window-size="${w},${height}" --virtual-time-budget=4000 \
    --screenshot="$out" "http://127.0.0.1:$port/.preview_admin_frame.html" 2>/dev/null
else
  w="${width:-1280}"
  out="$root/.shots/admin_mock_desktop_${w}_${role}.png"
  "$CHROME" --headless=new --disable-gpu --hide-scrollbars --force-device-scale-factor=1 \
    --window-size="${w},${height}" --virtual-time-budget=4000 \
    --screenshot="$out" "http://127.0.0.1:$port/.admin_mock.html?mockRole=${role}" 2>/dev/null
fi

[ -f "$out" ] && echo "$out" || { echo "FAILED" >&2; exit 1; }
