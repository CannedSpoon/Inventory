"""SQLite-backed storage for parts, stock locations, jobs and stock movements.

Stock is held per (part, location). Every change to stock is recorded in the
movements table, and parts booked out to a job are movements linked to that job,
so a job's parts list and value are derived from its movements.
"""

import re
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import unquote

SCHEMA_VERSION = 3
DEFAULT_LOCATION = "Main"
MAX_IMAGE_BYTES = 10 * 1024 * 1024

SORTABLE = {
    "part_number": "part_number COLLATE NOCASE",
    "description": "description COLLATE NOCASE",
    "category": "category COLLATE NOCASE",
    "quantity": "quantity",
    "unit_value": "unit_value",
    "total_value": "quantity * unit_value",
    "updated_at": "updated_at",
}

TABLES = """
CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    part_number TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    description TEXT    NOT NULL DEFAULT '',
    unit_value  REAL    NOT NULL DEFAULT 0 CHECK (unit_value >= 0),
    image       TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS locations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE COLLATE NOCASE,
    notes      TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS stock (
    item_id     INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    location_id INTEGER NOT NULL REFERENCES locations(id),
    quantity    INTEGER NOT NULL CHECK (quantity >= 0),
    PRIMARY KEY (item_id, location_id)
);
CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    number      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    description TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
-- quantity is the signed change to stock at the location. part_number,
-- description and location_name are snapshots so history survives deletes.
CREATE TABLE IF NOT EXISTS movements (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    kind          TEXT    NOT NULL,
    item_id       INTEGER REFERENCES items(id) ON DELETE SET NULL,
    part_number   TEXT    NOT NULL,
    description   TEXT    NOT NULL DEFAULT '',
    location_id   INTEGER REFERENCES locations(id) ON DELETE SET NULL,
    location_name TEXT    NOT NULL DEFAULT '',
    job_id        INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
    quantity      INTEGER NOT NULL,
    unit_value    REAL    NOT NULL DEFAULT 0,
    note          TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS movements_item ON movements(item_id);
CREATE INDEX IF NOT EXISTS movements_job ON movements(job_id);
"""

# Version 1 kept a single quantity on each item. Move it into a "Main" location.
MIGRATE_V1 = f"""
CREATE TABLE items_v2 (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    part_number TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    description TEXT    NOT NULL DEFAULT '',
    unit_value  REAL    NOT NULL DEFAULT 0 CHECK (unit_value >= 0),
    image       TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
INSERT INTO items_v2 (id, part_number, description, unit_value, created_at, updated_at)
    SELECT id, part_number, description, unit_value, created_at, updated_at FROM items;
CREATE TEMP TABLE legacy_qty AS
    SELECT id, part_number, description, quantity, unit_value FROM items WHERE quantity > 0;
DROP TABLE items;
ALTER TABLE items_v2 RENAME TO items;
{TABLES}
INSERT INTO locations (name) SELECT '{DEFAULT_LOCATION}' WHERE EXISTS (SELECT 1 FROM legacy_qty);
INSERT INTO stock (item_id, location_id, quantity)
    SELECT q.id, l.id, q.quantity FROM legacy_qty q JOIN locations l ON l.name = '{DEFAULT_LOCATION}';
INSERT INTO movements (kind, item_id, part_number, description, location_id, location_name, quantity, unit_value, note)
    SELECT 'count', q.id, q.part_number, q.description, l.id, l.name, q.quantity, q.unit_value,
           'Carried over from previous version'
    FROM legacy_qty q JOIN locations l ON l.name = '{DEFAULT_LOCATION}';
DROP TABLE legacy_qty;
"""

# Version 3 adds part categories and a scannable code for each location.
MIGRATE_V3 = """
ALTER TABLE items ADD COLUMN category TEXT NOT NULL DEFAULT '';
CREATE INDEX items_category ON items(category COLLATE NOCASE);
ALTER TABLE locations ADD COLUMN code TEXT;
UPDATE locations SET code = printf('LOC-%04d', id);
CREATE UNIQUE INDEX locations_code ON locations(code COLLATE NOCASE);
"""

CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,39}$")

IMAGE_TYPES = [
    (b"\xff\xd8\xff", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
]


class ValidationError(ValueError):
    pass


class NotFound(LookupError):
    pass


# --- field validation ------------------------------------------------------

def _text(data, key, label, required=False, max_len=200):
    value = str(data.get(key) or "").strip()
    if required and not value:
        raise ValidationError(f"{label} is required")
    if len(value) > max_len:
        raise ValidationError(f"{label} must be {max_len} characters or fewer")
    return value


def _int(value, label="Quantity", minimum=0):
    try:
        number = int(str(value if value is not None else "").strip() or 0)
    except ValueError:
        raise ValidationError(f"{label} must be a whole number")
    if number < minimum:
        raise ValidationError(f"{label} must be at least {minimum}" if minimum else f"{label} cannot be negative")
    return number


def _money(value):
    raw = str(value if value is not None else "").replace("$", "").replace(",", "").strip() or "0"
    try:
        number = round(float(raw), 2)
    except ValueError:
        raise ValidationError("Value must be a number")
    if not 0 <= number < float("inf"):
        raise ValidationError("Value must be a non-negative number")
    return number


def clean_item(data, partial=False):
    """Validate part fields. With partial=True, only the fields present are checked."""
    out = {}
    if "part_number" in data or not partial:
        out["part_number"] = _text(data, "part_number", "Part number", required=True, max_len=100)
    if "description" in data or not partial:
        out["description"] = _text(data, "description", "Description", max_len=2000)
    if "category" in data or not partial:
        out["category"] = _text(data, "category", "Category", max_len=100)
    if "unit_value" in data or not partial:
        out["unit_value"] = _money(data.get("unit_value"))
    return out


def clean_code(value):
    code = str(value or "").strip()
    if not CODE_RE.match(code):
        raise ValidationError(
            "Code must be 1-40 letters, numbers, '-', '_', '.' or '/', starting with a letter or number"
        )
    return code


def scanned_code(text):
    """The code from scanned text, which may be a full label link like http://host/s/LOC-0001."""
    text = str(text or "").strip()
    for marker in ("/s/", "#scan/"):
        if marker in text:
            text = unquote(text.split(marker, 1)[1]).strip().rstrip("/")
    return text


def _like(term):
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _image_ext(data):
    for magic, ext in IMAGE_TYPES:
        if data.startswith(magic):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


class Store:
    def __init__(self, path="inventory.db", uploads_dir=None):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        base = Path.cwd() if path == ":memory:" else Path(path).resolve().parent
        self.uploads = Path(uploads_dir) if uploads_dir else base / "uploads"
        self._migrate()
        self.conn.execute("PRAGMA foreign_keys = ON")

    def _migrate(self):
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version < 2:
            columns = [r["name"] for r in self.conn.execute("PRAGMA table_info(items)")]
            script = MIGRATE_V1 if "quantity" in columns else TABLES
            self.conn.executescript(f"BEGIN; {script} PRAGMA user_version = 2; COMMIT;")
        if version < 3:
            self.conn.executescript(f"BEGIN; {MIGRATE_V3} PRAGMA user_version = 3; COMMIT;")

    @contextmanager
    def tx(self):
        """Serialise access and run the block in one transaction."""
        with self.lock:
            try:
                yield self.conn
            except BaseException:
                self.conn.rollback()
                raise
            self.conn.commit()

    def _one(self, sql, params=()):
        return self.conn.execute(sql, params).fetchone()

    # --- parts -------------------------------------------------------------

    def _item_row(self, item_id):
        row = self._one("SELECT * FROM items WHERE id = ?", (item_id,))
        if row is None:
            raise NotFound(f"Part {item_id} not found")
        return row

    def _with_stock(self, rows):
        """Turn item rows into dicts with per-location stock, quantity and total value."""
        items = [dict(r) for r in rows]
        if not items:
            return items
        ids = [i["id"] for i in items]
        stock = {}
        for s in self.conn.execute(
            f"""SELECT s.item_id, l.id AS location_id, l.name, s.quantity FROM stock s
                JOIN locations l ON l.id = s.location_id
                WHERE s.item_id IN ({",".join("?" * len(ids))}) AND s.quantity > 0
                ORDER BY l.name COLLATE NOCASE""",
            ids,
        ):
            stock.setdefault(s["item_id"], []).append(
                {"location_id": s["location_id"], "name": s["name"], "quantity": s["quantity"]}
            )
        for item in items:
            item["locations"] = stock.get(item["id"], [])
            item["quantity"] = sum(s["quantity"] for s in item["locations"])
            item["total_value"] = round(item["quantity"] * item["unit_value"], 2)
            item["image_url"] = f"/uploads/{item['image']}" if item.get("image") else None
        return items

    def search(self, query="", sort="part_number", order="asc", location_id=None, category=None):
        where, params = [], []
        for term in query.split():
            # Every word must match the P/N, description, category, or a location holding the part.
            where.append(
                """(i.part_number LIKE ? ESCAPE '\\' OR i.description LIKE ? ESCAPE '\\'
                    OR i.category LIKE ? ESCAPE '\\'
                    OR EXISTS (SELECT 1 FROM stock s JOIN locations l ON l.id = s.location_id
                               WHERE s.item_id = i.id AND s.quantity > 0 AND l.name LIKE ? ESCAPE '\\'))"""
            )
            params += [_like(term)] * 4
        if category is not None:
            where.append("i.category = ? COLLATE NOCASE")
            params.append(category)
        if location_id:
            where.append("EXISTS (SELECT 1 FROM stock s WHERE s.item_id = i.id AND s.location_id = ? AND s.quantity > 0)")
            params.append(int(location_id))
        sql = f"""SELECT * FROM (
                      SELECT i.*, COALESCE((SELECT SUM(quantity) FROM stock WHERE item_id = i.id), 0) AS quantity
                      FROM items i {"WHERE " + " AND ".join(where) if where else ""})
                  ORDER BY {SORTABLE.get(sort, SORTABLE["part_number"])} {"DESC" if order == "desc" else "ASC"}, id"""
        with self.lock:
            return self._with_stock(self.conn.execute(sql, params))

    def get(self, item_id):
        with self.lock:
            return self._with_stock([self._item_row(item_id)])[0]

    def create(self, data):
        fields = clean_item(data)
        quantity = _int(data.get("quantity"))
        try:
            with self.tx():
                cur = self.conn.execute(
                    "INSERT INTO items (part_number, description, category, unit_value) VALUES (?, ?, ?, ?)",
                    (fields["part_number"], fields["description"], fields["category"], fields["unit_value"]),
                )
                if quantity:
                    location = self._location(data.get("location"), create=True)
                    self._move("receive", cur.lastrowid, location, quantity, note="Initial stock")
        except sqlite3.IntegrityError:
            raise ValidationError(f"Part number '{fields['part_number']}' already exists")
        return self.get(cur.lastrowid)

    def update(self, item_id, data):
        fields = clean_item(data, partial=True)
        if fields:
            sets = ", ".join(f"{k} = ?" for k in fields)
            try:
                with self.tx():
                    self._item_row(item_id)
                    self.conn.execute(
                        f"UPDATE items SET {sets}, updated_at = datetime('now') WHERE id = ?",
                        (*fields.values(), item_id),
                    )
            except sqlite3.IntegrityError:
                raise ValidationError(f"Part number '{fields.get('part_number')}' already exists")
        return self.get(item_id)

    def delete(self, item_id):
        with self.tx():
            image = self._item_row(item_id)["image"]
            self.conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
        self._remove_file(image)

    # --- pictures ----------------------------------------------------------

    def set_image(self, item_id, data):
        if len(data) > MAX_IMAGE_BYTES:
            raise ValidationError("Picture must be 10 MB or smaller")
        ext = _image_ext(data)
        if not ext:
            raise ValidationError("Picture must be a JPEG, PNG, GIF or WebP image")
        with self.tx():
            old = self._item_row(item_id)["image"]
            self.uploads.mkdir(parents=True, exist_ok=True)
            name = f"{item_id}-{secrets.token_hex(8)}.{ext}"
            (self.uploads / name).write_bytes(data)
            self.conn.execute(
                "UPDATE items SET image = ?, updated_at = datetime('now') WHERE id = ?", (name, item_id)
            )
        self._remove_file(old)
        return self.get(item_id)

    def remove_image(self, item_id):
        with self.tx():
            old = self._item_row(item_id)["image"]
            self.conn.execute("UPDATE items SET image = NULL, updated_at = datetime('now') WHERE id = ?", (item_id,))
        self._remove_file(old)
        return self.get(item_id)

    def _remove_file(self, name):
        if name:
            (self.uploads / name).unlink(missing_ok=True)

    # --- stock movements ---------------------------------------------------

    def _location(self, name, create=False):
        name = str(name or "").strip()
        if not name:
            raise ValidationError("Location is required")
        row = self._one("SELECT * FROM locations WHERE name = ?", (name,))
        if row:
            return row
        if not create:
            raise ValidationError(f"Unknown location '{name}'")
        if len(name) > 100:
            raise ValidationError("Location name must be 100 characters or fewer")
        cur = self.conn.execute("INSERT INTO locations (name) VALUES (?)", (name,))
        self._assign_code(cur.lastrowid)
        return self._one("SELECT * FROM locations WHERE id = ?", (cur.lastrowid,))

    def _assign_code(self, location_id, code=None):
        """Set a location's code, or generate LOC-0001 style when none is given."""
        if code is None:
            code, n = f"LOC-{location_id:04d}", 1
            while self._one("SELECT 1 FROM locations WHERE code = ? COLLATE NOCASE AND id != ?", (code, location_id)):
                code, n = f"LOC-{location_id:04d}-{n}", n + 1
        try:
            self.conn.execute("UPDATE locations SET code = ? WHERE id = ?", (code, location_id))
        except sqlite3.IntegrityError:
            raise ValidationError(f"Code '{code}' is already used by another location")

    def _job(self, number, create=False):
        number = str(number or "").strip()
        if not number:
            raise ValidationError("Job is required")
        row = self._one("SELECT * FROM jobs WHERE number = ?", (number,))
        if row:
            return row
        if not create:
            raise ValidationError(f"Unknown job '{number}'")
        if len(number) > 100:
            raise ValidationError("Job number must be 100 characters or fewer")
        cur = self.conn.execute("INSERT INTO jobs (number) VALUES (?)", (number,))
        return self._one("SELECT * FROM jobs WHERE id = ?", (cur.lastrowid,))

    def _stock_at(self, item_id, location_id):
        row = self._one("SELECT quantity FROM stock WHERE item_id = ? AND location_id = ?", (item_id, location_id))
        return row["quantity"] if row else 0

    def _move(self, kind, item_id, location, delta, job=None, note=""):
        """Apply a signed stock change at a location and record it. Caller holds the transaction."""
        item = self._item_row(item_id)
        current = self._stock_at(item_id, location["id"])
        if current + delta < 0:
            raise ValidationError(f"Only {current} of {item['part_number']} at {location['name']}")
        if current + delta == 0:
            self.conn.execute("DELETE FROM stock WHERE item_id = ? AND location_id = ?", (item_id, location["id"]))
        else:
            self.conn.execute(
                """INSERT INTO stock (item_id, location_id, quantity) VALUES (?, ?, ?)
                   ON CONFLICT (item_id, location_id) DO UPDATE SET quantity = excluded.quantity""",
                (item_id, location["id"], current + delta),
            )
        self.conn.execute(
            """INSERT INTO movements (kind, item_id, part_number, description, location_id, location_name,
                                      job_id, quantity, unit_value, note)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (kind, item_id, item["part_number"], item["description"], location["id"], location["name"],
             job["id"] if job else None, delta, item["unit_value"], str(note or "").strip()[:500]),
        )
        self.conn.execute("UPDATE items SET updated_at = datetime('now') WHERE id = ?", (item_id,))

    def receive(self, item_id, data):
        """Add new stock to a location (created if it doesn't exist)."""
        quantity = _int(data.get("quantity"), minimum=1)
        with self.tx():
            self._move("receive", item_id, self._location(data.get("location"), create=True), quantity,
                       note=data.get("note"))
        return self.get(item_id)

    def book_out(self, item_id, data):
        """Take stock from a location and book it to a job (created if it doesn't exist)."""
        quantity = _int(data.get("quantity"), minimum=1)
        with self.tx():
            location = self._location(data.get("location"))
            job = self._job(data.get("job"), create=True)
            if job["status"] != "open":
                raise ValidationError(f"Job {job['number']} is closed")
            self._move("book_out", item_id, location, -quantity, job=job, note=data.get("note"))
        return self.get(item_id)

    def return_from_job(self, item_id, data):
        """Return unused parts from a job back into a location."""
        quantity = _int(data.get("quantity"), minimum=1)
        with self.tx():
            job = self._job(data.get("job"))
            booked = -self._one(
                "SELECT COALESCE(SUM(quantity), 0) FROM movements WHERE job_id = ? AND item_id = ?",
                (job["id"], item_id),
            )[0]
            if quantity > booked:
                raise ValidationError(f"Only {booked} booked to job {job['number']}")
            self._move("return", item_id, self._location(data.get("location"), create=True), quantity,
                       job=job, note=data.get("note"))
        return self.get(item_id)

    def transfer(self, item_id, data):
        """Move stock from one location to another."""
        quantity = _int(data.get("quantity"), minimum=1)
        with self.tx():
            source = self._location(data.get("from_location"))
            dest = self._location(data.get("to_location"), create=True)
            if source["id"] == dest["id"]:
                raise ValidationError("Choose a different location to move to")
            self._move("transfer", item_id, source, -quantity, note=data.get("note"))
            self._move("transfer", item_id, dest, quantity, note=data.get("note"))
        return self.get(item_id)

    def count(self, item_id, data):
        """Set the quantity at a location to a counted figure (stocktake correction)."""
        quantity = _int(data.get("quantity"))
        with self.tx():
            location = self._location(data.get("location"), create=True)
            delta = quantity - self._stock_at(item_id, location["id"])
            if delta:
                self._move("count", item_id, location, delta, note=data.get("note"))
        return self.get(item_id)

    def history(self, item_id=None, job_id=None, query="", limit=500):
        where, params = [], []
        if item_id is not None:
            where.append("m.item_id = ?")
            params.append(item_id)
        if job_id is not None:
            where.append("m.job_id = ?")
            params.append(job_id)
        for term in query.split():
            where.append(
                """(COALESCE(i.part_number, m.part_number) LIKE ? ESCAPE '\\'
                    OR COALESCE(i.description, m.description) LIKE ? ESCAPE '\\'
                    OR COALESCE(l.name, m.location_name) LIKE ? ESCAPE '\\'
                    OR j.number LIKE ? ESCAPE '\\' OR m.note LIKE ? ESCAPE '\\' OR m.kind LIKE ? ESCAPE '\\')"""
            )
            params += [_like(term)] * 6
        params.append(int(limit))
        with self.lock:
            rows = self.conn.execute(
                f"""SELECT m.id, m.created_at, m.kind, m.item_id, m.quantity, m.unit_value, m.note,
                           COALESCE(i.part_number, m.part_number) AS part_number,
                           COALESCE(i.description, m.description) AS description,
                           COALESCE(l.name, m.location_name) AS location,
                           m.job_id, j.number AS job
                    FROM movements m
                    LEFT JOIN items i ON i.id = m.item_id
                    LEFT JOIN locations l ON l.id = m.location_id
                    LEFT JOIN jobs j ON j.id = m.job_id
                    {"WHERE " + " AND ".join(where) if where else ""}
                    ORDER BY m.id DESC LIMIT ?""",
                params,
            ).fetchall()
        return [dict(r, value=round(r["quantity"] * r["unit_value"], 2)) for r in rows]

    # --- locations ---------------------------------------------------------

    def locations(self):
        with self.lock:
            rows = self.conn.execute(
                """SELECT l.*, COUNT(s.item_id) AS parts, COALESCE(SUM(s.quantity), 0) AS units,
                          COALESCE(SUM(s.quantity * i.unit_value), 0) AS total_value
                   FROM locations l
                   LEFT JOIN stock s ON s.location_id = l.id AND s.quantity > 0
                   LEFT JOIN items i ON i.id = s.item_id
                   GROUP BY l.id ORDER BY l.name COLLATE NOCASE"""
            ).fetchall()
        return [dict(r, total_value=round(r["total_value"], 2)) for r in rows]

    def get_location(self, location_id):
        with self.lock:
            for loc in self.locations():
                if loc["id"] == location_id:
                    return loc
        raise NotFound(f"Location {location_id} not found")

    def create_location(self, data):
        name = _text(data, "name", "Location name", required=True, max_len=100)
        code = clean_code(data["code"]) if str(data.get("code") or "").strip() else None
        with self.tx():
            try:
                cur = self.conn.execute(
                    "INSERT INTO locations (name, notes) VALUES (?, ?)", (name, _text(data, "notes", "Notes", max_len=500))
                )
            except sqlite3.IntegrityError:
                raise ValidationError(f"Location '{name}' already exists")
            self._assign_code(cur.lastrowid, code)
        return self.get_location(cur.lastrowid)

    def update_location(self, location_id, data):
        fields = {}
        if "name" in data:
            fields["name"] = _text(data, "name", "Location name", required=True, max_len=100)
        if "notes" in data:
            fields["notes"] = _text(data, "notes", "Notes", max_len=500)
        if "code" in data:
            fields["code"] = clean_code(data["code"])
        try:
            with self.tx():
                if not self._one("SELECT 1 FROM locations WHERE id = ?", (location_id,)):
                    raise NotFound(f"Location {location_id} not found")
                if fields:
                    sets = ", ".join(f"{k} = ?" for k in fields)
                    self.conn.execute(f"UPDATE locations SET {sets} WHERE id = ?", (*fields.values(), location_id))
        except sqlite3.IntegrityError as e:
            if "code" in str(e):
                raise ValidationError(f"Code '{fields.get('code')}' is already used by another location")
            raise ValidationError(f"Location '{fields.get('name')}' already exists")
        return self.get_location(location_id)

    def location_contents(self, location_id):
        """A location with the parts stored in it."""
        with self.lock:
            location = self.get_location(location_id)
            rows = self.conn.execute(
                """SELECT i.*, s.quantity AS here FROM stock s JOIN items i ON i.id = s.item_id
                   WHERE s.location_id = ? AND s.quantity > 0 ORDER BY i.part_number COLLATE NOCASE""",
                (location_id,),
            ).fetchall()
            here = {r["id"]: r["here"] for r in rows}
            items = self._with_stock(rows)
        for item in items:
            item["here"] = here[item["id"]]
            item["here_value"] = round(item["here"] * item["unit_value"], 2)
        location["items"] = items
        return location

    def scan(self, text):
        """Work out what a scanned or typed code refers to: a location code, a P/N,
        a location name or a job number (in that order)."""
        code = scanned_code(text)
        if not code:
            raise ValidationError("Nothing to look up")
        with self.lock:
            for kind, sql in (
                ("location", "SELECT id FROM locations WHERE code = ? COLLATE NOCASE"),
                ("part", "SELECT id FROM items WHERE part_number = ?"),
                ("location", "SELECT id FROM locations WHERE name = ?"),
                ("job", "SELECT id FROM jobs WHERE number = ?"),
            ):
                row = self._one(sql, (code,))
                if row:
                    return {"type": kind, "id": row["id"], "code": code}
        raise NotFound(f"Nothing found for '{code}'")

    def categories(self):
        with self.lock:
            rows = self.conn.execute(
                """SELECT MIN(category) AS name, COUNT(*) AS parts FROM items WHERE category != ''
                   GROUP BY category COLLATE NOCASE ORDER BY category COLLATE NOCASE"""
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_location(self, location_id):
        with self.tx():
            loc = self._one("SELECT * FROM locations WHERE id = ?", (location_id,))
            if not loc:
                raise NotFound(f"Location {location_id} not found")
            if self._one("SELECT 1 FROM stock WHERE location_id = ? AND quantity > 0", (location_id,)):
                raise ValidationError(f"{loc['name']} still holds stock. Move or book it out first.")
            self.conn.execute("DELETE FROM stock WHERE location_id = ?", (location_id,))
            self.conn.execute("DELETE FROM locations WHERE id = ?", (location_id,))

    # --- jobs --------------------------------------------------------------

    JOB_TOTALS = """SELECT j.*, COALESCE(-SUM(m.quantity), 0) AS units,
                           COALESCE(-SUM(m.quantity * m.unit_value), 0) AS total_value
                    FROM jobs j LEFT JOIN movements m ON m.job_id = j.id"""

    def jobs(self, query="", status=""):
        where, params = [], []
        for term in query.split():
            where.append("(j.number LIKE ? ESCAPE '\\' OR j.description LIKE ? ESCAPE '\\')")
            params += [_like(term)] * 2
        if status in ("open", "closed"):
            where.append("j.status = ?")
            params.append(status)
        with self.lock:
            rows = self.conn.execute(
                f"""{self.JOB_TOTALS} {"WHERE " + " AND ".join(where) if where else ""}
                    GROUP BY j.id ORDER BY j.status = 'closed', j.created_at DESC, j.id DESC""",
                params,
            ).fetchall()
        return [dict(r, total_value=round(r["total_value"], 2)) for r in rows]

    def get_job(self, job_id):
        """A job with the net parts booked to it and its movement history."""
        with self.lock:
            row = self._one(f"{self.JOB_TOTALS} WHERE j.id = ? GROUP BY j.id", (job_id,))
            if row is None:
                raise NotFound(f"Job {job_id} not found")
            parts = self.conn.execute(
                """SELECT m.item_id, COALESCE(i.part_number, m.part_number) AS part_number,
                          COALESCE(i.description, m.description) AS description,
                          -SUM(m.quantity) AS quantity, -SUM(m.quantity * m.unit_value) AS total_value
                   FROM movements m LEFT JOIN items i ON i.id = m.item_id
                   WHERE m.job_id = ?
                   GROUP BY COALESCE(CAST(m.item_id AS TEXT), 'deleted:' || m.part_number)
                   HAVING SUM(m.quantity) != 0
                   ORDER BY part_number COLLATE NOCASE""",
                (job_id,),
            ).fetchall()
            job = dict(row, total_value=round(row["total_value"], 2))
            job["parts"] = [dict(p, total_value=round(p["total_value"], 2)) for p in parts]
            job["history"] = self.history(job_id=job_id)
        return job

    def create_job(self, data):
        number = _text(data, "number", "Job number", required=True, max_len=100)
        try:
            with self.tx():
                cur = self.conn.execute(
                    "INSERT INTO jobs (number, description) VALUES (?, ?)",
                    (number, _text(data, "description", "Description", max_len=2000)),
                )
        except sqlite3.IntegrityError:
            raise ValidationError(f"Job '{number}' already exists")
        return self.get_job(cur.lastrowid)

    def update_job(self, job_id, data):
        fields = {}
        if "number" in data:
            fields["number"] = _text(data, "number", "Job number", required=True, max_len=100)
        if "description" in data:
            fields["description"] = _text(data, "description", "Description", max_len=2000)
        if "status" in data:
            if data["status"] not in ("open", "closed"):
                raise ValidationError("Status must be 'open' or 'closed'")
            fields["status"] = data["status"]
        try:
            with self.tx():
                if not self._one("SELECT 1 FROM jobs WHERE id = ?", (job_id,)):
                    raise NotFound(f"Job {job_id} not found")
                if fields:
                    sets = ", ".join(f"{k} = ?" for k in fields)
                    self.conn.execute(f"UPDATE jobs SET {sets} WHERE id = ?", (*fields.values(), job_id))
        except sqlite3.IntegrityError:
            raise ValidationError(f"Job '{fields.get('number')}' already exists")
        return self.get_job(job_id)

    def delete_job(self, job_id):
        with self.tx():
            job = self._one("SELECT * FROM jobs WHERE id = ?", (job_id,))
            if not job:
                raise NotFound(f"Job {job_id} not found")
            if self._one("SELECT 1 FROM movements WHERE job_id = ?", (job_id,)):
                raise ValidationError(f"Job {job['number']} has parts booked to it. Close it instead.")
            self.conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))

    # --- summary and import ------------------------------------------------

    def summary(self):
        with self.lock:
            r = self._one(
                """SELECT (SELECT COUNT(*) FROM items),
                          (SELECT COALESCE(SUM(quantity), 0) FROM stock),
                          (SELECT COALESCE(SUM(s.quantity * i.unit_value), 0) FROM stock s JOIN items i ON i.id = s.item_id),
                          (SELECT COUNT(*) FROM locations),
                          (SELECT COUNT(*) FROM jobs WHERE status = 'open')"""
            )
        return {"items": r[0], "units": r[1], "total_value": round(r[2], 2), "locations": r[3], "open_jobs": r[4]}

    def import_rows(self, rows):
        """Upsert parts keyed by P/N. A quantity sets the stock count at the row's location
        (or the default location if none is given). Returns counts and per-row errors."""
        created = updated = 0
        errors = []
        for n, row in enumerate(rows, start=2):  # row 1 is the CSV header
            row = {k: v for k, v in row.items() if v not in (None, "")}
            try:
                pn = _text(row, "part_number", "Part number", required=True, max_len=100)
                with self.lock:
                    existing = self._one("SELECT id FROM items WHERE part_number = ?", (pn,))
                    if existing:
                        item_id = self.update(existing["id"], clean_item(row, partial=True))["id"]
                        updated += 1
                    else:
                        item_id = self.create({k: v for k, v in row.items() if k != "quantity"})["id"]
                        created += 1
                    if "quantity" in row:
                        self.count(item_id, {"location": row.get("location") or DEFAULT_LOCATION,
                                             "quantity": row["quantity"], "note": "CSV import"})
            except ValidationError as e:
                errors.append(f"Row {n}: {e}")
        return {"created": created, "updated": updated, "errors": errors}
