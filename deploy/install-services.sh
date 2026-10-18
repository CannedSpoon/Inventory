#!/usr/bin/env bash
# Install or refresh the systemd units for the app and its daily backup. Run with sudo.
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> Installing the app and backup services"
for unit in inventory.service inventory-backup.service; do
    sed "s#@APP_DIR@#$APP_DIR#g" "$APP_DIR/deploy/$unit" > "/etc/systemd/system/$unit"
done
cp "$APP_DIR/deploy/inventory-backup.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --quiet inventory.service inventory-backup.timer
systemctl start inventory-backup.timer
