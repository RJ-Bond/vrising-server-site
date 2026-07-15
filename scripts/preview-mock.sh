#!/usr/bin/env bash
# Screenshot a PUBLIC page (clans.html, events.html, leaderboard.html, servers.html, …)
# with realistic mock data instead of the loading/empty/error states plain preview.sh
# shows (no backend in this sandbox). Anonymous visitor — no login state.
#   bash scripts/preview-mock.sh <page.html> [mobile|desktop] [width] [height]
#
# Builds a throwaway copy of the page with scripts/public-mock-fetch.js injected as
# the first <script>, so window.fetch resolves with canned JSON before the page's own
# scripts run. For admin.html specifically, use scripts/preview-admin.sh instead
# (that one also seeds a fake logged-in-as-admin session).
set -u
page="${1:?usage: preview-mock.sh <page.html> [mobile|desktop] [width] [height]}"
mode="${2:-desktop}"; width="${3:-}"; height="${4:-4000}"
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

mock_js="$root/frontend/.public_mock_fetch.js"
mock_html="$root/frontend/.public_mock_${page}"
frame="$root/frontend/.preview_mock_frame.html"
cleanup() { rm -f "$mock_js" "$mock_html" "$frame"; }
trap cleanup EXIT

cp "$root/scripts/public-mock-fetch.js" "$mock_js"
awk '{
  print
  if (!done && $0 ~ /<body[^>]*>/) { print "<script src=\"/.public_mock_fetch.js\"></script>"; done=1 }
}' "$root/frontend/$page" > "$mock_html"

mock_url_path=".public_mock_${page}"
if [ "$mode" = "mobile" ]; then
  w="${width:-390}"
  printf '<!doctype html><meta charset=utf-8><body style="margin:0;background:#06000f"><iframe src="/%s" style="width:%spx;height:%spx;border:0;display:block"></iframe>' "$mock_url_path" "$w" "$height" > "$frame"
  out="$root/.shots/${page%.html}_mock_mobile_${w}.png"
  "$CHROME" --headless=new --disable-gpu --hide-scrollbars --force-device-scale-factor=1 \
    --window-size="${w},${height}" --virtual-time-budget=4000 \
    --screenshot="$out" "http://127.0.0.1:$port/.preview_mock_frame.html" 2>/dev/null
else
  w="${width:-1280}"
  out="$root/.shots/${page%.html}_mock_desktop_${w}.png"
  "$CHROME" --headless=new --disable-gpu --hide-scrollbars --force-device-scale-factor=1 \
    --window-size="${w},${height}" --virtual-time-budget=4000 \
    --screenshot="$out" "http://127.0.0.1:$port/$mock_url_path" 2>/dev/null
fi

[ -f "$out" ] && echo "$out" || { echo "FAILED" >&2; exit 1; }
