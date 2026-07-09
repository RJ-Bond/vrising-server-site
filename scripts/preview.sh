#!/usr/bin/env bash
# One-command visual preview of a frontend page via headless Chrome.
#   bash scripts/preview.sh <page.html> [mobile|desktop] [width]
#     bash scripts/preview.sh index.html            # desktop 1280
#     bash scripts/preview.sh servers.html mobile    # mobile 390 (accurate)
#
# Serves frontend/ over HTTP (scripts/serve.ps1) so absolute-path assets
# (/theme.css, /components.css, /index.js …) resolve. Desktop shots hit the
# page directly; MOBILE shots render it inside a 390px <iframe> — headless
# Chrome ignores <meta viewport> in plain desktop mode, so a direct
# --window-size=390 shot renders too wide; the iframe forces a true 390px
# layout viewport. NB: no backend, so data regions show loading/empty states.
set -u
page="${1:-index.html}"; mode="${2:-desktop}"; width="${3:-}"
port=8977
root="$(cd "$(dirname "$0")/.." && pwd)"
winroot="$(cygpath -m "$root" 2>/dev/null || printf '%s' "$root" | sed -E 's#^/([a-zA-Z])/#\1:/#')"
CHROME="/c/Program Files/Google/Chrome/Application/chrome.exe"
[ -x "$CHROME" ] || CHROME="/c/Program Files (x86)/Google/Chrome/Application/chrome.exe"
mkdir -p "$root/.shots"

# Start the static server if it isn't already answering.
up="$(powershell.exe -NoProfile -Command "try{(Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:$port/theme.css' -TimeoutSec 2).StatusCode}catch{0}" 2>/dev/null | tr -d '\r')"
if [ "$up" != "200" ]; then
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$winroot/scripts/serve.ps1" -Port "$port" -Root "$winroot/frontend" >/dev/null 2>&1 &
  sleep 1.6
fi

if [ "$mode" = "mobile" ]; then
  w="${width:-390}"
  frame="$root/frontend/.preview_frame.html"
  printf '<!doctype html><meta charset=utf-8><body style="margin:0;background:#06000f"><iframe src="/%s" style="width:%spx;height:4000px;border:0;display:block"></iframe>' "$page" "$w" > "$frame"
  out="$root/.shots/${page%.html}_mobile_${w}.png"
  "$CHROME" --headless=new --disable-gpu --hide-scrollbars --force-device-scale-factor=1 \
    --window-size="${w},4000" --virtual-time-budget=4000 \
    --screenshot="$out" "http://127.0.0.1:$port/.preview_frame.html" 2>/dev/null
  rm -f "$frame"
else
  w="${width:-1280}"
  out="$root/.shots/${page%.html}_desktop_${w}.png"
  "$CHROME" --headless=new --disable-gpu --hide-scrollbars --force-device-scale-factor=1 \
    --window-size="${w},1400" --virtual-time-budget=4000 \
    --screenshot="$out" "http://127.0.0.1:$port/$page" 2>/dev/null
fi

[ -f "$out" ] && echo "$out" || { echo "FAILED" >&2; exit 1; }
