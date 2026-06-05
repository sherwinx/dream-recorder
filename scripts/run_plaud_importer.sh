#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="/Users/spaceylamb/Desktop/vibe_coding/dreamrecorder/dream-recorder"
cd "$REPO_DIR"

export PATH="/usr/local/opt/node@20/bin:/Users/spaceylamb/.npm-global/bin:/Users/spaceylamb/opt/miniconda3/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PYTHONPATH="$REPO_DIR"

exec /Users/spaceylamb/opt/miniconda3/bin/python scripts/plaud_importer.py sync \
  --plaud-cli plaud \
  --outbox-dir db/plaud_outbox \
  --pi-import-url "${PLAUD_PI_IMPORT_URL:-http://10.0.0.2:5001/api/import/plaud}" \
  --token "${PLAUD_IMPORT_TOKEN:-}" \
  --lookback-days "${PLAUD_IMPORT_LOOKBACK_DAYS:-30}"
