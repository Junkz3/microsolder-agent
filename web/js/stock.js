// Stock section — donor management + search.
// See docs/superpowers/specs/2026-05-08-stock-inventory-design.md §11.

import { t } from "./i18n.js";

const API = "/api/stock";

// One-letter type codes for the dense column layout. Keeps the row scanable
// at a glance — the full type name is in the tooltip.
const TYPE_LABEL = {
  capacitor: "C",
  resistor: "R",
  inductor: "L",
  diode: "D",
  transistor: "Q",
  ferrite: "FB",
  ic: "IC",
  connector: "J",
  led: "LED",
  crystal: "Y",
  oscillator: "Y",
  fuse: "F",
  switch: "SW",
  relay: "K",
  transformer: "TR",
  module: "M",
  power_symbol: "PS",
  test_point: "TP",
  mounting: "MT",
  antenna: "ANT",
  other: "?",
};

const TYPE_FAMILY = {
  // Passives → cyan family
  capacitor: "passive", resistor: "passive", inductor: "passive",
  diode: "passive", ferrite: "passive",
  // Actives → violet
  ic: "active", transistor: "active", led: "active",
  oscillator: "active", crystal: "active",
  // Mechanical → amber
  connector: "mech", switch: "mech", fuse: "mech",
  test_point: "mech", mounting: "mech", antenna: "mech",
};

// Keep state for the harvest mode so filter + sort survive checkbox toggles.
const _harvestState = {
  donorId: null,
  parts: [],
  filter: "",
  typeFilter: "",
  sort: "refdes",
};

async function fetchJson(path, opts) {
  const r = await fetch(API + path, opts);
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return r.json();
}

function escapeHtml(s) {
  if (s == null) return "";
  return String(s).replace(/[&<>"']/g, c => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function typeChip(type) {
  const label = TYPE_LABEL[type] || "?";
  const family = TYPE_FAMILY[type] || "other";
  return `<span class="type-chip type-${family}" title="${escapeHtml(type)}">${label}</span>`;
}

function roleChip(role, safety) {
  if (!role) return `<span class="role-chip role-unknown" title="${t("stock.col_role")}">—</span>`;
  return `<span class="role-chip safety-${safety || "exact_only"}">${escapeHtml(role)}</span>`;
}

function critDot(crit) {
  return `<span class="crit-dot crit-${crit}" title="${escapeHtml(crit)}"></span>`;
}

async function loadDonors() {
  const { donors } = await fetchJson("/donors");
  const list = document.getElementById("stock-donors-list");
  list.innerHTML = "";
  let totalAvail = 0;
  let totalCons = 0;
  for (const d of donors) {
    totalAvail += d.parts_available;
    totalCons += d.parts_consumed;
    const card = document.createElement("div");
    card.className = "donor-card";
    card.innerHTML = `
      <div class="donor-head">
        <span class="donor-id mono">${escapeHtml(d.donor_id)}</span>
        <span class="donor-condition mono">${escapeHtml(d.condition)}</span>
      </div>
      <div class="donor-label">${escapeHtml(d.label)}</div>
      <div class="donor-stats mono">
        <b>${d.parts_available}</b> / ${d.parts_total} ${t("stock.available").toLowerCase()}
        ${d.has_parts_index ? "" : ` · <span class="warn">${t("stock.no_parts_index")}</span>`}
      </div>
      <div class="donor-actions">
        <button class="btn-sm" data-action="harvest" data-donor="${escapeHtml(d.donor_id)}">${t("stock.harvest")}</button>
        <button class="btn-sm btn-danger" data-action="unmark" data-donor="${escapeHtml(d.donor_id)}">${t("stock.remove")}</button>
      </div>
    `;
    list.appendChild(card);
  }
  document.getElementById("stock-donors-count").textContent = donors.length;
  document.getElementById("stock-available-count").textContent = totalAvail;
  document.getElementById("stock-consumed-count").textContent = totalCons;

  list.onclick = async (ev) => {
    const btn = ev.target.closest("[data-action]");
    if (!btn) return;
    const donorId = btn.dataset.donor;
    if (btn.dataset.action === "unmark") {
      if (!confirm(`${t("stock.remove")} ${donorId}?`)) return;
      await fetchJson(`/donors/${donorId}`, { method: "DELETE" });
      await loadDonors();
    } else if (btn.dataset.action === "harvest") {
      openHarvestMode(donorId);
    }
  };
}

function _filterAndSort(parts) {
  const q = _harvestState.filter.trim().toLowerCase();
  const tFilter = _harvestState.typeFilter;
  let rows = parts;
  if (q) {
    rows = rows.filter(p => {
      const hay = `${p.refdes} ${p.value_canonical || ""} ${p.role_in_design || ""} ${p.mpn || ""}`.toLowerCase();
      return hay.includes(q);
    });
  }
  if (tFilter) rows = rows.filter(p => p.type === tFilter);

  const sortKey = _harvestState.sort;
  rows = rows.slice().sort((a, b) => {
    if (sortKey === "refdes") return a.refdes.localeCompare(b.refdes, undefined, { numeric: true });
    if (sortKey === "type") return (a.type || "").localeCompare(b.type || "") || a.refdes.localeCompare(b.refdes);
    if (sortKey === "value") return (a.value_canonical || "").localeCompare(b.value_canonical || "");
    if (sortKey === "role") return (a.role_in_design || "zzz").localeCompare(b.role_in_design || "zzz");
    if (sortKey === "crit") {
      const order = { high: 0, medium: 1, low: 2 };
      return (order[a.criticality_in_design] ?? 9) - (order[b.criticality_in_design] ?? 9);
    }
    return 0;
  });
  return rows;
}

function _renderHarvestRows() {
  const rows = _filterAndSort(_harvestState.parts);
  const tbody = document.getElementById("harvest-tbody");
  if (!tbody) return;
  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="9" class="harvest-empty">No matching parts.</td></tr>`;
    return;
  }
  tbody.innerHTML = rows.map(p => `
    <tr class="harvest-row ${p.available ? "" : "consumed"}">
      <td><input type="checkbox" data-refdes="${escapeHtml(p.refdes)}" ${p.available ? "" : "checked"}></td>
      <td class="mono refdes">${escapeHtml(p.refdes)}</td>
      <td>${typeChip(p.type)}</td>
      <td class="mono value">${escapeHtml(p.value_canonical || "—")}</td>
      <td class="mono pkg">${escapeHtml(p.package || "—")}</td>
      <td class="mono mpn dim">${escapeHtml(p.mpn || "")}</td>
      <td>${roleChip(p.role_in_design, p.safety_class)}</td>
      <td>${critDot(p.criticality_in_design)}</td>
      <td class="mono pages dim">${(p.pages || []).join(",") || "—"}</td>
    </tr>
  `).join("");

  // Header summary count
  const countEl = document.getElementById("harvest-row-count");
  if (countEl) countEl.textContent = `${rows.length} / ${_harvestState.parts.length}`;
}

async function openHarvestMode(donorId) {
  const { parts } = await fetchJson(`/donors/${donorId}/parts`);
  _harvestState.donorId = donorId;
  _harvestState.parts = parts;
  _harvestState.filter = "";
  _harvestState.typeFilter = "";
  _harvestState.sort = "refdes";

  // Build the type filter options from what's actually present, in the
  // canonical order. Caps + resistors first (most populous on most boards).
  const typeCounts = {};
  for (const p of parts) typeCounts[p.type] = (typeCounts[p.type] || 0) + 1;
  const orderedTypes = Object.keys(typeCounts).sort((a, b) => typeCounts[b] - typeCounts[a]);
  const typeOpts = ['<option value="">All types</option>']
    .concat(orderedTypes.map(t => `<option value="${escapeHtml(t)}">${TYPE_LABEL[t] || "?"} · ${escapeHtml(t)} (${typeCounts[t]})</option>`))
    .join("");

  const overlay = document.createElement("div");
  overlay.className = "harvest-overlay";
  overlay.innerHTML = `
    <div class="harvest-panel glass">
      <header class="harvest-head">
        <div class="harvest-title">
          <h3>${t("stock.harvest_title")}</h3>
          <span class="mono dim">${escapeHtml(donorId)}</span>
        </div>
        <button class="btn-sm" data-close>${t("stock.close")}</button>
      </header>
      <div class="harvest-controls">
        <input class="harvest-filter" id="harvest-filter" type="search"
               placeholder="${t("stock.filter_donor_placeholder")}">
        <select class="harvest-type-filter" id="harvest-type-filter">${typeOpts}</select>
        <select class="harvest-sort" id="harvest-sort">
          <option value="refdes">↕ Refdes</option>
          <option value="type">↕ Type</option>
          <option value="value">↕ Value</option>
          <option value="role">↕ Role</option>
          <option value="crit">↕ Criticality</option>
        </select>
        <span class="mono dim" id="harvest-row-count"></span>
      </div>
      <div class="harvest-table-wrap">
        <table class="harvest-table">
          <thead>
            <tr>
              <th></th>
              <th>${t("stock.col_refdes")}</th>
              <th>${t("stock.col_type")}</th>
              <th>${t("stock.col_value")}</th>
              <th>${t("stock.col_pkg")}</th>
              <th>MPN</th>
              <th>${t("stock.col_role")}</th>
              <th>${t("stock.col_crit")}</th>
              <th>${t("stock.col_page")}</th>
            </tr>
          </thead>
          <tbody id="harvest-tbody"></tbody>
        </table>
      </div>
    </div>
  `;
  document.body.appendChild(overlay);
  _renderHarvestRows();

  overlay.querySelector("[data-close]").onclick = () => overlay.remove();
  overlay.addEventListener("click", (ev) => {
    if (ev.target === overlay) overlay.remove();
  });

  document.getElementById("harvest-filter").addEventListener("input", (ev) => {
    _harvestState.filter = ev.target.value;
    _renderHarvestRows();
  });
  document.getElementById("harvest-type-filter").addEventListener("change", (ev) => {
    _harvestState.typeFilter = ev.target.value;
    _renderHarvestRows();
  });
  document.getElementById("harvest-sort").addEventListener("change", (ev) => {
    _harvestState.sort = ev.target.value;
    _renderHarvestRows();
  });

  overlay.addEventListener("change", async (ev) => {
    const cb = ev.target.closest('input[type="checkbox"][data-refdes]');
    if (!cb) return;
    const ref = cb.dataset.refdes;
    if (cb.checked) {
      await fetchJson(`/donors/${donorId}/consume`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refdes: ref }),
      });
    } else {
      await fetchJson(`/donors/${donorId}/consume/${ref}`, { method: "DELETE" });
    }
    // Update local state so the row's class / sort survives a re-render.
    const part = _harvestState.parts.find(p => p.refdes === ref);
    if (part) part.available = !cb.checked;
  });
}

async function runSearch() {
  const body = {
    type: document.getElementById("stock-q-type").value || null,
    value_canonical: document.getElementById("stock-q-value").value || null,
    package: document.getElementById("stock-q-package").value || null,
    voltage_min: parseFloat(document.getElementById("stock-q-voltage").value) || null,
    requested_role: document.getElementById("stock-q-role").value || null,
  };
  Object.keys(body).forEach(k => body[k] == null && delete body[k]);
  if (!body.type) {
    alert("Select a component type to search.");
    return;
  }
  const res = await fetchJson("/search", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  renderSearchResults(res);
}

function renderSearchResults(res) {
  const out = document.getElementById("stock-results");
  out.innerHTML = "";
  if (res.empty_reason) {
    out.innerHTML = `<div class="empty">${escapeHtml(res.empty_reason)}</div>`;
    return;
  }
  const section = (titleKey, matches, kind) => {
    if (!matches.length) return "";
    return `
      <div class="result-group result-${kind}">
        <h3>${t(titleKey)} <span class="dim mono">(${matches.length})</span></h3>
        <table class="result-table">
          <thead>
            <tr>
              <th>${t("stock.col_refdes")}</th>
              <th>${t("stock.col_type")}</th>
              <th>${t("stock.col_value")}</th>
              <th>${t("stock.col_pkg")}</th>
              <th>${t("stock.col_donor")}</th>
              <th>${t("stock.col_page")}</th>
              <th>${t("stock.col_crit")}</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            ${matches.map(m => `
              <tr>
                <td class="mono refdes">${escapeHtml(m.refdes)}</td>
                <td>${typeChip(m.type || "")}</td>
                <td class="mono value">${escapeHtml(m.value_canonical || "—")}</td>
                <td class="mono pkg">${escapeHtml(m.package || "—")}</td>
                <td class="mono donor">${escapeHtml(m.donor_label)}</td>
                <td class="mono pages dim">${(m.pages || []).join(",") || "—"}</td>
                <td>${critDot(m.criticality_in_donor)}</td>
                <td><button class="btn-sm" data-mark-consumed
                            data-donor="${escapeHtml(m.donor_id)}"
                            data-refdes="${escapeHtml(m.refdes)}">${t("stock.mark_consumed")}</button></td>
              </tr>
            `).join("")}
          </tbody>
        </table>
      </div>
    `;
  };
  out.innerHTML = section("stock.exact_matches", res.exact_matches, "exact");
  out.onclick = async (ev) => {
    const btn = ev.target.closest("[data-mark-consumed]");
    if (!btn) return;
    await fetchJson(`/donors/${btn.dataset.donor}/consume`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refdes: btn.dataset.refdes }),
    });
    btn.disabled = true;
    btn.textContent = t("stock.consumed");
    await loadDonors();
  };
}

async function showAddDonorDialog() {
  const slug = prompt("device_slug (must match memory/{slug}/):");
  if (!slug) return;
  const label = prompt("Label (e.g. 'iPhone X HS broken screen 2024-001'):");
  if (!label) return;
  try {
    const r = await fetchJson("/donors", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ device_slug: slug, label, condition: "donor_only" }),
    });
    alert(`Created ${r.donor_id}${r.has_parts_index ? "" : " (no parts_index — schematic not ingested)"}`);
    await loadDonors();
  } catch (e) {
    alert("Failed: " + e.message);
  }
}

export function initStockSection() {
  document.getElementById("stock-search-btn").onclick = runSearch;
  document.getElementById("stock-add-donor-btn").onclick = showAddDonorDialog;
  loadDonors();
}
