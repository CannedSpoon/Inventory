"""HTTP server exposing a JSON API and the web UI. Uses only the standard library."""

import argparse
import csv
import io
import json
import mimetypes
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .store import NotFound, Store, ValidationError

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
CSV_FIELDS = ["part_number", "description", "quantity", "unit_value", "total_value"]
MAX_BODY = 10 * 1024 * 1024

ITEM_RE = re.compile(r"^/api/items/(\d+)$")
ADJUST_RE = re.compile(r"^/api/items/(\d+)/adjust$")


def make_handler(store):
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
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def error(self, status, message):
            self.send(status, {"error": message})

        def body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ValidationError("Request body too large")
            return self.rfile.read(length)

        def json_body(self):
            try:
                data = json.loads(self.body() or b"{}")
            except json.JSONDecodeError:
                raise ValidationError("Invalid JSON")
            if not isinstance(data, dict):
                raise ValidationError("Expected a JSON object")
            return data

        def dispatch(self, fn):
            try:
                fn()
            except ValidationError as e:
                self.error(HTTPStatus.BAD_REQUEST, str(e))
            except NotFound as e:
                self.error(HTTPStatus.NOT_FOUND, str(e))

        # --- routes ------------------------------------------------------
        def do_GET(self):
            self.dispatch(self._get)

        def do_POST(self):
            self.dispatch(self._post)

        def do_PUT(self):
            self.dispatch(self._put)

        def do_DELETE(self):
            self.dispatch(self._delete)

        def _get(self):
            url = urlparse(self.path)
            qs = {k: v[0] for k, v in parse_qs(url.query).items()}
            if url.path == "/api/items":
                items = store.search(qs.get("q", ""), qs.get("sort", "part_number"), qs.get("order", "asc"))
                return self.send(HTTPStatus.OK, {"items": items, "summary": store.summary()})
            if m := ITEM_RE.match(url.path):
                return self.send(HTTPStatus.OK, store.get(int(m[1])))
            if url.path == "/api/export.csv":
                buf = io.StringIO()
                writer = csv.DictWriter(buf, fieldnames=CSV_FIELDS, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(store.search(qs.get("q", "")))
                return self.send(
                    HTTPStatus.OK, buf.getvalue(), "text/csv; charset=utf-8",
                    {"Content-Disposition": 'attachment; filename="inventory.csv"'},
                )
            self.static(url.path)

        def _post(self):
            path = urlparse(self.path).path
            if path == "/api/items":
                return self.send(HTTPStatus.CREATED, store.create(self.json_body()))
            if m := ADJUST_RE.match(path):
                return self.send(HTTPStatus.OK, store.adjust(int(m[1]), self.json_body().get("delta")))
            if path == "/api/import":
                text = self.body().decode("utf-8-sig", errors="replace")
                reader = csv.DictReader(io.StringIO(text))
                if not reader.fieldnames or "part_number" not in [f.strip().lower() for f in reader.fieldnames]:
                    raise ValidationError("CSV must have a header row including 'part_number'")
                rows = [{(k or "").strip().lower(): v for k, v in row.items()} for row in reader]
                return self.send(HTTPStatus.OK, store.import_rows(rows))
            self.error(HTTPStatus.NOT_FOUND, "Not found")

        def _put(self):
            if m := ITEM_RE.match(urlparse(self.path).path):
                return self.send(HTTPStatus.OK, store.update(int(m[1]), self.json_body()))
            self.error(HTTPStatus.NOT_FOUND, "Not found")

        def _delete(self):
            if m := ITEM_RE.match(urlparse(self.path).path):
                store.delete(int(m[1]))
                return self.send(HTTPStatus.NO_CONTENT)
            self.error(HTTPStatus.NOT_FOUND, "Not found")

        def static(self, path):
            if path == "/":
                path = "/index.html"
            file = (STATIC_DIR / path.lstrip("/")).resolve()
            if STATIC_DIR not in file.parents or not file.is_file():
                return self.error(HTTPStatus.NOT_FOUND, "Not found")
            ctype = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
            self.send(HTTPStatus.OK, file.read_bytes(), ctype)

    return Handler


def make_server(host="127.0.0.1", port=8000, db="inventory.db"):
    return ThreadingHTTPServer((host, port), make_handler(Store(db)))


def main():
    parser = argparse.ArgumentParser(description="Run the inventory tracking server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default="inventory.db", help="SQLite database file (default: inventory.db)")
    args = parser.parse_args()
    server = make_server(args.host, args.port, args.db)
    print(f"Inventory running at http://{args.host}:{args.port}  (database: {args.db})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
