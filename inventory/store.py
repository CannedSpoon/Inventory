"""SQLite-backed storage for inventory items."""

import sqlite3
import threading

SORTABLE = {
    "part_number": "part_number COLLATE NOCASE",
    "description": "description COLLATE NOCASE",
    "quantity": "quantity",
    "unit_value": "unit_value",
    "total_value": "quantity * unit_value",
    "updated_at": "updated_at",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    part_number TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    description TEXT    NOT NULL DEFAULT '',
    quantity    INTEGER NOT NULL DEFAULT 0 CHECK (quantity >= 0),
    unit_value  REAL    NOT NULL DEFAULT 0 CHECK (unit_value >= 0),
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""


class ValidationError(ValueError):
    pass


class NotFound(LookupError):
    pass


def clean(data, partial=False):
    """Validate and normalise item fields. With partial=True, only given fields are checked."""
    out = {}
    if "part_number" in data or not partial:
        pn = str(data.get("part_number") or "").strip()
        if not pn:
            raise ValidationError("Part number is required")
        if len(pn) > 100:
            raise ValidationError("Part number must be 100 characters or fewer")
        out["part_number"] = pn
    if "description" in data or not partial:
        out["description"] = str(data.get("description") or "").strip()
    if "quantity" in data or not partial:
        try:
            qty = int(data.get("quantity") or 0)
        except (TypeError, ValueError):
            raise ValidationError("Quantity must be a whole number")
        if qty < 0:
            raise ValidationError("Quantity cannot be negative")
        out["quantity"] = qty
    if "unit_value" in data or not partial:
        raw = str(data.get("unit_value") or 0).replace("$", "").replace(",", "").strip()
        try:
            value = round(float(raw), 2)
        except ValueError:
            raise ValidationError("Value must be a number")
        if value < 0 or value != value or value == float("inf"):
            raise ValidationError("Value must be a non-negative number")
        out["unit_value"] = value
    return out


def _row(row):
    item = dict(row)
    item["total_value"] = round(item["quantity"] * item["unit_value"], 2)
    return item


class Store:
    def __init__(self, path="inventory.db"):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.lock = threading.Lock()

    def search(self, query="", sort="part_number", order="asc"):
        sql = "SELECT * FROM items"
        params = []
        terms = query.split()
        if terms:
            # Every whitespace-separated term must match the P/N or description.
            clauses = []
            for term in terms:
                like = "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                clauses.append("(part_number LIKE ? ESCAPE '\\' OR description LIKE ? ESCAPE '\\')")
                params += [like, like]
            sql += " WHERE " + " AND ".join(clauses)
        sql += f" ORDER BY {SORTABLE.get(sort, SORTABLE['part_number'])} {'DESC' if order == 'desc' else 'ASC'}, id"
        with self.lock:
            return [_row(r) for r in self.conn.execute(sql, params)]

    def summary(self):
        with self.lock:
            r = self.conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(quantity), 0), COALESCE(SUM(quantity * unit_value), 0) FROM items"
            ).fetchone()
        return {"items": r[0], "units": r[1], "total_value": round(r[2], 2)}

    def get(self, item_id):
        with self.lock:
            row = self.conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            raise NotFound(f"Item {item_id} not found")
        return _row(row)

    def create(self, data):
        fields = clean(data)
        try:
            with self.lock, self.conn:
                cur = self.conn.execute(
                    "INSERT INTO items (part_number, description, quantity, unit_value) VALUES (?, ?, ?, ?)",
                    (fields["part_number"], fields["description"], fields["quantity"], fields["unit_value"]),
                )
        except sqlite3.IntegrityError:
            raise ValidationError(f"Part number '{fields['part_number']}' already exists")
        return self.get(cur.lastrowid)

    def update(self, item_id, data):
        fields = clean(data, partial=True)
        if not fields:
            return self.get(item_id)
        sets = ", ".join(f"{k} = ?" for k in fields)
        try:
            with self.lock, self.conn:
                cur = self.conn.execute(
                    f"UPDATE items SET {sets}, updated_at = datetime('now') WHERE id = ?",
                    (*fields.values(), item_id),
                )
        except sqlite3.IntegrityError:
            raise ValidationError(f"Part number '{fields.get('part_number')}' already exists")
        if cur.rowcount == 0:
            raise NotFound(f"Item {item_id} not found")
        return self.get(item_id)

    def adjust(self, item_id, delta):
        """Add (or remove, if negative) stock from an item."""
        try:
            delta = int(delta)
        except (TypeError, ValueError):
            raise ValidationError("Adjustment must be a whole number")
        item = self.get(item_id)
        if item["quantity"] + delta < 0:
            raise ValidationError(f"Only {item['quantity']} in stock")
        with self.lock, self.conn:
            self.conn.execute(
                "UPDATE items SET quantity = quantity + ?, updated_at = datetime('now') WHERE id = ?",
                (delta, item_id),
            )
        return self.get(item_id)

    def delete(self, item_id):
        with self.lock, self.conn:
            cur = self.conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
        if cur.rowcount == 0:
            raise NotFound(f"Item {item_id} not found")

    def import_rows(self, rows):
        """Upsert rows keyed by part number. Returns counts and per-row errors."""
        created = updated = 0
        errors = []
        for n, row in enumerate(rows, start=2):  # row 1 is the CSV header
            try:
                fields = clean(row)
                with self.lock:
                    existing = self.conn.execute(
                        "SELECT id FROM items WHERE part_number = ?", (fields["part_number"],)
                    ).fetchone()
                if existing:
                    self.update(existing["id"], fields)
                    updated += 1
                else:
                    self.create(fields)
                    created += 1
            except ValidationError as e:
                errors.append(f"Row {n}: {e}")
        return {"created": created, "updated": updated, "errors": errors}
