# Hosting on Google Cloud (free tier)

This puts the inventory app on a small Google Cloud server so you can use it from your phone
anywhere, at an address like `https://myshop.duckdns.org`, with a free HTTPS certificate.
It takes about 30 minutes. You can do it all from a computer, or from your phone's browser,
although the Google Cloud console is easier on a bigger screen.

## What it costs

Google's free tier includes one **e2-micro** server with a **30 GB standard disk** in one of
three US regions (Oregon `us-west1`, Iowa `us-central1`, South Carolina `us-east1`). Outside
those, or with a different disk type, you pay.

Two things can still cost a little:

- **The public IP address.** Google charges for IPv4 addresses, and it isn't clear the free tier
  covers this. If it doesn't, expect roughly **$3–4 a month**. Check the cost estimate shown when
  you create the server (step 2).
- **Network traffic** over about 1 GB a month leaving Google. Normal use by a few people stays well under this.

Set a budget alert (step 1) so you're emailed before anything adds up.

## 1. Create a Google Cloud account

1. Go to <https://console.cloud.google.com> and sign in with a Google account.
2. Accept the terms and set up billing. A card is required, even for the free tier.
3. Create a project, e.g. `inventory`.
4. **Budget alert:** *Billing › Budgets & alerts › Create budget*. Set an amount like $5 and keep the email alerts.

## 2. Create the server

*Compute Engine › VM instances › Create instance* (enable the Compute Engine API if asked). Set:

| Setting | Value |
| --- | --- |
| Name | `inventory` |
| Region | `us-west1`, `us-central1` or `us-east1` (any zone) |
| Machine type | **E2 › e2-micro** |
| Boot disk (click *Change*) | **Debian 12**, disk type **Standard persistent disk**, size **30 GB** (the default *Balanced* disk is **not** free) |
| Firewall | Tick **Allow HTTP traffic** and **Allow HTTPS traffic** |
| Networking › network interface › External IPv4 address | **Reserve static external IP address** (so the address never changes) |

Check the monthly estimate on the right, then click **Create**. When it's running, note the
**External IP** shown in the VM list, e.g. `34.82.10.20`.

## 3. Get a free web address (DuckDNS)

1. Go to <https://www.duckdns.org> and sign in (Google, GitHub, etc.).
2. Add a subdomain, e.g. `myshop`, giving you `myshop.duckdns.org`.
3. Put your server's **External IP** in the *current ip* box and click **update ip**.

## 4. Install the app

In the Google Cloud VM list, click **SSH** next to your server. A terminal opens in your browser.
Paste these lines, replacing `myshop.duckdns.org` with your address:

```bash
sudo apt-get update && sudo apt-get install -y git
sudo git clone https://github.com/CannedSpoon/Inventory /opt/inventory
sudo bash /opt/inventory/deploy/setup.sh myshop.duckdns.org
```

The script installs everything, then asks you to **choose a username and password**. That's your
account. When it says *All done*, open `https://myshop.duckdns.org` on your phone and sign in. The first
visit can take a minute while the HTTPS certificate is set up.

**Make it feel like an app:** in your phone's browser menu, choose **Add to Home Screen**.

## 5. Add the people who use it

In the app, tap your name (top right) › **Add person**. Each person gets their own sign-in, and the
History tab shows who received, booked out or moved stock.

## Moving your existing data to the server

If you've been running the app on your own computer:

1. Update that copy to this version and start it. Open it in a browser, and on first visit create an account
   (only possible on that computer). Then tap your name › **Download backup**.
2. In the server's SSH window, use the **⚙ › Upload file** button to upload the zip.
3. Run (with your file's name):
   ```bash
   sudo bash /opt/inventory/deploy/restore.sh ~/inventory-backup-2026-10-18.zip
   ```

## Backups

- The server makes a backup every day and keeps the last 14, in `/var/lib/inventory/backups`.
- Those are on the same disk as the data, so also tap your name › **Download backup** now and then and keep
  the zip somewhere else, such as Google Drive.
- To restore any backup: `sudo bash /opt/inventory/deploy/restore.sh path/to/backup.zip`

## Updating to a new version

In the app, tap **your name**. A dot on it means an update is available. Under **Updates**, tap
**Update now**. The server backs up your data, downloads the new version and restarts. The page reloads
itself after about a minute.

If you set up the server before the **Update now** button existed, turn it on by running this once in
the SSH window. It's also the way to update by hand:

```bash
sudo bash /opt/inventory/deploy/update.sh
```

If an update fails, the app shows the error. The full log is in `/var/lib/inventory/update.log`.

## Forgotten password

Passwords can't be looked up, but you can set a new one in the SSH window:

```bash
sudo -u inventory python3 /opt/inventory/run.py --db /var/lib/inventory/inventory.db --set-password USERNAME
```

## If something doesn't work

| Problem | Check |
| --- | --- |
| Site doesn't load / certificate error | DuckDNS has the server's current External IP; HTTP and HTTPS were allowed when creating the server (*VM › Edit › Firewalls*). Then `sudo journalctl -u caddy -n 50` |
| "Bad gateway" | The app isn't running: `sudo systemctl status inventory` and `sudo journalctl -u inventory -n 50` |
| Charges appearing | Region, machine type and **Standard** disk match step 2; see *Billing › Reports* |
