#!/usr/bin/env bash
# Update to the latest version of the app and restart it. Your data is not touched.
#
#   sudo bash /opt/inventory/deploy/update.sh
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> Backing up first"
systemctl start inventory-backup.service
echo "==> Downloading the latest version"
git -C "$APP_DIR" pull --ff-only
bash "$APP_DIR/deploy/install-services.sh"
systemctl restart inventory
echo "Updated and restarted."
