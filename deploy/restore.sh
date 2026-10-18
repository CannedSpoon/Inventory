#!/usr/bin/env bash
# Replace the server's data with a backup zip (from "Download backup" in the app,
# or one of the daily backups in /var/lib/inventory/backups).
#
#   sudo bash /opt/inventory/deploy/restore.sh inventory-backup-2026-10-18.zip
set -euo pipefail
ZIP="${1:-}"
if [[ -z "$ZIP" || ! -f "$ZIP" ]]; then
    echo "Usage: sudo bash $0 path/to/inventory-backup.zip"
    exit 1
fi
DATA_DIR=/var/lib/inventory
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"

python3 -c "
import sys, zipfile
names = zipfile.ZipFile(sys.argv[1]).namelist()
if 'inventory.db' not in names: sys.exit('That zip is not an inventory backup (no inventory.db inside).')
" "$ZIP"

read -rp "This replaces ALL data on this server with the backup. Type yes to continue: " OK </dev/tty
[[ "$OK" == "yes" ]] || { echo "Cancelled."; exit 1; }

echo "==> Saving a backup of the current data first"
systemctl start inventory-backup.service
systemctl stop inventory
rm -rf "$DATA_DIR/inventory.db" "$DATA_DIR/inventory.db-journal" "$DATA_DIR/uploads"
python3 -m zipfile -e "$ZIP" "$DATA_DIR"
install -d "$DATA_DIR/uploads"
chown -R inventory:inventory "$DATA_DIR"

USERS=$(sudo -u inventory python3 -c "
import sys; sys.path.insert(0, '$APP_DIR')
from inventory.store import Store
print(Store('$DATA_DIR/inventory.db', '$DATA_DIR/uploads').user_count())")
if [[ "$USERS" == "0" ]]; then
    echo "The backup has no accounts (it came from an older version). Create one now."
    read -rp "Choose a username: " NAME </dev/tty
    sudo -u inventory python3 "$APP_DIR/run.py" --db "$DATA_DIR/inventory.db" --uploads "$DATA_DIR/uploads" --set-password "$NAME"
fi
systemctl start inventory
echo "Restored. Everyone will need to sign in again."
