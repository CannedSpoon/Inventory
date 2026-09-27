import json
import threading
import unittest
import urllib.error
import urllib.request

from inventory.server import make_server
from inventory.store import NotFound, Store, ValidationError


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.store.create({"part_number": "RES-10K", "description": "Resistor 10k ohm 1/4W", "quantity": 500, "unit_value": 0.02})
        self.store.create({"part_number": "CAP-100U", "description": "Capacitor 100uF electrolytic", "quantity": 40, "unit_value": "$0.15"})
        self.store.create({"part_number": "MCU-328P", "description": "ATmega328P microcontroller", "quantity": 3, "unit_value": "2,500.00"})

    def test_total_value_and_summary(self):
        item = self.store.search("MCU")[0]
        self.assertEqual(item["total_value"], 7500.0)
        self.assertEqual(self.store.summary(), {"items": 3, "units": 543, "total_value": 7516.0})

    def test_search_matches_pn_and_description_case_insensitively(self):
        self.assertEqual([i["part_number"] for i in self.store.search("res")], ["RES-10K"])
        self.assertEqual([i["part_number"] for i in self.store.search("capacitor")], ["CAP-100U"])
        # every term must match
        self.assertEqual([i["part_number"] for i in self.store.search("resistor 1/4")], ["RES-10K"])
        self.assertEqual(self.store.search("resistor atmega"), [])

    def test_search_treats_wildcards_literally(self):
        self.assertEqual(self.store.search("%"), [])
        self.assertEqual(self.store.search("_"), [])

    def test_sort(self):
        pns = [i["part_number"] for i in self.store.search(sort="total_value", order="desc")]
        self.assertEqual(pns, ["MCU-328P", "RES-10K", "CAP-100U"])

    def test_duplicate_part_number_rejected(self):
        with self.assertRaises(ValidationError):
            self.store.create({"part_number": "res-10k"})

    def test_validation(self):
        for bad in ({"part_number": ""}, {"part_number": "X", "quantity": -1},
                    {"part_number": "X", "quantity": "abc"}, {"part_number": "X", "unit_value": "-5"}):
            with self.assertRaises(ValidationError):
                self.store.create(bad)

    def test_update_and_adjust(self):
        item_id = self.store.search("MCU")[0]["id"]
        self.assertEqual(self.store.update(item_id, {"description": "Updated"})["description"], "Updated")
        self.assertEqual(self.store.adjust(item_id, 2)["quantity"], 5)
        with self.assertRaises(ValidationError):
            self.store.adjust(item_id, -6)

    def test_delete(self):
        item_id = self.store.search("MCU")[0]["id"]
        self.store.delete(item_id)
        with self.assertRaises(NotFound):
            self.store.get(item_id)

    def test_import_upserts(self):
        result = self.store.import_rows([
            {"part_number": "RES-10K", "description": "Resistor", "quantity": "1000", "unit_value": "0.01"},
            {"part_number": "NEW-1", "description": "New part", "quantity": "5", "unit_value": "1"},
            {"part_number": "", "description": "bad"},
        ])
        self.assertEqual(result["created"], 1)
        self.assertEqual(result["updated"], 1)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(self.store.search("RES-10K")[0]["quantity"], 1000)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.server = make_server("127.0.0.1", 0, ":memory:")
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def request(self, method, path, body=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.base + path, data=data, method=method)
        try:
            with urllib.request.urlopen(req) as res:
                text = res.read().decode()
                return res.status, text
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def test_crud_flow(self):
        status, body = self.request("POST", "/api/items", {"part_number": "ABC-1", "description": "Widget", "quantity": 4, "unit_value": 2.5})
        self.assertEqual(status, 201)
        item = json.loads(body)
        self.assertEqual(item["total_value"], 10.0)

        status, body = self.request("GET", "/api/items?q=widget")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["summary"]["total_value"], 10.0)

        status, body = self.request("POST", f"/api/items/{item['id']}/adjust", {"delta": -1})
        self.assertEqual(json.loads(body)["quantity"], 3)

        status, body = self.request("PUT", f"/api/items/{item['id']}", {"unit_value": 3})
        self.assertEqual(json.loads(body)["total_value"], 9.0)

        status, _ = self.request("DELETE", f"/api/items/{item['id']}")
        self.assertEqual(status, 204)
        status, _ = self.request("GET", f"/api/items/{item['id']}")
        self.assertEqual(status, 404)

    def test_errors_return_json(self):
        status, body = self.request("POST", "/api/items", {"part_number": ""})
        self.assertEqual(status, 400)
        self.assertIn("required", json.loads(body)["error"])

    def test_csv_round_trip(self):
        csv_text = "Part_Number,Description,Quantity,Unit_Value\nP1,\"Bolt, M3\",10,0.05\nP2,Nut,20,0.02\n"
        status, body = self.request("POST", "/api/import", raw=csv_text.encode())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["created"], 2)
        status, body = self.request("GET", "/api/export.csv")
        self.assertEqual(status, 200)
        self.assertIn('P1,"Bolt, M3",10,0.05,0.5', body)

    def test_serves_ui_and_blocks_traversal(self):
        status, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("<title>Inventory</title>", body)
        status, _ = self.request("GET", "/../inventory/store.py")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
