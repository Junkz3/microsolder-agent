// Landing hero — captures {device_label, symptom}, kicks the existing
// /pipeline/repairs endpoint, and renders a live narrated timeline of the
// pipeline phases as the agent learns the device. When the pipeline finishes
// (or the pack was already on disk) the page redirects into the workspace
// at ?repair={id}&device={slug}.
//
// No classifier here — the existing pipeline (Scout → Registry → Mapper? →
// Writers ×3 → Auditor) does device identification + knowledge construction
// in one shot. The narrator agent (api/pipeline/phase_narrator.py) emits a
// `phase_narration` event after each phase_finished; we render those into
// the timeline rows so the technician watches the agent learn.

import { mountMascot, setMascotState } from './mascot.js';
import { prettifySlug } from './router.js';

const STATUS_NEUTRAL = "";
const STATUS_LOADING = "loading";
const STATUS_ERROR = "error";

const PHASE_ORDER = ["scout", "registry", "mapper", "writers", "audit"];

let isSubmitting = false;
let progressWs = null;
let pipelineStartedAt = 0;
let _landingMascot = null;

function setLandingMascot(state) {
  if (!_landingMascot) return;
  setMascotState(_landingMascot, state);
}

const _landingDateFmt = new Intl.DateTimeFormat("fr-FR", {
  day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
});

async function loadAndRenderSidebar() {
  const sidebar = document.getElementById("landingSidebar");
  const list = document.getElementById("landingSidebarList");
  const count = document.getElementById("landingSidebarCount");
  if (!sidebar || !list) return;

  let repairs = [];
  try {
    const res = await fetch("/pipeline/repairs");
    if (res.ok) repairs = await res.json();
  } catch (err) {
    console.warn("[landing] loadRepairs failed", err);
  }
  if (!repairs || repairs.length === 0) {
    sidebar.hidden = true;
    return;
  }

  // Most recent first.
  repairs.sort((a, b) => {
    const ta = new Date(a.created_at).getTime() || 0;
    const tb = new Date(b.created_at).getTime() || 0;
    return tb - ta;
  });

  if (count) {
    const key = repairs.length > 1 ? "landing.sidebar.count_many" : "landing.sidebar.count_one";
    count.textContent = window.t ? window.t(key, { n: repairs.length }) : `${repairs.length} repairs`;
  }

  list.innerHTML = "";
  for (const r of repairs) {
    const li = document.createElement("li");
    li.className = "landing-sidebar-item";

    const a = document.createElement("a");
    a.className = "landing-sidebar-link";
    a.href = `?device=${encodeURIComponent(r.device_slug)}&repair=${encodeURIComponent(r.repair_id)}#home`;

    const dev = document.createElement("span");
    dev.className = "landing-sidebar-device";
    dev.textContent = prettifySlug(r.device_slug);

    const sym = document.createElement("span");
    sym.className = "landing-sidebar-symptom";
    sym.textContent = r.symptom || "—";
    if (r.symptom) sym.title = r.symptom;

    const meta = document.createElement("span");
    meta.className = "landing-sidebar-meta";
    const dateStr = r.created_at
      ? _landingDateFmt.format(new Date(r.created_at)).replace(/,\s*/g, " ")
      : "";
    const ridShort = (r.repair_id || "").slice(0, 8);
    meta.textContent = dateStr ? `${dateStr} · ${ridShort}` : ridShort;

    a.appendChild(dev);
    a.appendChild(sym);
    a.appendChild(meta);
    li.appendChild(a);

    const del = document.createElement("button");
    del.type = "button";
    del.className = "landing-sidebar-delete";
    del.setAttribute("aria-label", window.t ? window.t("landing.sidebar.delete_aria") : "Delete this repair");
    del.title = window.t ? window.t("landing.sidebar.delete_title") : "Delete";
    del.textContent = "×";
    del.addEventListener("click", (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      onDeleteRepairClick(r.repair_id, li, del);
    });
    li.appendChild(del);

    list.appendChild(li);
  }
  sidebar.hidden = false;
}

async function onDeleteRepairClick(repairId, itemEl, btnEl) {
  const t = window.t || ((k) => k);
  const ok = window.confirm(t("landing.delete.confirm"));
  if (!ok) return;

  btnEl.disabled = true;
  try {
    const res = await fetch(`/pipeline/repairs/${encodeURIComponent(repairId)}`, {
      method: "DELETE",
    });
    if (!res.ok) {
      const detail = await res.text().catch(() => "");
      throw new Error(`HTTP ${res.status} ${detail}`);
    }
  } catch (err) {
    console.error("[landing] delete failed", err);
    setStatus(t("landing.status.error_delete", { error: err.message || err }), STATUS_ERROR);
    btnEl.disabled = false;
    return;
  }

  itemEl.remove();
  const list = document.getElementById("landingSidebarList");
  const count = document.getElementById("landingSidebarCount");
  const remaining = list ? list.children.length : 0;
  if (count) {
    if (remaining > 0) {
      const key = remaining > 1 ? "landing.sidebar.count_many" : "landing.sidebar.count_one";
      count.textContent = t(key, { n: remaining });
    } else {
      count.textContent = "";
    }
  }
  if (remaining === 0) {
    const sidebar = document.getElementById("landingSidebar");
    if (sidebar) sidebar.hidden = true;
  }
}

export function showLanding() {
  document.body.classList.add("show-landing");
  const ov = document.getElementById("landing-overlay");
  if (ov) ov.hidden = false;
  // Mount the hero mascot once; reopens reset to idle. Sidebar refetches
  // every reopen so a fresh leaveSession() shows the latest repair list.
  if (!_landingMascot) {
    _landingMascot = mountMascot(document.getElementById("landingMascot"), {
      size: "md", state: "idle",
    });
  } else {
    setLandingMascot("idle");
  }
  loadAndRenderSidebar();
  loadPacksForSuggest();
  setTimeout(() => document.getElementById("landingDevice")?.focus(), 50);
}

function hideLanding() {
  document.body.classList.remove("show-landing");
  const ov = document.getElementById("landing-overlay");
  if (ov) ov.hidden = true;
  if (progressWs && progressWs.readyState <= 1) {
    try { progressWs.close(); } catch (_) {}
  }
  progressWs = null;
}

function setStatus(msg, kind) {
  const el = document.getElementById("landingStatus");
  if (!el) return;
  el.textContent = msg || "";
  el.classList.remove("error");
  if (kind === STATUS_ERROR) el.classList.add("error");
}

function setSubmitting(on) {
  isSubmitting = on;
  const btn = document.getElementById("landingSubmit");
  if (btn) btn.disabled = on;
  const dev = document.getElementById("landingDevice");
  const sym = document.getElementById("landingSymptom");
  if (dev) dev.disabled = on;
  if (sym) sym.disabled = on;
}

function showTimeline() {
  const tl = document.getElementById("landingTimeline");
  if (tl) tl.hidden = false;
  pipelineStartedAt = Date.now();
  startEtaTicker();
}

function startEtaTicker() {
  const eta = document.getElementById("landingTimelineEta");
  if (!eta) return;
  if (window.__landingEtaTimer) clearInterval(window.__landingEtaTimer);
  const t = window.t || ((k) => k);
  const tick = () => {
    const elapsed = Math.max(0, (Date.now() - pipelineStartedAt) / 1000);
    eta.textContent = t("landing.timeline.elapsed", { n: elapsed.toFixed(0) });
  };
  tick();
  window.__landingEtaTimer = setInterval(tick, 250);
}

function stopEtaTicker() {
  if (window.__landingEtaTimer) {
    clearInterval(window.__landingEtaTimer);
    window.__landingEtaTimer = null;
  }
}

function setPhaseState(phase, state) {
  // state ∈ "running" | "done" | "failed"
  const li = document.querySelector(`.landing-phase[data-phase="${phase}"]`);
  if (!li) return;
  li.hidden = false;  // mapper starts hidden until a phase_started arrives
  li.classList.remove("is-running", "is-done", "is-failed");
  if (state === "running") li.classList.add("is-running");
  if (state === "done") li.classList.add("is-done");
  if (state === "failed") li.classList.add("is-failed");
}

function setPhaseNarration(phase, text) {
  const li = document.querySelector(`.landing-phase[data-phase="${phase}"]`);
  if (!li) return;
  const slot = li.querySelector(".landing-phase-narration");
  if (!slot) return;
  slot.textContent = text;
  li.classList.add("has-narration");
}

function setTimelineTitle(text) {
  const t = document.getElementById("landingTimelineTitle");
  if (t) t.textContent = text;
}

function resetTimeline() {
  PHASE_ORDER.forEach((p) => {
    const li = document.querySelector(`.landing-phase[data-phase="${p}"]`);
    if (!li) return;
    li.classList.remove("is-running", "is-done", "is-failed", "has-narration");
    if (p === "mapper") li.hidden = true;
    const slot = li.querySelector(".landing-phase-narration");
    if (slot) slot.textContent = "";
  });
}

async function onSubmit(ev) {
  ev.preventDefault();
  if (isSubmitting) return;
  const t = window.t || ((k) => k);
  const deviceEl = document.getElementById("landingDevice");
  const symptomEl = document.getElementById("landingSymptom");
  const device = (deviceEl?.value || "").trim();
  const symptom = (symptomEl?.value || "").trim();

  if (device.length < 2) {
    setStatus(t("landing.status.validation_device"), STATUS_ERROR);
    deviceEl?.focus();
    return;
  }
  if (symptom.length < 5) {
    setStatus(t("landing.status.validation_symptom"), STATUS_ERROR);
    symptomEl?.focus();
    return;
  }

  setStatus(t("landing.status.checking"), STATUS_LOADING);
  setSubmitting(true);
  setLandingMascot("thinking");
  resetTimeline();

  try {
    // If the tech picked a known device from the autocomplete, send the
    // canonical slug so the backend skips re-slugification and lands on
    // the right pack — sidesteps near-but-not-identical spellings.
    const payload = { device_label: device, symptom };
    if (_selectedDeviceSlug) payload.device_slug = _selectedDeviceSlug;
    const res = await fetch("/pipeline/repairs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const detail = await res.text().catch(() => "");
      throw new Error(`HTTP ${res.status} ${detail}`);
    }
    const repair = await res.json();
    const rid = repair.repair_id;
    const slug = repair.device_slug;
    if (!rid || !slug) throw new Error(t("landing.status.error_invalid_response"));

    // Three response shapes, three UX flows.
    // Branch 2 — symptom already covered by a known rule: no LLM work,
    // fast redirect to workspace.
    if (!repair.pipeline_started) {
      if (repair.matched_rule_id) {
        setStatus(
          t("landing.status.rule_match", { rule_id: repair.matched_rule_id }),
          STATUS_NEUTRAL,
        );
      } else {
        setStatus(
          t("landing.status.device_known", { device: repair.device_label }),
          STATUS_NEUTRAL,
        );
      }
      // Pack on disk → play an accelerated fake-timeline (~15–17s) so the
      // tech sees the cache hit as a fast pipeline run, then navigate.
      // setStatus message above stays as the lead-in; setTimelineTitle
      // takes over once showTimeline() inside the helper fires.
      playCachedPipelineTimeline(slug, rid, repair.device_label || slug)
        .catch((err) => {
          console.warn("[landing] cached timeline failed, falling back to direct nav", err);
          goToWorkspace(rid, slug);
        });
      return;
    }

    // Branch 3 — pack exists but the symptom is new: the backend kicked
    // a real targeted expand in background. We play the same fake-timeline
    // as branch 2 (pack is on disk, agent works from existing rules even
    // if the expand hasn't finished). The expand runs silently — harmless.
    if (repair.pipeline_kind === "expand") {
      setStatus(
        t("landing.status.device_known", { device: repair.device_label }),
        STATUS_NEUTRAL,
      );
      playCachedPipelineTimeline(slug, rid, repair.device_label || slug)
        .catch((err) => {
          console.warn("[landing] cached timeline (expand) failed, falling back", err);
          goToWorkspace(rid, slug, "#home");
        });
      return;
    }

    // Branch 1 — full pipeline on a fresh device (~5-10 min).
    setStatus(t("landing.status.build_new"), STATUS_NEUTRAL);
    showTimeline();
    setTimelineTitle(t("landing.timeline.title_build", { device: repair.device_label }));
    subscribeToProgress(slug, rid);
  } catch (err) {
    console.error("[landing] submit failed", err);
    setStatus(t("landing.status.error_create", { error: err.message || err }), STATUS_ERROR);
    setLandingMascot("error");
    setSubmitting(false);
  }
}

function subscribeToProgress(slug, repairId) {
  if (progressWs && progressWs.readyState <= 1) {
    try { progressWs.close(); } catch (_) {}
  }
  const proto = (location.protocol === "https:") ? "wss:" : "ws:";
  const url = `${proto}//${location.host}/pipeline/progress/${encodeURIComponent(slug)}`;

  progressWs = new WebSocket(url);

  progressWs.addEventListener("message", (ev) => {
    let data;
    try { data = JSON.parse(ev.data); }
    catch { return; }
    handleProgressEvent(data, slug, repairId);
  });

  progressWs.addEventListener("error", (ev) => {
    console.warn("[landing] progress WS error", ev);
    setStatus((window.t || ((k) => k))("landing.status.ws_lost"), STATUS_ERROR);
  });

  progressWs.addEventListener("close", () => {
    stopEtaTicker();
  });
}

function handleProgressEvent(ev, slug, repairId) {
  const t = window.t || ((k) => k);
  switch (ev.type) {
    case "subscribed":
      break;
    case "pipeline_started":
      setStatus(t("landing.status.pipeline_started", { device: ev.device_label || ev.device_slug || slug }), STATUS_LOADING);
      break;
    case "phase_started": {
      const phase = ev.phase;
      if (PHASE_ORDER.includes(phase) || phase === "expand") {
        setPhaseState(phase, "running");
        setLandingMascot("working");
      }
      break;
    }
    case "phase_finished": {
      const phase = ev.phase;
      if (PHASE_ORDER.includes(phase) || phase === "expand") {
        setPhaseState(phase, "done");
      }
      break;
    }
    case "phase_narration": {
      const phase = ev.phase;
      const text = (ev.text || "").trim();
      if (text && PHASE_ORDER.includes(phase)) setPhaseNarration(phase, text);
      break;
    }
    case "pipeline_finished": {
      setTimelineTitle(t("landing.timeline.title_ready", { status: ev.status || "" }));
      setStatus(t("landing.status.ready"), STATUS_NEUTRAL);
      stopEtaTicker();
      setLandingMascot("success");
      // 2500 ms grace gives the audit phase narration (Haiku ~800-1600 ms)
      // time to land on the WS bus and render before we navigate away.
      setTimeout(() => goToWorkspace(repairId, slug), 2500);
      break;
    }
    case "pipeline_failed": {
      setTimelineTitle(t("landing.timeline.title_failed"));
      setStatus(t("landing.status.error_pipeline", { error: ev.error || ev.status || t("landing.status.error_unknown") }), STATUS_ERROR);
      const running = document.querySelector(".landing-phase.is-running");
      if (running) {
        running.classList.remove("is-running");
        running.classList.add("is-failed");
      }
      stopEtaTicker();
      setLandingMascot("error");
      setSubmitting(false);
      break;
    }
    default:
      break;
  }
}

function setExpandMode() {
  // Collapse the 5-phase pipeline timeline into a single "enrichment"
  // row — the expand path runs a targeted Scout + Registry rebuild +
  // Clinicien and doesn't traverse Mapper / Writers / Auditor. Showing
  // 5 pending dots that never advance (because phase events carry
  // phase: "expand" which isn't in PHASE_ORDER) looks broken.
  const t = window.t || ((k) => k);
  const tl = document.getElementById("landingTimeline");
  if (!tl) return;
  tl.classList.add("landing-timeline-expand");
  const phases = tl.querySelectorAll(".landing-phase");
  phases.forEach((el, i) => {
    if (i === 0) {
      // Repurpose the first row as the single "expand" marker. Drop the
      // [data-i18n] hook so applyDom() doesn't restore the old "scout" label.
      el.dataset.phase = "expand";
      el.classList.remove("is-done", "is-failed");
      el.classList.add("is-running");
      const label = el.querySelector(".landing-phase-label");
      if (label) {
        label.removeAttribute("data-i18n");
        label.textContent = t("landing.timeline.phase_expand");
      }
      const narr = el.querySelector(".landing-phase-narration");
      if (narr) narr.textContent = "";
    } else {
      // Hide the other phase rows in expand mode.
      el.hidden = true;
    }
  });
}


function _sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

// Plays a fake 5-phase pipeline timeline at ~3s per phase, then the
// mascot success state, then navigates to the workspace. Used when the
// backend signals `pipeline_started: false` (pack already on disk) so
// the technician sees the cache hit as a fast pipeline run instead of
// an instant flash. ~15s total + 1.5s success grace = ~16–17s.
async function playCachedPipelineTimeline(slug, repairId, deviceLabel) {
  const t = window.t || ((k) => k);
  showTimeline();
  setTimelineTitle(t("landing.timeline.title_loading", { device: deviceLabel }));
  setLandingMascot("working");

  // PHASE_ORDER includes "mapper" which the live pipeline marks hidden
  // until a phase event arrives. For a cache hit we want to show all
  // phases marching past, so unhide it first.
  const mapperRow = document.querySelector('.landing-phase[data-phase="mapper"]');
  if (mapperRow) mapperRow.hidden = false;

  const PER_PHASE_MS = 3000;
  for (const phase of PHASE_ORDER) {
    setPhaseState(phase, "running");
    await _sleep(PER_PHASE_MS * 0.7);
    setPhaseState(phase, "done");
    await _sleep(PER_PHASE_MS * 0.3);
  }

  setLandingMascot("success");
  setTimelineTitle(t("landing.timeline.title_ready", { status: deviceLabel }));
  await _sleep(1500);
  // Cache hit: land on the repair dashboard (#home) so the tech sees the
  // findings + timeline straight away, not the graph view that the live
  // pipeline path defaults to.
  goToWorkspace(repairId, slug, "#home");
}

function goToWorkspace(repairId, slug, hash = "#graphe") {
  // Land the tech on the graph view (loads graph + memory bank + opens
  // the LLM chat panel via openLLMPanelIfRepairParam) rather than the
  // home / repair_dashboard which only surfaces findings + timeline.
  // The dashboard remains reachable via the left rail #home button.
  //
  // Strip the landing overlay first so a hash-only navigation (when
  // the query params are already on the URL from a prior session)
  // doesn't leave the overlay sitting on top of the freshly-loaded
  // graph view.
  hideLanding();
  // Close any active progress WS so it can't fire late events (e.g. a
  // duplicate pipeline_finished) onto the page after navigation.
  if (progressWs && progressWs.readyState <= 1) {
    try { progressWs.close(); } catch (_) {}
  }
  progressWs = null;

  const target = new URL(location.origin + location.pathname);
  target.searchParams.set("repair", repairId);
  target.searchParams.set("device", slug);
  target.hash = hash;

  // Force a real navigation. location.href to the same URL is a no-op
  // and location.href to a hash-only delta does not reload the page —
  // either case would leave the landing module's state inconsistent
  // with the post-pipeline view. location.assign + reload on duplicate
  // guarantees a clean bootstrap of main.js with the new query params.
  if (target.toString() === location.href) {
    location.reload();
  } else {
    location.assign(target.toString());
  }
}

function onChipClick(ev) {
  const btn = ev.target.closest(".landing-chip");
  if (!btn) return;
  const dev = document.getElementById("landingDevice");
  const sym = document.getElementById("landingSymptom");
  // Chips don't carry a canonical slug; clearing here prevents a stale
  // _selectedDeviceSlug from the autocomplete leaking onto a chip submit.
  _selectedDeviceSlug = null;
  if (dev && btn.dataset.device) dev.value = btn.dataset.device;
  if (sym) {
    // Prefer the i18n key if present so the chip's symptom matches the active
    // locale; fall back to the literal data-symptom attribute.
    const key = btn.dataset.symptomKey;
    const fallback = btn.dataset.symptom || "";
    if (key && window.t) sym.value = window.t(key);
    else if (fallback) sym.value = fallback;
  }
  sym?.focus();
}

// ============================================================
// Device autocomplete — surfaces devices already known under the device
// input as the technician types. Sourced from /pipeline/taxonomy so the
// list is deduplicated to ONE entry per (brand, model) — no
// "iPhone X" / "iPhone X logic board" / "iPhone X bench" noise.
// Cached for the session in `_devicesCache`. Keyboard nav: ↑/↓/Enter/Esc.
//
// At selection, we store the canonical slug of the chosen pack on the
// form so onSubmit can pass `device_slug` to the backend explicitly,
// guaranteeing a cache hit on the right pack rather than re-slugifying
// the label and risking a miss on a near-but-not-identical spelling.
// ============================================================

let _devicesCache = null;
let _suggestActiveIdx = -1;
let _selectedDeviceSlug = null;

function _escapeHtml(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

// Flatten a TaxonomyTree into a plain list with one entry per
// (brand, model) — picks the most-complete pack as the canonical
// representative. Uncategorized packs become individual entries.
function _flattenTaxonomy(tree) {
  const out = [];
  const brands = (tree && tree.brands) || {};
  for (const [brand, models] of Object.entries(brands)) {
    for (const [model, packs] of Object.entries(models || {})) {
      if (!Array.isArray(packs) || packs.length === 0) continue;
      // Prefer a complete pack; fall back to the first one.
      const canonical = packs.find((p) => p && p.complete) || packs[0];
      out.push({
        label: model,
        subtitle: brand,
        slug: canonical.device_slug,
        device_label: canonical.device_label || model,
        complete: Boolean(canonical.complete),
      });
    }
  }
  for (const p of (tree && tree.uncategorized) || []) {
    if (!p || !p.device_slug) continue;
    out.push({
      label: p.device_label || prettifySlug(p.device_slug),
      subtitle: null,
      slug: p.device_slug,
      device_label: p.device_label || prettifySlug(p.device_slug),
      complete: Boolean(p.complete),
    });
  }
  // Sort: complete first, then alphabetical by label.
  out.sort((a, b) => {
    if (a.complete !== b.complete) return a.complete ? -1 : 1;
    return a.label.localeCompare(b.label);
  });
  return out;
}

async function loadPacksForSuggest() {
  try {
    const res = await fetch("/pipeline/taxonomy");
    if (res.ok) {
      const tree = await res.json();
      _devicesCache = _flattenTaxonomy(tree);
    } else {
      _devicesCache = [];
    }
  } catch (err) {
    console.warn("[landing] loadPacksForSuggest failed", err);
    _devicesCache = [];
  }
}

function _matchDevices(query) {
  if (!_devicesCache || _devicesCache.length === 0) return [];
  const q = (query || "").trim().toLowerCase();
  if (q.length < 1) return [];
  return _devicesCache
    .filter((d) => {
      const label = (d.label || "").toLowerCase();
      const sub = (d.subtitle || "").toLowerCase();
      const slug = (d.slug || "").toLowerCase();
      return label.includes(q) || sub.includes(q) || slug.includes(q);
    })
    .slice(0, 6);
}

function _renderSuggest(query) {
  const box = document.getElementById("landingSuggest");
  if (!box) return;
  const matches = _matchDevices(query);
  if (matches.length === 0) {
    box.hidden = true;
    box.innerHTML = "";
    _suggestActiveIdx = -1;
    return;
  }
  const tFn = window.t || ((k) => k);
  const draftLabel = tFn("landing.suggest.draft");
  box.innerHTML = matches.map((d, i) => {
    const safeLabel = _escapeHtml(d.label);
    const safeSub = d.subtitle ? _escapeHtml(d.subtitle) : "";
    const safeSlug = _escapeHtml(d.slug);
    const iconClass = d.complete ? "is-complete" : "is-partial";
    const iconText = d.complete ? "✓" : "•";
    const meta = d.complete ? safeSub : (safeSub ? `${safeSub} · ${draftLabel}` : draftLabel);
    // data-label = the short model name (e.g. "iPhone 12") that lands in
    // the input on selection. NOT d.device_label, which is the raw
    // registry label (e.g. "Apple iPhone 12 logic board") and would
    // pollute the input with brand + form-factor noise.
    return `<div class="landing-suggest-item" role="option" `
      + `data-slug="${safeSlug}" data-label="${safeLabel}" data-index="${i}">`
      + `<span class="landing-suggest-icon ${iconClass}" aria-hidden="true">${iconText}</span>`
      + `<span class="landing-suggest-label">${safeLabel}</span>`
      + `<span class="landing-suggest-meta">${meta}</span>`
      + `</div>`;
  }).join("");
  box.hidden = false;
  _suggestActiveIdx = -1;
}

function _setSuggestActive(idx) {
  const items = document.querySelectorAll(".landing-suggest-item");
  if (items.length === 0) return;
  const clamped = Math.max(0, Math.min(idx, items.length - 1));
  items.forEach((el, i) => el.classList.toggle("is-active", i === clamped));
  _suggestActiveIdx = clamped;
  items[clamped].scrollIntoView({ block: "nearest" });
}

function _selectSuggest(label, slug) {
  const dev = document.getElementById("landingDevice");
  const sym = document.getElementById("landingSymptom");
  if (dev) dev.value = label;
  // Pin the canonical slug so onSubmit sends device_slug to the backend
  // (skips re-slugification of the label and guarantees the cache hit
  // on the right pack — defends against near-but-not-identical spellings).
  _selectedDeviceSlug = slug || null;
  _hideSuggest();
  if (sym) sym.focus();
}

function _hideSuggest() {
  const box = document.getElementById("landingSuggest");
  if (box) {
    box.hidden = true;
    box.innerHTML = "";
  }
  _suggestActiveIdx = -1;
}

function _initSuggest() {
  const dev = document.getElementById("landingDevice");
  const box = document.getElementById("landingSuggest");
  if (!dev || !box) return;

  dev.addEventListener("input", () => {
    // Free-text editing invalidates the previously-selected slug — the
    // tech may now be heading toward a different (or unknown) device.
    _selectedDeviceSlug = null;
    _renderSuggest(dev.value);
  });

  dev.addEventListener("focus", () => {
    if (dev.value && dev.value.length >= 1) _renderSuggest(dev.value);
  });

  dev.addEventListener("keydown", (ev) => {
    const items = document.querySelectorAll(".landing-suggest-item");
    if (items.length === 0) return;
    if (ev.key === "ArrowDown") {
      ev.preventDefault();
      _setSuggestActive(_suggestActiveIdx < 0 ? 0 : _suggestActiveIdx + 1);
    } else if (ev.key === "ArrowUp") {
      ev.preventDefault();
      _setSuggestActive(_suggestActiveIdx <= 0 ? items.length - 1 : _suggestActiveIdx - 1);
    } else if (ev.key === "Enter" && _suggestActiveIdx >= 0) {
      // Only intercept Enter when the user has explicitly highlighted a
      // suggestion via arrows. Otherwise let the form submit naturally.
      ev.preventDefault();
      const item = items[_suggestActiveIdx];
      if (item) _selectSuggest(item.dataset.label, item.dataset.slug);
    } else if (ev.key === "Escape") {
      _hideSuggest();
    }
  });

  // Hide on blur, but with a small delay so a click on a suggestion
  // (which fires after blur) gets processed first.
  dev.addEventListener("blur", () => setTimeout(_hideSuggest, 150));

  box.addEventListener("mousedown", (ev) => {
    // Use mousedown (not click) so it fires before blur on the input.
    const item = ev.target.closest(".landing-suggest-item");
    if (item && item.dataset.label) {
      ev.preventDefault();
      _selectSuggest(item.dataset.label, item.dataset.slug);
    }
  });
}

export function initLanding() {
  const form = document.getElementById("landingForm");
  if (form) form.addEventListener("submit", onSubmit);
  const chips = document.getElementById("landingChips");
  if (chips) chips.addEventListener("click", onChipClick);
  _initSuggest();
}
