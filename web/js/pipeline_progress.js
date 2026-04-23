// Pipeline progress drawer — bottom-right glass UI that streams live
// pipeline events via WS /pipeline/progress/{slug} and transitions the
// 4-step stepper (Scout → Registry → Writers → Audit) as events flow.
//
// Entry point: openPipelineProgress({repair_id, device_slug, device_label,
// pipeline_started}). When pipeline_started=false the pack is already
// complete on disk, so we skip the drawer and redirect immediately.

const PHASES = [
  {key: "scout",    label: "Scout",     sub: "Recherche web"},
  {key: "registry", label: "Registry",  sub: "Vocabulaire canonique"},
  {key: "writers",  label: "Writers",   sub: "Graphe · Règles · Dictionnaire"},
  {key: "audit",    label: "Audit",     sub: "QA & cohérence"},
];

const STATE = {
  ws: null,
  slug: null,
  deviceLabel: null,
  done: false,
  failed: false,
  redirectTimer: null,
};

function el(id) { return document.getElementById(id); }

function fmtElapsed(sec) {
  if (typeof sec !== "number" || !isFinite(sec)) return "—";
  if (sec < 1) return `${(sec * 1000).toFixed(0)} ms`;
  if (sec < 60) return `${sec.toFixed(1)} s`;
  const m = Math.floor(sec / 60);
  const s = Math.round(sec % 60);
  return `${m}m ${s}s`;
}

function escHtml(s) {
  if (s === null || s === undefined) return "";
  return String(s).replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function buildDrawer() {
  if (el("pipelineProgressDrawer")) return;
  const drawer = document.createElement("aside");
  drawer.className = "pp-drawer";
  drawer.id = "pipelineProgressDrawer";
  drawer.setAttribute("role", "status");
  drawer.setAttribute("aria-live", "polite");
  drawer.innerHTML = `
    <header class="pp-head">
      <span class="pp-dot" aria-hidden="true"></span>
      <div class="pp-title">
        <span class="lbl">Construction de la mémoire</span>
        <span class="name" id="ppDeviceLabel">—</span>
      </div>
      <button class="pp-close" id="ppClose" aria-label="Fermer le panneau de progression" type="button">
        <svg class="icon icon-sm" viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg>
      </button>
    </header>
    <div class="pp-body" id="ppBody">
      ${PHASES.map((p, i) => `
        <div class="pp-step" data-step="${p.key}" data-idx="${i}">
          <div class="pp-step-mark" aria-hidden="true"></div>
          <div class="pp-step-lbl">${escHtml(p.label)}</div>
          <div class="pp-step-sub">${escHtml(p.sub)}</div>
          <div class="pp-step-time" data-role="time">—</div>
        </div>`).join("")}
    </div>
    <footer class="pp-foot">
      <div class="pp-status" id="ppStatus">En attente des premiers événements…</div>
      <button class="pp-cta hidden" id="ppCta" type="button"></button>
    </footer>
  `;
  document.body.appendChild(drawer);

  el("ppClose").addEventListener("click", closeDrawer);
}

function openDrawer(deviceLabel) {
  buildDrawer();
  el("ppDeviceLabel").textContent = deviceLabel || "—";
  el("ppStatus").textContent = "Connexion au pipeline…";
  el("ppStatus").className = "pp-status";
  el("ppCta").classList.add("hidden");
  el("ppCta").textContent = "";
  // Reset step states
  document.querySelectorAll("#pipelineProgressDrawer .pp-step").forEach(s => {
    s.classList.remove("running", "done", "error");
    s.querySelector('[data-role="time"]').textContent = "—";
    const sub = s.querySelector(".pp-step-sub");
    const originalSub = PHASES.find(p => p.key === s.dataset.step)?.sub || "";
    if (sub) sub.textContent = originalSub;
  });
  // Remove any lingering error panel
  document.getElementById("ppErrorDetail")?.remove();
  requestAnimationFrame(() => {
    el("pipelineProgressDrawer").classList.add("open");
  });
}

function closeDrawer() {
  const drawer = el("pipelineProgressDrawer");
  if (!drawer) return;
  drawer.classList.remove("open");
  if (STATE.ws) {
    try { STATE.ws.close(1000, "user-closed"); } catch (_) { /* noop */ }
    STATE.ws = null;
  }
  if (STATE.redirectTimer) {
    clearTimeout(STATE.redirectTimer);
    STATE.redirectTimer = null;
  }
}

function setStepState(phaseKey, klass) {
  const step = document.querySelector(`#pipelineProgressDrawer .pp-step[data-step="${phaseKey}"]`);
  if (!step) return;
  step.classList.remove("running", "done", "error");
  if (klass) step.classList.add(klass);
}

function setStepTime(phaseKey, text) {
  const step = document.querySelector(`#pipelineProgressDrawer .pp-step[data-step="${phaseKey}"]`);
  if (!step) return;
  const cell = step.querySelector('[data-role="time"]');
  if (cell) cell.textContent = text;
}

function setStepCounts(phaseKey, counts) {
  if (!counts || typeof counts !== "object") return;
  const step = document.querySelector(`#pipelineProgressDrawer .pp-step[data-step="${phaseKey}"]`);
  if (!step) return;
  const sub = step.querySelector(".pp-step-sub");
  if (!sub) return;
  const parts = Object.entries(counts).map(([k, v]) => `${v} ${k}`);
  sub.textContent = parts.join(" · ");
}

function setStatus(text, klass) {
  const s = el("ppStatus");
  if (!s) return;
  s.textContent = text;
  s.className = "pp-status" + (klass ? " " + klass : "");
}

function showCta(label, iconPath, primary, onClick) {
  const btn = el("ppCta");
  if (!btn) return;
  btn.classList.remove("hidden");
  btn.classList.toggle("primary", !!primary);
  btn.innerHTML = `${iconPath ? `<svg class="icon icon-sm" viewBox="0 0 24 24">${iconPath}</svg>` : ""}${escHtml(label)}`;
  btn.onclick = onClick;
}

function showErrorDetail(msg) {
  if (!msg) return;
  document.getElementById("ppErrorDetail")?.remove();
  const div = document.createElement("div");
  div.className = "pp-error-detail";
  div.id = "ppErrorDetail";
  div.textContent = msg;
  el("ppBody").appendChild(div);
}

function handleEvent(ev) {
  switch (ev.type) {
    case "subscribed":
      // Ack — the pipeline may already have started. Wait for pipeline_started
      // or the first phase_started to flip the UI.
      break;

    case "pipeline_started":
      setStatus("Pipeline démarré…");
      break;

    case "phase_started":
      setStepState(ev.phase, "running");
      setStepTime(ev.phase, "en cours…");
      setStatus(`Phase en cours · <b>${escHtml(ev.phase || "")}</b>`);
      break;

    case "phase_finished":
      setStepState(ev.phase, "done");
      setStepTime(ev.phase, fmtElapsed(ev.elapsed_s));
      if (ev.counts) setStepCounts(ev.phase, ev.counts);
      break;

    case "pipeline_finished": {
      STATE.done = true;
      const score = typeof ev.consistency_score === "number"
        ? ev.consistency_score.toFixed(2) : "—";
      const status = ev.status || "APPROVED";
      setStatus(`Mémoire prête · audit <b>${escHtml(status)}</b> · cohérence <b>${score}</b>`, "ok");
      showCta(
        "Voir la Memory Bank",
        '<path d="M5 12h14M13 6l6 6-6 6"/>',
        true,
        () => redirectToMemoryBank(),
      );
      // Auto-redirect after 2s unless the user clicks Close first.
      STATE.redirectTimer = setTimeout(redirectToMemoryBank, 2000);
      break;
    }

    case "pipeline_failed": {
      STATE.failed = true;
      // Paint the currently running step as error.
      const running = document.querySelector("#pipelineProgressDrawer .pp-step.running");
      if (running) {
        running.classList.remove("running");
        running.classList.add("error");
        running.querySelector('[data-role="time"]').textContent = "échec";
      }
      const status = ev.status || "ERROR";
      setStatus(`Pipeline en échec · <b>${escHtml(status)}</b>`, "err");
      if (ev.error) showErrorDetail(ev.error);
      showCta("Fermer", "", false, closeDrawer);
      break;
    }

    default:
      // Unknown event type — ignore silently; forward-compat.
      break;
  }
}

function redirectToMemoryBank() {
  if (!STATE.slug) return;
  if (STATE.redirectTimer) {
    clearTimeout(STATE.redirectTimer);
    STATE.redirectTimer = null;
  }
  closeDrawer();
  window.location.href = `?device=${encodeURIComponent(STATE.slug)}#memory-bank`;
}

/* ---------- public API ---------- */

export function openPipelineProgress(repairResponse) {
  if (!repairResponse || !repairResponse.device_slug) return;

  // Pack already complete on disk — skip the drawer and open the graph view
  // for this device. Consistent with clicking a home card: the user lands on
  // the rich visual representation of the pack, not the read-only data dump.
  if (repairResponse.pipeline_started === false) {
    window.location.href = `?device=${encodeURIComponent(repairResponse.device_slug)}`;
    return;
  }

  STATE.slug = repairResponse.device_slug;
  STATE.deviceLabel = repairResponse.device_label || repairResponse.device_slug;
  STATE.done = false;
  STATE.failed = false;
  STATE.redirectTimer = null;

  openDrawer(STATE.deviceLabel);

  const wsProto = window.location.protocol === "https:" ? "wss:" : "ws:";
  const url = `${wsProto}//${window.location.host}/pipeline/progress/${encodeURIComponent(STATE.slug)}`;
  const ws = new WebSocket(url);
  STATE.ws = ws;

  ws.addEventListener("message", ev => {
    let payload;
    try { payload = JSON.parse(ev.data); }
    catch (_) { return; }
    handleEvent(payload);
  });
  ws.addEventListener("error", () => {
    setStatus("Connexion perdue au flux de progression.", "err");
  });
  ws.addEventListener("close", () => {
    STATE.ws = null;
    // If the pipeline didn't reach a terminal event before the close, flag it.
    if (!STATE.done && !STATE.failed) {
      setStatus("Connexion fermée avant la fin du pipeline.", "err");
      showCta("Fermer", "", false, closeDrawer);
    }
  });
}

export function initPipelineProgress() {
  // Future-proof hook — currently the drawer is lazy-built when first shown,
  // so there's nothing to wire at bootstrap. Kept symmetric with the other
  // init* modules so main.js stays consistent.
}
