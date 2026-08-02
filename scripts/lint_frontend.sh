#!/usr/bin/env bash
# Lint frontend/*.js with ESLint (flat config: eslint.config.js at repo root).
# Pure linter only — no bundler, no build step; frontend/ is still served
# as-is by nginx (see CLAUDE.md). Vendored/minified libs (frontend/*.min.js)
# are excluded via eslint.config.js's global ignores.
#   bash scripts/lint_frontend.sh
set -euo pipefail
cd "$(dirname "$0")/.."
npx --yes eslint frontend
