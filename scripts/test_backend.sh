#!/usr/bin/env bash
# Run the backend pytest suite (backend/tests/) via a throwaway uv-managed Python.
# See scripts/check_backend.sh for the faster import-only check.
#   bash scripts/test_backend.sh
set -euo pipefail
cd "$(dirname "$0")/.."
# See scripts/check_backend.sh for why these need to point somewhere real/writable —
# same import-time UPLOAD_DIR.mkdir() in backend/helpers.py runs here too, since the
# test suite imports backend.main transitively via conftest.py's fixtures.
export UPLOAD_DIR="$(mktemp -d)/uploads"
export BACKUP_DIR="$(mktemp -d)/backups"
uv run --python 3.12 --with-requirements requirements-dev.txt pytest backend/tests "$@"
