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
    const res = await fetch("/pipeline/repairs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ device_label: device, symptom }),
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

    // Branch 3 — pack exists but the symptom is new: targeted enrich
    // (~3 min). Show a simplified "enrichment" timeline rather than the
    // full 5-phase pipeline layout.
    if (repair.pipeline_kind === "expand") {
      setStatus(
        t("landing.status.expand", { device: repair.device_label }),
        STATUS_NEUTRAL,
      );
      showTimeline();
      setTimelineTitle(t("landing.timeline.title_expand", { device: repair.device_label }));
      setExpandMode();
      subscribeToProgress(slug, rid);
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
  showTimeline();
  setTimelineTitle(`Chargement de la fiche · ${deviceLabel}`);
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
  goToWorkspace(repairId, slug);
}

function goToWorkspace(repairId, slug) {
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
  target.hash = "#graphe";

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

export function initLanding() {
  const form = document.getElementById("landingForm");
  if (form) form.addEventListener("submit", onSubmit);
  const chips = document.getElementById("landingChips");
  if (chips) chips.addEventListener("click", onChipClick);
}
