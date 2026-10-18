#!/usr/bin/env bash
# Install or refresh the systemd units for the app, its daily backup, and the
# updater behind the app's "Update now" button. Run with sudo.
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> Installing the app, backup and updater services"
for unit in inventory.service inventory-backup.service inventory-update.service; do
    sed "s#@APP_DIR@#$APP_DIR#g" "$APP_DIR/deploy/$unit" > "/etc/systemd/system/$unit"
done
cp "$APP_DIR/deploy/inventory-backup.timer" "$APP_DIR/deploy/inventory-update.path" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --quiet inventory.service inventory-backup.timer inventory-update.path
systemctl start inventory-backup.timer inventory-update.path
