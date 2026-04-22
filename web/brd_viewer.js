const BRD_URL  = '/boards/mnt-reform-motherboard.brd';
const PARSE_URL = '/api/board/parse';

const state = { board: null };

function renderSkeleton(root) {
  root.innerHTML = `
    <div class="summary-card" style="opacity:.5;pointer-events:none">
      <div class="sc-row"><span class="sc-label">board_id</span><span class="sc-value">—</span></div>
      <div class="sc-row"><span class="sc-label">format</span><span class="sc-value">—</span></div>
      <div class="sc-row"><span class="sc-label">composants</span><span class="sc-value">—</span></div>
      <div class="sc-row"><span class="sc-label">pins</span><span class="sc-value">—</span></div>
      <div class="sc-row"><span class="sc-label">nets</span><span class="sc-value">—</span></div>
      <div class="sc-row"><span class="sc-label">sha256</span><span class="sc-value">—</span></div>
      <div class="sc-status">Chargement…</div>
    </div>`;
}

function renderBoard(root, board) {
  const hashShort = (board.file_hash || '').slice(0, 16);
  root.innerHTML = `
    <div class="summary-card">
      <div class="sc-row"><span class="sc-label">board_id</span><span class="sc-value">${board.board_id || '—'}</span></div>
      <div class="sc-row"><span class="sc-label">format</span><span class="sc-value">${board.source_format || '—'}</span></div>
      <div class="sc-row"><span class="sc-label">composants</span><span class="sc-value">${(board.parts || []).length}</span></div>
      <div class="sc-row"><span class="sc-label">pins</span><span class="sc-value">${(board.pins || []).length}</span></div>
      <div class="sc-row"><span class="sc-label">nets</span><span class="sc-value">${(board.nets || []).length}</span></div>
      <div class="sc-row"><span class="sc-label">sha256</span><span class="sc-value">${hashShort}…</span></div>
      <div class="sc-status sc-ok">Board parsé avec succès</div>
    </div>`;
}

function renderError(root, detail) {
  const code = (detail && detail.detail) || 'ERREUR';
  const msg  = (detail && detail.message) || 'Erreur inconnue';
  root.innerHTML = `
    <div class="error-card">
      <div class="ec-code">${code}</div>
      <div class="ec-msg">${msg}</div>
    </div>`;
}

export async function initBoardview(containerEl) {
  if (state.board) return;
  if (!containerEl) return;

  renderSkeleton(containerEl);

  let blob;
  try {
    const res = await fetch(BRD_URL);
    if (!res.ok) throw { detail: 'FETCH_FAILED', message: `HTTP ${res.status} sur ${BRD_URL}` };
    blob = await res.blob();
  } catch (err) {
    renderError(containerEl, err.detail ? err : { detail: 'FETCH_FAILED', message: String(err) });
    return;
  }

  const form = new FormData();
  form.append('file', blob, 'mnt-reform-motherboard.brd');

  let board;
  try {
    const res = await fetch(PARSE_URL, { method: 'POST', body: form });
    const data = await res.json();
    if (!res.ok) {
      renderError(containerEl, data);
      return;
    }
    board = data;
  } catch (err) {
    renderError(containerEl, { detail: 'PARSE_FAILED', message: String(err) });
    return;
  }

  state.board = board;
  renderBoard(containerEl, board);
}

window.initBoardview = initBoardview;
