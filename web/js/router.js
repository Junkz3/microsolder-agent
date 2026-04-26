// Hash-based section router + chrome (topbar crumbs, mode pill, metabar).
// Owns navigation between the 8 app sections and refreshes the chrome when
// the active section or current device changes.

export const SECTIONS = ["home", "pcb", "schematic", "graphe", "profile"];

// SECTION_META holds i18n keys instead of literal strings — resolved at
// render time inside updateChrome(). On locale switch, refreshChrome() is
// re-invoked through the i18n.onChange hook below.
const SECTION_META = {
  home:          {crumbKey: "router.section.home",      mode: {tagKey: "router.mode.journal_tag", subKey: "router.mode.journal_repairs",   color: "cyan"}},
  pcb:           {crumbKey: "router.section.pcb",       mode: {tagKey: "router.mode.tool_tag",    subKey: "router.mode.tool_boardview",    color: "cyan"}},
  schematic:     {crumbKey: "router.section.schematic", mode: {tagKey: "router.mode.tool_tag",    subKey: "router.mode.tool_schematic",    color: "emerald"}},
  graphe:        {crumbKey: "router.section.graphe",    mode: {tagKey: "router.mode.wait_tag",    subKey: "router.mode.wait_no_memory",    color: "amber"}},
  profile:       {crumbKey: "router.section.profile",   mode: {tagKey: "router.mode.profile_tag", subKey: "router.mode.profile_sub",       color: "cyan"}},
};

export function prettifySlug(slug) {
  if (!slug) return "";
  return slug.replace(/-/g, " ").replace(/\b\w/g, c => c.toUpperCase());
}

async function loadPackSummary(slug) {
  if (!slug) return null;
  try {
    const res = await fetch(`/pipeline/packs/${encodeURIComponent(slug)}`);
    if (!res.ok) return null;
    return await res.json();
  } catch (err) {
    console.warn("loadPackSummary failed", err);
    return null;
  }
}

// Repair metadata cache for the topbar — keyed by repair_id, populated
// lazily by ensureRepairMeta on cache miss. Lets the breadcrumb show the
// human symptom and the session-pill show the start date instead of the
// raw UUID, without making updateChrome async.
const _repairCache = new Map();
const _repairCacheInFlight = new Map();

async function ensureRepairMeta(repairId) {
  if (!repairId) return null;
  if (_repairCache.has(repairId)) return _repairCache.get(repairId);
  if (_repairCacheInFlight.has(repairId)) return _repairCacheInFlight.get(repairId);
  const p = (async () => {
    try {
      const res = await fetch(`/pipeline/repairs/${encodeURIComponent(repairId)}`);
      if (!res.ok) return null;
      const data = await res.json();
      _repairCache.set(repairId, data);
      return data;
    } catch (_err) {
      return null;
    }
  })();
  _repairCacheInFlight.set(repairId, p);
  try { return await p; } finally { _repairCacheInFlight.delete(repairId); }
}

const _dateFmt = new Intl.DateTimeFormat("fr-FR", {
  day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
});
function formatRepairDate(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  // fr-FR formats as "26 avr., 14:32" — drop the comma to read as one phrase.
  return _dateFmt.format(d).replace(/,\s*/g, " ");
}

function truncateForCrumb(text, max = 38) {
  if (!text) return "";
  return text.length > max ? text.slice(0, max - 1).trimEnd() + "…" : text;
}

function renderCrumbs(items) {
  const el = document.getElementById("crumbs");
  el.innerHTML = "";
  items.forEach((it, i) => {
    if (i > 0) {
      const sep = document.createElement("span");
      sep.className = "sep";
      sep.textContent = "/";
      el.appendChild(sep);
    }
    const text = typeof it === "string" ? it : it.text;
    const title = typeof it === "string" ? null : it.title;
    const span = document.createElement("span");
    if (i === items.length - 1) span.classList.add("active");
    span.textContent = text;
    if (title) span.title = title;
    el.appendChild(span);
  });
}

function isPackComplete(pack) {
  return !!(pack && pack.has_registry && pack.has_knowledge_graph
         && pack.has_rules && pack.has_dictionary && pack.has_audit_verdict);
}

function packMissingFiles(pack) {
  if (!pack) return [];
  const missing = [];
  if (!pack.has_registry)        missing.push("registry");
  if (!pack.has_knowledge_graph) missing.push("graph");
  if (!pack.has_rules)           missing.push("rules");
  if (!pack.has_dictionary)      missing.push("dictionary");
  if (!pack.has_audit_verdict)   missing.push("audit");
  return missing;
}

function updateChrome(section, deviceSlug, pack) {
  const t = (window.t || ((k) => k));
  let meta = SECTION_META[section] || SECTION_META.home;
  // Home's mode-pill reflects whether a session is active. Without a session,
  // it reads the journal/repairs default. With a session, it reads "Session"
  // to signal we're on the dashboard, not the list.
  const activeSession = currentSession();
  if (section === "home" && activeSession) {
    meta = { ...meta, mode: { ...meta.mode, subKey: "router.mode.journal_session" } };
  }

  // Mode pill — static per-section, overridden on Graphe by pack state.
  let mode = meta.mode;
  if (section === "graphe") {
    if (!deviceSlug) {
      mode = {tagKey: "router.mode.wait_tag", subKey: "router.mode.wait_no_repair", color: "amber"};
    } else if (isPackComplete(pack)) {
      mode = {tagKey: "router.mode.memory_tag", subKey: "router.mode.memory_graph", color: "cyan"};
    } else if (pack) {
      mode = {tagKey: "router.mode.build_tag", subKey: "router.mode.build_in_progress", color: "amber"};
    } else {
      mode = {tagKey: "router.mode.wait_tag", subKey: "router.mode.wait_unbuilt", color: "amber"};
    }
  }
  const pill = document.getElementById("modePill");
  pill.className = `mode-pill ${mode.color}`;
  document.getElementById("modePillText").textContent = `${t(mode.tagKey)} · ${t(mode.subKey)}`;

  // Repair metadata for the active session — drives the symptom in the
  // breadcrumb and the start date in the session-pill. Synchronous read;
  // a cache miss kicks off an async fetch + re-render at the bottom.
  const sessionMeta = activeSession ? _repairCache.get(activeSession.repair) : null;

  // Session pill — persistent across sections when a session is active.
  const sessionPill = document.getElementById("sessionPill");
  if (sessionPill) {
    if (activeSession) {
      sessionPill.classList.remove("hidden");
      const devEl = document.getElementById("sessionPillDevice");
      const ridEl = document.getElementById("sessionPillRid");
      if (devEl) devEl.textContent = prettifySlug(activeSession.device);
      if (ridEl) {
        if (sessionMeta && sessionMeta.created_at) {
          ridEl.textContent = formatRepairDate(sessionMeta.created_at);
          ridEl.title = `Repair ${activeSession.repair}`;
        } else {
          ridEl.textContent = activeSession.repair.slice(0, 8);
          ridEl.title = `Repair ${activeSession.repair}`;
        }
      }
    } else {
      sessionPill.classList.add("hidden");
    }
  }

  // Breadcrumbs — contextual path: device / symptom / section.
  // The brand name already lives in the .brand block on the left, so we
  // don't repeat it here. The symptom comes from the repair metadata; on
  // cache miss we fall back to the UUID-short and refresh asynchronously.
  const crumbs = [];
  if (activeSession) {
    crumbs.push(prettifySlug(activeSession.device));
    if (sessionMeta && sessionMeta.symptom) {
      crumbs.push({ text: truncateForCrumb(sessionMeta.symptom), title: sessionMeta.symptom });
    } else {
      crumbs.push(activeSession.repair.slice(0, 8));
    }
  } else if (deviceSlug) {
    crumbs.push(prettifySlug(deviceSlug));
  }
  crumbs.push(t(meta.crumbKey));
  renderCrumbs(crumbs);

  // Async upgrade — fetch repair meta on miss, re-render the chrome once
  // it lands. The cache prevents the recursive call from looping.
  if (activeSession && !sessionMeta) {
    ensureRepairMeta(activeSession.repair).then(m => {
      if (m) updateChrome(section, deviceSlug, pack);
    });
  }

  // Metabar — Graphe-only. body.no-metabar pulls .canvas/.home/.stub up.
  document.body.classList.toggle("no-metabar", section !== "graphe");
  // Section-specific class so scoped styles (boardview colour config rows in
  // the Tweaks panel, etc.) can show / hide per active section.
  document.body.dataset.section = section;
  if (section !== "graphe") return;

  const deviceEl = document.getElementById("metaDevice");
  const statusEl = document.getElementById("metaStatus");
  if (!deviceSlug) {
    deviceEl.innerHTML = `<span style="color:var(--text-3)">${t("router.metabar.no_repair")}</span>`;
    statusEl.className = "warn info";
    statusEl.innerHTML = `<svg class="icon icon-sm" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 8v5M12 16h.01"/></svg>${t("router.metabar.open_repair_to_view_graph")}`;
    return;
  }

  deviceEl.innerHTML = `<span class="tag">${deviceSlug}</span><span>·</span><span>${prettifySlug(deviceSlug)}</span>`;

  if (!pack) {
    statusEl.className = "warn";
    statusEl.innerHTML = `<svg class="icon icon-sm" viewBox="0 0 24 24"><path d="M12 3l10 18H2z"/><path d="M12 10v5M12 18v.01"/></svg>${t("router.metabar.no_memory_for_device")}`;
  } else if (isPackComplete(pack)) {
    statusEl.className = "warn ok";
    statusEl.innerHTML = `<svg class="icon icon-sm" viewBox="0 0 24 24"><path d="M5 12l5 5L20 7"/></svg>${t("router.metabar.memory_loaded_approved")}`;
  } else {
    const missing = packMissingFiles(pack);
    statusEl.className = "warn";
    statusEl.innerHTML = `<svg class="icon icon-sm" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>${t("router.metabar.memory_building_missing", { missing: missing.join(", ") })}`;
  }
}

function refreshChrome(section) {
  const params = new URLSearchParams(window.location.search);
  const slug = params.get("device");

  // Provisional synchronous update (no pack yet) — prevents FOUC.
  updateChrome(section, slug, null);

  // For Graphe with a device, fetch pack summary and refine.
  if (section === "graphe" && slug) {
    loadPackSummary(slug).then(pack => {
      // Guard: user may have navigated away while fetch was in flight.
      if (currentSection() === section) updateChrome(section, slug, pack);
    });
  }
}

export function currentSection() {
  const h = (window.location.hash || "#home").slice(1);
  return SECTIONS.includes(h) ? h : "home";
}

function setActiveRail(which) {
  document.querySelectorAll(".rail-btn").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.section === which);
  });
}

export function navigate(section) {
  if (!SECTIONS.includes(section)) section = "home";
  setActiveRail(section);
  // Hide all known section DOMs, show the target.
  document.getElementById("homeSection").classList.toggle("hidden", section !== "home");
  // The "graphe" section is a merged Mémoire view — the visible child
  // (canvas vs memoryBank) is driven by the view mode (graph|md).
  // When leaving this section, hide both children so they don't leak
  // into another route.
  const inMemoire = section === "graphe";
  if (!inMemoire) {
    document.getElementById("canvas").classList.add("hidden");
    document.getElementById("memoryBank").classList.add("hidden");
  } else {
    applyMemoireMode(currentViewMode());
  }
  document.getElementById("profileSection").classList.toggle("hidden", section !== "profile");
  document.querySelectorAll("[data-section-stub]").forEach(el => {
    el.classList.toggle("hidden", el.dataset.sectionStub !== section);
  });
  refreshChrome(section);
  if (section === "pcb") {
    // brd_viewer.js loads as a deferred module; on first-load navigation
    // (user hits /#pcb directly) the function may not be defined yet when
    // navigate() runs from the boot IIFE. Try now, and retry once when
    // the module is guaranteed to have executed.
    const runPcbInit = () => {
      const root = document.getElementById("brdRoot");
      if (root && typeof window.initBoardview === "function") {
        window.initBoardview(root);
        return true;
      }
      return false;
    };
    if (!runPcbInit()) {
      window.addEventListener("load", runPcbInit, { once: true });
    }
  }
}

export function wireRouter() {
  window.addEventListener("hashchange", () => navigate(currentSection()));
  // Re-render the topbar chrome (mode pill, breadcrumbs, metabar status text)
  // when the user toggles EN/FR. The DOM-level [data-i18n] elements are
  // refreshed by i18n.applyDom; chrome content that is built imperatively
  // from current section + pack state must be redrawn here.
  if (window.i18n && typeof window.i18n.onChange === "function") {
    window.i18n.onChange(() => refreshChrome(currentSection()));
  }
  document.querySelectorAll(".rail-btn[data-section]").forEach(btn => {
    btn.addEventListener("click", () => {
      window.location.hash = "#" + btn.dataset.section;
    });
  });
  // Toggle buttons: clicking sets the mode + re-applies. The actual
  // memory-bank data fetch on first entry in md mode is handled by
  // main.js (which owns loadMemoryBank).
  document.querySelectorAll(".view-toggle-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      const mode = btn.dataset.view;
      applyMemoireMode(mode);
      // On first entry into Brut mode, make sure the memory bank is
      // populated — loadMemoryBank is idempotent.
      if (mode === "md") {
        import("./memory_bank.js").then(m => m.loadMemoryBank?.());
      } else {
        // Switching back to Visuel: the canvas just became visible with
        // real dimensions. Trigger the graph load (idempotent via
        // _graphLoadedSlug guard in main.js) so layoutNodes + fitToScreen
        // see correct clientWidth/clientHeight. If we don't do this, a
        // load attempted while canvas was hidden bails out without
        // marking the slug mounted, and the view would stay empty.
        window.__maybeLoadGraph?.();
      }
    });
  });
}

/**
 * Which memoire view is active, derived from the `view` query param.
 * Defaults to "graph" when absent or invalid.
 */
export function currentViewMode() {
  const v = new URLSearchParams(window.location.search).get("view");
  return v === "md" ? "md" : "graph";
}

/**
 * Apply the memoire view mode — toggle DOM visibility of canvas vs
 * memoryBank, update the toggle-button active state, hide/show the
 * graph-specific filter chips in the metabar, and update the URL's
 * `view` param without reloading the page.
 */
export function applyMemoireMode(mode) {
  mode = mode === "md" ? "md" : "graph";
  document.getElementById("canvas").classList.toggle("hidden", mode !== "graph");
  document.getElementById("memoryBank").classList.toggle("hidden", mode !== "md");
  // Graph-specific filter chips + search live in .metabar .filters.
  const filtersEl = document.querySelector(".metabar .filters");
  if (filtersEl) filtersEl.classList.toggle("hidden", mode !== "graph");
  document.querySelectorAll(".view-toggle-btn").forEach(btn => {
    const on = btn.dataset.view === mode;
    btn.classList.toggle("active", on);
    btn.setAttribute("aria-pressed", on ? "true" : "false");
  });
  // Persist the choice in the URL without reloading — replaceState keeps
  // history clean (toggling back and forth shouldn't pollute back-button).
  const url = new URL(window.location.href);
  if (mode === "md") {
    url.searchParams.set("view", "md");
  } else {
    url.searchParams.delete("view");
  }
  window.history.replaceState({}, "", url.toString());
}

/**
 * Return the currently active repair session, derived from URL query params.
 * A session is defined by the SIMULTANEOUS presence of ?device= and ?repair=.
 * Re-derived on every call — zero hidden state.
 */
export function currentSession() {
  const params = new URLSearchParams(window.location.search);
  const device = params.get("device");
  const repair = params.get("repair");
  if (device && repair) return { device, repair };
  return null;
}

/**
 * Quit the active session: strip ?device= + ?repair=, hash to #home, close
 * chat panel, re-render the list. Called from the dashboard's Quitter button
 * and the topbar session pill's [×].
 */
export async function leaveSession() {
  const url = new URL(window.location.href);
  url.searchParams.delete("device");
  url.searchParams.delete("repair");
  url.hash = "#home";
  window.history.replaceState({}, "", url.toString());
  // Close the chat panel if open. llmClose is a <button>; if the panel
  // isn't mounted yet the optional chaining silently skips.
  document.getElementById("llmClose")?.click();
  // Refresh chrome (drops the pill) and swap to list mode.
  navigate("home");
  // Quitting a session always returns to the landing hero — the tech is
  // declaring "I'm done with this repair", so the start screen (where they
  // can pick another device or open a new diagnostic) is the right next
  // step. The journal list stays accessible from the rail's home button.
  // hideRepairDashboard() runs explicitly because history.replaceState()
  // does NOT fire a hashchange event, so the hashchange dispatch in main.js
  // that would normally call it never runs.
  const { hideRepairDashboard } = await import("./home.js");
  hideRepairDashboard();
  const { showLanding } = await import("./landing.js");
  showLanding();
}
