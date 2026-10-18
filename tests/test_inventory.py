import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from inventory.server import make_server
from inventory.store import NotFound, Store, ValidationError

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(":memory:", uploads_dir=self.tmp.name)
        s = self.store
        self.res = s.create({"part_number": "RES-10K", "description": "Resistor 10k ohm 1/4W",
                             "unit_value": 0.02, "quantity": 500, "location": "Shelf A"})
        self.cap = s.create({"part_number": "CAP-100U", "description": "Capacitor 100uF electrolytic",
                             "unit_value": "$0.15", "quantity": 40, "location": "Shelf B"})
        self.mcu = s.create({"part_number": "MCU-328P", "description": "ATmega328P microcontroller",
                             "unit_value": "2,500.00", "quantity": 3, "location": "Shelf A"})
        s.receive(self.mcu["id"], {"location": "Van 1", "quantity": 2})

    def tearDown(self):
        self.tmp.cleanup()

    def pns(self, items):
        return [i["part_number"] for i in items]


class PartTests(StoreTestCase):
    def test_quantity_and_value_span_locations(self):
        mcu = self.store.get(self.mcu["id"])
        self.assertEqual(mcu["quantity"], 5)
        self.assertEqual(mcu["total_value"], 12500.0)
        self.assertEqual([(l["name"], l["quantity"]) for l in mcu["locations"]], [("Shelf A", 3), ("Van 1", 2)])
        summary = self.store.summary()
        self.assertEqual((summary["items"], summary["units"], summary["total_value"]), (3, 545, 12516.0))
        self.assertEqual(summary["locations"], 3)

    def test_search_matches_pn_description_and_location(self):
        self.assertEqual(self.pns(self.store.search("res")), ["RES-10K"])
        self.assertEqual(self.pns(self.store.search("capacitor")), ["CAP-100U"])
        self.assertEqual(self.pns(self.store.search("resistor 1/4")), ["RES-10K"])
        self.assertEqual(self.pns(self.store.search("van")), ["MCU-328P"])
        self.assertEqual(self.store.search("resistor atmega"), [])
        self.assertEqual(self.store.search("%"), [])

    def test_location_filter(self):
        shelf_a = next(l for l in self.store.locations() if l["name"] == "Shelf A")
        self.assertEqual(self.pns(self.store.search(location_id=shelf_a["id"])), ["MCU-328P", "RES-10K"])

    def test_sort(self):
        self.assertEqual(self.pns(self.store.search(sort="total_value", order="desc")),
                         ["MCU-328P", "RES-10K", "CAP-100U"])
        self.assertEqual(self.pns(self.store.search(sort="quantity")), ["MCU-328P", "CAP-100U", "RES-10K"])

    def test_validation(self):
        with self.assertRaises(ValidationError):
            self.store.create({"part_number": "res-10k"})
        for bad in ({"part_number": ""}, {"part_number": "X", "unit_value": "-5"},
                    {"part_number": "X", "quantity": "abc"}, {"part_number": "X", "quantity": 5}):
            with self.assertRaises(ValidationError):
                self.store.create(bad)
        self.assertEqual(len(self.store.search()), 3)  # failed creates leave nothing behind

    def test_update_and_delete_keep_history(self):
        self.store.update(self.cap["id"], {"description": "Updated"})
        self.assertEqual(self.store.get(self.cap["id"])["description"], "Updated")
        self.store.book_out(self.cap["id"], {"location": "Shelf B", "job": "J1", "quantity": 4})
        self.store.delete(self.cap["id"])
        with self.assertRaises(NotFound):
            self.store.get(self.cap["id"])
        job = self.store.get_job(self.store.jobs()[0]["id"])
        self.assertEqual([(p["part_number"], p["quantity"]) for p in job["parts"]], [("CAP-100U", 4)])

    def test_image_upload_replace_and_delete(self):
        item = self.store.set_image(self.res["id"], PNG)
        first = Path(self.tmp.name, item["image"])
        self.assertTrue(first.exists())
        self.assertTrue(item["image_url"].endswith(".png"))
        item = self.store.set_image(self.res["id"], b"\xff\xd8\xff" + b"\x00" * 10)
        self.assertFalse(first.exists())
        self.assertTrue(item["image"].endswith(".jpg"))
        with self.assertRaises(ValidationError):
            self.store.set_image(self.res["id"], b"<script>alert(1)</script>")
        self.store.delete(self.res["id"])
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])


class StockTests(StoreTestCase):
    def test_book_out_and_return(self):
        s = self.store
        s.book_out(self.res["id"], {"location": "Shelf A", "job": "JOB-42", "quantity": 100, "note": "Panel"})
        s.book_out(self.mcu["id"], {"location": "Van 1", "job": "job-42", "quantity": 1})
        self.assertEqual(s.get(self.res["id"])["quantity"], 400)

        job = s.jobs()[0]
        self.assertEqual((job["number"], job["units"], job["total_value"]), ("JOB-42", 101, 2502.0))

        s.return_from_job(self.res["id"], {"job": "JOB-42", "location": "Shelf C", "quantity": 30})
        detail = s.get_job(job["id"])
        self.assertEqual([(p["part_number"], p["quantity"]) for p in detail["parts"]],
                         [("MCU-328P", 1), ("RES-10K", 70)])
        self.assertEqual(detail["total_value"], 2501.4)
        self.assertEqual(len(detail["history"]), 3)
        with self.assertRaises(ValidationError):
            s.return_from_job(self.res["id"], {"job": "JOB-42", "location": "Shelf A", "quantity": 71})

    def test_job_value_fixed_at_booking(self):
        s = self.store
        s.book_out(self.cap["id"], {"location": "Shelf B", "job": "J1", "quantity": 10})
        s.update(self.cap["id"], {"unit_value": 1})
        self.assertEqual(s.jobs()[0]["total_value"], 1.5)

    def test_book_out_rules(self):
        s = self.store
        with self.assertRaises(ValidationError):  # more than is at that location
            s.book_out(self.mcu["id"], {"location": "Van 1", "job": "J1", "quantity": 3})
        with self.assertRaises(ValidationError):  # location doesn't hold it
            s.book_out(self.mcu["id"], {"location": "Nowhere", "job": "J1", "quantity": 1})
        with self.assertRaises(ValidationError):
            s.book_out(self.mcu["id"], {"location": "Van 1", "job": "", "quantity": 1})
        with self.assertRaises(ValidationError):
            s.book_out(self.mcu["id"], {"location": "Van 1", "job": "J1", "quantity": 0})
        self.assertEqual(s.jobs(), [])  # failed bookings don't leave a job behind
        job = s.create_job({"number": "J2"})
        s.update_job(job["id"], {"status": "closed"})
        with self.assertRaises(ValidationError):
            s.book_out(self.mcu["id"], {"location": "Van 1", "job": "J2", "quantity": 1})

    def test_transfer(self):
        s = self.store
        item = s.transfer(self.mcu["id"], {"from_location": "Shelf A", "to_location": "Van 1", "quantity": 3})
        self.assertEqual([(l["name"], l["quantity"]) for l in item["locations"]], [("Van 1", 5)])
        with self.assertRaises(ValidationError):
            s.transfer(self.mcu["id"], {"from_location": "Van 1", "to_location": "Van 1", "quantity": 1})
        with self.assertRaises(ValidationError):
            s.transfer(self.mcu["id"], {"from_location": "Van 1", "to_location": "Shelf A", "quantity": 6})

    def test_count_sets_quantity(self):
        item = self.store.count(self.res["id"], {"location": "Shelf A", "quantity": 480, "note": "Stocktake"})
        self.assertEqual(item["quantity"], 480)
        self.assertEqual(self.store.history(item_id=self.res["id"])[0]["quantity"], -20)

    def test_history_search(self):
        self.store.book_out(self.res["id"], {"location": "Shelf A", "job": "JOB-7", "quantity": 1})
        self.assertEqual([h["kind"] for h in self.store.history(query="job-7")], ["book_out"])


class LocationAndJobTests(StoreTestCase):
    def test_location_crud(self):
        s = self.store
        loc = s.create_location({"name": "Bin 4", "notes": "Top"})
        with self.assertRaises(ValidationError):
            s.create_location({"name": "bin 4"})
        self.assertEqual(s.update_location(loc["id"], {"name": "Bin 5"})["name"], "Bin 5")
        s.delete_location(loc["id"])
        shelf_a = next(l for l in s.locations() if l["name"] == "Shelf A")
        self.assertEqual((shelf_a["parts"], shelf_a["units"]), (2, 503))
        with self.assertRaises(ValidationError):
            s.delete_location(shelf_a["id"])

    def test_job_crud(self):
        s = self.store
        job = s.create_job({"number": "J100", "description": "Kitchen refit"})
        self.assertEqual(s.jobs("kitchen")[0]["number"], "J100")
        s.update_job(job["id"], {"status": "closed"})
        self.assertEqual(s.jobs(status="open"), [])
        s.delete_job(job["id"])
        s.book_out(self.cap["id"], {"location": "Shelf B", "job": "J101", "quantity": 1})
        with self.assertRaises(ValidationError):
            s.delete_job(s.jobs()[0]["id"])


class ImportAndMigrationTests(StoreTestCase):
    def test_import_upserts_stock_per_location(self):
        result = self.store.import_rows([
            {"part_number": "RES-10K", "location": "Shelf A", "quantity": "1000", "unit_value": "0.01"},
            {"part_number": "RES-10K", "location": "Van 2", "quantity": "50"},
            {"part_number": "NEW-1", "description": "New part", "quantity": "5", "unit_value": "1"},
            {"part_number": "", "description": "bad"},
        ])
        self.assertEqual((result["created"], result["updated"], len(result["errors"])), (1, 2, 1))
        res = self.store.get(self.res["id"])
        self.assertEqual(res["quantity"], 1050)
        self.assertEqual(res["unit_value"], 0.01)
        self.assertEqual(self.store.search("NEW-1")[0]["locations"][0]["name"], "Main")

    def test_migrates_version_1_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp, "old.db"))
            old = sqlite3.connect(path)
            old.executescript("""
                CREATE TABLE items (id INTEGER PRIMARY KEY AUTOINCREMENT,
                    part_number TEXT NOT NULL UNIQUE COLLATE NOCASE, description TEXT NOT NULL DEFAULT '',
                    quantity INTEGER NOT NULL DEFAULT 0, unit_value REAL NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now')));
                INSERT INTO items (part_number, description, quantity, unit_value) VALUES
                    ('A-1', 'Alpha', 10, 2.5), ('B-2', 'Beta', 0, 1);
            """)
            old.close()
            store = Store(path, uploads_dir=tmp)
            items = store.search()
            self.assertEqual([(i["part_number"], i["quantity"]) for i in items], [("A-1", 10), ("B-2", 0)])
            self.assertEqual(items[0]["locations"][0]["name"], "Main")
            self.assertEqual(store.summary()["total_value"], 25.0)
            store.conn.close()
            Store(path, uploads_dir=tmp).conn.close()  # opening again is a no-op


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = make_server("127.0.0.1", 0, ":memory:", self.tmp.name)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def request(self, method, path, body=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.base + path, data=data, method=method)
        try:
            with urllib.request.urlopen(req) as res:
                text = res.read()
                return res.status, text.decode(errors="replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def json(self, method, path, body=None, expect=200):
        status, text = self.request(method, path, body)
        self.assertEqual(status, expect, text)
        return json.loads(text) if text else None

    def test_full_flow(self):
        item = self.json("POST", "/api/items", {"part_number": "ABC-1", "description": "Widget",
                                                "unit_value": 2.5, "quantity": 4, "location": "Shelf A"}, 201)
        iid = item["id"]
        self.json("POST", f"/api/items/{iid}/receive", {"location": "Van", "quantity": 6})
        self.json("POST", f"/api/items/{iid}/transfer", {"from_location": "Van", "to_location": "Shelf A", "quantity": 1})
        self.json("POST", f"/api/items/{iid}/book-out", {"location": "Shelf A", "job": "J-9", "quantity": 3})
        self.json("POST", f"/api/items/{iid}/return", {"location": "Van", "job": "J-9", "quantity": 1})
        item = self.json("POST", f"/api/items/{iid}/count", {"location": "Van", "quantity": 7})
        self.assertEqual([(l["name"], l["quantity"]) for l in item["locations"]], [("Shelf A", 2), ("Van", 7)])

        data = self.json("GET", "/api/items?q=widget")
        self.assertEqual(data["summary"]["open_jobs"], 1)
        job = self.json("GET", "/api/jobs")[0]
        self.assertEqual((job["number"], job["units"], job["total_value"]), ("J-9", 2, 5.0))
        detail = self.json("GET", f"/api/jobs/{job['id']}")
        self.assertEqual(detail["parts"][0]["quantity"], 2)
        status, text = self.request("GET", f"/api/jobs/{job['id']}/export.csv")
        self.assertIn("J-9,ABC-1,Widget,2,5.0", text)

        self.assertEqual(len(self.json("GET", f"/api/items/{iid}/history")), 7)
        self.assertEqual(len(self.json("GET", "/api/history?q=J-9")), 2)
        self.assertEqual([l["name"] for l in self.json("GET", "/api/locations")], ["Shelf A", "Van"])
        self.json("PUT", f"/api/jobs/{job['id']}", {"status": "closed"})
        self.json("DELETE", f"/api/items/{iid}", expect=204)
        self.json("GET", f"/api/items/{iid}", expect=404)

    def test_errors_return_json(self):
        status, body = self.request("POST", "/api/items", {"part_number": ""})
        self.assertEqual(status, 400)
        self.assertIn("required", json.loads(body)["error"])
        status, _ = self.request("POST", "/api/items/999/receive", {"location": "A", "quantity": 1})
        self.assertEqual(status, 404)
        for path in ("/api/history?limit=abc", "/api/items?location_id=abc"):
            self.assertEqual(self.request("GET", path)[0], 400, path)

    def test_image_upload_and_serving(self):
        item = self.json("POST", "/api/items", {"part_number": "P1"}, 201)
        status, text = self.request("POST", f"/api/items/{item['id']}/image", raw=PNG)
        self.assertEqual(status, 200, text)
        url = json.loads(text)["image_url"]
        with urllib.request.urlopen(self.base + url) as res:
            self.assertEqual(res.headers["Content-Type"], "image/png")
            self.assertEqual(res.read(), PNG)
        status, _ = self.request("POST", f"/api/items/{item['id']}/image", raw=b"not an image")
        self.assertEqual(status, 400)
        self.assertIsNone(self.json("DELETE", f"/api/items/{item['id']}/image")["image_url"])

    def test_csv_round_trip(self):
        csv_text = "Part_Number,Description,Location,Quantity,Unit_Value\nP1,\"Bolt, M3\",Bin 1,10,0.05\nP1,,Bin 2,4,\nP2,Nut,,20,0.02\n"
        status, body = self.request("POST", "/api/import", raw=csv_text.encode())
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["created"], 2)
        status, body = self.request("GET", "/api/export.csv")
        self.assertIn('P1,"Bolt, M3",Bin 1,10,0.05,0.5', body)
        self.assertIn('P1,"Bolt, M3",Bin 2,4,0.05,0.2', body)
        self.assertIn("P2,Nut,Main,20,0.02,0.4", body)

    def test_serves_ui_and_blocks_traversal(self):
        status, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("<title>Inventory</title>", body)
        for path in ("/../inventory/store.py", "/%2e%2e/inventory/store.py", "/uploads/../../etc/passwd",
                     "/uploads/%2e%2e%2f%2e%2e%2fetc%2fpasswd"):
            status, _ = self.request("GET", path)
            self.assertEqual(status, 404, path)


if __name__ == "__main__":
    unittest.main()
