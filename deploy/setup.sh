#!/usr/bin/env bash
# Set up the inventory tracker on a fresh Debian or Ubuntu server, such as a
# Google Cloud e2-micro. Caddy serves it over HTTPS with a free certificate.
#
#   sudo bash /opt/inventory/deploy/setup.sh your-name.duckdns.org
#
# Safe to run again: it keeps your data and only asks for an account if none exists.
set -euo pipefail

DOMAIN="${1:-}"
if [[ -z "$DOMAIN" ]]; then
    echo "Usage: sudo bash $0 your-name.duckdns.org"
    exit 1
fi
if [[ $EUID -ne 0 ]]; then
    echo "Please run with sudo."
    exit 1
fi

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DATA_DIR=/var/lib/inventory

echo "==> Installing Python and Caddy"
apt-get update -q
apt-get install -y -q python3 caddy

# The e2-micro has 1 GB of memory; a little swap keeps updates from running out.
if ! swapon --show | grep -q .; then
    echo "==> Adding 1 GB of swap"
    fallocate -l 1G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile >/dev/null
    swapon /swapfile
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "==> Creating the 'inventory' user and data folder ($DATA_DIR)"
id inventory &>/dev/null || useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin inventory
install -d -o inventory -g inventory -m 750 "$DATA_DIR" "$DATA_DIR/uploads" "$DATA_DIR/backups"

bash "$APP_DIR/deploy/install-services.sh"

echo "==> Setting up HTTPS for $DOMAIN"
cat > /etc/caddy/Caddyfile <<CADDY
$DOMAIN {
    encode gzip
    reverse_proxy 127.0.0.1:8000
}
CADDY
systemctl restart caddy

USERS=$(sudo -u inventory python3 -c "
import sys; sys.path.insert(0, '$APP_DIR')
from inventory.store import Store
print(Store('$DATA_DIR/inventory.db', '$DATA_DIR/uploads').user_count())")
if [[ "$USERS" == "0" ]]; then
    echo
    echo "==> Create your account"
    read -rp "Choose a username: " NAME </dev/tty
    sudo -u inventory python3 "$APP_DIR/run.py" --db "$DATA_DIR/inventory.db" --uploads "$DATA_DIR/uploads" --set-password "$NAME"
fi

systemctl restart inventory
echo
echo "All done. Open https://$DOMAIN on your phone and sign in."
echo "(The first visit can take a minute while the HTTPS certificate is issued.)"
