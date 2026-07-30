#!/usr/bin/env bash
# Fast backend sanity check: import every backend module for real, via a throwaway
# uv-managed Python + the exact pinned requirements.txt.
#
# This sandbox has no system Python (only a non-functional Windows Store stub), which
# meant backend.py changes this session were pushed after only reading the diff — no
# syntax/import check at all. `uv` (already on PATH) can download and run a real
# CPython on demand, so there's no excuse for that anymore. Run this before pushing
# any backend/*.py change.
#   bash scripts/check_backend.sh
set -euo pipefail
cd "$(dirname "$0")/.."
# backend/helpers.py's UPLOAD_DIR/BACKUP_DIR default to /data/... (the production
# Docker volume) and UPLOAD_DIR.mkdir()s at import time — on a bare CI runner or a
# fresh dev machine /data doesn't exist and can't be created without root, so this
# import-only check would otherwise crash before it even gets to check anything. On
# Windows this "worked" only by accident (git-bash resolves the leading "/" against the
# current drive, e.g. D:\data, which happened to be writable) — pointed at a real temp
# dir here so the check means the same thing on every platform, CI included.
export UPLOAD_DIR="$(mktemp -d)/uploads"
export BACKUP_DIR="$(mktemp -d)/backups"
uv run --python 3.12 --with-requirements requirements.txt python -c "
import backend.main, backend.models, backend.schemas, backend.auth, backend.database, backend.monitor
print('backend modules import cleanly')
"
