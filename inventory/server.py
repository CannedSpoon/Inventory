"""HTTP server exposing a JSON API and the web UI. Uses only the standard library."""

import argparse
import csv
import getpass
import io
import json
import mimetypes
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
from datetime import datetime
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from .store import SESSION_DAYS, NotFound, Store, ValidationError

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
CSV_FIELDS = ["part_number", "description", "category", "location", "quantity", "unit_value", "total_value"]
LABEL_FIELDS = {
    "locations": ["code", "name", "notes", "link", "contents"],
    "parts": ["code", "part_number", "description", "category", "link"],
}
JOB_CSV_FIELDS = ["job", "part_number", "description", "quantity", "total_value"]
MAX_BODY = 12 * 1024 * 1024

STOCK_ACTIONS = {
    "receive": "receive",
    "book-out": "book_out",
    "return": "return_from_job",
    "transfer": "transfer",
    "count": "count",
}

ROUTES = [
    # (method, pattern, handler name)
    ("GET", r"/api/session", "get_session"),
    ("POST", r"/api/login", "login"),
    ("POST", r"/api/logout", "logout"),
    ("POST", r"/api/setup", "first_account"),
    ("GET", r"/api/users", "list_users"),
    ("POST", r"/api/users", "create_user"),
    ("DELETE", r"/api/users/(\d+)", "delete_user"),
    ("POST", r"/api/password", "change_password"),
    ("GET", r"/api/backup\.zip", "backup"),
    ("GET", r"/api/items", "list_items"),
    ("POST", r"/api/items", "create_item"),
    ("GET", r"/api/items/(\d+)", "get_item"),
    ("PUT", r"/api/items/(\d+)", "update_item"),
    ("DELETE", r"/api/items/(\d+)", "delete_item"),
    ("GET", r"/api/items/(\d+)/history", "item_history"),
    ("POST", r"/api/items/(\d+)/image", "upload_image"),
    ("DELETE", r"/api/items/(\d+)/image", "delete_image"),
    ("POST", r"/api/items/(\d+)/(receive|book-out|return|transfer|count)", "stock_action"),
    ("GET", r"/api/locations", "list_locations"),
    ("POST", r"/api/locations", "create_location"),
    ("GET", r"/api/locations/(\d+)", "get_location"),
    ("PUT", r"/api/locations/(\d+)", "update_location"),
    ("DELETE", r"/api/locations/(\d+)", "delete_location"),
    ("GET", r"/api/jobs", "list_jobs"),
    ("POST", r"/api/jobs", "create_job"),
    ("GET", r"/api/jobs/(\d+)", "get_job"),
    ("PUT", r"/api/jobs/(\d+)", "update_job"),
    ("DELETE", r"/api/jobs/(\d+)", "delete_job"),
    ("GET", r"/api/jobs/(\d+)/export\.csv", "export_job"),
    ("GET", r"/api/history", "history"),
    ("GET", r"/api/summary", "summary"),
    ("GET", r"/api/categories", "list_categories"),
    ("GET", r"/api/scan", "scan"),
    ("GET", r"/api/info", "info"),
    ("GET", r"/api/labels\.csv", "export_labels"),
    ("GET", r"/api/export\.csv", "export_items"),
    ("POST", r"/api/import", "import_items"),
]
ROUTES = [(m, re.compile(f"^{p}$"), h) for m, p, h in ROUTES]
PUBLIC_ROUTES = {"get_session", "login", "logout", "first_account"}  # everything else needs a signed-in user
COOKIE = "inventory_session"


class LoginLimiter:
    """Refuse logins from an address after too many recent failures."""

    def __init__(self, attempts=10, window=15 * 60):
        self.attempts, self.window = attempts, window
        self.failures = {}
        self.lock = threading.Lock()

    def _recent(self, key):
        cutoff = time.monotonic() - self.window
        times = [t for t in self.failures.get(key, []) if t > cutoff]
        if times:
            self.failures[key] = times
        else:
            self.failures.pop(key, None)
        return times

    def blocked_for(self, key):
        """Seconds until this address may try again (0 if it may now)."""
        with self.lock:
            times = self._recent(key)
            if len(times) < self.attempts:
                return 0
            return int(times[0] + self.window - time.monotonic()) + 1

    def failed(self, key):
        with self.lock:
            self.failures.setdefault(key, []).append(time.monotonic())

    def succeeded(self, key):
        with self.lock:
            self.failures.pop(key, None)


def lan_address():
    """This machine's address on the local network (no packets are sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return None


def to_csv(fields, rows):
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def make_handler(store, behind_proxy=False):
    limiter = LoginLimiter()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        # --- helpers -----------------------------------------------------
        def send(self, status, body=b"", content_type="application/json", headers=None):
            if isinstance(body, (dict, list)):
                body = json.dumps(body).encode()
            elif isinstance(body, str):
                body = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "same-origin")
            if content_type == "application/json":
                self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def send_csv(self, text, filename):
            self.send(HTTPStatus.OK, text, "text/csv; charset=utf-8",
                      {"Content-Disposition": f'attachment; filename="{filename}"'})

        def error(self, status, message):
            self.send(status, {"error": message})

        def body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ValidationError("Upload is too large")
            return self.rfile.read(length)

        def json_body(self):
            try:
                data = json.loads(self.body() or b"{}")
            except json.JSONDecodeError:
                raise ValidationError("Invalid JSON")
            if not isinstance(data, dict):
                raise ValidationError("Expected a JSON object")
            return data

        # --- sign-in helpers ---------------------------------------------
        def client_ip(self):
            forwarded = self.headers.get("X-Forwarded-For")
            if behind_proxy and forwarded:
                return forwarded.split(",")[-1].strip()  # the address our proxy saw
            return self.client_address[0]

        def is_https(self):
            return behind_proxy and self.headers.get("X-Forwarded-Proto", "").lower() == "https"

        def session_token(self):
            try:
                morsel = SimpleCookie(self.headers.get("Cookie", "")).get(COOKIE)
            except CookieError:
                return None
            return morsel.value if morsel else None

        def session_cookie(self, token, max_age):
            secure = "; Secure" if self.is_https() else ""
            return {"Set-Cookie": f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}{secure}"}

        def can_setup(self):
            """The first account can be created in the browser only on the computer running the app."""
            local = not behind_proxy and self.client_address[0] in ("127.0.0.1", "::1")
            return local and store.user_count() == 0

        def handle_request(self, method):
            url = urlparse(self.path)
            self.query = {k: v[0] for k, v in parse_qs(url.query).items()}
            if method == "GET" and url.path.startswith("/s/"):
                # Short link printed in label QR codes; the page looks the code up.
                code = unquote(url.path[len("/s/"):])
                return self.send(HTTPStatus.FOUND, "", "text/plain", {"Location": "/#scan/" + quote(code, safe="")})
            self.user = store.session_user(self.session_token())
            store.local.user = self.user["username"] if self.user else None
            # Browsers won't send this header cross-site without permission, which blocks forged requests.
            if method != "GET" and self.headers.get("X-Requested-With") != "inventory":
                return self.error(HTTPStatus.FORBIDDEN, "Missing X-Requested-With header")
            try:
                for route_method, pattern, name in ROUTES:
                    if route_method == method and (m := pattern.match(url.path)):
                        if name not in PUBLIC_ROUTES and not self.user:
                            return self.error(HTTPStatus.UNAUTHORIZED, "Please sign in")
                        args = [int(a) if a.isdigit() else a for a in m.groups()]
                        return getattr(self, name)(*args)
                if method == "GET" and not url.path.startswith("/api/"):
                    if url.path.startswith("/uploads/") and not self.user:
                        return self.error(HTTPStatus.UNAUTHORIZED, "Please sign in")
                    return self.serve_file(unquote(url.path))
                self.error(HTTPStatus.NOT_FOUND, "Not found")
            except ValidationError as e:
                self.error(HTTPStatus.BAD_REQUEST, str(e))
            except ValueError:  # e.g. a non-numeric query parameter
                self.error(HTTPStatus.BAD_REQUEST, "Invalid request")
            except NotFound as e:
                self.error(HTTPStatus.NOT_FOUND, str(e))

        def do_GET(self):
            self.handle_request("GET")

        def do_POST(self):
            self.handle_request("POST")

        def do_PUT(self):
            self.handle_request("PUT")

        def do_DELETE(self):
            self.handle_request("DELETE")

        # --- accounts ----------------------------------------------------
        def get_session(self):
            needs_setup = store.user_count() == 0
            self.send(HTTPStatus.OK, {"user": self.user, "needs_setup": needs_setup,
                                      "can_setup": needs_setup and self.can_setup()})

        def login(self):
            ip = self.client_ip()
            wait = limiter.blocked_for(ip)
            if wait:
                return self.error(HTTPStatus.TOO_MANY_REQUESTS,
                                  f"Too many failed sign-ins. Try again in {wait // 60 + 1} minutes.")
            data = self.json_body()
            user = store.authenticate(data.get("username"), data.get("password"))
            if not user:
                limiter.failed(ip)
                return self.error(HTTPStatus.UNAUTHORIZED, "Wrong username or password")
            limiter.succeeded(ip)
            token = store.create_session(user["id"])
            self.send(HTTPStatus.OK, {"user": user}, headers=self.session_cookie(token, SESSION_DAYS * 86400))

        def logout(self):
            store.delete_session(self.session_token())
            self.send(HTTPStatus.OK, {"user": None}, headers=self.session_cookie("", 0))

        def first_account(self):
            if not self.can_setup():
                return self.error(HTTPStatus.FORBIDDEN, "Accounts can only be created by a signed-in user")
            data = self.json_body()
            store.set_password(data.get("username"), data.get("password"), create="only")
            user = store.authenticate(data.get("username"), data.get("password"))
            token = store.create_session(user["id"])
            self.send(HTTPStatus.CREATED, {"user": user}, headers=self.session_cookie(token, SESSION_DAYS * 86400))

        def list_users(self):
            self.send(HTTPStatus.OK, store.users())

        def create_user(self):
            self.send(HTTPStatus.CREATED, store.create_user(self.json_body()))

        def delete_user(self, user_id):
            store.delete_user(user_id, self.user["id"])
            self.send(HTTPStatus.NO_CONTENT)

        def change_password(self):
            data = self.json_body()
            store.change_password(self.user["id"], data.get("current"), data.get("new"), keep_token=self.session_token())
            self.send(HTTPStatus.OK, {"ok": True})

        def backup(self):
            with tempfile.TemporaryFile() as f:
                store.write_backup(f)
                size = f.tell()
                f.seek(0)
                name = f"inventory-backup-{datetime.now():%Y-%m-%d}.zip"
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Length", str(size))
                self.send_header("Content-Disposition", f'attachment; filename="{name}"')
                self.end_headers()
                shutil.copyfileobj(f, self.wfile)

        # --- parts -------------------------------------------------------
        def list_items(self):
            q = self.query
            items = store.search(q.get("q", ""), q.get("sort", "part_number"), q.get("order", "asc"),
                                 q.get("location_id") or None, self.category())
            self.send(HTTPStatus.OK, {"items": items, "summary": store.summary()})

        def category(self):
            """The category filter: absent means all, '__none__' means uncategorised."""
            cat = self.query.get("category")
            return "" if cat == "__none__" else cat

        def create_item(self):
            self.send(HTTPStatus.CREATED, store.create(self.json_body()))

        def get_item(self, item_id):
            self.send(HTTPStatus.OK, store.get(item_id))

        def update_item(self, item_id):
            self.send(HTTPStatus.OK, store.update(item_id, self.json_body()))

        def delete_item(self, item_id):
            store.delete(item_id)
            self.send(HTTPStatus.NO_CONTENT)

        def item_history(self, item_id):
            store.get(item_id)
            self.send(HTTPStatus.OK, store.history(item_id=item_id))

        def upload_image(self, item_id):
            self.send(HTTPStatus.OK, store.set_image(item_id, self.body()))

        def delete_image(self, item_id):
            self.send(HTTPStatus.OK, store.remove_image(item_id))

        def stock_action(self, item_id, action):
            self.send(HTTPStatus.OK, getattr(store, STOCK_ACTIONS[action])(item_id, self.json_body()))

        def export_items(self):
            rows = []
            for item in store.search(self.query.get("q", ""), location_id=self.query.get("location_id") or None,
                                     category=self.category()):
                for loc in item["locations"] or [{"name": "", "quantity": 0}]:
                    rows.append({**item, "location": loc["name"], "quantity": loc["quantity"],
                                 "total_value": round(loc["quantity"] * item["unit_value"], 2)})
            self.send_csv(to_csv(CSV_FIELDS, rows), "inventory.csv")

        def import_items(self):
            text = self.body().decode("utf-8-sig", errors="replace")
            reader = csv.DictReader(io.StringIO(text))
            if not reader.fieldnames or "part_number" not in [f.strip().lower() for f in reader.fieldnames]:
                raise ValidationError("CSV must have a header row including 'part_number'")
            rows = [{(k or "").strip().lower(): v for k, v in row.items()} for row in reader]
            self.send(HTTPStatus.OK, store.import_rows(rows))

        # --- locations ---------------------------------------------------
        def list_locations(self):
            self.send(HTTPStatus.OK, store.locations())

        def create_location(self):
            self.send(HTTPStatus.CREATED, store.create_location(self.json_body()))

        def get_location(self, location_id):
            self.send(HTTPStatus.OK, store.location_contents(location_id))

        def update_location(self, location_id):
            self.send(HTTPStatus.OK, store.update_location(location_id, self.json_body()))

        def delete_location(self, location_id):
            store.delete_location(location_id)
            self.send(HTTPStatus.NO_CONTENT)

        # --- jobs --------------------------------------------------------
        def list_jobs(self):
            self.send(HTTPStatus.OK, store.jobs(self.query.get("q", ""), self.query.get("status", "")))

        def create_job(self):
            self.send(HTTPStatus.CREATED, store.create_job(self.json_body()))

        def get_job(self, job_id):
            self.send(HTTPStatus.OK, store.get_job(job_id))

        def update_job(self, job_id):
            self.send(HTTPStatus.OK, store.update_job(job_id, self.json_body()))

        def delete_job(self, job_id):
            store.delete_job(job_id)
            self.send(HTTPStatus.NO_CONTENT)

        def export_job(self, job_id):
            job = store.get_job(job_id)
            rows = [{**p, "job": job["number"]} for p in job["parts"]]
            safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", job["number"])
            self.send_csv(to_csv(JOB_CSV_FIELDS, rows), f"job-{safe}.csv")

        def list_categories(self):
            self.send(HTTPStatus.OK, store.categories())

        def scan(self):
            self.send(HTTPStatus.OK, store.scan(self.query.get("code", "")))

        def info(self):
            host, port = self.server.server_address[:2]
            if host in ("0.0.0.0", "", "::"):
                host = lan_address()
            elif host.startswith("127.") or host in ("localhost", "::1"):
                host = None  # only reachable from this computer
            self.send(HTTPStatus.OK, {"lan_url": f"http://{host}:{port}" if host else None})

        def export_labels(self):
            """Label data for a P-touch Editor database merge."""
            kind = self.query.get("type", "locations")
            if kind not in LABEL_FIELDS:
                raise ValidationError("type must be 'locations' or 'parts'")
            base = self.query.get("base", "").rstrip("/")
            ids = {int(i) for i in self.query.get("ids", "").split(",") if i.strip()}
            rows = []
            if kind == "locations":
                for loc in store.locations():
                    if ids and loc["id"] not in ids:
                        continue
                    items = store.location_contents(loc["id"])["items"]
                    contents = "; ".join(f"{i['part_number']} x{i['here']}" for i in items[:8])
                    rows.append({**loc, "link": f"{base}/s/{quote(loc['code'], safe='')}",
                                 "contents": contents + ("; ..." if len(items) > 8 else "")})
            else:
                for item in store.search():
                    if ids and item["id"] not in ids:
                        continue
                    rows.append({**item, "code": item["part_number"],
                                 "link": f"{base}/s/{quote(item['part_number'], safe='')}"})
            # BOM so P-touch Editor and Excel read it as UTF-8
            self.send_csv("\ufeff" + to_csv(LABEL_FIELDS[kind], rows), f"labels-{kind}.csv")

        def summary(self):
            self.send(HTTPStatus.OK, store.summary())

        def history(self):
            self.send(HTTPStatus.OK, store.history(query=self.query.get("q", ""),
                                                   limit=min(int(self.query.get("limit") or 500), 5000)))

        # --- files -------------------------------------------------------
        def serve_file(self, path):
            if path.startswith("/uploads/"):
                root, rel = store.uploads.resolve(), path[len("/uploads/"):]
            else:
                root, rel = STATIC_DIR, ("index.html" if path == "/" else path.lstrip("/"))
            file = (root / rel).resolve()
            if root not in file.parents or not file.is_file():
                return self.error(HTTPStatus.NOT_FOUND, "Not found")
            ctype = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
            self.send(HTTPStatus.OK, file.read_bytes(), ctype, {"X-Content-Type-Options": "nosniff"})

    return Handler


def make_server(host="127.0.0.1", port=8000, db="inventory.db", uploads=None, behind_proxy=False):
    store = Store(db, uploads)
    server = ThreadingHTTPServer((host, port), make_handler(store, behind_proxy))
    server.store = store
    return server


def set_password_cli(store, username):
    password = getpass.getpass(f"New password for {username}: ")
    if getpass.getpass("Repeat it: ") != password:
        sys.exit("Passwords don't match.")
    try:
        store.set_password(username, password)
    except ValidationError as e:
        sys.exit(str(e))
    print(f"Password set for {username}.")


def backup_cli(store, folder, keep):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"inventory-backup-{datetime.now():%Y-%m-%d-%H%M%S}.zip"
    with open(path, "wb") as f:
        store.write_backup(f)
    for old in sorted(folder.glob("inventory-backup-*.zip"))[:-keep]:
        old.unlink()
    print(f"Backup written to {path}")


def main():
    parser = argparse.ArgumentParser(description="Run the inventory tracking server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default="inventory.db", help="SQLite database file (default: inventory.db)")
    parser.add_argument("--uploads", help="Folder for part pictures (default: 'uploads' next to the database)")
    parser.add_argument("--behind-proxy", action="store_true",
                        help="Running behind a reverse proxy such as Caddy: trust its X-Forwarded-* headers")
    parser.add_argument("--set-password", metavar="USERNAME",
                        help="Create a user, or reset their password, then exit")
    parser.add_argument("--backup", metavar="FOLDER", help="Write a backup zip to FOLDER, then exit")
    parser.add_argument("--keep", type=int, default=14, help="Backups to keep with --backup (default: 14)")
    args = parser.parse_args()
    if args.set_password or args.backup:
        store = Store(args.db, args.uploads)
        if args.set_password:
            set_password_cli(store, args.set_password)
        if args.backup:
            backup_cli(store, args.backup, max(args.keep, 1))
        return
    server = make_server(args.host, args.port, args.db, args.uploads, args.behind_proxy)
    print(f"Inventory running at http://{args.host}:{args.port}  (database: {args.db})")
    if not server.store.user_count():
        print(f"No accounts yet: open http://127.0.0.1:{args.port} on this computer to create one,")
        print(f"or run: python3 run.py --db {args.db} --set-password YOUR_NAME")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
