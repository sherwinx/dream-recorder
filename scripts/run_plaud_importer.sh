#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="/Users/spaceylamb/Desktop/vibe_coding/dreamrecorder/dream-recorder"
cd "$REPO_DIR"

# /tmp logs are wiped on reboot, which is how a three-week outage went unnoticed.
LOG_FILE="$REPO_DIR/logs/plaud-import.log"
mkdir -p "$(dirname "$LOG_FILE")"
log() { printf '[%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" | tee -a "$LOG_FILE"; }

# launchd never sources .zshrc, so nvm's node is not on PATH. Resolve it rather
# than hardcoding an install path that a `brew uninstall` can silently remove.
if ! NODE_BIN="$("$REPO_DIR/scripts/resolve_node_bin.sh" 2>>"$LOG_FILE")"; then
  log "FATAL: no usable node found; the Plaud CLI cannot run"
  exit 1
fi
log "starting sync with node from $NODE_BIN"

export PATH="$NODE_BIN:/Users/spaceylamb/.npm-global/bin:/Users/spaceylamb/opt/miniconda3/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PYTHONPATH="$REPO_DIR"
# Dream transcripts are sensitive. Plaud CLI 0.3.x otherwise sends command,
# account/device, and recording identifiers to its US-hosted telemetry service.
export PLAUD_TELEMETRY_DISABLED=1

# PAUSED: Plaud content is pulled into the outbox but sent nowhere -- no Day One
# entry, no upload to the Dream Recorder Pi. Set this to 1 (or delete the line)
# to resume; the outbox backlog is delivered on the next run.
export PLAUD_SYNC_ENABLED=0

# The launchd plist invokes this via `/bin/zsh <script>`, so the shebang above is
# ignored and the body must stay bash/zsh portable -- no PIPESTATUS. Appending
# straight to the log keeps $? pointing at python (no pipeline) while still
# streaming, so `tail -f` shows progress during a multi-minute pull.
set +e
/Users/spaceylamb/opt/miniconda3/bin/python -u scripts/plaud_importer.py sync \
  --plaud-cli plaud \
  --outbox-dir db/plaud_outbox \
  --pi-import-url "${PLAUD_PI_IMPORT_URL:-http://10.0.0.2:5001/api/import/plaud}" \
  --token "${PLAUD_IMPORT_TOKEN:-}" \
  --lookback-days "${PLAUD_IMPORT_LOOKBACK_DAYS:-30}" \
  --pi-optional >>"$LOG_FILE" 2>&1
run_status=$?  # not `status`: that name is a read-only special parameter in zsh
set -e

if [ "$run_status" -eq 0 ]; then
  log "sync finished ok"
else
  log "sync FAILED (exit $run_status)"
fi
exit "$run_status"
