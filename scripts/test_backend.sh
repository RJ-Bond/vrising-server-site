#!/usr/bin/env bash
# Run the backend pytest suite (backend/tests/) via a throwaway uv-managed Python.
# See scripts/check_backend.sh for the faster import-only check.
#   bash scripts/test_backend.sh
set -euo pipefail
cd "$(dirname "$0")/.."
uv run --python 3.12 --with-requirements requirements-dev.txt pytest backend/tests "$@"
