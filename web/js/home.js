// Home (journal des réparations) + the "nouvelle réparation" modal.
//
// renderHome() renders the repair grid from a /pipeline/repairs response
// (taxonomy supplies the brand > model > version grouping). The pure
// pack list at /pipeline/packs is no longer surfaced here — repairs own
// the home view and reuse pack metadata via the taxonomy payload.
// initNewRepairModal() wires the modal's open/close/submit handlers plus
// its own document-level keydown interceptor. The keydown listener is
// intentionally registered before main.js adds its global Cmd+K / Esc
// handler — stopImmediatePropagation() in this handler only works if it
// runs first.

import { openPipelineProgress } from './pipeline_progress.js';
import { leaveSession } from './router.js';
import { openPanel } from './llm.js';
import { ICON_CHECK } from './icons.js';

export async function loadTaxonomy() {
  try {
    const res = await fetch("/pipeline/taxonomy");
    if (!res.ok) return {brands: {}, uncategorized: []};
    return await res.json();
  } catch (err) {
    console.warn("loadTaxonomy failed", err);
    return {brands: {}, uncategorized: []};
  }
}

export async function loadRepairs() {
  try {
    const res = await fetch("/pipeline/repairs");
    if (!res.ok) return [];
    return await res.json();
  } catch (err) {
    console.warn("loadRepairs failed", err);
    return [];
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => (
    {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]
  ));
}

function humanizeSlug(slug) {
  return slug.replace(/-/g, " ").replace(/^./, c => c.toUpperCase());
}

// Strip a trailing form_factor ("motherboard", "logic board") from a label
// that was typed with the form_factor glued on. Used when we don't have a
// taxonomy.model to fall back on.
function stripFormFactor(label, formFactor) {
  if (!label || !formFactor) return label;
  const ff = formFactor.trim();
  if (!ff) return label;
  const re = new RegExp("\\s+" + ff.replace(/[.*+?^${}()|[\\]\\\\]/g, "\\$&") + "\\s*$", "i");
  return label.replace(re, "").trim() || label;
}

// The device NAME — what the board is, not what form it takes. Prefer the
// clean `taxonomy.model` (set by the Registry Builder from the dump) over
// the raw user-typed `device_label` which usually glues the form_factor on.
// Brand is included by default so the name reads standalone; set
// `includeBrand: false` inside brand-grouped UI sections.
function deviceName(entry, { includeBrand = true } = {}) {
  const brand = entry.brand || "";
  const model = entry.model || "";
  if (brand && model) return includeBrand ? `${brand} ${model}` : model;
  if (model) return model;
  return stripFormFactor(entry.device_label || humanizeSlug(entry.device_slug), entry.form_factor);
}

// Index the taxonomy so each repair can be resolved to {brand, model,
// form_factor, version} without an extra fetch per card.
function indexTaxonomyBySlug(taxonomy) {
  const index = new Map();
  for (const [brand, models] of Object.entries(taxonomy.brands || {})) {
    for (const [modelName, packs] of Object.entries(models)) {
      for (const p of packs) {
        index.set(p.device_slug, { ...p, brand, model: modelName });
      }
    }
  }
  for (const p of (taxonomy.uncategorized || [])) {
    index.set(p.device_slug, { ...p, brand: null, model: null });
  }
  return index;
}

function relativeTimeFr(isoString) {
  if (!isoString) return "—";
  const then = new Date(isoString);
  if (isNaN(then)) return isoString;
  const diffMs = Date.now() - then.getTime();
  const mins = Math.floor(diffMs / 60000);
  if (mins < 1) return "à l'instant";
  if (mins < 60) return `il y a ${mins} min`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `il y a ${hours} h`;
  const days = Math.floor(hours / 24);
  if (days === 1) return "hier";
  if (days < 7) return `il y a ${days} j`;
  return then.toLocaleDateString("fr-FR", { day: "numeric", month: "short", year: "numeric" });
}

const STATUS_LABEL = {
  open: "ouverte",
  in_progress: "en cours",
  closed: "clôturée",
};

function statusBadgeHTML(status) {
  const label = STATUS_LABEL[status] || status || "ouverte";
  const cls = status === "closed" ? "ok" : (status === "in_progress" ? "warn" : "");
  return `<span class="badge ${cls}">${escapeHtml(label)}</span>`;
}

function repairCardHTML(repair, taxEntry) {
  const when = relativeTimeFr(repair.created_at);
  const symptom = repair.symptom || "—";
  const truncated = symptom.length > 120 ? symptom.slice(0, 118) + "…" : symptom;
  const deviceContext = taxEntry
    ? deviceName(taxEntry, { includeBrand: false })
    : repair.device_slug;
  const form = taxEntry?.form_factor
    ? `<span class="badge mono">${escapeHtml(taxEntry.form_factor)}</span>`
    : "";
  // Explicit #home hash so the bootstrap/hashchange dispatch renders the
  // dashboard (not the list) and not the graphe either. Query params are
  // preserved across later intra-section navigation.
  const href = `?device=${encodeURIComponent(repair.device_slug)}&repair=${encodeURIComponent(repair.repair_id)}#home`;
  return `
    <a class="home-card" href="${href}">
      <div class="repair-top">
        <div class="slug">${escapeHtml(repair.repair_id.slice(0, 8))} · ${escapeHtml(when)}</div>
        <div class="badges">${statusBadgeHTML(repair.status)}${form}</div>
      </div>
      <div class="name">${escapeHtml(deviceContext)}</div>
      <div class="repair-symptom">${escapeHtml(truncated)}</div>
    </a>
  `;
}

function deviceBlockHTML(taxEntry, repairs) {
  const sorted = repairs.slice().sort((a, b) => (b.created_at || "").localeCompare(a.created_at || ""));
  const cards = sorted.map(r => repairCardHTML(r, taxEntry)).join("");
  const modelName = taxEntry?.model || deviceName(taxEntry, { includeBrand: false });
  return `
    <div class="home-model">
      <div class="home-model-head">
        <span class="home-model-name">${escapeHtml(modelName)}</span>
        <span class="home-model-count mono">${sorted.length} ${sorted.length > 1 ? 'réparations' : 'réparation'}</span>
      </div>
      <div class="home-grid">${cards}</div>
    </div>
  `;
}

function brandBlockHTML(brandName, devicesMap) {
  const slugs = Array.from(devicesMap.keys()).sort((a, b) => a.localeCompare(b));
  const totalRepairs = slugs.reduce((n, s) => n + devicesMap.get(s).repairs.length, 0);
  const counter = `${totalRepairs} ${totalRepairs > 1 ? 'réparations' : 'réparation'} · ${slugs.length} ${slugs.length > 1 ? 'devices' : 'device'}`;
  const body = slugs
    .map(slug => {
      const { taxEntry, repairs } = devicesMap.get(slug);
      return deviceBlockHTML(taxEntry, repairs);
    })
    .join("");
  return `
    <section class="home-brand">
      <header class="home-brand-head">
        <h2 class="home-brand-name">${escapeHtml(brandName)}</h2>
        <span class="home-brand-count mono">${counter}</span>
      </header>
      <div class="home-brand-body">${body}</div>
    </section>
  `;
}

export function renderHome(taxonomy, repairs = []) {
  const container = document.getElementById("homeSections");
  const empty = document.getElementById("homeEmpty");
  container.innerHTML = "";

  if (!repairs || repairs.length === 0) {
    empty.classList.remove("hidden");
    return;
  }
  empty.classList.add("hidden");

  const taxIndex = indexTaxonomyBySlug(taxonomy);
  // Group repairs by brand → device_slug → list of repairs.
  const byBrand = new Map();  // brand → Map(slug → {taxEntry, repairs})
  for (const r of repairs) {
    const tax = taxIndex.get(r.device_slug) || null;
    const brand = tax?.brand || "Non catégorisé";
    if (!byBrand.has(brand)) byBrand.set(brand, new Map());
    const devices = byBrand.get(brand);
    if (!devices.has(r.device_slug)) {
      devices.set(r.device_slug, {
        taxEntry: tax || { device_slug: r.device_slug, device_label: r.device_label },
        repairs: [],
      });
    }
    devices.get(r.device_slug).repairs.push(r);
  }

  const brandNames = Array.from(byBrand.keys()).sort((a, b) => {
    if (a === "Non catégorisé") return 1;
    if (b === "Non catégorisé") return -1;
    return a.localeCompare(b);
  });
  container.innerHTML = brandNames
    .map(brand => brandBlockHTML(brand, byBrand.get(brand)))
    .join("");
}

// ───────────────────────────────────────────────────────────────
// Repair dashboard — the focused "session hub" state of #home.
// Activated when currentSession() returns non-null.
// ───────────────────────────────────────────────────────────────

export async function renderRepairDashboard(session) {
  const { device: slug, repair: rid } = session;

  // Toggle visibility: hide list states, show dashboard.
  document.getElementById("homeSections")?.classList.add("hidden");
  document.getElementById("homeEmpty")?.classList.add("hidden");
  document.getElementById("repairDashboard")?.classList.remove("hidden");
  // Also hide the list's H1 / CTA while in dashboard mode.
  document.querySelector("#homeSection .home-head")?.classList.add("hidden");

  // Fetch in parallel — list of Promise results, each tolerates failure.
  const [repair, convs, pack, findings, taxonomy] = await Promise.all([
    fetchJSON(`/pipeline/repairs/${encodeURIComponent(rid)}`, null),
    fetchJSON(`/pipeline/repairs/${encodeURIComponent(rid)}/conversations`, { conversations: [] }),
    fetchJSON(`/pipeline/packs/${encodeURIComponent(slug)}`, null),
    fetchJSON(`/pipeline/packs/${encodeURIComponent(slug)}/findings`, []),
    loadTaxonomy(),
  ]);

  const taxIndex = indexTaxonomyBySlug(taxonomy);
  const taxEntry = taxIndex.get(slug) || null;

  renderDashboardHeader(repair, taxEntry, slug, rid);
  renderDashboardData(slug, rid, pack);
  renderCapabilities(pack);
  renderDashboardConvs(convs.conversations || [], rid);
  renderDashboardFindings(findings, rid);
  renderDashboardTimeline(repair, convs.conversations || [], findings, pack);
  renderDashboardPack(pack, slug, rid);
  wireDashboardHandlers();
  wireUploadHandlers(slug, rid);
  wireFixButton(slug, rid);
}

// Mid-dashboard re-render after an upload completes — same payload as the
// initial mount, but we don't touch conversations / findings / timeline
// because those are unaffected by a boardview/schematic upload.
async function refreshDashboardData(slug, rid) {
  const pack = await fetchJSON(`/pipeline/packs/${encodeURIComponent(slug)}`, null);
  renderDashboardData(slug, rid, pack);
  renderCapabilities(pack);
  renderDashboardPack(pack, slug, rid);
}

export function hideRepairDashboard() {
  document.getElementById("repairDashboard")?.classList.add("hidden");
  document.getElementById("homeSections")?.classList.remove("hidden");
  document.querySelector("#homeSection .home-head")?.classList.remove("hidden");
  document.getElementById("dashboardFixBtn")?.classList.add("hidden");
}

async function fetchJSON(url, fallback) {
  try {
    const res = await fetch(url);
    if (!res.ok) return fallback;
    return await res.json();
  } catch (err) {
    console.warn("[dashboard] fetch failed", url, err);
    return fallback;
  }
}

function renderDashboardHeader(repair, taxEntry, slug, rid) {
  const slugEl = document.getElementById("rdSlug");
  const deviceEl = document.getElementById("rdDevice");
  const symptomEl = document.getElementById("rdSymptom");
  const badgesEl = document.getElementById("rdBadges");
  if (!slugEl || !deviceEl || !symptomEl || !badgesEl) return;

  slugEl.textContent = slug;
  deviceEl.textContent = taxEntry
    ? deviceName(taxEntry, { includeBrand: true })
    : (repair?.device_label || humanizeSlug(slug));
  symptomEl.textContent = repair?.symptom || "—";

  const created = repair?.created_at ? relativeTimeFr(repair.created_at) : "—";
  const status = repair?.status || "open";
  const form = taxEntry?.form_factor
    ? `<span class="badge mono">${escapeHtml(taxEntry.form_factor)}</span>`
    : "";
  badgesEl.innerHTML =
    `${statusBadgeHTML(status)}` +
    `<span class="badge mono">${escapeHtml(rid.slice(0, 8))}</span>` +
    form +
    `<span class="rd-created">créée ${escapeHtml(created)}</span>`;
}

const ICONS = {
  arrowRight: '<svg viewBox="0 0 24 24"><path d="M5 12h14M13 6l6 6-6 6"/></svg>',
  upload:     '<svg viewBox="0 0 24 24"><path d="M12 17V5"/><path d="M5 12l7-7 7 7"/><path d="M5 19h14"/></svg>',
};

// Pretty file-size formatter — KB/MB with one decimal. Used in card metas
// after an upload so the tech sees "iphone-x.brd · 2.4 MB" not raw bytes.
function fmtBytes(n) {
  if (!Number.isFinite(n) || n <= 0) return "—";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v < 10 && i > 0 ? 1 : 0)} ${units[i]}`;
}

// ───────────────────────────────────────────────────────────────
// Data-aware dashboard — per-input cards + per-derived-data cards.
// Each card boils down to one of: on / off / building / loading / error.
// ───────────────────────────────────────────────────────────────

function renderDashboardData(slug, rid, pack) {
  const qs = `?device=${encodeURIComponent(slug)}&repair=${encodeURIComponent(rid)}`;

  // ── INPUT 1 — Schematic PDF ────────────────────────────────────────
  setCardState("rdCardSchematic", pack?.has_schematic_pdf ? "on" : "off");
  setCardField("rdCardSchematicState", pack?.has_schematic_pdf
    ? (pack.has_electrical_graph ? "compilé" : "importé")
    : "à importer");
  setCardField("rdCardSchematicMeta", pack?.has_schematic_pdf
    ? (pack.has_electrical_graph
        ? "PDF + electrical_graph.json — l'agent peut simuler & hypothétiser."
        : "PDF importé. Compilation en cours ou en attente.")
    : "Aucun PDF — l'agent travaille à l'aveugle sur le hardware.");
  toggleEl("rdCardSchematicLoss", !pack?.has_schematic_pdf);

  const schemActions = document.getElementById("rdCardSchematicActions");
  if (schemActions) {
    schemActions.innerHTML = "";
    if (pack?.has_schematic_pdf) {
      schemActions.appendChild(linkButton(`${qs}#schematic`,
        ICONS.arrowRight + " Ouvrir", "is-primary"));
      schemActions.appendChild(actionButton(ICONS.upload + " Remplacer", () => {
        document.getElementById("rdUploadSchematic")?.click();
      }));
    } else {
      schemActions.appendChild(actionButton(
        ICONS.upload + " Importer un PDF", () => {
          document.getElementById("rdUploadSchematic")?.click();
        }, "is-warn"));
    }
  }

  // ── INPUT 2 — Boardview ─────────────────────────────────────────────
  setCardState("rdCardBoardview", pack?.has_boardview ? "on" : "off");
  setCardField("rdCardBoardviewState", pack?.has_boardview ? "importé" : "à importer");
  setCardField("rdCardBoardviewFmt", pack?.boardview_format
    ? `format ${pack.boardview_format}`
    : ".brd / .kicad_pcb / .fz / .tvw…");
  setCardField("rdCardBoardviewMeta", pack?.has_boardview
    ? `12 outils visuels disponibles · format ${pack.boardview_format || "détecté"}.`
    : "L'agent ne peut pas pointer un composant ni mesurer une distance.");
  toggleEl("rdCardBoardviewLoss", !pack?.has_boardview);

  const bvActions = document.getElementById("rdCardBoardviewActions");
  if (bvActions) {
    bvActions.innerHTML = "";
    if (pack?.has_boardview) {
      bvActions.appendChild(linkButton(`${qs}#pcb`,
        ICONS.arrowRight + " Ouvrir", "is-primary"));
      bvActions.appendChild(actionButton(ICONS.upload + " Remplacer", () => {
        document.getElementById("rdUploadBoardview")?.click();
      }));
    } else {
      bvActions.appendChild(actionButton(
        ICONS.upload + " Importer un boardview", () => {
          document.getElementById("rdUploadBoardview")?.click();
        }, "is-warn"));
    }
  }

  // ── DERIVED 1 — Knowledge graph (causal pack) ──────────────────────
  const packComplete = !!(pack && pack.has_registry && pack.has_knowledge_graph
    && pack.has_rules && pack.has_dictionary && pack.has_audit_verdict);
  const packPartial = !!(pack && (pack.has_registry || pack.has_knowledge_graph
    || pack.has_rules || pack.has_dictionary));
  const knowledgeState = packComplete ? "on" : (packPartial ? "building" : "off");
  setCardState("rdCardKnowledge", knowledgeState);
  setCardField("rdCardKnowledgeState",
    packComplete ? "approuvé" : (packPartial ? "en construction" : "vide"));
  setCardField("rdCardKnowledgeMeta",
    packComplete ? "registry + graph + rules + dictionary + audit. Prêt pour le diag."
    : packPartial ? "Pipeline en cours — l'agent pourra l'utiliser dès la fin."
    : "Construit automatiquement quand la réparation démarre.");
  const knowledgeActions = document.getElementById("rdCardKnowledgeActions");
  if (knowledgeActions) {
    knowledgeActions.innerHTML = "";
    if (packComplete || packPartial) {
      knowledgeActions.appendChild(linkButton(`${qs}#graphe`,
        ICONS.arrowRight + " Voir le graphe", packComplete ? "is-primary" : ""));
    }
  }

  // ── DERIVED 2 — Electrical graph (compiled from schematic PDF) ──────
  const electricalState = pack?.has_electrical_graph
    ? "on"
    : (pack?.has_schematic_pdf ? "building" : "off");
  setCardState("rdCardElectrical", electricalState);
  setCardField("rdCardElectricalState", pack?.has_electrical_graph
    ? "compilé"
    : (pack?.has_schematic_pdf ? "compilation" : "indisponible"));
  setCardField("rdCardElectricalMeta", pack?.has_electrical_graph
    ? "Nets, rails et boot sequence prêts. mb_schematic_graph + simulator OK."
    : (pack?.has_schematic_pdf
        ? "Le schematic est importé — la compilation se fait en arrière-plan."
        : "Importer un schematic PDF débloque ce graphe et le simulateur."));
  const electricalActions = document.getElementById("rdCardElectricalActions");
  if (electricalActions) {
    electricalActions.innerHTML = "";
    if (pack?.has_electrical_graph) {
      electricalActions.appendChild(linkButton(`${qs}#schematic`,
        ICONS.arrowRight + " Ouvrir", "is-primary"));
    }
  }

  // ── DERIVED 3 — Memory bank (rules + findings + dictionary) ────────
  const memoryState = pack?.has_rules ? "on" : (pack?.has_registry ? "building" : "off");
  setCardState("rdCardMemory", memoryState);
  setCardField("rdCardMemoryState", pack?.has_rules
    ? "active"
    : (pack?.has_registry ? "construction" : "vide"));
  setCardField("rdCardMemoryMeta", pack?.has_rules
    ? "L'agent peut faire mb_get_component, mb_get_rules_for_symptoms, mb_record_finding."
    : (pack?.has_registry
        ? "Vocabulaire en place, Clinicien rédige les règles…"
        : "Sera peuplée par le pipeline. Toujours active dès que les rules existent."));
  const memoryActions = document.getElementById("rdCardMemoryActions");
  if (memoryActions) {
    memoryActions.innerHTML = "";
    if (pack?.has_rules || pack?.has_registry) {
      memoryActions.appendChild(linkButton(`${qs}&view=md#graphe`,
        ICONS.arrowRight + " Ouvrir", pack?.has_rules ? "is-primary" : ""));
    }
  }
}

// Capability banner — single ribbon at the top showing what the AI has
// access to right now. Reads as a mission-status header.
function renderCapabilities(pack) {
  const cap = document.getElementById("rdCap");
  const title = document.getElementById("rdCapTitle");
  const body = document.getElementById("rdCapBody");
  const score = document.getElementById("rdCapScore");
  const list = document.getElementById("rdCapList");
  if (!cap || !title || !body || !score || !list) return;

  const flags = {
    schematic: !!pack?.has_schematic_pdf,
    boardview: !!pack?.has_boardview,
    graph:     !!(pack && pack.has_knowledge_graph && pack.has_rules),
    memory:    !!pack?.has_rules,
  };
  const onCount = Object.values(flags).filter(Boolean).length;
  let level = "minimal";
  let label = "Capacités IA limitées";
  let blurb = "Crée la mémoire de ce device et importe schematic + boardview pour débloquer tous les outils.";
  if (onCount === 4) {
    level = "full";
    label = "Capacités IA — toutes débloquées";
    blurb = "Schematic + boardview + mémoire chargés. L'agent dispose de l'ensemble de ses outils visuels et causaux.";
  } else if (onCount >= 2) {
    level = "partial";
    label = "Capacités IA — partielles";
    blurb = "Une partie de la boîte à outils est active. Importe les sources manquantes pour débloquer le reste.";
  } else if (onCount === 1) {
    level = "minimal";
    label = "Capacités IA — minimales";
    blurb = "L'agent peut discuter mais ne peut ni montrer ni simuler. Importe schematic + boardview pour le rendre opérationnel.";
  } else {
    level = "minimal";
    label = "Capacités IA — agent à froid";
    blurb = "Aucune source liée à ce device. Démarre une réparation et importe schematic + boardview.";
  }
  cap.dataset.level = level;
  title.textContent = label;
  body.textContent = blurb;
  score.textContent = `${onCount} / 4 sources actives`;

  const rows = [
    { key: "schematic", label: "Schematic", on: "simulateur, hypothesize", off: "off" },
    { key: "boardview", label: "Boardview", on: "12 bv_* tools", off: "off" },
    { key: "graph",     label: "Graphe",    on: "rules + dictionary",  off: "off" },
    { key: "memory",    label: "Memory",    on: "mb_* tools",          off: "off" },
  ];
  list.innerHTML = rows.map(r => {
    const on = flags[r.key];
    return `<li class="rd-cap-pill ${on ? "on" : "off"}">
      <span class="rd-cap-pill-dot"></span>
      <span class="rd-cap-pill-label">${escapeHtml(r.label)}</span>
      <span class="rd-cap-pill-tag">${escapeHtml(on ? r.on : r.off)}</span>
    </li>`;
  }).join("");
}

// Helpers ───────────────────────────────────────────────────────
function setCardState(id, state) {
  const el = document.getElementById(id);
  if (el) el.dataset.state = state;
}
function setCardField(id, text) {
  const el = document.getElementById(id);
  if (el) el.textContent = text;
}
function toggleEl(id, on) {
  const el = document.getElementById(id);
  if (el) el.hidden = !on;
}
function linkButton(href, html, extra = "") {
  const a = document.createElement("a");
  a.className = `rd-data-card-btn ${extra}`.trim();
  a.href = href;
  a.innerHTML = html;
  return a;
}
function actionButton(html, onclick, extra = "") {
  const b = document.createElement("button");
  b.type = "button";
  b.className = `rd-data-card-btn ${extra}`.trim();
  b.innerHTML = html;
  b.addEventListener("click", onclick);
  return b;
}

// ───────────────────────────────────────────────────────────────
// Upload wiring — POST /pipeline/packs/{slug}/documents
// Schematic = .pdf  →  kind=schematic_pdf
// Boardview = parser-supported extensions  →  kind=boardview
// ───────────────────────────────────────────────────────────────
let _uploadHandlersWired = false;
function wireUploadHandlers(slug, rid) {
  // Always re-bind the per-session slug/rid even on re-mount.
  const schemInput = document.getElementById("rdUploadSchematic");
  const bvInput = document.getElementById("rdUploadBoardview");
  if (schemInput) {
    schemInput.value = "";
    schemInput.onchange = (ev) => {
      const file = ev.target.files?.[0];
      if (file) handleUpload(slug, rid, file, "schematic_pdf");
      ev.target.value = "";
    };
  }
  if (bvInput) {
    bvInput.value = "";
    bvInput.onchange = (ev) => {
      const file = ev.target.files?.[0];
      if (file) handleUpload(slug, rid, file, "boardview");
      ev.target.value = "";
    };
  }
  if (_uploadHandlersWired) return;
  _uploadHandlersWired = true;

  // Drag-drop on the off-state cards. Visual hint via .is-dragover.
  const wireDrop = (cardId, kind) => {
    const card = document.getElementById(cardId);
    if (!card) return;
    card.addEventListener("dragenter", (ev) => {
      if (card.dataset.state !== "off") return;
      ev.preventDefault();
      card.classList.add("is-dragover");
    });
    card.addEventListener("dragover", (ev) => {
      if (card.dataset.state !== "off") return;
      ev.preventDefault();
      ev.dataTransfer.dropEffect = "copy";
    });
    card.addEventListener("dragleave", () => card.classList.remove("is-dragover"));
    card.addEventListener("drop", (ev) => {
      ev.preventDefault();
      card.classList.remove("is-dragover");
      const file = ev.dataTransfer?.files?.[0];
      if (!file) return;
      const slugNow = new URLSearchParams(window.location.search).get("device");
      const ridNow = new URLSearchParams(window.location.search).get("repair");
      if (!slugNow || !ridNow) return;
      handleUpload(slugNow, ridNow, file, kind);
    });
  };
  wireDrop("rdCardSchematic", "schematic_pdf");
  wireDrop("rdCardBoardview", "boardview");
}

async function handleUpload(slug, rid, file, kind) {
  const cardId = kind === "schematic_pdf" ? "rdCardSchematic" : "rdCardBoardview";
  const card = document.getElementById(cardId);
  if (card) card.dataset.state = "building";

  showToast("info", `Import ${kind === "schematic_pdf" ? "schematic" : "boardview"} en cours…`,
    `${file.name} · ${fmtBytes(file.size)}`);

  const fd = new FormData();
  fd.append("kind", kind);
  fd.append("file", file);

  try {
    const res = await fetch(`/pipeline/packs/${encodeURIComponent(slug)}/documents`, {
      method: "POST",
      body: fd,
    });
    if (!res.ok) {
      let detail = "";
      try { detail = (await res.json()).detail || ""; } catch (_) { /* noop */ }
      showToast("warn", "Échec de l'import",
        `${res.status} ${detail || "essaie un autre fichier"}`);
      // Restore previous state on failure.
      await refreshDashboardData(slug, rid);
      return;
    }
    showToast("ok", "Import terminé",
      `${file.name} · ${fmtBytes(file.size)}`);
    await refreshDashboardData(slug, rid);
  } catch (err) {
    console.error("upload failed", err);
    showToast("warn", "Réseau", "impossible de joindre le backend.");
    await refreshDashboardData(slug, rid);
  }
}

let _toastTimer = null;
function showToast(tone, title, sub) {
  const toast = document.getElementById("rdToast");
  const titleEl = document.getElementById("rdToastTitle");
  const subEl = document.getElementById("rdToastSub");
  const iconEl = document.getElementById("rdToastIcon");
  if (!toast || !titleEl || !subEl || !iconEl) return;
  toast.dataset.tone = tone;
  titleEl.textContent = title;
  subEl.textContent = sub || "";
  iconEl.innerHTML = tone === "ok"
    ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12l5 5L20 7"/></svg>'
    : tone === "warn"
    ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 8v5M12 16h.01"/></svg>'
    : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9" opacity=".3"/><path d="M21 12a9 9 0 00-9-9"><animateTransform attributeName="transform" type="rotate" from="0 12 12" to="360 12 12" dur="1s" repeatCount="indefinite"/></path></svg>';
  toast.classList.remove("hidden");
  if (_toastTimer) clearTimeout(_toastTimer);
  // "info" stays until the next showToast (upload-in-progress); ok/warn auto-clear.
  if (tone !== "info") {
    _toastTimer = setTimeout(() => toast.classList.add("hidden"), 3600);
  }
}

function renderDashboardConvs(conversations, rid) {
  const body = document.getElementById("rdConvBody");
  const count = document.getElementById("rdConvCount");
  if (!body || !count) return;
  count.textContent = String(conversations.length);
  body.innerHTML = "";
  if (conversations.length === 0) {
    body.innerHTML = '<div class="rd-block-empty">Aucune conversation — démarre une discussion avec l\'agent.</div>';
  } else {
    for (const c of conversations) {
      const row = document.createElement("button");
      row.type = "button";
      row.className = "rd-conv-row";
      row.dataset.convId = c.id;
      const tier = (c.tier || "fast").toLowerCase();
      const title = escapeHtml((c.title || `Conversation ${c.id.slice(0, 6)}`).slice(0, 80));
      const ago = c.last_turn_at ? relativeTimeFr(c.last_turn_at) : "—";
      const cost = typeof c.cost_usd === "number" ? `$${c.cost_usd.toFixed(3)}` : "—";
      row.innerHTML =
        `<span class="rd-conv-tier t-${tier}">${tier.toUpperCase()}</span>` +
        `<span class="rd-conv-title">${title}</span>` +
        `<span class="rd-conv-meta">${c.turns || 0} turns · ${cost} · ${escapeHtml(ago)}</span>`;
      row.addEventListener("click", () => {
        openPanel(c.id);  // single connect targeting the right conv
      });
      body.appendChild(row);
    }
  }
  const newBtn = document.createElement("button");
  newBtn.type = "button";
  newBtn.className = "rd-conv-new";
  newBtn.textContent = "+ Nouvelle conversation";
  newBtn.addEventListener("click", () => {
    openPanel("new");  // single connect; backend lazy-materializes on first message
  });
  body.appendChild(newBtn);
}

function renderDashboardFindings(findings, currentRid) {
  const body = document.getElementById("rdFindingsBody");
  const count = document.getElementById("rdFindingsCount");
  if (!body || !count) return;
  count.textContent = String(findings.length);
  if (findings.length === 0) {
    body.innerHTML = '<div class="rd-block-empty">Aucun finding pour ce device. L\'agent en enregistre via <code>mb_record_finding</code> quand tu confirmes une panne.</div>';
    return;
  }
  body.innerHTML = "";
  const currentShort = currentRid.slice(0, 8);
  for (const f of findings) {
    const row = document.createElement("div");
    row.className = "rd-finding-row";
    const isCurrent = f.session_id && f.session_id.startsWith(currentShort);
    const sessionChip = isCurrent
      ? `<span class="rd-finding-session current">ce repair</span>`
      : (f.session_id
          ? `<span class="rd-finding-session">${escapeHtml(f.session_id.slice(0, 8))}</span>`
          : `<span class="rd-finding-session">—</span>`);
    const notes = f.notes
      ? `<p class="rd-finding-notes">${escapeHtml(f.notes)}</p>`
      : "";
    row.innerHTML =
      `<div class="rd-finding-top">` +
        `<span class="rd-finding-refdes">${escapeHtml(f.refdes)}</span>` +
        `<span class="rd-finding-symptom">${escapeHtml(f.symptom)}</span>` +
        sessionChip +
      `</div>` +
      `<p class="rd-finding-cause">${escapeHtml(f.confirmed_cause || "—")}</p>` +
      notes;
    body.appendChild(row);
  }
}

function renderDashboardTimeline(repair, conversations, findings, pack) {
  const body = document.getElementById("rdTimelineBody");
  if (!body) return;
  const events = [];
  if (repair?.created_at) {
    events.push({ when: repair.created_at, label: "Session ouverte", kind: "cyan" });
  }
  for (const c of conversations) {
    if (c.last_turn_at) {
      events.push({
        when: c.last_turn_at,
        label: `Activité · ${(c.tier || "fast").toLowerCase()} · ${c.turns || 0} turns`,
        kind: "emerald",
      });
    }
  }
  for (const f of findings) {
    if (f.created_at) {
      events.push({
        when: f.created_at,
        label: `Finding ${f.refdes || "?"} confirmé`,
        kind: "violet",
      });
    }
  }
  if (pack?.audit_verdict) {
    events.push({
      when: repair?.created_at || new Date().toISOString(),
      label: `Pack audité — ${pack.audit_verdict}`,
      kind: pack.audit_verdict === "APPROVED" ? "emerald" : "amber",
    });
  }
  events.sort((a, b) => (b.when || "").localeCompare(a.when || ""));
  const MAX = 8;
  const shown = events.slice(0, MAX);
  body.innerHTML = shown.map(e => (
    `<li class="rd-timeline-item">` +
      `<span class="rd-timeline-node ${e.kind}"></span>` +
      `<span class="rd-timeline-when">${escapeHtml(relativeTimeFr(e.when))}</span>` +
      `<span class="rd-timeline-label">${escapeHtml(e.label)}</span>` +
    `</li>`
  )).join("");
  if (events.length > MAX) {
    body.innerHTML += `<li class="rd-timeline-item"><span class="rd-timeline-node"></span><span class="rd-timeline-label">+${events.length - MAX} plus anciens</span></li>`;
  }
  if (events.length === 0) {
    body.innerHTML = '<li class="rd-block-empty">Aucune activité.</li>';
  }
}

function renderDashboardPack(pack, slug, rid) {
  const body = document.getElementById("rdPackBody");
  if (!body) return;
  if (!pack) {
    body.innerHTML = '<div class="rd-block-empty">Aucun pack — la mémoire du device n\'est pas encore construite.</div>';
    return;
  }
  const arts = [
    { key: "has_registry", label: "registry" },
    { key: "has_knowledge_graph", label: "knowledge_graph" },
    { key: "has_rules", label: "rules" },
    { key: "has_dictionary", label: "dictionary" },
    { key: "has_audit_verdict", label: "audit" },
  ];
  const presentCount = arts.filter(a => !!pack[a.key]).length;
  const complete = presentCount === arts.length;
  const statusLabel = complete ? "APPROUVÉ" : "en construction";
  const statusClass = complete ? "ok" : "warn";
  const rows = arts.map(a => {
    const on = !!pack[a.key];
    return `<li class="rd-pack-row ${on ? "on" : "off"}">` +
      `<span class="rd-pack-tick" aria-hidden="true">${on ? ICON_CHECK : "·"}</span>` +
      `<span class="rd-pack-label">${a.label}</span>` +
    `</li>`;
  }).join("");
  body.innerHTML =
    `<div class="rd-pack-status">` +
      `<span class="rd-pack-status-label ${statusClass}">${statusLabel}</span>` +
      `<span class="rd-pack-count">${presentCount}/${arts.length}</span>` +
    `</div>` +
    `<ul class="rd-pack-rows">${rows}</ul>`;
}

let _dashboardHandlersWired = false;
function wireDashboardHandlers() {
  if (_dashboardHandlersWired) return;
  _dashboardHandlersWired = true;
  document.getElementById("rdLeaveBtn")?.addEventListener("click", () => {
    leaveSession();
  });
}

function wireFixButton(slug, rid) {
  const btn = document.getElementById("dashboardFixBtn");
  if (!btn) return;
  // Expose a reset hook so llm.js can clear the pending state when the
  // validation flow fails (agent refuses, MA tool missing, error event).
  const resetBtn = () => {
    btn.disabled = false;
    btn.innerHTML = ICON_CHECK + " Marquer fix";
    btn.classList.remove("is-validated");
    if (btn._fixTimeoutId) { clearTimeout(btn._fixTimeoutId); btn._fixTimeoutId = null; }
  };
  window.__resetDashboardFixBtn = resetBtn;
  btn.classList.remove("hidden");
  resetBtn();
  btn.onclick = () => {
    const ws = window.__diagnosticWS;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      btn.textContent = "Ouvre le chat d'abord";
      setTimeout(() => { btn.innerHTML = ICON_CHECK + " Marquer fix"; }, 1800);
      return;
    }
    ws.send(JSON.stringify({ type: "validation.start", repair_id: rid }));
    btn.disabled = true;
    btn.textContent = "… Claude valide";
    // Safety timeout: if the agent never fires simulation.repair_validated
    // (MA tool missing, refusal, error), reset after 25s so the button
    // isn't permanently stuck.
    btn._fixTimeoutId = setTimeout(() => {
      btn.textContent = "Échec — réessaie";
      setTimeout(resetBtn, 2200);
    }, 25000);
  };
}

/* ---------- NEW REPAIR MODAL ---------- */
const newRepairBackdrop = document.getElementById("newRepairBackdrop");
const newRepairForm     = document.getElementById("newRepairForm");
const newRepairDevice   = document.getElementById("newRepairDevice");
const newRepairSymptom  = document.getElementById("newRepairSymptom");
const newRepairSubmit   = document.getElementById("newRepairSubmit");
const newRepairError    = document.getElementById("newRepairError");
const newRepairCombo    = document.getElementById("newRepairCombo");
const newRepairPanel    = document.getElementById("newRepairComboPanel");
const newRepairHint     = document.getElementById("newRepairDeviceHint");
const newRepairRebuildRow = document.getElementById("newRepairRebuildRow");
const newRepairForceRebuild = document.getElementById("newRepairForceRebuild");
let   newRepairLastFocus = null;
let   comboEntries = [];      // flat list of known devices
let   comboActiveIndex = -1;  // keyboard-highlighted option
// When the user PICKS an existing entry from the combobox we keep the pack's
// original device_label + slug here so the submit hits the exact same slug
// server-side. Free typing resets both — we only want this mapping for clicks.
let   selectedEntryLabel = null;
let   selectedEntrySlug = null;

function openNewRepair() {
  newRepairLastFocus = document.activeElement;
  newRepairForm.reset();
  setNewRepairError(null);
  setNewRepairBusy(false);
  newRepairRebuildRow.hidden = true;
  newRepairForceRebuild.checked = false;
  selectedEntryLabel = null;
  selectedEntrySlug = null;
  newRepairBackdrop.classList.add("open");
  newRepairBackdrop.setAttribute("aria-hidden", "false");
  // Kick off the taxonomy fetch and cache it for the session — small payload.
  refreshComboEntries();
  // Let the backdrop fade-in paint, then focus first field.
  requestAnimationFrame(() => newRepairDevice.focus());
}

function closeNewRepair() {
  if (!newRepairBackdrop.classList.contains("open")) return;
  newRepairBackdrop.classList.remove("open");
  newRepairBackdrop.setAttribute("aria-hidden", "true");
  setNewRepairBusy(false);
  hideComboPanel();
  if (newRepairLastFocus && typeof newRepairLastFocus.focus === "function") {
    newRepairLastFocus.focus();
  }
}

/* ---------- Combobox — device autocomplete ---------- */

async function refreshComboEntries() {
  const tax = await loadTaxonomy();
  const entries = [];
  for (const [brand, models] of Object.entries(tax.brands || {})) {
    for (const [modelName, packs] of Object.entries(models)) {
      for (const p of packs) {
        entries.push({ ...p, brand, model: modelName });
      }
    }
  }
  for (const p of tax.uncategorized || []) {
    entries.push({ ...p, brand: null, model: null });
  }
  comboEntries = entries;
}

// Normalize: lowercase, strip accents, collapse whitespace. Used both on the
// query and on every candidate field so the match is case- and accent-agnostic.
function normalize(s) {
  return (s || "")
    .toString()
    .toLowerCase()
    .normalize("NFD").replace(/[̀-ͯ]/g, "")
    .replace(/[^a-z0-9]+/g, " ")
    .trim();
}

function scoreEntry(entry, qNorm, qTokens, qInline) {
  // Concatenate the searchable surface once, then rank.
  const haystack = normalize(
    [entry.device_label, entry.device_slug, entry.brand, entry.model, entry.version, entry.form_factor]
      .filter(Boolean).join(" ")
  );
  if (!qNorm) return 1;                         // empty query → all pass
  if (haystack === qNorm) return 1000;          // exact full-label match
  if (haystack.startsWith(qNorm)) return 500;   // prefix match
  if (haystack.includes(qNorm)) return 300;     // contiguous substring
  // Space-insensitive substring: lets "iphoneX" match "iPhone X", handy when
  // the tech omits spaces or concatenates brand+model.
  const haystackInline = haystack.replace(/ /g, "");
  if (qInline && haystackInline.includes(qInline)) return 200;
  // Token coverage: every query token must appear somewhere in the haystack.
  // Tolerates word-level reordering ("motherboard reform mnt"), and partial
  // prefix typing ("refo" matches "reform").
  let covered = 0;
  for (const t of qTokens) {
    if (!t) continue;
    if (haystack.includes(t)) covered++;
  }
  if (covered === qTokens.length) return 100 + covered;
  if (covered >= Math.ceil(qTokens.length / 2)) return 30 + covered;
  return 0;
}

function filterEntries(query) {
  const qNorm = normalize(query);
  const qTokens = qNorm.split(" ").filter(Boolean);
  const qInline = qNorm.replace(/ /g, "");
  return comboEntries
    .map(entry => ({ entry, score: scoreEntry(entry, qNorm, qTokens, qInline) }))
    .filter(x => x.score > 0)
    .sort((a, b) => b.score - a.score || a.entry.device_label.localeCompare(b.entry.device_label));
}

// Highlight every occurrence of the normalized query's substrings in the raw
// label, without stripping the original casing.
function highlight(raw, query) {
  if (!query) return escapeHtml(raw);
  const qNorm = normalize(query);
  if (!qNorm) return escapeHtml(raw);
  const rawNorm = normalize(raw);
  const idx = rawNorm.indexOf(qNorm);
  if (idx === -1) return escapeHtml(raw);
  // Map back to original-string offsets. normalize collapses whitespace and
  // strips accents 1:1 so the offsets are the same length; good enough here.
  const start = idx;
  const end = idx + qNorm.length;
  return escapeHtml(raw.slice(0, start))
       + "<mark>" + escapeHtml(raw.slice(start, end)) + "</mark>"
       + escapeHtml(raw.slice(end));
}

function renderComboPanel(query) {
  const results = filterEntries(query);
  const groups = new Map(); // brand → entries[]
  for (const { entry } of results) {
    const key = entry.brand || "Non catégorisé";
    (groups.get(key) || groups.set(key, []).get(key)).push(entry);
  }

  const parts = [];
  const trimmed = query.trim();
  const exactExists = results.some(r => normalize(r.entry.device_label) === normalize(trimmed));
  if (trimmed && !exactExists) {
    parts.push(`
      <button type="button" class="combo-option combo-create" data-action="create"
              data-label="${escapeHtml(trimmed)}" role="option">
        <span class="combo-label">+ Créer « ${escapeHtml(trimmed)} »</span>
        <span class="combo-meta"><span class="combo-badge">nouveau</span></span>
      </button>
    `);
  }

  if (groups.size === 0 && !trimmed) {
    parts.push('<div class="combo-empty">Aucun device connu — tape un nom pour en créer un.</div>');
  }

  const sortedBrands = Array.from(groups.keys()).sort((a, b) => a.localeCompare(b));
  for (const brand of sortedBrands) {
    const entries = groups.get(brand);
    parts.push(`
      <div class="combo-section">
        <div class="combo-section-head">
          <span>${escapeHtml(brand)}</span>
          <span class="combo-section-count">${entries.length}</span>
        </div>
    `);
    for (const e of entries) {
      // Inside a brand section the brand is already in the header — show only
      // the model/device, keep the form_factor as a separate mono chip.
      const name = deviceName(e, { includeBrand: false });
      const badges = [
        e.complete ? '<span class="combo-badge ok">audité</span>' : '<span class="combo-badge">partiel</span>',
        e.form_factor ? `<span class="combo-badge">${escapeHtml(e.form_factor)}</span>` : '',
      ].filter(Boolean).join("");
      parts.push(`
        <button type="button" class="combo-option" role="option"
                data-action="select"
                data-slug="${escapeHtml(e.device_slug)}"
                data-label="${escapeHtml(e.device_label)}"
                data-complete="${e.complete ? "1" : "0"}">
          <span class="combo-label">${highlight(name, query)}</span>
          <span class="combo-meta">${badges}</span>
        </button>
      `);
    }
    parts.push('</div>');
  }

  newRepairPanel.innerHTML = parts.join("");
  newRepairPanel.hidden = false;
  newRepairDevice.setAttribute("aria-expanded", "true");
  comboActiveIndex = -1;
  syncComboActive();
}

function hideComboPanel() {
  newRepairPanel.hidden = true;
  newRepairDevice.setAttribute("aria-expanded", "false");
  comboActiveIndex = -1;
}

function comboOptions() {
  return Array.from(newRepairPanel.querySelectorAll(".combo-option"));
}

function syncComboActive() {
  comboOptions().forEach((el, i) => el.classList.toggle("active", i === comboActiveIndex));
}

function comboMoveActive(delta) {
  const opts = comboOptions();
  if (opts.length === 0) return;
  comboActiveIndex = (comboActiveIndex + delta + opts.length) % opts.length;
  syncComboActive();
  opts[comboActiveIndex].scrollIntoView({ block: "nearest" });
}

// Picking an existing entry from the combobox. We display the CLEAN name
// ({brand} {model}) in the input — no form_factor clutter — but we keep the
// original device_label + slug aside so the submit resolves to the exact
// same pack slug server-side.
function applyExistingEntry(entry) {
  newRepairDevice.value = deviceName(entry, { includeBrand: true });
  selectedEntryLabel = entry.device_label;
  selectedEntrySlug = entry.device_slug;
  hideComboPanel();
  applyRebuildStateForEntry(entry);
}

// Picking the "+ Créer « … »" row — the user wants a brand-new device
// with whatever string they typed.
function applyNewDeviceSelection(rawText) {
  newRepairDevice.value = rawText;
  selectedEntryLabel = null;
  selectedEntrySlug = null;
  hideComboPanel();
  applyRebuildStateForTyped();
}

function applyRebuildStateForEntry(entry) {
  if (entry.complete) {
    newRepairRebuildRow.hidden = false;
    newRepairHint.textContent =
      "Pack déjà construit — la session rouvre directement. Coche pour regénérer.";
  } else {
    newRepairRebuildRow.hidden = true;
    newRepairForceRebuild.checked = false;
    newRepairHint.textContent =
      "Pack existe mais incomplet — le pipeline va compléter les artefacts manquants.";
  }
}

function applyRebuildStateForTyped() {
  newRepairRebuildRow.hidden = true;
  newRepairForceRebuild.checked = false;
  newRepairHint.textContent =
    "Tape le nom du device (marque + modèle). Le type de board est détecté automatiquement.";
}

function commitOption(el) {
  if (!el) return;
  if (el.dataset.action === "select") {
    const slug = el.dataset.slug;
    const entry = comboEntries.find(e => e.device_slug === slug);
    if (entry) applyExistingEntry(entry);
  } else if (el.dataset.action === "create") {
    applyNewDeviceSelection(el.dataset.label);
  }
}

function initCombo() {
  newRepairDevice.addEventListener("focus", () => {
    renderComboPanel(newRepairDevice.value);
  });
  newRepairDevice.addEventListener("input", () => {
    // Free typing — the picked-entry mapping no longer applies.
    selectedEntryLabel = null;
    selectedEntrySlug = null;
    renderComboPanel(newRepairDevice.value);
    applyRebuildStateForTyped();
  });
  newRepairDevice.addEventListener("keydown", ev => {
    if (newRepairPanel.hidden) return;
    if (ev.key === "ArrowDown") { ev.preventDefault(); comboMoveActive(1); return; }
    if (ev.key === "ArrowUp")   { ev.preventDefault(); comboMoveActive(-1); return; }
    if (ev.key === "Enter" && comboActiveIndex >= 0) {
      ev.preventDefault();
      commitOption(comboOptions()[comboActiveIndex]);
      return;
    }
    if (ev.key === "Escape") {
      ev.preventDefault();
      ev.stopPropagation();
      hideComboPanel();
    }
  });
  newRepairPanel.addEventListener("mousedown", ev => {
    const opt = ev.target.closest(".combo-option");
    if (!opt) return;
    ev.preventDefault();  // keep input focus
    commitOption(opt);
  });
  // Click outside closes.
  document.addEventListener("mousedown", ev => {
    if (newRepairPanel.hidden) return;
    if (newRepairCombo.contains(ev.target)) return;
    hideComboPanel();
  });
  // Tab-away from the input closes the panel too. setTimeout lets an in-panel
  // click fire first (since mousedown on an option preventDefault'd the blur).
  newRepairDevice.addEventListener("blur", () => {
    setTimeout(() => {
      if (!newRepairCombo.contains(document.activeElement)) hideComboPanel();
    }, 120);
  });
}

function setNewRepairError(msg, opts) {
  if (!msg) {
    newRepairError.hidden = true;
    newRepairError.textContent = "";
    return;
  }
  newRepairError.hidden = false;
  newRepairError.innerHTML = "";
  if (opts && opts.title) {
    const s = document.createElement("strong");
    s.textContent = opts.title;
    newRepairError.appendChild(s);
  }
  newRepairError.appendChild(document.createTextNode(msg));
}

function setNewRepairBusy(busy) {
  newRepairSubmit.disabled  = busy;
  newRepairDevice.disabled  = busy;
  newRepairSymptom.disabled = busy;
  newRepairSubmit.setAttribute("aria-busy", busy ? "true" : "false");
  const label = newRepairSubmit.querySelector(".btn-label");
  if (label) {
    label.innerHTML = busy
      ? '<span class="modal-spinner" aria-hidden="true"></span> Création…'
      : "Démarrer le diagnostic";
  }
}

async function submitNewRepair(ev) {
  ev.preventDefault();
  // When the user picked an existing entry from the combobox, send its
  // ORIGINAL device_label AND the canonical device_slug so the backend
  // resolves to the exact pack on disk — regardless of any Registry-rewrite
  // drift between device_label and the directory name. Only fall back to the
  // input value for a brand-new device the user typed out.
  const typedValue = newRepairDevice.value.trim();
  const device_label = selectedEntryLabel || typedValue;
  const device_slug  = selectedEntrySlug || null;
  const symptom      = newRepairSymptom.value.trim();
  const force_rebuild = newRepairForceRebuild.checked;
  if (device_label.length < 2) {
    setNewRepairError("Le nom du device doit faire au moins 2 caractères.", {title:"Champ incomplet — "});
    newRepairDevice.focus();
    return;
  }
  if (symptom.length < 5) {
    setNewRepairError("Décris le symptôme — 5 caractères minimum.", {title:"Champ incomplet — "});
    newRepairSymptom.focus();
    return;
  }
  setNewRepairError(null);
  setNewRepairBusy(true);
  try {
    const res = await fetch("/pipeline/repairs", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({device_label, device_slug, symptom, force_rebuild}),
    });
    if (!res.ok) {
      let detail = "";
      try { detail = (await res.json()).detail || ""; } catch (_) { /* noop */ }
      setNewRepairError(`Le backend a répondu ${res.status}. ${detail}`.trim(), {title:"Erreur — "});
      setNewRepairBusy(false);
      return;
    }
    const repair = await res.json();
    // Close the modal, then hand off to the pipeline progress drawer which
    // either redirects immediately (pack already built) or streams live events.
    closeNewRepair();
    openPipelineProgress(repair);
  } catch (err) {
    console.error("newRepair submit failed", err);
    setNewRepairError(
      "Impossible de joindre le serveur. Vérifie que le backend tourne.",
      {title:"Réseau — "}
    );
    setNewRepairBusy(false);
  }
}

function trapNewRepairFocus(ev) {
  if (ev.key !== "Tab") return;
  const focusables = newRepairBackdrop.querySelectorAll(
    'button:not([disabled]), input:not([disabled]), textarea:not([disabled]), [href], [tabindex]:not([tabindex="-1"])'
  );
  if (focusables.length === 0) return;
  const first = focusables[0];
  const last  = focusables[focusables.length - 1];
  if (ev.shiftKey && document.activeElement === first) {
    ev.preventDefault(); last.focus();
  } else if (!ev.shiftKey && document.activeElement === last) {
    ev.preventDefault(); first.focus();
  }
}

export function initNewRepairModal() {
  document.getElementById("homeNewBtn").addEventListener("click", openNewRepair);
  document.getElementById("newRepairClose").addEventListener("click", closeNewRepair);
  document.getElementById("newRepairCancel").addEventListener("click", closeNewRepair);
  newRepairForm.addEventListener("submit", submitNewRepair);
  newRepairBackdrop.addEventListener("click", ev => {
    if (ev.target === newRepairBackdrop) closeNewRepair();
  });
  initCombo();

  // Registered BEFORE the global ESC/Cmd+K handler, so we can intercept those
  // keys while the modal is open without closing the Inspector or stealing focus.
  document.addEventListener("keydown", ev => {
    if (!newRepairBackdrop.classList.contains("open")) return;
    if (ev.key === "Escape") {
      ev.preventDefault(); ev.stopImmediatePropagation(); closeNewRepair(); return;
    }
    if ((ev.metaKey || ev.ctrlKey) && ev.key.toLowerCase() === "k") {
      ev.preventDefault(); ev.stopImmediatePropagation(); return;
    }
    if ((ev.metaKey || ev.ctrlKey) && ev.key === "Enter") {
      ev.preventDefault(); ev.stopImmediatePropagation();
      if (!newRepairSubmit.disabled) newRepairForm.requestSubmit();
      return;
    }
    trapNewRepairFocus(ev);
  });
}
