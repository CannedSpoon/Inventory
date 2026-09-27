"use strict";

const $ = (sel) => document.querySelector(sel);
const money = new Intl.NumberFormat(undefined, { style: "currency", currency: "USD" });
const number = new Intl.NumberFormat();

const state = { q: "", sort: "part_number", order: "asc", editingId: null };
let searchTimer;

async function api(method, path, body, isText = false) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.body = isText ? body : JSON.stringify(body);
    opts.headers["Content-Type"] = isText ? "text/csv" : "application/json";
  }
  const res = await fetch(path, opts);
  if (res.status === 204) return null;
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// Escape text, then wrap any search-term matches in <mark>.
function highlight(text) {
  const safe = escapeHtml(text);
  const terms = state.q.split(/\s+/).filter(Boolean).map((t) => escapeHtml(t).replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  if (!terms.length) return safe;
  return safe.replace(new RegExp(`(${terms.join("|")})`, "gi"), "<mark>$1</mark>");
}

function showMessage(text, isError = false) {
  const el = $("#message");
  el.textContent = text;
  el.className = isError ? "error" : "";
  el.hidden = false;
  clearTimeout(el._timer);
  el._timer = setTimeout(() => (el.hidden = true), isError ? 8000 : 4000);
}

async function load() {
  const params = new URLSearchParams({ q: state.q, sort: state.sort, order: state.order });
  try {
    const { items, summary } = await api("GET", `/api/items?${params}`);
    render(items);
    $("#stat-items").textContent = number.format(summary.items);
    $("#stat-units").textContent = number.format(summary.units);
    $("#stat-value").textContent = money.format(summary.total_value);
    $("#export-btn").href = `/api/export.csv?${new URLSearchParams({ q: state.q })}`;
  } catch (e) {
    showMessage(`Could not load inventory: ${e.message}`, true);
  }
}

function render(items) {
  $("#rows").innerHTML = items
    .map(
      (it) => `
    <tr data-id="${it.id}">
      <td class="pn">${highlight(it.part_number)}</td>
      <td class="desc">${highlight(it.description)}</td>
      <td class="num${it.quantity === 0 ? " low" : ""}">${number.format(it.quantity)}</td>
      <td class="num">${money.format(it.unit_value)}</td>
      <td class="num">${money.format(it.total_value)}</td>
      <td class="actions">
        <button data-action="dec" title="Remove one">−</button>
        <button data-action="inc" title="Add one">+</button>
        <button data-action="edit">Edit</button>
        <button data-action="delete" title="Delete">✕</button>
      </td>
    </tr>`
    )
    .join("");
  $("#empty").hidden = items.length > 0;
  $("#empty").textContent = state.q ? `No items match “${state.q}”.` : "No items yet — click “+ Add item” to get started.";
  document.querySelectorAll("th[data-sort]").forEach((th) => {
    th.classList.toggle("sorted-asc", th.dataset.sort === state.sort && state.order === "asc");
    th.classList.toggle("sorted-desc", th.dataset.sort === state.sort && state.order === "desc");
  });
}

function openDialog(item) {
  const form = $("#item-form");
  form.reset();
  state.editingId = item ? item.id : null;
  $("#dialog-title").textContent = item ? `Edit ${item.part_number}` : "Add item";
  if (item) {
    for (const k of ["part_number", "description", "quantity", "unit_value"]) form.elements[k].value = item[k];
  }
  $("#form-error").hidden = true;
  $("#item-dialog").showModal();
  form.elements.part_number.focus();
}

$("#item-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target.elements;
  const data = {
    part_number: f.part_number.value,
    description: f.description.value,
    quantity: f.quantity.value,
    unit_value: f.unit_value.value,
  };
  try {
    if (state.editingId) await api("PUT", `/api/items/${state.editingId}`, data);
    else await api("POST", "/api/items", data);
    $("#item-dialog").close();
    showMessage(`Saved ${data.part_number.trim()}.`);
    load();
  } catch (err) {
    $("#form-error").textContent = err.message;
    $("#form-error").hidden = false;
  }
});

$("#cancel-btn").addEventListener("click", () => $("#item-dialog").close());
$("#add-btn").addEventListener("click", () => openDialog(null));

$("#search").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.q = e.target.value.trim();
    load();
  }, 150);
});

document.querySelector("thead").addEventListener("click", (e) => {
  const key = e.target.dataset.sort;
  if (!key) return;
  state.order = state.sort === key && state.order === "asc" ? "desc" : "asc";
  state.sort = key;
  load();
});

$("#rows").addEventListener("click", async (e) => {
  const action = e.target.dataset.action;
  if (!action) return;
  const id = e.target.closest("tr").dataset.id;
  try {
    if (action === "inc" || action === "dec") {
      await api("POST", `/api/items/${id}/adjust`, { delta: action === "inc" ? 1 : -1 });
    } else if (action === "edit") {
      openDialog(await api("GET", `/api/items/${id}`));
      return;
    } else if (action === "delete") {
      const item = await api("GET", `/api/items/${id}`);
      if (!confirm(`Delete ${item.part_number}? This cannot be undone.`)) return;
      await api("DELETE", `/api/items/${id}`);
      showMessage(`Deleted ${item.part_number}.`);
    }
    load();
  } catch (err) {
    showMessage(err.message, true);
  }
});

$("#import-file").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  try {
    const r = await api("POST", "/api/import", await file.text(), true);
    let msg = `Imported: ${r.created} added, ${r.updated} updated.`;
    if (r.errors.length) msg += ` ${r.errors.length} skipped — ${r.errors.slice(0, 3).join("; ")}`;
    showMessage(msg, r.errors.length > 0);
    load();
  } catch (err) {
    showMessage(`Import failed: ${err.message}`, true);
  }
  e.target.value = "";
});

// "/" focuses search from anywhere.
document.addEventListener("keydown", (e) => {
  if (e.key === "/" && !["INPUT", "TEXTAREA"].includes(document.activeElement.tagName)) {
    e.preventDefault();
    $("#search").focus();
  }
});

load();
