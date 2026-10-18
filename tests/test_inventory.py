import http.cookiejar
import io
import json
import sqlite3
import zipfile
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from inventory.server import make_server
from inventory.store import NotFound, Store, ValidationError
from inventory.store import scanned_code as scanned

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPG = b"\xff\xd8\xff" + b"\x00" * 32


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

    def test_multiple_pictures(self):
        s, rid = self.store, self.res["id"]
        first = s.add_image(rid, PNG)
        self.assertTrue(first["image_url"].endswith(".png"))
        item = s.add_image(rid, JPG)
        item = s.add_image(rid, PNG)
        self.assertEqual(len(item["images"]), 3)
        self.assertEqual(item["image_url"], first["image_url"])  # the first one stays the main picture
        files = lambda: sorted(p.name for p in Path(self.tmp.name).iterdir())
        self.assertEqual(len(files()), 3)

        jpg = item["images"][1]
        item = s.make_main_image(rid, jpg["id"])
        self.assertEqual(item["image_url"], jpg["url"])
        self.assertEqual([i["id"] for i in item["images"]][0], jpg["id"])

        item = s.remove_image(rid, jpg["id"])
        self.assertEqual(len(item["images"]), 2)
        self.assertEqual(len(files()), 2)
        self.assertEqual(item["image_url"], first["image_url"])
        with self.assertRaises(NotFound):
            s.remove_image(self.cap["id"], item["images"][0]["id"])  # belongs to another part
        with self.assertRaises(ValidationError):
            s.add_image(rid, b"<script>alert(1)</script>")
        s.delete(rid)
        self.assertEqual(files(), [])

    def test_picture_limit(self):
        from inventory import store as store_module
        for _ in range(store_module.MAX_IMAGES_PER_PART):
            self.store.add_image(self.cap["id"], PNG)
        with self.assertRaises(ValidationError):
            self.store.add_image(self.cap["id"], PNG)


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


class CategoryTests(StoreTestCase):
    def test_categories(self):
        s = self.store
        s.update(self.res["id"], {"category": "Passives"})
        s.update(self.cap["id"], {"category": "passives"})
        s.update(self.mcu["id"], {"category": "Chips"})
        self.assertEqual([(c["name"], c["parts"]) for c in s.categories()], [("Chips", 1), ("Passives", 2)])
        self.assertEqual(self.pns(s.search(category="PASSIVES")), ["CAP-100U", "RES-10K"])
        self.assertEqual(self.pns(s.search("chips")), ["MCU-328P"])
        s.update(self.mcu["id"], {"category": ""})
        self.assertEqual(self.pns(s.search(category="")), ["MCU-328P"])
        new = s.create({"part_number": "N1", "category": " Tools "})
        self.assertEqual(new["category"], "Tools")


class CodeAndScanTests(StoreTestCase):
    def loc(self, name):
        return next(l for l in self.store.locations() if l["name"] == name)

    def test_locations_get_codes(self):
        codes = {l["name"]: l["code"] for l in self.store.locations()}
        self.assertEqual(codes, {"Shelf A": "LOC-0001", "Shelf B": "LOC-0002", "Van 1": "LOC-0003"})
        loc = self.store.create_location({"name": "Bin 9", "code": "BIN-9"})
        self.assertEqual(loc["code"], "BIN-9")
        with self.assertRaises(ValidationError):
            self.store.create_location({"name": "Bin 10", "code": "bin-9"})
        with self.assertRaises(ValidationError):
            self.store.update_location(loc["id"], {"code": "has space"})
        self.assertFalse(any(l["name"] == "Bin 10" for l in self.store.locations()))
        # an auto code that is already taken gets a suffix
        self.store.update_location(loc["id"], {"code": "LOC-0005"})
        self.assertEqual(self.store.create_location({"name": "Bin 11"})["code"], "LOC-0005-1")

    def test_location_contents(self):
        loc = self.store.location_contents(self.loc("Shelf A")["id"])
        self.assertEqual([(i["part_number"], i["here"], i["quantity"]) for i in loc["items"]],
                         [("MCU-328P", 3, 5), ("RES-10K", 500, 500)])
        self.assertEqual(loc["items"][0]["here_value"], 7500.0)

    def test_scan(self):
        s = self.store
        shelf = self.loc("Shelf A")
        for text in ("LOC-0001", "loc-0001", " http://192.168.1.5:8000/s/LOC-0001 ",
                     "http://x/#scan/LOC-0001", "Shelf A"):
            self.assertEqual(s.scan(text), {"type": "location", "id": shelf["id"], "code": scanned(text)}, text)
        self.assertEqual(s.scan("res-10k")["type"], "part")
        self.assertEqual(s.scan("http://x/s/RES-10K")["id"], self.res["id"])
        s.create_job({"number": "J-77"})
        self.assertEqual(s.scan("J-77")["type"], "job")
        with self.assertRaises(NotFound):
            s.scan("nope")
        with self.assertRaises(ValidationError):
            s.scan("  ")


class ImportAndMigrationTests(StoreTestCase):
    def test_migrates_version_2_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp, "v2.db"))
            from inventory import store as store_module
            old = sqlite3.connect(path)
            old.executescript(f"BEGIN; {store_module.TABLES} PRAGMA user_version = 2; COMMIT;")
            old.execute("INSERT INTO locations (name) VALUES ('Van'), ('Shed')")
            old.commit()
            old.close()
            store = Store(path, uploads_dir=tmp)
            self.assertEqual([(l["name"], l["code"]) for l in store.locations()], [("Shed", "LOC-0002"), ("Van", "LOC-0001")])
            self.assertEqual(store.conn.execute("PRAGMA user_version").fetchone()[0], store_module.SCHEMA_VERSION)
            store.conn.close()

    def test_migrates_single_picture_to_pictures(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp, "v4.db"))
            from inventory import store as store_module
            old = sqlite3.connect(path)
            old.executescript(f"BEGIN; {store_module.TABLES} PRAGMA user_version = 2; COMMIT;")
            old.executescript(f"BEGIN; {store_module.MIGRATE_V3} {store_module.MIGRATE_V4} PRAGMA user_version = 4; COMMIT;")
            old.execute("INSERT INTO items (part_number, image) VALUES ('A', '1-abc.png'), ('B', NULL)")
            old.commit()
            old.close()
            store = Store(path, uploads_dir=tmp)
            a, b = store.search()
            self.assertEqual(a["images"][0]["url"], "/uploads/1-abc.png")
            self.assertEqual(a["image_url"], "/uploads/1-abc.png")
            self.assertEqual((b["images"], b["image_url"]), ([], None))
            store.conn.close()

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
            self.assertEqual(items[0]["category"], "")
            self.assertEqual(store.locations()[0]["code"], "LOC-0001")
            self.assertEqual(store.summary()["total_value"], 25.0)
            store.conn.close()
            Store(path, uploads_dir=tmp).conn.close()  # opening again is a no-op


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class ServerTestCase(unittest.TestCase):
    behind_proxy = False
    sign_in = True

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = make_server("127.0.0.1", 0, ":memory:", self.tmp.name, behind_proxy=self.behind_proxy)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.opener = self.new_browser()
        if self.sign_in:
            self.server.store.set_password("tester", "password123")
            self.assertEqual(self.request("POST", "/api/login", {"username": "tester", "password": "password123"})[0], 200)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def new_browser(self):
        return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(self, method, path, body=None, raw=None, opener=None, headers=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        hdrs = {"X-Requested-With": "inventory"} if headers is None else headers
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=hdrs)
        try:
            with (opener or self.opener).open(req) as res:
                text = res.read()
                return res.status, text.decode(errors="replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def json(self, method, path, body=None, expect=200, opener=None):
        status, text = self.request(method, path, body, opener=opener)
        self.assertEqual(status, expect, text)
        return json.loads(text) if text else None


class ApiTests(ServerTestCase):
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
        iid = item["id"]
        status, text = self.request("POST", f"/api/items/{iid}/images", raw=PNG)
        self.assertEqual(status, 200, text)
        url = json.loads(text)["image_url"]
        with self.opener.open(self.base + url) as res:
            self.assertEqual(res.headers["Content-Type"], "image/png")
            self.assertEqual(res.read(), PNG)
        status, text = self.request("POST", f"/api/items/{iid}/images", raw=JPG)
        images = json.loads(text)["images"]
        self.assertEqual(len(images), 2)
        item = self.json("POST", f"/api/items/{iid}/images/{images[1]['id']}/main")
        self.assertEqual(item["image_url"], images[1]["url"])
        status, _ = self.request("POST", f"/api/items/{iid}/images", raw=b"not an image")
        self.assertEqual(status, 400)
        self.json("DELETE", f"/api/items/{iid}/images/{images[0]['id']}")
        item = self.json("DELETE", f"/api/items/{iid}/images/{images[1]['id']}")
        self.assertIsNone(item["image_url"])
        self.assertEqual(self.request("DELETE", f"/api/items/{iid}/images/{images[1]['id']}")[0], 404)

    def test_csv_round_trip(self):
        csv_text = ("Part_Number,Description,Category,Location,Quantity,Unit_Value\n"
                    "P1,\"Bolt, M3\",Fasteners,Bin 1,10,0.05\nP1,,,Bin 2,4,\nP2,Nut,,,20,0.02\n")
        status, body = self.request("POST", "/api/import", raw=csv_text.encode())
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["created"], 2)
        status, body = self.request("GET", "/api/export.csv")
        self.assertIn('P1,"Bolt, M3",Fasteners,Bin 1,10,0.05,0.5', body)
        self.assertIn('P1,"Bolt, M3",Fasteners,Bin 2,4,0.05,0.2', body)
        self.assertIn("P2,Nut,,Main,20,0.02,0.4", body)
        status, body = self.request("GET", "/api/export.csv?category=Fasteners")
        self.assertNotIn("P2", body)

    def test_scan_labels_and_short_links(self):
        self.json("POST", "/api/items", {"part_number": "ABC/1", "category": "Widgets", "quantity": 3, "location": "Tote 1"}, 201)
        loc = self.json("GET", "/api/locations")[0]
        self.assertEqual(loc["code"], "LOC-0001")
        self.assertEqual(self.json("GET", "/api/scan?code=" + urllib.parse.quote("http://h:1/s/LOC-0001")),
                         {"type": "location", "id": loc["id"], "code": "LOC-0001"})
        self.assertEqual(self.request("GET", "/api/scan?code=zzz")[0], 404)
        detail = self.json("GET", f"/api/locations/{loc['id']}")
        self.assertEqual(detail["items"][0]["here"], 3)
        self.assertEqual(self.json("GET", "/api/categories"), [{"name": "Widgets", "parts": 1}])
        self.assertIn("lan_url", self.json("GET", "/api/info"))

        status, body = self.request("GET", "/api/labels.csv?type=locations&base=http://10.0.0.2:8000/")
        self.assertTrue(body.startswith("\ufeffcode,name,notes,link,contents"))
        self.assertIn("LOC-0001,Tote 1,,http://10.0.0.2:8000/s/LOC-0001,ABC/1 x3", body)
        status, body = self.request("GET", "/api/labels.csv?type=parts&base=http://h")
        self.assertIn("ABC/1,ABC/1,,Widgets,http://h/s/ABC%2F1", body)

        # Short links redirect to the page, which looks the code up
        opener = urllib.request.build_opener(NoRedirect)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            opener.open(self.base + "/s/ABC%2F1")
        self.assertEqual(ctx.exception.code, 302)
        self.assertEqual(ctx.exception.headers["Location"], "/#scan/ABC%2F1")

    def test_serves_ui_and_blocks_traversal(self):
        status, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("<title>Inventory</title>", body)
        for path in ("/../inventory/store.py", "/%2e%2e/inventory/store.py", "/uploads/../../etc/passwd",
                     "/uploads/%2e%2e%2f%2e%2e%2fetc%2fpasswd"):
            status, _ = self.request("GET", path)
            self.assertEqual(status, 404, path)


class AuthTests(ServerTestCase):
    def test_everything_needs_sign_in(self):
        anon = self.new_browser()
        self.assertEqual(self.json("GET", "/api/session", opener=anon)["user"], None)
        for method, path in (("GET", "/api/items"), ("GET", "/api/locations/1"), ("GET", "/api/backup.zip"),
                             ("GET", "/api/export.csv"), ("POST", "/api/items"), ("GET", "/uploads/1-x.png")):
            self.assertEqual(self.request(method, path, {} if method == "POST" else None, opener=anon)[0], 401, path)
        self.assertEqual(self.request("GET", "/", opener=anon)[0], 200)
        self.assertEqual(self.json("GET", "/api/session")["user"]["username"], "tester")

    def test_changes_need_the_custom_header(self):
        status, _ = self.request("POST", "/api/items", {"part_number": "X"}, headers={})
        self.assertEqual(status, 403)
        self.assertEqual(self.request("POST", "/api/items", {"part_number": "X"})[0], 201)

    def test_wrong_password_and_lockout(self):
        anon = self.new_browser()
        for _ in range(10):
            status, _ = self.request("POST", "/api/login", {"username": "tester", "password": "nope"}, opener=anon)
            self.assertEqual(status, 401)
        status, body = self.request("POST", "/api/login", {"username": "tester", "password": "password123"}, opener=anon)
        self.assertEqual(status, 429)
        self.assertIn("Too many", body)

    def test_logout(self):
        self.json("POST", "/api/logout")
        self.assertEqual(self.request("GET", "/api/items")[0], 401)

    def test_users_and_passwords(self):
        bob = self.json("POST", "/api/users", {"username": "bob", "password": "bobspassword"}, 201)
        self.assertEqual(self.request("POST", "/api/users", {"username": "BOB", "password": "x" * 8})[0], 400)
        self.assertEqual(self.request("POST", "/api/users", {"username": "eve", "password": "short"})[0], 400)
        self.assertEqual([u["username"] for u in self.json("GET", "/api/users")], ["bob", "tester"])
        me = self.json("GET", "/api/session")["user"]
        self.assertEqual(self.request("DELETE", f"/api/users/{me['id']}")[0], 400)

        # bob signs in elsewhere, then tester changes their own password: tester stays signed in here
        other = self.new_browser()
        self.json("POST", "/api/login", {"username": "tester", "password": "password123"}, opener=other)
        self.assertEqual(self.request("POST", "/api/password", {"current": "wrong", "new": "newpassword1"})[0], 400)
        self.json("POST", "/api/password", {"current": "password123", "new": "newpassword1"})
        self.assertEqual(self.request("GET", "/api/items")[0], 200)
        self.assertEqual(self.request("GET", "/api/items", opener=other)[0], 401)  # signed out elsewhere

        self.json("DELETE", f"/api/users/{bob['id']}", expect=204)
        self.assertEqual(self.request("POST", "/api/login", {"username": "bob", "password": "bobspassword"},
                                      opener=self.new_browser())[0], 401)

    def test_movements_record_who(self):
        item = self.json("POST", "/api/items", {"part_number": "P1", "quantity": 2, "location": "Bin"}, 201)
        self.json("POST", f"/api/items/{item['id']}/book-out", {"location": "Bin", "job": "J1", "quantity": 1})
        self.assertEqual({h["user_name"] for h in self.json("GET", "/api/history")}, {"tester"})
        self.assertEqual(len(self.json("GET", "/api/history?q=tester")), 2)

    def test_backup_zip(self):
        item = self.json("POST", "/api/items", {"part_number": "P1"}, 201)
        self.request("POST", f"/api/items/{item['id']}/images", raw=PNG)
        with self.opener.open(urllib.request.Request(self.base + "/api/backup.zip")) as res:
            self.assertEqual(res.headers["Content-Type"], "application/zip")
            zf = zipfile.ZipFile(io.BytesIO(res.read()))
        names = zf.namelist()
        self.assertIn("inventory.db", names)
        self.assertTrue(any(n.startswith("uploads/1-") for n in names))
        with tempfile.TemporaryDirectory() as tmp:
            zf.extract("inventory.db", tmp)
            db = sqlite3.connect(str(Path(tmp, "inventory.db")))
            self.assertEqual(db.execute("SELECT part_number FROM items").fetchall(), [("P1",)])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)
            db.close()


class FirstRunTests(ServerTestCase):
    sign_in = False

    def test_first_account_created_in_browser_on_this_computer(self):
        session = self.json("GET", "/api/session")
        self.assertEqual((session["needs_setup"], session["can_setup"]), (True, True))
        self.assertEqual(self.request("POST", "/api/setup", {"username": "me", "password": "short"})[0], 400)
        self.json("POST", "/api/setup", {"username": "me", "password": "longenough"}, 201)
        self.assertEqual(self.json("GET", "/api/session")["user"]["username"], "me")
        self.assertEqual(self.request("GET", "/api/items")[0], 200)
        # only the very first account
        self.assertEqual(self.request("POST", "/api/setup", {"username": "x", "password": "longenough"},
                                      opener=self.new_browser())[0], 403)


class BehindProxyTests(ServerTestCase):
    behind_proxy = True
    sign_in = False

    def test_no_browser_setup_and_secure_cookie(self):
        # Behind a proxy every request comes from 127.0.0.1, so browser setup must stay off.
        self.assertFalse(self.json("GET", "/api/session")["can_setup"])
        self.assertEqual(self.request("POST", "/api/setup", {"username": "me", "password": "longenough"})[0], 403)
        self.server.store.set_password("me", "longenough")
        req = urllib.request.Request(
            self.base + "/api/login", data=json.dumps({"username": "me", "password": "longenough"}).encode(),
            method="POST", headers={"X-Requested-With": "inventory", "X-Forwarded-Proto": "https",
                                    "X-Forwarded-For": "203.0.113.9"})
        with urllib.request.urlopen(req) as res:
            cookie = res.headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        self.assertIn("Secure", cookie)

    def test_lockout_is_per_client_address(self):
        self.server.store.set_password("me", "longenough")
        def login(ip, password):
            return self.request("POST", "/api/login", {"username": "me", "password": password}, opener=self.new_browser(),
                                headers={"X-Requested-With": "inventory", "X-Forwarded-For": ip})[0]
        for _ in range(10):
            self.assertEqual(login("198.51.100.1", "wrong"), 401)
        self.assertEqual(login("198.51.100.1", "longenough"), 429)
        self.assertEqual(login("198.51.100.2", "longenough"), 200)


class VersionTests(unittest.TestCase):
    def make_checkout(self, tmp, head="ref: refs/heads/main\n", packed=False):
        git = Path(tmp, ".git")
        (git / "refs" / "heads").mkdir(parents=True)
        (git / "HEAD").write_text(head)
        if packed:
            (git / "packed-refs").write_text("# pack-refs with: peeled\nabc123 refs/heads/main\n")
        else:
            (git / "refs" / "heads" / "main").write_text("abc123\n")
        (git / "config").write_text('[core]\n\tbare = false\n[remote "origin"]\n'
                                    '\turl = https://github.com/Someone/Inventory.git\n\tfetch = +refs/heads/*\n')
        return Path(tmp)

    def test_reads_local_version_and_repo(self):
        from inventory.version import github_repo, local_version
        with tempfile.TemporaryDirectory() as tmp:
            app = self.make_checkout(tmp)
            self.assertEqual(local_version(app), {"commit": "abc123", "branch": "main"})
            self.assertEqual(github_repo(app), "Someone/Inventory")
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(local_version(self.make_checkout(tmp, packed=True))["commit"], "abc123")
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(local_version(Path(tmp)), {"commit": None, "branch": None})

    def test_checker(self):
        from inventory.version import UpdateChecker
        calls = []

        def fetch(url):
            calls.append(url)
            return {"status": "ahead", "ahead_by": 2, "commits": [
                {"sha": "def1", "commit": {"message": "Add photos\n\nlong text", "committer": {"date": "2026-10-01T00:00:00Z"}}},
                {"sha": "def2", "commit": {"message": "Fix labels", "committer": {"date": "2026-10-02T00:00:00Z"}}},
            ]}
        with tempfile.TemporaryDirectory() as tmp:
            checker = UpdateChecker(self.make_checkout(tmp), fetch=fetch)
            result = checker.check()
            self.assertEqual(calls, ["https://api.github.com/repos/Someone/Inventory/compare/abc123...main"])
            self.assertEqual((result["available"], result["behind_by"], result["latest"]), (True, 2, "def2"))
            self.assertEqual([c["message"] for c in result["changes"]], ["Fix labels", "Add photos"])
            checker.check()
            self.assertEqual(len(calls), 1)  # cached
            checker.check(force=True)
            self.assertEqual(len(calls), 2)

            checker = UpdateChecker(Path(tmp), fetch=lambda url: {"status": "identical", "ahead_by": 0, "commits": []})
            self.assertFalse(checker.check()["available"])

            def offline(url):
                raise urllib.error.URLError("no network")
            with self.assertRaises(RuntimeError):
                UpdateChecker(Path(tmp), fetch=offline).check()
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(RuntimeError):
            UpdateChecker(Path(tmp), fetch=fetch).check()  # not a git checkout


class FakeChecker:
    def check(self, force=False):
        return {"available": True, "behind_by": 1, "latest": "zzz", "changes": [{"message": "New", "date": None}]}


class UpdateApiTests(ServerTestCase):
    def setUp(self):
        self.trigger_dir = tempfile.TemporaryDirectory()
        self.trigger = Path(self.trigger_dir.name, "update-requested")
        super().setUp()

    def tearDown(self):
        super().tearDown()
        self.trigger_dir.cleanup()

    def test_update_flow(self):
        self.server.shutdown()
        self.server.server_close()
        self.server = make_server("127.0.0.1", 0, ":memory:", self.tmp.name,
                                  update_trigger=str(self.trigger), checker=FakeChecker())
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.server.store.set_password("tester", "password123")
        self.json("POST", "/api/login", {"username": "tester", "password": "password123"})

        version = self.json("GET", "/api/version")
        self.assertEqual((version["can_update"], version["update_pending"]), (True, False))
        self.assertEqual(self.json("GET", "/api/updates")["changes"][0]["message"], "New")
        self.json("POST", "/api/updates/install", expect=202)
        self.assertIn("Requested by tester", self.trigger.read_text())
        self.assertTrue(self.json("GET", "/api/version")["update_pending"])
        Path(self.trigger_dir.name, "update.log").write_text("=== update started\n=== update finished\n")
        self.assertEqual(self.json("GET", "/api/updates/log")["lines"][-1], "=== update finished")
        self.assertEqual(self.request("POST", "/api/updates/install", opener=self.new_browser())[0], 401)

    def test_without_updater_installed(self):
        self.assertFalse(self.json("GET", "/api/version")["can_update"])
        status, body = self.request("POST", "/api/updates/install")
        self.assertEqual(status, 400)
        self.assertIn("git pull", body)
        self.assertEqual(self.json("GET", "/api/updates/log"), {"lines": []})


if __name__ == "__main__":
    unittest.main()
