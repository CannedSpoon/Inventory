"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const money = new Intl.NumberFormat(undefined, { style: "currency", currency: "USD" });
const number = new Intl.NumberFormat();

const state = {
  view: "parts",
  parts: { q: "", sort: "part_number", order: "asc", locationId: "", category: "" },
  lastParts: [],
  jobs: { q: "", status: "open" },
  history: { q: "" },
  locations: [],
  detail: null, // function that re-renders the open detail dialog
};

const KIND_LABELS = { receive: "Received", book_out: "Booked out", return: "Returned", transfer: "Moved", count: "Count" };

// --- helpers ---------------------------------------------------------------

async function api(method, path, body, contentType) {
  const opts = { method, headers: { "X-Requested-With": "inventory" } };
  if (body !== undefined) {
    opts.body = contentType ? body : JSON.stringify(body);
    opts.headers["Content-Type"] = contentType || "application/json";
  }
  const res = await fetch(path, opts);
  if (res.status === 204) return null;
  const data = await res.json().catch(() => ({ error: res.statusText }));
  if (res.status === 401 && !path.startsWith("/api/login")) showLogin();
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
  const [summary, locations, jobs, categories] = await Promise.all([
    api("GET", "/api/summary"),
    api("GET", "/api/locations"),
    api("GET", "/api/jobs?status=open"),
    api("GET", "/api/categories"),
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

  $("#dl-categories").innerHTML = categories.map((c) => `<option value="${esc(c.name)}">`).join("");
  const catFilter = $("#category-filter");
  catFilter.innerHTML =
    `<option value="">All categories</option>` +
    categories.map((c) => `<option value="${esc(c.name)}">${esc(c.name)} (${number.format(c.parts)})</option>`).join("") +
    `<option value="__none__">No category</option>`;
  const cat = state.parts.category;
  if (cat && cat !== "__none__" && !categories.some((c) => c.name.toLowerCase() === cat.toLowerCase())) state.parts.category = "";
  catFilter.value = state.parts.category;
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
  if (p.category) params.set("category", p.category);
  const { items } = await api("GET", `/api/items?${params}`);
  state.lastParts = items;
  $("#parts-rows").innerHTML = items
    .map(
      (it) => `
    <tr data-id="${it.id}" class="clickable">
      <td class="thumb-col">${thumb(it)}</td>
      <td class="pn">${highlight(it.part_number, p.q)}</td>
      <td class="desc">${highlight(it.description, p.q)}</td>
      <td class="cat">${it.category ? `<span class="tag">${highlight(it.category, p.q)}</span>` : ""}</td>
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
    p.q || p.locationId || p.category ? "No parts match your search." : "No parts yet. Click “+ Add part” to get started.");
  $$("#view-parts th[data-sort]").forEach((th) => {
    th.classList.toggle("sorted-asc", th.dataset.sort === p.sort && p.order === "asc");
    th.classList.toggle("sorted-desc", th.dataset.sort === p.sort && p.order === "desc");
  });
  const exportParams = new URLSearchParams({ q: p.q });
  if (p.locationId) exportParams.set("location_id", p.locationId);
  if (p.category) exportParams.set("category", p.category);
  $("#export-parts").href = `/api/export.csv?${exportParams}`;
}

function thumb(item) {
  return item.image_url ? `<img class="thumb" src="${esc(item.image_url)}" alt="" loading="lazy">` : `<span class="thumb none"></span>`;
}

$("#parts-search").addEventListener("input", debounce((e) => {
  state.parts.q = e.target.value.trim();
  loadParts().catch((err) => showMessage(err.message, true));
}));

$("#category-filter").addEventListener("change", (e) => {
  state.parts.category = e.target.value;
  loadParts().catch((err) => showMessage(err.message, true));
});

$("#label-parts").addEventListener("click", () => {
  if (!state.lastParts.length) return showMessage("No parts listed to print labels for.", true);
  labelDialog("parts", state.lastParts.map(partLabel));
});

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
    <tr data-id="${l.id}" class="clickable">
      <td class="pn">${esc(l.name)}</td>
      <td class="code">${esc(l.code)}</td>
      <td class="desc">${esc(l.notes)}</td>
      <td class="num">${number.format(l.parts)}</td>
      <td class="num">${number.format(l.units)}</td>
      <td class="num">${money.format(l.total_value)}</td>
      <td class="actions">
        <button data-action="label">Label</button>
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
  const row = e.target.closest("tr");
  if (!row) return;
  const action = e.target.closest("button")?.dataset.action;
  const loc = state.locations.find((l) => String(l.id) === row.dataset.id);
  if (!action) {
    openLocation(loc.id);
  } else if (action === "label") {
    labelDialog("locations", [locationLabel(loc)]);
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
$("#label-locations").addEventListener("click", () => {
  if (!state.locations.length) return showMessage("Add a location first.", true);
  labelDialog("locations", state.locations.map(locationLabel));
});

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
      <td>${highlight(h.user_name, q)}</td>
    </tr>`
    )
    .join("");
}

$("#history-search").addEventListener("input", debounce((e) => {
  state.history.q = e.target.value.trim();
  loadHistory().catch((err) => showMessage(err.message, true));
}));

// Job and location links anywhere open that job or location.
document.addEventListener("click", (e) => {
  const link = e.target.closest("a[data-job], a[data-location]");
  if (!link) return;
  e.preventDefault();
  if (link.dataset.job) openJob(link.dataset.job);
  else openLocation(link.dataset.location);
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
    f.type === "password" ? "" : `autocomplete="off"`,
  ].join(" ");
  let input;
  if (f.type === "textarea") input = `<textarea ${attrs} rows="3">${esc(f.value)}</textarea>`;
  else if (f.type === "select")
    input = `<select ${attrs}>${(f.options || [])
      .map((o) => `<option value="${esc(o.value)}"${o.value === f.value ? " selected" : ""}>${esc(o.label)}</option>`)
      .join("")}</select>`;
  else input = `<input type="${f.type || "text"}" ${attrs} value="${esc(f.value ?? "")}"${f.type === "password" ? ' autocomplete="new-password"' : ""}>`;
  return `<label data-field="${f.name}">${esc(f.label)}${input}${f.hint ? `<small>${esc(f.hint)}</small>` : ""}</label>`;
}

function formDialog({ title, intro = "", fields, submitLabel = "Save", onSubmit, setup, extraButtons = [], wide = false }) {
  const dlg = $("#form-dialog");
  dlg.innerHTML = `
    <form>
      <h2>${esc(title)}</h2>
      ${intro}
      ${fields.map(fieldHtml).join("")}
      <p class="error" hidden></p>
      <div class="actions">
        ${extraButtons.map((b, i) => `<button type="button" data-extra="${i}">${esc(b.label)}</button>`).join("")}
        <button type="button" data-close>Cancel</button>
        <button type="submit" class="primary">${esc(submitLabel)}</button>
      </div>
    </form>`;
  dlg.classList.toggle("medium", wide);
  const form = $("form", dlg);
  $$("[data-extra]", form).forEach((btn) =>
    btn.addEventListener("click", async () => {
      const box = $(".error", form);
      box.hidden = true;
      try {
        await extraButtons[btn.dataset.extra].onClick(Object.fromEntries(new FormData(form)), form);
      } catch (err) {
        box.textContent = err.message;
        box.hidden = false;
      }
    })
  );
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
    { name: "category", label: "Category", list: "dl-categories", value: item?.category,
      placeholder: "e.g. Resistors, Fasteners", hint: "Pick one, or type a new category." },
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
    parts = (await api("GET", "/api/items")).items.filter((p) => mode === "receive" || p.quantity > 0);
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
          if (f.location.tagName !== "SELECT") return;
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
            <p class="description">${item.category ? `<span class="tag">${esc(item.category)}</span> ` : ""}${esc(item.description) || "<em>No description</em>"}</p>
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
              <button data-act="label">Print label</button>
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
                    <td><a href="#" data-location="${l.location_id}">${esc(l.name)}</a></td>
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
              <thead><tr><th>When</th><th>Action</th><th>Location</th><th>Job</th><th class="num">Qty</th><th class="num">Value</th><th>Note</th><th>By</th></tr></thead>
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
        else if (act === "label") labelDialog("parts", [partLabel(item)]);
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
              <thead><tr><th>When</th><th>Action</th><th>P/N</th><th>Location</th><th>Job</th><th class="num">Qty</th><th class="num">Value</th><th>Note</th><th>By</th></tr></thead>
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
      { name: "code", label: "Label code", value: loc?.code, placeholder: "Automatic, e.g. LOC-0007",
        hint: "What the barcode holds. Letters, numbers, - _ . / only. Changing it means reprinting the label." },
      { name: "notes", label: "Notes", type: "textarea", value: loc?.notes },
    ],
    onSubmit: async (data) => {
      if (loc && !data.code.trim()) delete data.code; // keep the existing code
      const saved = loc ? await api("PUT", `/api/locations/${loc.id}`, data) : await api("POST", "/api/locations", data);
      showMessage(`Saved ${saved.name} (${saved.code}).`);
      refresh();
    },
  });
}

// --- location (container) details ------------------------------------------

async function openLocation(id) {
  const dlg = $("#detail-dialog");
  const render = async () => {
    const loc = await api("GET", `/api/locations/${id}`);
    dlg.innerHTML = `
      <div class="detail">
        <button class="close" data-act="close" aria-label="Close">✕</button>
        <div class="info">
          <h2>${esc(loc.name)} <span class="code-chip">${esc(loc.code)}</span></h2>
          ${loc.notes ? `<p class="description">${esc(loc.notes)}</p>` : ""}
          <dl>
            <div><dt>Parts</dt><dd>${number.format(loc.parts)}</dd></div>
            <div><dt>Units</dt><dd>${number.format(loc.units)}</dd></div>
            <div><dt>Value</dt><dd>${money.format(loc.total_value)}</dd></div>
          </dl>
          <div class="button-row">
            <button class="primary" data-act="receive">Add stock here</button>
            <button data-act="label">Print label</button>
            <button data-act="edit">Edit</button>
            ${loc.units ? "" : `<button data-act="delete" class="danger">Delete</button>`}
          </div>
        </div>

        <h3>In this location</h3>
        ${loc.items.length
          ? `<div class="table-wrap"><table class="cards">
              <thead><tr><th class="thumb-col"></th><th>P/N</th><th>Description</th><th>Category</th>
                <th class="num">Qty here</th><th class="num">Value</th><th></th></tr></thead>
              <tbody>${loc.items
                .map(
                  (it) => `<tr data-id="${it.id}" class="clickable">
                    <td class="thumb-col">${thumb(it)}</td>
                    <td class="pn">${esc(it.part_number)}</td>
                    <td class="desc">${esc(it.description)}</td>
                    <td class="cat">${it.category ? `<span class="tag">${esc(it.category)}</span>` : ""}</td>
                    <td class="num qty">${number.format(it.here)}${it.quantity > it.here ? `<small class="muted"> of ${number.format(it.quantity)}</small>` : ""}</td>
                    <td class="num total">${money.format(it.here_value)}</td>
                    <td class="actions">
                      <button data-act="book_out">Book out</button>
                      <button data-act="transfer">Move</button>
                    </td></tr>`
                )
                .join("")}</tbody></table></div>`
          : `<p class="muted">Nothing here yet. Use “Add stock here” to put parts in this location.</p>`}
      </div>`;

    dlg.onclick = async (e) => {
      if (e.target.closest("a")) return;
      const act = e.target.closest("button")?.dataset.act;
      const row = e.target.closest("tr[data-id]");
      try {
        if (act === "close") dlg.close();
        else if (act === "receive") stockDialog("receive", null, { location: loc.name });
        else if (act === "label") labelDialog("locations", [locationLabel(loc)]);
        else if (act === "edit") locationForm(loc);
        else if (act === "delete") {
          if (!confirm(`Delete location ${loc.name}?`)) return;
          await api("DELETE", `/api/locations/${loc.id}`);
          dlg.close();
          showMessage(`Deleted ${loc.name}.`);
          refresh();
        } else if (act && row) {
          const item = loc.items.find((it) => String(it.id) === row.dataset.id);
          stockDialog(act, item, { location: loc.name });
        } else if (!act && row) openPart(row.dataset.id);
      } catch (err) {
        showMessage(err.message, true);
      }
    };
  };
  await showDetail(render);
}

// --- scanning ----------------------------------------------------------------

// Look up a scanned or typed code (location code, P/N, location name or job number) and open it.
async function scan(text) {
  const code = String(text || "").trim();
  if (!code) return;
  const hit = await api("GET", `/api/scan?${new URLSearchParams({ code })}`);
  if ($("#form-dialog").open) $("#form-dialog").close();
  if (hit.type === "location") await openLocation(hit.id);
  else if (hit.type === "part") await openPart(hit.id);
  else await openJob(hit.id);
}

function scanDialog() {
  formDialog({
    title: "Scan a label",
    intro: `<p class="muted">Scan a label with a barcode scanner, or type a location code, P/N or job number.
      On a phone, point the camera app at a label's QR code instead.</p>`,
    fields: [{ name: "code", label: "Code", autofocus: true, placeholder: "e.g. LOC-0003" }],
    submitLabel: "Look up",
    onSubmit: (data) => scan(data.code),
  });
}

$("#scan-btn").addEventListener("click", scanDialog);

// --- labels ------------------------------------------------------------------

const LABEL_SETTINGS = "labelSettings";
const isLocalHost = (host) => ["localhost", "127.0.0.1", "::1", "[::1]"].includes(host);
let serverInfo = null;

function labelSettings() {
  try {
    return JSON.parse(localStorage.getItem(LABEL_SETTINGS)) || {};
  } catch {
    return {};
  }
}

function saveLabelSettings(settings) {
  try {
    localStorage.setItem(LABEL_SETTINGS, JSON.stringify(settings));
  } catch {
    // settings just won't be remembered
  }
}

// The address phones should use: this page's address, unless it's localhost.
async function defaultLinkBase() {
  if (!isLocalHost(location.hostname)) return location.origin;
  serverInfo = serverInfo || (await api("GET", "/api/info").catch(() => ({})));
  return serverInfo.lan_url || location.origin;
}

function partLabel(item) {
  return { id: item.id, code: item.part_number, title: item.description, subtitle: item.category };
}

function locationLabel(loc) {
  return { id: loc.id, code: loc.code, title: loc.name, subtitle: loc.notes };
}

async function labelDialog(kind, labels) {
  const saved = labelSettings();
  const base = saved.base || (await defaultLinkBase());
  let rendered = []; // [{label, canvas}] for the ticked labels

  const settingsFrom = (data) => ({ tape: Number(data.tape), type: data.type, base: data.base.trim().replace(/\/+$/, "") });
  const withLink = (label, base) => ({ ...label, link: `${base}/s/${encodeURIComponent(label.code)}` });
  const picked = () => rendered.filter((r) => r.canvas && r.checkbox.checked);

  formDialog({
    title: labels.length > 1 ? `Print ${labels.length} labels` : `Label for ${labels[0].code}`,
    wide: true,
    fields: [
      { name: "tape", label: "Tape width", type: "select", value: String(saved.tape || 24),
        options: Object.keys(Labels.TAPES).sort((a, b) => b - a).map((w) => ({ value: w, label: `${w} mm` })) },
      { name: "type", label: "Code type", type: "select", value: saved.type || "qr",
        options: [
          { value: "qr", label: "QR code: scan with a phone camera or a 2D scanner" },
          { value: "code128", label: "Barcode (Code 128): works with any handheld scanner" },
        ] },
      { name: "base", label: "Web address in QR codes", value: base, hint: " " },
    ],
    submitLabel: "Print",
    extraButtons: [
      {
        label: "Download PNG",
        onClick: async () => {
          const chosen = picked();
          if (!chosen.length) throw new Error("Tick at least one label");
          for (const [i, r] of chosen.entries()) {
            const a = document.createElement("a");
            a.href = URL.createObjectURL(await Labels.png(r.canvas));
            a.download = `label-${r.label.code.replace(/[^\w.-]+/g, "_")}.png`;
            a.click();
            setTimeout(() => URL.revokeObjectURL(a.href), 10000);
            if (i < chosen.length - 1) await new Promise((res) => setTimeout(res, 400));
          }
        },
      },
      {
        label: "P-touch Editor CSV",
        onClick: async (data) => {
          const chosen = picked();
          if (!chosen.length) throw new Error("Tick at least one label");
          const params = new URLSearchParams({ type: kind, base: settingsFrom(data).base, ids: chosen.map((r) => r.label.id).join(",") });
          const a = document.createElement("a");
          a.href = `/api/labels.csv?${params}`;
          a.click();
        },
      },
    ],
    setup: (form) => {
      const preview = document.createElement("div");
      preview.className = "label-list";
      $(".error", form).before(preview);
      const hint = $("[data-field=base] small", form);

      const update = () => {
        const data = Object.fromEntries(new FormData(form));
        const settings = settingsFrom(data);
        saveLabelSettings(settings);
        $("[data-field=base]", form).hidden = settings.type !== "qr";
        let host = "";
        try {
          host = new URL(settings.base).hostname;
        } catch {
          host = "";
        }
        hint.textContent = isLocalHost(host)
          ? "⚠ Phones can't reach “localhost”. Start the app with --host 0.0.0.0 and use this computer's network address."
          : "The address phones use to reach this app. Scanning a QR code opens the location or part.";
        hint.classList.toggle("error", isLocalHost(host));

        const wasChecked = new Map(rendered.map((r) => [r.label.code, r.checkbox.checked]));
        preview.innerHTML = "";
        rendered = labels.map((label) => {
          const row = document.createElement("label");
          row.className = "label-item";
          const checkbox = document.createElement("input");
          checkbox.type = "checkbox";
          checkbox.checked = wasChecked.get(label.code) ?? true;
          checkbox.hidden = labels.length === 1;
          row.append(checkbox);
          let canvas = null;
          try {
            canvas = Labels.render(withLink(label, settings.base), settings);
            canvas.title = `${Labels.mm(canvas.width)} × ${settings.tape} mm`;
            row.append(canvas);
          } catch (err) {
            checkbox.checked = false;
            checkbox.disabled = true;
            row.insertAdjacentHTML("beforeend", `<span class="error">${esc(label.code)}: ${esc(err.message)}</span>`);
          }
          preview.append(row);
          return { label, canvas, checkbox };
        });
      };
      form.addEventListener("change", (e) => e.target.type !== "checkbox" && update());
      $("[name=base]", form).addEventListener("input", debounce(update, 300));
      update();
    },
    onSubmit: async (data) => {
      const chosen = picked();
      if (!chosen.length) throw new Error("Tick at least one label");
      await printLabels(chosen.map((r) => r.canvas), Number(data.tape));
    },
  });
}

// Print through the P-touch printer driver: one label per page, page sized to the tape.
async function printLabels(canvases, tape) {
  const height = Labels.TAPES[tape];
  const length = Labels.mm(Math.max(...canvases.map((c) => c.width)));
  const area = $("#print-area");
  area.innerHTML =
    `<style>@page { size: ${length}mm ${tape}mm; margin: 0; }</style>` +
    canvases
      .map(
        (c) => `<div class="print-label" style="width:${length}mm;height:${tape}mm">
          <img src="${c.toDataURL("image/png")}" style="width:${Labels.mm(c.width)}mm;height:${Labels.mm(height)}mm" alt=""></div>`
      )
      .join("");
  await Promise.all($$("img", area).map((img) => img.decode().catch(() => {})));
  window.print();
}

window.addEventListener("afterprint", () => ($("#print-area").innerHTML = ""));

// --- signing in ------------------------------------------------------------

let setupMode = false;

function showLogin(session = {}) {
  state.user = null;
  $$("dialog[open]").forEach((d) => d.close());
  document.body.classList.add("signed-out");
  const form = $("#login-form");
  setupMode = !!(session.needs_setup && session.can_setup);
  $("#confirm-row").hidden = !setupMode;
  form.elements.confirm.required = setupMode;
  $("button[type=submit]", form).textContent = setupMode ? "Create account" : "Sign in";
  form.elements.password.autocomplete = setupMode ? "new-password" : "current-password";
  $("#login-intro").textContent = setupMode
    ? "Welcome! Create the first account. You can add more people afterwards."
    : session.needs_setup
      ? "No accounts yet. On the computer running the app, run: python3 run.py --set-password YOUR_NAME"
      : "Sign in to continue.";
  form.elements.username.focus();
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.target;
  const f = form.elements;
  const box = $(".error", form);
  box.hidden = true;
  try {
    if (setupMode && f.password.value !== f.confirm.value) throw new Error("Passwords don't match");
    const body = { username: f.username.value, password: f.password.value };
    const { user } = await api("POST", setupMode ? "/api/setup" : "/api/login", body);
    form.reset();
    signedIn(user);
  } catch (err) {
    box.textContent = err.message;
    box.hidden = false;
  }
});

function signedIn(user) {
  state.user = user;
  document.body.classList.remove("signed-out");
  $("#account-btn").textContent = user.username;
  setView();
}

async function start() {
  try {
    const session = await api("GET", "/api/session");
    if (session.user) signedIn(session.user);
    else showLogin(session);
  } catch (err) {
    showLogin();
    const box = $("#login-form .error");
    box.textContent = `Can't reach the server: ${err.message}`;
    box.hidden = false;
  }
}

// --- account, users and backup ---------------------------------------------

async function accountDialog() {
  const users = await api("GET", "/api/users");
  formDialog({
    title: `Signed in as ${state.user.username}`,
    wide: true,
    intro: `
      <div class="button-row">
        <a class="button" href="/api/backup.zip">Download backup</a>
        <button type="button" data-act="logout">Sign out</button>
      </div>
      <p class="muted small">The backup is a zip of all your data and pictures. Keep a copy somewhere safe.</p>
      <h3>People who can sign in</h3>
      <table class="users">${users
        .map(
          (u) => `<tr><td><b>${esc(u.username)}</b></td><td class="muted">added ${when(u.created_at, false)}</td>
            <td class="actions">${u.id === state.user.id
              ? `<span class="muted">you</span>`
              : `<button type="button" data-remove="${u.id}" data-name="${esc(u.username)}">Remove</button>`}</td></tr>`
        )
        .join("")}</table>
      <div><button type="button" data-act="add-user">+ Add person</button></div>
      <h3>Change your password</h3>`,
    fields: [
      { name: "current", label: "Current password", type: "password", required: true },
      { name: "new", label: "New password (at least 8 characters)", type: "password", required: true, min: undefined },
      { name: "repeat", label: "Repeat new password", type: "password", required: true },
    ],
    submitLabel: "Change password",
    setup: (form) => {
      form.addEventListener("click", async (e) => {
        const btn = e.target.closest("button");
        if (!btn) return;
        try {
          if (btn.dataset.act === "logout") {
            await api("POST", "/api/logout");
            showLogin();
          } else if (btn.dataset.act === "add-user") {
            addUserDialog();
          } else if (btn.dataset.remove) {
            if (!confirm(`Remove ${btn.dataset.name}? They won't be able to sign in any more.`)) return;
            await api("DELETE", `/api/users/${btn.dataset.remove}`);
            showMessage(`Removed ${btn.dataset.name}.`);
            accountDialog();
          }
        } catch (err) {
          showMessage(err.message, true);
        }
      });
    },
    onSubmit: async (data) => {
      if (data.new !== data.repeat) throw new Error("The new passwords don't match");
      await api("POST", "/api/password", { current: data.current, new: data.new });
      showMessage("Password changed. Any other devices have been signed out.");
    },
  });
}

function addUserDialog() {
  formDialog({
    title: "Add person",
    intro: `<p class="muted">They'll sign in with this username and password, and can change the password themselves.</p>`,
    fields: [
      { name: "username", label: "Username", required: true, autofocus: true, placeholder: "e.g. sam" },
      { name: "password", label: "Password (at least 8 characters)", type: "password", required: true },
    ],
    submitLabel: "Add",
    onSubmit: async (data) => {
      const user = await api("POST", "/api/users", data);
      showMessage(`Added ${user.username}.`);
    },
  });
}

$("#account-btn").addEventListener("click", () => accountDialog().catch((err) => showMessage(err.message, true)));

// --- navigation ------------------------------------------------------------

function setView() {
  if (!state.user) return; // the hash (e.g. a scanned label) is handled after signing in
  let view = location.hash.slice(1);
  if (view.startsWith("scan/")) {
    // Opened from a label's QR code (/s/CODE redirects here).
    const code = decodeURIComponent(view.slice("scan/".length));
    history.replaceState(null, "", `#${state.view}`);
    view = state.view;
    scan(code).catch((err) => showMessage(err.message, true));
  }
  state.view = ["parts", "jobs", "locations", "history"].includes(view) ? view : "parts";
  $$(".view").forEach((s) => (s.hidden = s.id !== `view-${state.view}`));
  $$(".tabs button").forEach((b) => b.setAttribute("aria-selected", b.dataset.view === state.view));
  refresh();
}

$$(".tabs button").forEach((b) => b.addEventListener("click", () => (location.hash = b.dataset.view)));
window.addEventListener("hashchange", setView);

// Handheld barcode scanners type the code quickly and press Enter. Catch that
// anywhere outside a text box and look the code up. "/" focuses the search box.
let scanBuffer = "";
let scanTime = 0;
document.addEventListener("keydown", (e) => {
  if (["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName) || $("#form-dialog").open) return;
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const now = Date.now();
  if (now - scanTime > 100) scanBuffer = "";
  scanTime = now;
  if (e.key === "Enter") {
    if (scanBuffer.length >= 3) {
      e.preventDefault();
      scan(scanBuffer).catch((err) => showMessage(err.message, true));
    }
    scanBuffer = "";
  } else if (e.key === "/" && !scanBuffer && !$("dialog[open]") && $(`#view-${state.view} .search`)) {
    e.preventDefault();
    $(`#view-${state.view} .search`).focus();
  } else if (e.key.length === 1) {
    scanBuffer += e.key;
  }
});

start();
