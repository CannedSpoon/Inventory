# Inventory

A searchable inventory tracking system for parts. It tracks where each part is kept, which jobs
parts are booked out to, and what everything is worth.

![Parts list](docs/screenshot.png)

## Features

**Parts**
- Each part has a **P/N** (part number), **description**, **unit value** and an optional **picture**.
- Stock is held in one or more **locations** (a shelf, a bin, a van…). The parts list shows how many are at each location, plus the total quantity and value.
- **Search** as you type by P/N, description or location name. Every word must match, and matches are highlighted. Press `/` to jump to the search box.
- **Filter** by location, and **sort** by any column.
- **Pictures:** upload a photo of a part. Phone photos are shrunk before uploading.

**Stock movements** (from a part's row, or by clicking a part to open its details)
- **Receive:** add new stock to a location. Type a new location name to create it.
- **Book out to job:** take stock from a location and book it to a job. Type a new job number to create the job.
- **Move:** transfer stock from one location to another.
- **Count:** set the quantity at a location after a stocktake.
- **Return:** send unused parts from a job back into stock (from the job's page).

You can't take out more than a location holds, and every movement is recorded in **History**.

**Jobs**
- Each job shows the parts booked to it (less any returns), how many, and their value.
- A part's value is fixed at the time it's booked out, so later price changes don't alter old jobs.
- You can book parts out from the job's page, close a finished job (closed jobs can't take more parts), reopen it, and export its parts list to CSV.

![Job page](docs/job.png)

**Locations:** see what each location holds and its value, add notes, rename, or delete (once empty).

**CSV export/import:** export gives one row per part per location. Import adds new part numbers, updates existing ones, and sets the stock count at each row's location.

Works on phones as well as desktops.

## Running it

Requires Python 3.8+ and nothing else.

```bash
python3 run.py
```

Then open <http://127.0.0.1:8000>.

Options:

```bash
python3 run.py --port 9000 --db /path/to/my-inventory.db
python3 run.py --host 0.0.0.0     # reachable from phones/other computers on your network
python3 run.py --uploads /path/to/pictures
```

Data is stored in `inventory.db` and pictures in an `uploads/` folder next to it. Back up both.

**Upgrading from the first version:** your existing database is converted automatically the first time
you start the new version. Existing stock is placed in a location called **Main**; rename it or move
stock to other locations from there.

## CSV import format

The first row must be a header. Column names are case-insensitive; only `part_number` is required.

```csv
part_number,description,location,quantity,unit_value
RES-10K,Resistor 10k ohm 1/4W,Shelf A,500,0.02
RES-10K,,Van 2,50,
MCU-328P,ATmega328P microcontroller,Shelf B,12,2.85
```

- A part number that already exists updates that part; blank cells leave the existing value alone.
- `quantity` sets the count at that `location`. With no location, `Main` is used.
- Rows with errors are skipped and reported.

## API

| Method | Path | Description |
| --- | --- | --- |
| GET | `/api/items?q=&sort=&order=&location_id=` | Search parts, with stock per location and summary totals |
| POST | `/api/items` | Create a part (`part_number`, `description`, `unit_value`, optional `quantity` + `location`) |
| GET / PUT / DELETE | `/api/items/{id}` | Get, update or delete a part |
| POST / DELETE | `/api/items/{id}/image` | Upload (raw image body: JPEG, PNG, GIF or WebP, max 10 MB) or remove the picture |
| POST | `/api/items/{id}/receive` | `{location, quantity, note}` |
| POST | `/api/items/{id}/book-out` | `{location, job, quantity, note}` |
| POST | `/api/items/{id}/return` | `{job, location, quantity, note}` |
| POST | `/api/items/{id}/transfer` | `{from_location, to_location, quantity, note}` |
| POST | `/api/items/{id}/count` | `{location, quantity, note}`: sets the quantity at the location |
| GET | `/api/items/{id}/history` | Movements for a part |
| GET / POST | `/api/locations` | List (with totals) or create locations |
| PUT / DELETE | `/api/locations/{id}` | Rename/edit notes, or delete an empty location |
| GET / POST | `/api/jobs?q=&status=` | List or create jobs |
| GET / PUT / DELETE | `/api/jobs/{id}` | Job with its parts and history; update `number`/`description`/`status`; delete (only if nothing booked) |
| GET | `/api/jobs/{id}/export.csv` | Parts booked to a job as CSV |
| GET | `/api/history?q=` | All stock movements |
| GET | `/api/summary` | Totals for the header |
| GET | `/api/export.csv?q=&location_id=` | Download stock as CSV |
| POST | `/api/import` | Upload CSV text |

## Tests

```bash
python3 -m unittest -v
```
