#!/usr/bin/env bash
# Run as root by inventory-update.service when someone taps "Update now" in the app.
# The app can only create the request file; this script decides what happens.
# Output goes to /var/lib/inventory/update.log, which the app shows if an update fails.
set -uo pipefail
DATA_DIR=/var/lib/inventory
REQUEST="$DATA_DIR/update-requested"
LOG="$DATA_DIR/update.log"
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"

WHO=$(head -c 200 "$REQUEST" 2>/dev/null | tr -cd '[:print:] ')
rm -f "$REQUEST"  # first, so the watcher doesn't start another update
{
    echo "=== $(date '+%Y-%m-%d %H:%M:%S') update started. $WHO"
    if bash "$APP_DIR/deploy/update.sh"; then
        echo "=== update finished"
    else
        echo "=== UPDATE FAILED (details above)"
    fi
} >> "$LOG" 2>&1
tail -n 300 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
chmod 644 "$LOG"
