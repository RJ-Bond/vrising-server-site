#!/usr/bin/env bash
# Lint backend/*.py with ruff (rules + ignores documented in pyproject.toml).
# Uses a throwaway uv-managed Python, same pattern as check_backend.sh/test_backend.sh.
#   bash scripts/lint_backend.sh
set -euo pipefail
cd "$(dirname "$0")/.."
uv run --python 3.12 --with ruff ruff check backend/
