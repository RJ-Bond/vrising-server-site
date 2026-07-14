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
uv run --python 3.12 --with-requirements requirements.txt python -c "
import backend.main, backend.models, backend.schemas, backend.auth, backend.database, backend.monitor
print('backend modules import cleanly')
"
