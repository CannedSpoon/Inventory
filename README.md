# Inventory

A simple, searchable inventory tracking system. Each item has a **P/N** (part number),
**description**, **quantity**, and **unit value**. The app calculates each item's total value
(qty × unit value) and the overall inventory totals.

![Screenshot](docs/screenshot.png)

## Features

- **Search** by P/N or description as you type. Search ignores case, every word must match, and matches are highlighted. Press `/` to jump to the search box.
- **Add / edit / delete** items. Part numbers are unique.
- **Quick stock adjust** with the − / + buttons on each row.
- **Sort** by any column (click a header; click again to reverse).
- **Totals** at the top: number of parts, total units, and total inventory value.
- Quantities of zero are shown in red.
- **CSV export** (respects the current search) and **CSV import** (adds new part numbers, updates existing ones).
- Data is stored in a local SQLite file (`inventory.db`).

## Running it

Requires Python 3.8+ and nothing else.

```bash
python3 run.py
```

Then open <http://127.0.0.1:8000>.

Options:

```bash
python3 run.py --port 9000 --db /path/to/my-inventory.db
python3 run.py --host 0.0.0.0     # make it reachable from other machines on your network
```

## CSV import format

The first row must be a header. Column names are case-insensitive; only `part_number` is required.

```csv
part_number,description,quantity,unit_value
RES-10K,Resistor 10k ohm 1/4W,500,0.02
MCU-328P,ATmega328P microcontroller,12,2.85
```

Rows whose part number already exists update that item. Rows with errors are skipped and reported.

## API

| Method | Path | Description |
| --- | --- | --- |
| GET | `/api/items?q=&sort=&order=` | Search items (`sort`: `part_number`, `description`, `quantity`, `unit_value`, `total_value`; `order`: `asc`/`desc`). Returns items plus summary totals. |
| GET | `/api/items/{id}` | Get one item |
| POST | `/api/items` | Create an item (JSON body) |
| PUT | `/api/items/{id}` | Update any fields of an item |
| POST | `/api/items/{id}/adjust` | Change quantity by `{"delta": n}` |
| DELETE | `/api/items/{id}` | Delete an item |
| GET | `/api/export.csv?q=` | Download as CSV |
| POST | `/api/import` | Upload CSV text |

## Tests

```bash
python3 -m unittest -v
```
