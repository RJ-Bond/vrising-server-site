#!/usr/bin/env bash
set -u
cd "$(dirname "$0")/.." || exit 2
fail=0

# CSS custom properties defined in the shared stylesheets
defined=$( { grep -oE '\-\-[a-z-]+' frontend/theme.css; grep -oE '\-\-[a-z-]+' frontend/components.css; } 2>/dev/null | sort -u )

for f in frontend/*.html; do
  [ -e "$f" ] || continue

  # (a) <style> / </style> balance
  o=$(grep -c '<style>' "$f")
  c=$(grep -c '</style>' "$f")
  if [ "$o" != "$c" ]; then
    echo "$f : <style> imbalance ($o/$c)"
    fail=1
  fi

  # (b) undefined var(--x)
  filedef=$(grep -oE '\-\-[a-z-]+\s*:' "$f" | grep -oE '\-\-[a-z-]+' | sort -u)
  jsdef=$(grep -oE "setProperty\(\s*['\"]\-\-[a-z-]+" "$f" | grep -oE '\-\-[a-z-]+' | sort -u)
  used=$(grep -oE 'var\(\s*--[a-z-]+' "$f" | grep -oE '\-\-[a-z-]+' | sort -u)

  for v in $used; do
    printf '%s\n' "$defined" | grep -qxF -- "$v" && continue
    printf '%s\n' "$filedef" | grep -qxF -- "$v" && continue
    printf '%s\n' "$jsdef"   | grep -qxF -- "$v" && continue
    echo "$f : undefined $v"
    fail=1
  done
done

# (c) undefined vars inside the extracted/shared stylesheets themselves.
#     Only no-fallback usages -- var(--x) -- are flagged; var(--x, default) is safe.
for f in frontend/theme.css frontend/components.css frontend/index.css; do
  [ -e "$f" ] || continue
  filedef=$(grep -oE '\-\-[a-z-]+\s*:' "$f" | grep -oE '\-\-[a-z-]+' | sort -u)
  used=$(grep -oE 'var\(\s*--[a-z-]+\s*\)' "$f" | grep -oE '\-\-[a-z-]+' | sort -u)
  for v in $used; do
    printf '%s\n' "$defined" | grep -qxF -- "$v" && continue
    printf '%s\n' "$filedef" | grep -qxF -- "$v" && continue
    echo "$f : undefined $v"
    fail=1
  done
done

if [ "$fail" = 0 ]; then
  echo "OK"
else
  echo "FAILED"
fi
exit $fail
