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

// Stub — Task 3.4 fills this in with the real classifier wiring.
async function processIntent(text) {
  console.log("[landing] would classify:", text);
  setStatus("(classifier wiring — branchement en Task 3.4)", STATUS_NEUTRAL);
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
