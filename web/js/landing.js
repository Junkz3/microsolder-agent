// Landing hero logic — handles form submission, chip clicks, classifier
// fetch, repair creation, and transition to the workspace. Owned entirely
// by this module: shows/hides itself via the body.show-landing class.

const STATUS_NEUTRAL = "";
const STATUS_LOADING = "loading";
const STATUS_ERROR = "error";

let isSubmitting = false;

export function showLanding() {
  document.body.classList.add("show-landing");
  const ov = document.getElementById("landing-overlay");
  if (ov) ov.hidden = false;
  setTimeout(() => document.getElementById("landingInput")?.focus(), 50);
}

export function hideLanding() {
  document.body.classList.remove("show-landing");
  const ov = document.getElementById("landing-overlay");
  if (ov) ov.hidden = true;
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
}

async function onSubmit(ev) {
  ev.preventDefault();
  if (isSubmitting) return;
  const input = document.getElementById("landingInput");
  const text = (input?.value || "").trim();
  if (text.length < 3) {
    setStatus("Décris un peu plus ce qui ne marche pas.", STATUS_ERROR);
    return;
  }
  setStatus("Je cherche…", STATUS_LOADING);
  setSubmitting(true);
  try {
    await processIntent(text);
  } catch (err) {
    console.error("[landing] submit failed", err);
    setStatus("Impossible de classifier — bascule en mode manuel.", STATUS_ERROR);
    // Phase 4 will add the dropdown fallback. For now, surface the error.
  } finally {
    setSubmitting(false);
  }
}

const CONFIDENCE_AUTO_THRESHOLD = 0.7;

async function processIntent(text) {
  let classification;
  try {
    const res = await fetch("/pipeline/classify-intent", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    if (!res.ok) {
      throw new Error(`HTTP ${res.status}`);
    }
    classification = await res.json();
  } catch (err) {
    console.error("[landing] classify failed", err);
    setStatus("Le classificateur est indisponible — choisis un appareil :", STATUS_ERROR);
    showFallbackPicker(text);
    return;
  }

  const top = (classification.candidates || [])[0];
  const symptom = (classification.symptoms && classification.symptoms.length >= 5)
    ? classification.symptoms
    : text;
  const autoConfirm = top && top.pack_exists && top.confidence >= CONFIDENCE_AUTO_THRESHOLD;

  if (autoConfirm) {
    setStatus(`Reconnu : ${top.label}. J'ouvre le diagnostic…`, STATUS_NEUTRAL);
    await openWorkspaceForSlug(top.slug, top.label, symptom);
    return;
  }

  if (top) {
    setStatus("Pas sûr… j'ouvre quand même, l'agent va te demander confirmation.", STATUS_NEUTRAL);
    await openWorkspaceForSlug(top.slug, top.label, symptom, {
      needsConfirm: true,
      candidates: classification.candidates,
    });
    return;
  }

  setStatus("Je n'ai pas reconnu ton appareil. Choisis dans la liste :", STATUS_ERROR);
  showFallbackPicker(text);
}

async function openWorkspaceForSlug(slug, label, symptom, opts = {}) {
  try {
    const body = {
      device_label: label || slug,
      device_slug: slug,
      symptom: symptom && symptom.length >= 5 ? symptom : "Diagnostic en cours.",
    };
    const res = await fetch("/pipeline/repairs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const repair = await res.json();
    const rid = repair.repair_id;
    if (!rid) throw new Error("missing repair_id in response");

    if (opts.needsConfirm && opts.candidates) {
      sessionStorage.setItem("microsolder.intent_candidates", JSON.stringify(opts.candidates));
    }

    const url = new URL(location.href);
    url.searchParams.set("repair", rid);
    if (repair.device_slug) url.searchParams.set("device", repair.device_slug);
    if (opts.needsConfirm) url.searchParams.set("confirm_intent", "1");
    location.href = url.toString();
  } catch (err) {
    console.error("[landing] repair create failed", err);
    setStatus("Impossible d'ouvrir le diagnostic — réessaie.", STATUS_ERROR);
  }
}

async function showFallbackPicker(originalText) {
  const status = document.getElementById("landingStatus");
  if (!status) return;
  // Avoid duplicating the picker if user clicks fallback twice.
  let existing = document.getElementById("landingFallbackPicker");
  if (existing) existing.remove();

  try {
    const res = await fetch("/pipeline/packs");
    if (!res.ok) return;
    const packs = await res.json();
    if (!packs.length) return;

    const node = document.createElement("div");
    node.id = "landingFallbackPicker";
    node.className = "landing-chips";
    node.style.marginTop = "12px";

    packs.slice(0, 8).forEach((p) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "landing-chip";
      b.textContent = p.device_slug;
      b.addEventListener("click", () => {
        openWorkspaceForSlug(p.device_slug, p.device_slug, originalText || "Diagnostic");
      });
      node.appendChild(b);
    });

    status.parentNode.insertBefore(node, status.nextSibling);
  } catch (err) {
    console.warn("[landing] fallback packs fetch failed", err);
  }
}

function onChipClick(ev) {
  const btn = ev.target.closest(".landing-chip");
  if (!btn) return;
  const input = document.getElementById("landingInput");
  if (input) {
    input.value = btn.dataset.text || btn.textContent;
    input.focus();
  }
}

export function initLanding() {
  const form = document.getElementById("landingForm");
  if (form) form.addEventListener("submit", onSubmit);
  const chips = document.getElementById("landingChips");
  if (chips) chips.addEventListener("click", onChipClick);
}
