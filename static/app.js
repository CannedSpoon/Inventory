"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const money = new Intl.NumberFormat(undefined, { style: "currency", currency: "USD" });
const number = new Intl.NumberFormat();

const state = {
  view: "parts",
  parts: { q: "", sort: "part_number", order: "asc", locationId: "" },
  jobs: { q: "", status: "open" },
  history: { q: "" },
  locations: [],
  detail: null, // function that re-renders the open detail dialog
};

const KIND_LABELS = { receive: "Received", book_out: "Booked out", return: "Returned", transfer: "Moved", count: "Count" };

// --- helpers ---------------------------------------------------------------

async function api(method, path, body, contentType) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.body = contentType ? body : JSON.stringify(body);
    opts.headers["Content-Type"] = contentType || "application/json";
  }
  const res = await fetch(path, opts);
  if (res.status === 204) return null;
  const data = await res.json().catch(() => ({ error: res.statusText }));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// Escape text, then wrap matches of the search words in <mark>.
function highlight(text, q) {
  const safe = esc(text);
  const terms = (q || "").split(/\s+/).filter(Boolean).map((t) => esc(t).replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  if (!terms.length) return safe;
  return safe.replace(new RegExp(`(${terms.join("|")})`, "gi"), "<mark>$1</mark>");
}

function when(utc, withTime = true) {
  const d = new Date(utc.replace(" ", "T") + "Z");
  return withTime ? d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : d.toLocaleDateString();
}

function signed(n) {
  return `<span class="${n < 0 ? "neg" : "pos"}">${n > 0 ? "+" : ""}${number.format(n)}</span>`;
}

function showMessage(text, isError = false) {
  const el = $("#message");
  el.textContent = text;
  el.className = isError ? "error" : "";
  el.hidden = false;
  clearTimeout(el._timer);
  el._timer = setTimeout(() => (el.hidden = true), isError ? 8000 : 4000);
}

function debounce(fn, ms = 150) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

// --- loading ---------------------------------------------------------------

async function refresh() {
  try {
    await loadLookups();
    await loadView();
    if (state.detail) await state.detail();
  } catch (e) {
    showMessage(`Could not load: ${e.message}`, true);
  }
}

async function loadLookups() {
  const [summary, locations, jobs] = await Promise.all([
    api("GET", "/api/summary"),
    api("GET", "/api/locations"),
    api("GET", "/api/jobs?status=open"),
  ]);
  $("#stat-items").textContent = number.format(summary.items);
  $("#stat-units").textContent = number.format(summary.units);
  $("#stat-value").textContent = money.format(summary.total_value);
  $("#stat-jobs").textContent = number.format(summary.open_jobs);

  state.locations = locations;
  $("#dl-locations").innerHTML = locations.map((l) => `<option value="${esc(l.name)}">`).join("");
  $("#dl-jobs").innerHTML = jobs.map((j) => `<option value="${esc(j.number)}">${esc(j.description)}</option>`).join("");
  const filter = $("#location-filter");
  filter.innerHTML =
    `<option value="">All locations</option>` +
    locations.map((l) => `<option value="${l.id}">${esc(l.name)} (${number.format(l.parts)})</option>`).join("");
  if (!locations.some((l) => String(l.id) === state.parts.locationId)) state.parts.locationId = "";
  filter.value = state.parts.locationId;
}

function loadView() {
  return { parts: loadParts, jobs: loadJobs, locations: loadLocations, history: loadHistory }[state.view]();
}

function setEmpty(id, show, text) {
  const el = $(id);
  el.hidden = !show;
  el.textContent = text;
}

// --- parts view ------------------------------------------------------------

async function loadParts() {
  const p = state.parts;
  const params = new URLSearchParams({ q: p.q, sort: p.sort, order: p.order });
  if (p.locationId) params.set("location_id", p.locationId);
  const { items } = await api("GET", `/api/items?${params}`);
  $("#parts-rows").innerHTML = items
    .map(
      (it) => `
    <tr data-id="${it.id}" class="clickable">
      <td class="thumb-col">${thumb(it)}</td>
      <td class="pn">${highlight(it.part_number, p.q)}</td>
      <td class="desc">${highlight(it.description, p.q)}</td>
      <td class="chips">${it.locations
        .map((l) => `<span class="chip">${highlight(l.name, p.q)} <b>${number.format(l.quantity)}</b></span>`)
        .join("")}</td>
      <td class="num qty${it.quantity === 0 ? " low" : ""}">${number.format(it.quantity)}</td>
      <td class="num unit">${money.format(it.unit_value)}</td>
      <td class="num total">${money.format(it.total_value)}</td>
      <td class="actions">
        <button data-action="book_out"${it.quantity ? "" : " disabled"}>Book out</button>
        <button data-action="receive">Receive</button>
      </td>
    </tr>`
    )
    .join("");
  setEmpty("#parts-empty", !items.length,
    p.q || p.locationId ? "No parts match your search." : "No parts yet. Click “+ Add part” to get started.");
  $$("#view-parts th[data-sort]").forEach((th) => {
    th.classList.toggle("sorted-asc", th.dataset.sort === p.sort && p.order === "asc");
    th.classList.toggle("sorted-desc", th.dataset.sort === p.sort && p.order === "desc");
  });
  const exportParams = new URLSearchParams({ q: p.q });
  if (p.locationId) exportParams.set("location_id", p.locationId);
  $("#export-parts").href = `/api/export.csv?${exportParams}`;
}

function thumb(item) {
  return item.image_url ? `<img class="thumb" src="${esc(item.image_url)}" alt="" loading="lazy">` : `<span class="thumb none"></span>`;
}

$("#parts-search").addEventListener("input", debounce((e) => {
  state.parts.q = e.target.value.trim();
  loadParts().catch((err) => showMessage(err.message, true));
}));

$("#location-filter").addEventListener("change", (e) => {
  state.parts.locationId = e.target.value;
  loadParts().catch((err) => showMessage(err.message, true));
});

$("#view-parts thead").addEventListener("click", (e) => {
  const key = e.target.dataset.sort;
  if (!key) return;
  const p = state.parts;
  p.order = p.sort === key && p.order === "asc" ? "desc" : "asc";
  p.sort = key;
  loadParts().catch((err) => showMessage(err.message, true));
});

$("#parts-rows").addEventListener("click", async (e) => {
  const row = e.target.closest("tr");
  if (!row) return;
  const id = row.dataset.id;
  const action = e.target.closest("button")?.dataset.action;
  try {
    if (action) stockDialog(action, await api("GET", `/api/items/${id}`));
    else openPart(id);
  } catch (err) {
    showMessage(err.message, true);
  }
});

$("#add-part").addEventListener("click", () => partForm(null));

$("#import-file").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  e.target.value = "";
  if (!file) return;
  try {
    const r = await api("POST", "/api/import", await file.text(), "text/csv");
    let msg = `Imported: ${r.created} added, ${r.updated} updated.`;
    if (r.errors.length) msg += ` ${r.errors.length} skipped: ${r.errors.slice(0, 3).join("; ")}`;
    showMessage(msg, r.errors.length > 0);
    refresh();
  } catch (err) {
    showMessage(`Import failed: ${err.message}`, true);
  }
});

// --- jobs view -------------------------------------------------------------

async function loadJobs() {
  const j = state.jobs;
  const jobs = await api("GET", `/api/jobs?${new URLSearchParams({ q: j.q, status: j.status })}`);
  $("#jobs-rows").innerHTML = jobs
    .map(
      (job) => `
    <tr data-id="${job.id}" class="clickable">
      <td class="pn">${highlight(job.number, j.q)}</td>
      <td class="desc">${highlight(job.description, j.q)}</td>
      <td>${statusBadge(job.status)}</td>
      <td class="num">${number.format(job.units)}</td>
      <td class="num">${money.format(job.total_value)}</td>
      <td>${when(job.created_at, false)}</td>
    </tr>`
    )
    .join("");
  setEmpty("#jobs-empty", !jobs.length,
    j.q ? "No jobs match your search." : `No ${j.status || ""} jobs. Jobs are created when you book parts out, or with “+ New job”.`);
}

function statusBadge(status) {
  return `<span class="badge ${status}">${status === "open" ? "Open" : "Closed"}</span>`;
}

$("#jobs-search").addEventListener("input", debounce((e) => {
  state.jobs.q = e.target.value.trim();
  loadJobs().catch((err) => showMessage(err.message, true));
}));

$("#jobs-status").addEventListener("change", (e) => {
  state.jobs.status = e.target.value;
  loadJobs().catch((err) => showMessage(err.message, true));
});

$("#jobs-rows").addEventListener("click", (e) => {
  const row = e.target.closest("tr");
  if (row) openJob(row.dataset.id);
});

$("#add-job").addEventListener("click", () => jobForm(null));

// --- locations view --------------------------------------------------------

async function loadLocations() {
  const locs = state.locations;
  $("#locations-rows").innerHTML = locs
    .map(
      (l) => `
    <tr data-id="${l.id}">
      <td class="pn">${esc(l.name)}</td>
      <td class="desc">${esc(l.notes)}</td>
      <td class="num">${number.format(l.parts)}</td>
      <td class="num">${number.format(l.units)}</td>
      <td class="num">${money.format(l.total_value)}</td>
      <td class="actions">
        <button data-action="view">View parts</button>
        <button data-action="edit">Edit</button>
        <button data-action="delete" title="Delete">✕</button>
      </td>
    </tr>`
    )
    .join("");
  setEmpty("#locations-empty", !locs.length,
    "No locations yet. Add one here, or type a new location name when you receive stock.");
}

$("#locations-rows").addEventListener("click", async (e) => {
  const action = e.target.closest("button")?.dataset.action;
  if (!action) return;
  const loc = state.locations.find((l) => String(l.id) === e.target.closest("tr").dataset.id);
  if (action === "view") {
    state.parts.locationId = String(loc.id);
    location.hash = "parts";
  } else if (action === "edit") {
    locationForm(loc);
  } else if (action === "delete") {
    if (!confirm(`Delete location ${loc.name}?`)) return;
    try {
      await api("DELETE", `/api/locations/${loc.id}`);
      showMessage(`Deleted ${loc.name}.`);
      refresh();
    } catch (err) {
      showMessage(err.message, true);
    }
  }
});

$("#add-location").addEventListener("click", () => locationForm(null));

// --- history view ----------------------------------------------------------

async function loadHistory() {
  const rows = await api("GET", `/api/history?${new URLSearchParams({ q: state.history.q })}`);
  $("#history-rows").innerHTML = historyRows(rows, true, state.history.q);
  setEmpty("#history-empty", !rows.length, state.history.q ? "Nothing matches your search." : "No stock movements yet.");
}

function historyRows(rows, showPart, q = "") {
  return rows
    .map(
      (h) => `
    <tr>
      <td class="nowrap">${when(h.created_at)}</td>
      <td><span class="kind ${h.kind}">${KIND_LABELS[h.kind] || esc(h.kind)}</span></td>
      ${showPart ? `<td class="pn">${highlight(h.part_number, q)}</td>` : ""}
      <td>${highlight(h.location, q)}</td>
      <td>${h.job ? `<a href="#" data-job="${h.job_id}">${highlight(h.job, q)}</a>` : ""}</td>
      <td class="num">${signed(h.quantity)}</td>
      <td class="num">${money.format(Math.abs(h.value))}</td>
      <td class="desc">${highlight(h.note, q)}</td>
    </tr>`
    )
    .join("");
}

$("#history-search").addEventListener("input", debounce((e) => {
  state.history.q = e.target.value.trim();
  loadHistory().catch((err) => showMessage(err.message, true));
}));

// Job links in any history table open that job.
document.addEventListener("click", (e) => {
  const link = e.target.closest("a[data-job]");
  if (!link) return;
  e.preventDefault();
  openJob(link.dataset.job);
});

// --- generic form dialog -----------------------------------------------------

function fieldHtml(f) {
  const attrs = [
    `name="${f.name}"`,
    f.required ? "required" : "",
    f.min !== undefined ? `min="${f.min}"` : "",
    f.max !== undefined ? `max="${f.max}"` : "",
    f.step ? `step="${f.step}"` : "",
    f.list ? `list="${f.list}"` : "",
    f.placeholder ? `placeholder="${esc(f.placeholder)}"` : "",
    f.readonly ? "readonly" : "",
    f.autofocus ? "autofocus" : "",
    `autocomplete="off"`,
  ].join(" ");
  let input;
  if (f.type === "textarea") input = `<textarea ${attrs} rows="3">${esc(f.value)}</textarea>`;
  else if (f.type === "select")
    input = `<select ${attrs}>${(f.options || [])
      .map((o) => `<option value="${esc(o.value)}"${o.value === f.value ? " selected" : ""}>${esc(o.label)}</option>`)
      .join("")}</select>`;
  else input = `<input type="${f.type || "text"}" ${attrs} value="${esc(f.value ?? "")}">`;
  return `<label data-field="${f.name}">${esc(f.label)}${input}${f.hint ? `<small>${esc(f.hint)}</small>` : ""}</label>`;
}

function formDialog({ title, intro = "", fields, submitLabel = "Save", onSubmit, setup }) {
  const dlg = $("#form-dialog");
  dlg.innerHTML = `
    <form>
      <h2>${esc(title)}</h2>
      ${intro}
      ${fields.map(fieldHtml).join("")}
      <p class="error" hidden></p>
      <div class="actions">
        <button type="button" data-close>Cancel</button>
        <button type="submit" class="primary">${esc(submitLabel)}</button>
      </div>
    </form>`;
  const form = $("form", dlg);
  $("[data-close]", form).addEventListener("click", () => dlg.close());
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const submit = $("button[type=submit]", form);
    submit.disabled = true;
    try {
      await onSubmit(Object.fromEntries(new FormData(form)), form);
      dlg.close();
    } catch (err) {
      const box = $(".error", form);
      box.textContent = err.message;
      box.hidden = false;
    } finally {
      submit.disabled = false;
    }
  });
  if (setup) setup(form);
  dlg.showModal();
  ($("[autofocus]", form) || $("input:not([readonly]), select, textarea", form))?.focus();
}

// --- parts: add / edit / stock actions -------------------------------------

function partForm(item) {
  const fields = [
    { name: "part_number", label: "P/N", required: true, value: item?.part_number, autofocus: true },
    { name: "description", label: "Description", type: "textarea", value: item?.description },
    { name: "unit_value", label: "Unit value ($)", type: "number", min: 0, step: "0.01", value: item?.unit_value ?? 0, required: true },
  ];
  if (!item) {
    fields.push(
      { name: "quantity", label: "Starting quantity", type: "number", min: 0, step: 1, value: 0 },
      { name: "location", label: "Location", list: "dl-locations", placeholder: "e.g. Shelf A, Van 2",
        hint: "Where the starting quantity is kept. Type a new name to create a location." }
    );
  }
  formDialog({
    title: item ? `Edit ${item.part_number}` : "Add part",
    fields,
    onSubmit: async (data) => {
      const saved = item ? await api("PUT", `/api/items/${item.id}`, data) : await api("POST", "/api/items", data);
      showMessage(`Saved ${saved.part_number}.`);
      refresh();
    },
  });
}

const STOCK_ACTIONS = {
  receive: { path: "receive", title: "Receive stock", submit: "Receive" },
  book_out: { path: "book-out", title: "Book out to job", submit: "Book out" },
  return: { path: "return", title: "Return from job", submit: "Return to stock" },
  transfer: { path: "transfer", title: "Move stock", submit: "Move" },
  count: { path: "count", title: "Stock count", submit: "Save count" },
};

function locationOptions(item) {
  return item.locations.map((l) => ({ value: l.name, label: `${l.name} (${number.format(l.quantity)} available)` }));
}

// Open a stock action. With item = null (booking from a job) the part is chosen in the form.
async function stockDialog(mode, item, preset = {}) {
  const cfg = STOCK_ACTIONS[mode];
  const fields = [];
  let parts = [];
  if (!item) {
    parts = (await api("GET", "/api/items")).items.filter((p) => p.quantity > 0);
    $("#dl-parts").innerHTML = parts.map((p) => `<option value="${esc(p.part_number)}">${esc(p.description)}</option>`).join("");
    fields.push({ name: "part", label: "P/N", list: "dl-parts", required: true, autofocus: true, placeholder: "Start typing a P/N…" });
  }
  const atLocation = (it, name) => it?.locations.find((l) => l.name === name)?.quantity ?? 0;
  const firstLoc = preset.location || item?.locations[0]?.name || "";

  if (mode === "receive") {
    fields.push({ name: "location", label: "Location", list: "dl-locations", required: true, value: preset.location || "",
                  autofocus: true, hint: "Type a new name to create a location." });
  } else if (mode === "book_out") {
    fields.push(
      { name: "location", label: "From location", type: "select", required: true, value: firstLoc, options: item ? locationOptions(item) : [] },
      { name: "job", label: "Job", list: "dl-jobs", required: true, value: preset.job || "", readonly: !!preset.job,
        autofocus: !!item && !preset.job, hint: preset.job ? "" : "Pick an open job, or type a new job number to create it." }
    );
  } else if (mode === "return") {
    fields.push(
      { name: "job", label: "Job", value: preset.job, readonly: true },
      { name: "location", label: "Return to location", list: "dl-locations", required: true, value: firstLoc, autofocus: true }
    );
  } else if (mode === "transfer") {
    fields.push(
      { name: "from_location", label: "From", type: "select", required: true, value: firstLoc, options: locationOptions(item) },
      { name: "to_location", label: "To", list: "dl-locations", required: true, autofocus: true,
        hint: "Type a new name to create a location." }
    );
  } else if (mode === "count") {
    fields.push({ name: "location", label: "Location", list: "dl-locations", required: true, value: firstLoc });
  }
  fields.push(
    mode === "count"
      ? { name: "quantity", label: "Counted quantity", type: "number", min: 0, step: 1, required: true, value: atLocation(item, firstLoc) }
      : { name: "quantity", label: "Quantity", type: "number", min: 1, step: 1, required: true, value: 1, max: preset.max },
    { name: "note", label: "Note (optional)" }
  );

  const intro = item
    ? `<p class="subject">${thumb(item)}<span><b>${esc(item.part_number)}</b> ${esc(item.description)}</span></p>`
    : "";
  formDialog({
    title: cfg.title,
    intro,
    fields,
    submitLabel: cfg.submit,
    setup: (form) => {
      const f = form.elements;
      // Keep the quantity limit in line with what's at the chosen location.
      const sync = () => {
        if (mode === "book_out" || mode === "transfer") {
          const loc = (mode === "transfer" ? f.from_location : f.location).value;
          f.quantity.max = atLocation(item, loc) || "";
        }
        if (mode === "count") f.quantity.value = atLocation(item, f.location.value);
      };
      if (f.part) {
        const pickPart = () => {
          const picked = parts.find((p) => p.part_number.toLowerCase() === f.part.value.trim().toLowerCase()) || null;
          if (picked === item) return; // change also fires on blur; don't reset the chosen location
          item = picked;
          const previous = f.location.value;
          f.location.innerHTML = item
            ? locationOptions(item).map((o) => `<option value="${esc(o.value)}">${esc(o.label)}</option>`).join("")
            : "";
          if (item?.locations.some((l) => l.name === previous)) f.location.value = previous;
          sync();
        };
        f.part.addEventListener("input", pickPart);
        f.part.addEventListener("change", pickPart);
      }
      (f.from_location || f.location)?.addEventListener(mode === "count" ? "change" : "input", sync);
      sync();
    },
    onSubmit: async (data) => {
      if (!item) throw new Error("Pick a part from the list");
      delete data.part;
      const updated = await api("POST", `/api/items/${item.id}/${cfg.path}`, data);
      const verb = { receive: "Received", book_out: "Booked out", return: "Returned", transfer: "Moved", count: "Counted" }[mode];
      showMessage(`${verb} ${data.quantity} × ${updated.part_number}${data.job ? ` (job ${data.job})` : ""}.`);
      refresh();
    },
  });
}

// --- part details ----------------------------------------------------------

async function openPart(id) {
  const dlg = $("#detail-dialog");
  const render = async () => {
    const [item, history] = await Promise.all([api("GET", `/api/items/${id}`), api("GET", `/api/items/${id}/history`)]);
    dlg.innerHTML = `
      <div class="detail">
        <button class="close" data-act="close" aria-label="Close">✕</button>
        <div class="detail-head">
          <div class="photo">
            ${item.image_url
              ? `<a href="${esc(item.image_url)}" target="_blank" rel="noopener"><img src="${esc(item.image_url)}" alt="Picture of ${esc(item.part_number)}"></a>`
              : `<div class="no-photo">No picture</div>`}
            <div class="photo-actions">
              <button data-act="upload">${item.image_url ? "Change picture" : "Add picture"}</button>
              ${item.image_url ? `<button data-act="remove-image">Remove</button>` : ""}
            </div>
          </div>
          <div class="info">
            <h2 class="pn">${esc(item.part_number)}</h2>
            <p class="description">${esc(item.description) || "<em>No description</em>"}</p>
            <dl>
              <div><dt>Quantity</dt><dd class="${item.quantity ? "" : "low"}">${number.format(item.quantity)}</dd></div>
              <div><dt>Unit value</dt><dd>${money.format(item.unit_value)}</dd></div>
              <div><dt>Total value</dt><dd>${money.format(item.total_value)}</dd></div>
            </dl>
            <div class="button-row">
              <button class="primary" data-act="book_out"${item.quantity ? "" : " disabled"}>Book out to job</button>
              <button data-act="receive">Receive stock</button>
              <button data-act="transfer"${item.quantity ? "" : " disabled"}>Move</button>
              <button data-act="edit">Edit</button>
              <button data-act="delete" class="danger">Delete</button>
            </div>
          </div>
        </div>

        <h3>Stock by location</h3>
        ${item.locations.length
          ? `<div class="table-wrap"><table>
              <thead><tr><th>Location</th><th class="num">Qty</th><th class="num">Value</th><th></th></tr></thead>
              <tbody>${item.locations
                .map(
                  (l) => `<tr data-loc="${esc(l.name)}">
                    <td>${esc(l.name)}</td>
                    <td class="num">${number.format(l.quantity)}</td>
                    <td class="num">${money.format(l.quantity * item.unit_value)}</td>
                    <td class="actions">
                      <button data-act="book_out">Book out</button>
                      <button data-act="transfer">Move</button>
                      <button data-act="count">Count</button>
                    </td></tr>`
                )
                .join("")}</tbody></table></div>`
          : `<p class="muted">Not in stock anywhere. Use “Receive stock” to add some.</p>`}

        <h3>History</h3>
        ${history.length
          ? `<div class="table-wrap"><table>
              <thead><tr><th>When</th><th>Action</th><th>Location</th><th>Job</th><th class="num">Qty</th><th class="num">Value</th><th>Note</th></tr></thead>
              <tbody>${historyRows(history, false)}</tbody></table></div>`
          : `<p class="muted">No movements yet.</p>`}
      </div>`;

    dlg.onclick = async (e) => {
      const act = e.target.closest("button")?.dataset.act;
      if (!act) return;
      const loc = e.target.closest("tr")?.dataset.loc;
      try {
        if (act === "close") dlg.close();
        else if (STOCK_ACTIONS[act]) stockDialog(act, item, { location: loc });
        else if (act === "edit") partForm(item);
        else if (act === "upload") pickImage(item);
        else if (act === "remove-image") {
          if (!confirm("Remove this picture?")) return;
          await api("DELETE", `/api/items/${item.id}/image`);
          refresh();
        } else if (act === "delete") {
          if (!confirm(`Delete ${item.part_number}? Its stock records are removed; job history is kept.`)) return;
          await api("DELETE", `/api/items/${item.id}`);
          dlg.close();
          showMessage(`Deleted ${item.part_number}.`);
          refresh();
        }
      } catch (err) {
        showMessage(err.message, true);
      }
    };
  };
  await showDetail(render);
}

async function showDetail(render) {
  const dlg = $("#detail-dialog");
  try {
    await render();
  } catch (err) {
    showMessage(err.message, true);
    return;
  }
  state.detail = render;
  if (!dlg.open) dlg.showModal();
}

$("#detail-dialog").addEventListener("close", () => (state.detail = null));
// Clicking the backdrop (outside the panel) closes the details.
$("#detail-dialog").addEventListener("click", (e) => {
  if (e.target === e.currentTarget) e.currentTarget.close();
});

// --- pictures --------------------------------------------------------------

let imageTarget = null;

function pickImage(item) {
  imageTarget = item;
  $("#image-file").click();
}

$("#image-file").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  e.target.value = "";
  if (!file || !imageTarget) return;
  try {
    const blob = await shrinkImage(file);
    await api("POST", `/api/items/${imageTarget.id}/image`, blob, blob.type || "application/octet-stream");
    showMessage(`Picture saved for ${imageTarget.part_number}.`);
    refresh();
  } catch (err) {
    showMessage(`Picture upload failed: ${err.message}`, true);
  }
});

// Phone photos are large: scale down to 1600px and re-encode as JPEG before uploading.
async function shrinkImage(file, max = 1600) {
  if (file.type === "image/gif") return file;
  try {
    const bmp = await createImageBitmap(file);
    const scale = Math.min(1, max / Math.max(bmp.width, bmp.height));
    if (scale === 1 && file.size < 1_500_000 && /^image\/(jpeg|png|webp)$/.test(file.type)) return file;
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(bmp.width * scale);
    canvas.height = Math.round(bmp.height * scale);
    const ctx = canvas.getContext("2d");
    ctx.fillStyle = "#fff";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(bmp, 0, 0, canvas.width, canvas.height);
    return await new Promise((resolve) => canvas.toBlob((b) => resolve(b || file), "image/jpeg", 0.85));
  } catch {
    return file;
  }
}

// --- job details -----------------------------------------------------------

async function openJob(id) {
  const dlg = $("#detail-dialog");
  const render = async () => {
    const job = await api("GET", `/api/jobs/${id}`);
    const isOpen = job.status === "open";
    dlg.innerHTML = `
      <div class="detail">
        <button class="close" data-act="close" aria-label="Close">✕</button>
        <div class="info">
          <h2><span class="pn">Job ${esc(job.number)}</span> ${statusBadge(job.status)}</h2>
          <p class="description">${esc(job.description) || "<em>No description</em>"}</p>
          <dl>
            <div><dt>Parts booked</dt><dd>${number.format(job.units)}</dd></div>
            <div><dt>Value</dt><dd>${money.format(job.total_value)}</dd></div>
            <div><dt>Created</dt><dd>${when(job.created_at, false)}</dd></div>
          </dl>
          <div class="button-row">
            ${isOpen ? `<button class="primary" data-act="book">Book part out</button>` : ""}
            <button data-act="edit">Edit</button>
            <button data-act="status">${isOpen ? "Close job" : "Reopen job"}</button>
            <a class="button" href="/api/jobs/${job.id}/export.csv">Export CSV</a>
            ${job.history.length ? "" : `<button data-act="delete" class="danger">Delete</button>`}
          </div>
        </div>

        <h3>Parts on this job</h3>
        ${job.parts.length
          ? `<div class="table-wrap"><table>
              <thead><tr><th>P/N</th><th>Description</th><th class="num">Qty</th><th class="num">Value</th><th></th></tr></thead>
              <tbody>${job.parts
                .map(
                  (p) => `<tr data-item="${p.item_id ?? ""}" data-qty="${p.quantity}">
                    <td class="pn">${esc(p.part_number)}</td>
                    <td class="desc">${esc(p.description)}</td>
                    <td class="num">${number.format(p.quantity)}</td>
                    <td class="num">${money.format(p.total_value)}</td>
                    <td class="actions">${p.item_id && p.quantity > 0 ? `<button data-act="return">Return</button>` : ""}</td>
                  </tr>`
                )
                .join("")}</tbody>
              <tfoot><tr><td colspan="2">Total</td><td class="num">${number.format(job.units)}</td><td class="num">${money.format(job.total_value)}</td><td></td></tr></tfoot>
            </table></div>`
          : `<p class="muted">No parts booked to this job yet.</p>`}

        <h3>History</h3>
        ${job.history.length
          ? `<div class="table-wrap"><table>
              <thead><tr><th>When</th><th>Action</th><th>P/N</th><th>Location</th><th>Job</th><th class="num">Qty</th><th class="num">Value</th><th>Note</th></tr></thead>
              <tbody>${historyRows(job.history, true)}</tbody></table></div>`
          : `<p class="muted">Nothing booked yet.</p>`}
      </div>`;

    dlg.onclick = async (e) => {
      const act = e.target.closest("button")?.dataset.act;
      if (!act) return;
      try {
        if (act === "close") dlg.close();
        else if (act === "book") stockDialog("book_out", null, { job: job.number });
        else if (act === "edit") jobForm(job);
        else if (act === "return") {
          const row = e.target.closest("tr");
          const item = await api("GET", `/api/items/${row.dataset.item}`);
          stockDialog("return", item, { job: job.number, max: Number(row.dataset.qty) });
        } else if (act === "status") {
          await api("PUT", `/api/jobs/${job.id}`, { status: isOpen ? "closed" : "open" });
          showMessage(`Job ${job.number} ${isOpen ? "closed" : "reopened"}.`);
          refresh();
        } else if (act === "delete") {
          if (!confirm(`Delete job ${job.number}?`)) return;
          await api("DELETE", `/api/jobs/${job.id}`);
          dlg.close();
          showMessage(`Deleted job ${job.number}.`);
          refresh();
        }
      } catch (err) {
        showMessage(err.message, true);
      }
    };
  };
  await showDetail(render);
}

function jobForm(job) {
  formDialog({
    title: job ? `Edit job ${job.number}` : "New job",
    fields: [
      { name: "number", label: "Job number", required: true, value: job?.number, autofocus: true },
      { name: "description", label: "Description", type: "textarea", value: job?.description,
        placeholder: "Customer, site, work to be done…" },
    ],
    onSubmit: async (data) => {
      const saved = job ? await api("PUT", `/api/jobs/${job.id}`, data) : await api("POST", "/api/jobs", data);
      showMessage(`Saved job ${saved.number}.`);
      if (!job) {
        await refresh();
        openJob(saved.id);
      } else refresh();
    },
  });
}

function locationForm(loc) {
  formDialog({
    title: loc ? `Edit ${loc.name}` : "Add location",
    fields: [
      { name: "name", label: "Name", required: true, value: loc?.name, autofocus: true, placeholder: "e.g. Shelf A, Van 2, Bin 14" },
      { name: "notes", label: "Notes", type: "textarea", value: loc?.notes },
    ],
    onSubmit: async (data) => {
      const saved = loc ? await api("PUT", `/api/locations/${loc.id}`, data) : await api("POST", "/api/locations", data);
      showMessage(`Saved ${saved.name}.`);
      refresh();
    },
  });
}

// --- navigation ------------------------------------------------------------

function setView() {
  const view = location.hash.slice(1);
  state.view = ["parts", "jobs", "locations", "history"].includes(view) ? view : "parts";
  $$(".view").forEach((s) => (s.hidden = s.id !== `view-${state.view}`));
  $$(".tabs button").forEach((b) => b.setAttribute("aria-selected", b.dataset.view === state.view));
  refresh();
}

$$(".tabs button").forEach((b) => b.addEventListener("click", () => (location.hash = b.dataset.view)));
window.addEventListener("hashchange", setView);

// "/" focuses the search box on the current tab.
document.addEventListener("keydown", (e) => {
  if (e.key !== "/" || ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName) || $("dialog[open]")) return;
  const search = $(`#view-${state.view} .search`);
  if (search) {
    e.preventDefault();
    search.focus();
  }
});

setView();
