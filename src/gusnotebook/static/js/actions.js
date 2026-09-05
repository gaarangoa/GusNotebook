/* What the user does to a notebook: edit, run, restart, generate, explain.
 *
 * Ends with the settings modal and the kernel-status indicator, both of which
 * are about the notebook as a whole rather than any one cell. */

// ---------- Editing ----------
const activeRuns = new Map();
// Browser run POSTs return as soon as their server worker starts. The matching
// cell_done SSE event resolves this promise, preserving Shift+Enter and Run all
// sequencing without monopolising an HTTP connection for the whole computation.
const runWaiters = new Map();
const runContexts = new Map();
const RUN_RECONCILE_MS = 5000;

// Cells typed into but not yet PATCHed. `cells[].source` lags by up to the save
// debounce, so it is not the answer to "what is in this editor" — and mountEditor()
// reconciles a reused CM view against it. Without this, any re-render landing
// inside that window (a run finishing, a cell added, a kernel event) would push
// the stale server text back over what the user just typed.
// Drafts and requests belong to a tab, including while another tab is visible.
function notebookDrafts(t = activeTab()) {
  if (!t) return new Map();
  return t.cellDrafts || (t.cellDrafts = new Map());
}
const unsaved = {
  get size() { return notebookDrafts().size; },
  has(id) { return notebookDrafts().has(id); },
  clear() { discardNotebookDrafts(activeTab()); },
  [Symbol.iterator]() { return notebookDrafts().keys(); },
};

function discardNotebookDrafts(t) {
  for (const draft of notebookDrafts(t).values()) clearTimeout(draft.timer);
  notebookDrafts(t).clear();
  if (t) t.dirty = false;
}

function captureCellDraft(id, t = activeTab()) {
  if (!t || t.closing || t.kind !== 'notebook' || active !== t.path) return null;
  const ed = document.getElementById('ed-' + id);
  const c = (t.cells || cells).find(c => c.id === id);
  if (!ed || !c) return null;
  const drafts = notebookDrafts(t);
  let draft = drafts.get(id);
  if (!draft) {
    if (ed.value === c.source) return null;
    draft = {source: ed.value, base: c.source, revision: 0, session: currentSession};
    drafts.set(id, draft);
  }
  if (draft.source !== ed.value) draft.revision++;
  draft.source = ed.value;
  c.source = ed.value;
  t.dirty = true;
  return draft;
}

function queueSave(id) {
  const t = activeTab();
  const draft = captureCellDraft(id, t);
  if (!draft) return;
  clearTimeout(draft.timer);
  draft.timer = setTimeout(() => {
    saveCell(id, t).catch(err => flash('Save failed: ' + errText(err)));
  }, 500);
  renderTabs();
}

async function saveCell(id, t = activeTab()) {
  if (!t || t.closing) return;
  const drafts = notebookDrafts(t);
  let draft = active === t.path ? captureCellDraft(id, t) : drafts.get(id);
  if (!draft) return;
  clearTimeout(draft.timer);
  if (draft.promise) {
    await draft.promise;
    if (drafts.has(id)) return saveCell(id, t);
    return;
  }
  const source = draft.source;
  const revision = draft.revision;
  const query = new URLSearchParams({notebook: t.path});
  draft.promise = api(`/api/cells/${id}?${query}`, {
    method: 'PATCH',
    headers: draft.session ? {'X-Session-Id': draft.session} : {},
    body: JSON.stringify({source, expected_source: draft.base}),
  }).then(() => {
    draft.base = source;
    if (drafts.get(id) === draft && draft.revision === revision) drafts.delete(id);
    t.dirty = drafts.size > 0;
    renderTabs();
  }).finally(() => { draft.promise = null; });
  await draft.promise;
  if (drafts.has(id)) return saveCell(id, t);
}

async function flushNotebook(t = activeTab()) {
  await Promise.all([...notebookDrafts(t).keys()].map(id => saveCell(id, t)));
}

async function saveActiveDocument() {
  const t = activeTab();
  if (!t) return;
  try {
    if (t.kind === 'notebook') {
      await flushNotebook(t);
      flash('Notebook saved');
    } else if (t.kind === 'text') {
      await saveText();
    }
  } catch (err) {
    flash('Save failed: ' + errText(err));
  }
}

/** Put back the source an agent or a snippet replaced. One step, this cell. */
async function undoCell(id) {
  const r = await api(`/api/cells/${id}/undo${nbq()}`, {method: 'POST'});
  if (r.error) { flash(r.error); return; }
  // Outputs were dropped server-side with the source that produced them, so a
  // plain reload is enough — nothing to reconcile.
  await load();
  scrollToCell(id, 'nearest');
}

function preserveCellViewport(id) {
  const pane = document.getElementById('notebook-pane');
  const before = document.querySelector(`.cell[data-id="${id}"]`);
  if (!pane || !before) return () => {};
  const paneTop = pane.getBoundingClientRect().top;
  const offset = before.getBoundingClientRect().top - paneTop;
  return () => {
    const after = document.querySelector(`.cell[data-id="${id}"]`);
    if (!after) return;
    pane.scrollTop += after.getBoundingClientRect().top - paneTop - offset;
  };
}

function preserveViewportSlot(id) {
  const pane = document.getElementById('notebook-pane');
  const before = document.querySelector(`.cell[data-id="${id}"]`);
  if (!pane || !before) return () => {};
  const paneTop = pane.getBoundingClientRect().top;
  const offset = before.getBoundingClientRect().top - paneTop;
  return targetId => {
    const after = document.querySelector(`.cell[data-id="${targetId}"]`);
    if (!after) return;
    pane.scrollTop += after.getBoundingClientRect().top - paneTop - offset;
  };
}

function editMarkdown(id) {
  const restore = preserveCellViewport(id);
  editing.add(id);
  render();
  const ed = document.getElementById('ed-' + id);
  if (ed) { ed.focus(); autosize(ed); }
  restore();
  requestAnimationFrame(restore);
}

async function renderMarkdown(id) {
  const restore = preserveCellViewport(id);
  await saveCell(id);
  editing.delete(id);
  render();
  restore();
  requestAnimationFrame(restore);
}

/**
 * Cmd/Ctrl+/ — comment or uncomment the selected lines, like an editor.
 * Uncomments only when every non-blank line is already commented; otherwise
 * comments the lot, inserting at the shallowest indent so blocks stay aligned.
 */
function toggleComment(el, cellType) {
  const marker = cellType === 'markdown' ? null : '#';
  const value = el.value;
  const selStart = el.selectionStart, selEnd = el.selectionEnd;

  // Expand the selection to whole lines.
  const from = value.lastIndexOf('\n', selStart - 1) + 1;
  let to = value.indexOf('\n', selEnd);
  if (to === -1) to = value.length;
  const lines = value.slice(from, to).split('\n');

  let out;
  if (marker === null) {
    // Markdown has no line comment — wrap the block in an HTML comment.
    const block = lines.join('\n');
    const m = /^\s*<!--\s?([\s\S]*?)\s?-->\s*$/.exec(block);
    out = (m ? m[1] : `<!-- ${block} -->`).split('\n');
  } else {
    const filled = lines.filter(l => l.trim());
    const commented = filled.length > 0 &&
      filled.every(l => l.trimStart().startsWith(marker));
    if (commented) {
      out = lines.map(l => l.replace(
        new RegExp(`^(\\s*)${marker} ?`), '$1'));
    } else {
      const indent = Math.min(...(filled.length ? filled : lines)
        .map(l => l.length - l.trimStart().length));
      out = lines.map(l => l.trim()
        ? l.slice(0, indent) + marker + ' ' + l.slice(indent)
        : l);
    }
  }

  const replacement = out.join('\n');
  el.value = value.slice(0, from) + replacement + value.slice(to);
  // Keep the same lines selected so repeated presses toggle the same block.
  el.selectionStart = from;
  el.selectionEnd = from + replacement.length;
  autosize(el);
}

function onEditorKey(e, id) {
  // Shift+Enter → run, then move to the next cell (append only at the end)
  if (e.key === 'Enter' && e.shiftKey) {
    e.preventDefault();
    runCell(id, true);
    return false;
  }
  // Move the current cell — the toolbar arrows this replaced are gone.
  if ((e.key === 'ArrowUp' || e.key === 'ArrowDown') &&
      (e.metaKey || e.ctrlKey) && e.shiftKey) {
    e.preventDefault();
    moveCell(id, e.key === 'ArrowUp' ? -1 : 1);
    return false;
  }
  // Toggle code ⇄ markdown.
  if (e.key === 'm' && (e.metaKey || e.ctrlKey) && e.shiftKey) {
    e.preventDefault();
    const c = getCell(id);
    if (c) changeType(id, c.cell_type === 'markdown' ? 'code' : 'markdown');
    return false;
  }
  if (e.key === '/' && (e.metaKey || e.ctrlKey)) {
    e.preventDefault();
    const c = getCell(id);
    toggleComment(e.target, c ? c.cell_type : 'code');
    queueSave(id);
    return false;
  }
  if (e.key === 's' && (e.metaKey || e.ctrlKey)) {
    e.preventDefault();
    saveActiveDocument();
    return false;
  }
  if (e.key === 'Escape') {
    e.target.blur();
    const c = getCell(id);
    if (c && c.cell_type === 'markdown') renderMarkdown(id);
    return false;
  }
  // Tab inserts 4 spaces rather than moving focus
  if (e.key === 'Tab') {
    e.preventDefault();
    const el = e.target, s = el.selectionStart, en = el.selectionEnd;
    el.value = el.value.slice(0, s) + '    ' + el.value.slice(en);
    el.selectionStart = el.selectionEnd = s + 4;
    queueSave(id);
    return false;
  }
  return true;
}

// ---------- Actions ----------

function notebookUrl(path, extra = {}) {
  const q = new URLSearchParams({notebook: path});
  for (const [k, v] of Object.entries(extra)) q.set(k, v);
  return '/api/notebook?' + q.toString();
}

function applyNotebookRunSnapshot(notebook, data) {
  const target = tab(notebook);
  const running = data.running_cells || {};
  const liveCells = notebook === active ? cells : (target && target.cells || []);
  const live = new Map((liveCells || []).filter(c => c._running).map(c => [c.id, c]));
  const nextCells = (data.cells || []).map(c => {
    const previous = live.get(c.id);
    return previous && Object.prototype.hasOwnProperty.call(running, c.id)
      ? {...c, outputs: previous.outputs, _running: true}
      : {...c, _running: Object.prototype.hasOwnProperty.call(running, c.id)};
  });
  if (target) {
    target.cells = nextCells;
    if (data.kernel_python) target.python = data.kernel_python;
    if (data.kernel_status) target.status = data.kernel_status;
  }
  if (notebook === active) {
    cells = nextCells;
    if (data.kernel_status) setKernelStatus(data.kernel_status);
    render();
  }
  return running;
}

function waitForRunDone(runId, notebook, cellId) {
  return new Promise(resolve => {
    let settled = false;
    let timer = null;
    const finish = value => {
      if (settled) return;
      settled = true;
      if (timer) clearTimeout(timer);
      runWaiters.delete(runId);
      runContexts.delete(runId);
      resolve(value || null);
    };
    const reconcile = async () => {
      if (settled || !runWaiters.has(runId)) return;
      try {
        const data = await api(notebookUrl(notebook));
        const running = data.running_cells || {};
        if (Object.prototype.hasOwnProperty.call(running, cellId)) {
          timer = setTimeout(reconcile, RUN_RECONCILE_MS);
          return;
        }
        applyNotebookRunSnapshot(notebook, data);
        finish({type: 'cell_done', notebook, cell_id: cellId,
                run_id: runId, reconciled: true});
        return;
      } catch (err) {
        // Keep waiting for SSE. A later reconnect or poll can still recover.
      }
      timer = setTimeout(reconcile, RUN_RECONCILE_MS);
    };
    runWaiters.set(runId, finish);
    runContexts.set(runId, {notebook, cellId});
    timer = setTimeout(reconcile, RUN_RECONCILE_MS);
  });
}

async function reconcileRunWaiters() {
  for (const [runId, context] of Array.from(runContexts.entries())) {
    const finish = runWaiters.get(runId);
    if (!finish) continue;
    try {
      const data = await api(notebookUrl(context.notebook));
      const running = data.running_cells || {};
      if (Object.prototype.hasOwnProperty.call(running, context.cellId)) continue;
      applyNotebookRunSnapshot(context.notebook, data);
      finish({type: 'cell_done', notebook: context.notebook,
              cell_id: context.cellId, run_id: runId, reconciled: true});
    } catch (err) {
      // EventSource reconnects automatically; this is only a best-effort nudge.
    }
  }
}

/**
 * Move to the cell after `id` — appending a new one only at the end of the
 * notebook. This is what ⇧⏎ does after running.
 */
async function advanceFrom(id) {
  const i = cells.findIndex(c => c.id === id);
  if (i === -1) return;

  if (i === cells.length - 1) {
    await addCell('code', id, {revealBlock: 'center'});
    return;
  }

  const next = cells[i + 1];
  selectCell(next.id);
  focusCellEditor(next.id);
  requestAnimationFrame(() => {
    focusCellEditor(next.id);
    scrollToCell(next.id, 'center', 'auto');
  });
  scrollToCell(next.id, 'center', 'auto');
}

async function runCell(id, advance = false) {
  const c = getCell(id);
  if (!c) return;

  if (c.cell_type === 'markdown') {
    if (editing.has(id) || document.getElementById('ed-' + id)) {
      await renderMarkdown(id);
    }
    if (advance) await advanceFrom(id);
    return;
  }
  // ⇧⏎ on prompt cells dispatches them to their generator.
  if (c.cell_type === 'ai') {
    await generateCell(id);
    return;
  }
  if (c.cell_type === 'vis') {
    await generateVisCell(id);
    return;
  }
  if (c.cell_type === 'raw') {
    await saveCell(id);
    if (advance) await advanceFrom(id);
    return;
  }

  const runTab = activeTab();
  const runSession = currentSession;
  try {
    await saveCell(id, runTab);
  } catch (error) {
    flash('Cannot run until the cell is saved: ' + errText(error));
    return;
  }
  const source = c.source;
  const notebook = runTab.path;
  const runId = CLIENT_ID + '-run-' + Date.now().toString(36) +
    Math.random().toString(36).slice(2);
  const finished = waitForRunDone(runId, notebook, id);
  activeRuns.set(notebook, runId);
  c.source = source;

  const outEl = active === notebook ? document.getElementById('out-' + id) : null;
  if (outEl && source.trim()) {
    c.outputs = [];
    setCellRunning(id, true);
    outEl.innerHTML = runningStatusHtml();
    showRunning(id);       // the spinner is inside a hidden box on a collapsed cell
  } else if (outEl && !source.trim()) {
    // Empty cell — clear any stale output immediately, don't wait for SSE.
    outEl.innerHTML = '';
    c.outputs = [];
    syncOutputView(id);
  }

  try {
    await api(`/api/cells/${id}/run?${new URLSearchParams({notebook})}`, {
      method: 'POST', headers: runSession ? {'X-Session-Id': runSession} : {},
      body: JSON.stringify({source, expected_source: source, run_id: runId})});
    // JupyterLab's ⇧⏎ starts execution and immediately moves focus to the next
    // cell. The output continues arriving asynchronously via SSE.
    if (advance && active === notebook) {
      await advanceFrom(id);
      scheduleFoldPreviewReset(id);
    }
    await finished;
  } catch (err) {
    setCellRunning(id, false);
    if (outEl) outEl.innerHTML = `<div class="outputs"><div class="output error">${escapeHtml(err)}</div></div>`;
    syncOutputView(id);
  } finally {
    runWaiters.delete(runId);
    runContexts.delete(runId);
    if (activeRuns.get(notebook) === runId) activeRuns.delete(notebook);
    if (active === notebook) scheduleFoldPreviewReset(id);
  }
}

/** Run the cell the caret is in. Reached via ⇧⏎; no toolbar button. */
function runSelected() {
  if (selected) runCell(selected);
  else if (cells.length) runCell(cells[0].id);
}

async function runAll() {
  for (const c of cells) {
    if (c.cell_type === 'code' && c.source.trim()) {
      scrollToCell(c.id);          // follow along as execution advances
      await runCell(c.id);
    } else if (c.cell_type === 'markdown') {
      editing.delete(c.id);
    }
  }
  render();
}

async function addCell(type = 'code', after = null, options = {}) {
  const anchorId = after || selected;
  const restore = anchorId ? preserveCellViewport(anchorId) : () => {};
  const revealBlock = options.revealBlock || 'nearest';
  const body = {cell_type: type};
  if (after) body.after = after;
  else if (selected) body.after = selected;
  const cell = await api('/api/cells' + nbq(), {method: 'POST', body: JSON.stringify(body)});
  requestCellFocus(cell.id, revealBlock);
  await load();
  restore();
  requestAnimationFrame(restore);
  if (!applyPendingCellFocus()) {
    selectCell(cell.id);
    focusCellEditor(cell.id);
    scrollToCell(cell.id, revealBlock, 'auto');
  }
  return cell;      // insertSkill() needs the id, to fill the cell it just made
}

async function deleteCell(id) {
  // Where the highlight lands afterwards, worked out before the cell is gone:
  // deleting the cell you're on should leave you on its neighbour, as it does in
  // JupyterLab, rather than on nothing.
  const i = cells.findIndex(c => c.id === id);
  const restoreSlot = preserveViewportSlot(id);
  const heir = (cells[i + 1] || cells[i - 1] || {}).id || null;
  await api(`/api/cells/${id}${nbq()}`, {method: 'DELETE'});
  selected = null;                 // so selectCell() below isn't a no-op
  await load();
  // The notebook is never left empty — the server appends a cell if the last one
  // went — so re-select whatever survived at that position.
  const target = heir && getCell(heir)
    ? heir
    : (cells.length ? cells[Math.min(i, cells.length - 1)].id : null);
  if (target) {
    selectCell(target);
    focusCellEditor(target);
    restoreSlot(target);
    requestAnimationFrame(() => {
      restoreSlot(target);
      focusCellEditor(target);
    });
  }
}

async function changeType(id, type) {
  const restore = preserveCellViewport(id);
  selected = id;
  await saveCell(id);
  if (type === 'markdown') editing.add(id); else editing.delete(id);
  await api(`/api/cells/${id}${nbq()}`,
            {method: 'PATCH', body: JSON.stringify({cell_type: type})});
  await load();
  restore();
  requestAnimationFrame(() => {
    restore();
    selectCell(id);
    focusCellEditor(id, false);
  });
  setTimeout(() => {
    restore();
    selectCell(id);
    focusCellEditor(id, false);
  }, 50);
}

async function moveCell(id, delta) {
  const i = cells.findIndex(c => c.id === id);
  const target = i + delta;
  if (i < 0 || target < 0 || target >= cells.length) return;
  await api(`/api/cells/${id}/move${nbq()}`,
            {method: 'POST', body: JSON.stringify({index: target})});
  await load();
}

// ---------- Inline LLM: an AI cell becomes a code cell ----------
/**
 * Send this cell's prompt to the inline LLM and replace it with the Python it
 * writes. The prompt is kept on the resulting cell so you can see what you
 * asked for and ask again.
 */
async function generateCell(id) {
  const c = getCell(id);
  if (!c) return;
  const ed = document.getElementById('ed-' + id);
  const promptText = (ed ? ed.value : c.source || '').trim();
  if (!promptText) {
    flash('Type what you want the code to do first.');
    if (ed) ed.focus();
    return;
  }
  await saveCell(id);

  const btn = document.getElementById('aigo-' + id);
  const meta = document.getElementById('aimodel-' + id);
  if (btn) { btn.disabled = true; btn.textContent = 'Writing…'; }
  if (meta) meta.textContent = '';

  try {
    const data = await api(`/api/cells/${id}/ai${nbq()}`, {
      method: 'POST', body: JSON.stringify({prompt: promptText}),
    });
    editing.delete(id);
    await load();
    selectCell(id);
    const newEd = document.getElementById('ed-' + id);
    if (newEd) newEd.focus();
    const u = data.usage || {};
    flash(`${data.model}${u.total_tokens ? ' · ' + u.total_tokens + ' tokens' : ''}`
          + ' — review the code, then ⇧⏎ to run it');
  } catch (err) {
    if (btn) { btn.disabled = false; btn.textContent = 'Generate'; }
    flash('Inline LLM: ' + errText(err));
  }
}

/** Ask again with the prompt already attached to a generated cell. */
async function regenerate(id) {
  const c = getCell(id);
  if (!c || !c.prompt) return;
  const outEl = document.getElementById('out-' + id);
  if (outEl) outEl.innerHTML = '<div class="spin" role="status">writing</div>';
  try {
    const data = await api(`/api/cells/${id}/ai${nbq()}`, {
      method: 'POST', body: JSON.stringify({prompt: c.prompt}),
    });
    await load();
    flash(`Rewritten by ${data.model}`);
  } catch (err) {
    if (outEl) outEl.innerHTML = '';
    flash('Inline LLM: ' + errText(err));
  }
}

function visAgentPrompt(cell, promptText) {
  return `Turn this GusNotebook Vis request into a normal runnable Python code cell.

Notebook: ${active}
Cell id to replace: ${cell.id}

User visualization request:
${promptText}

Required behavior:
- Treat the Vis cell as a code cell with an additional visualization prompt.
- Generate one compact Python code cell. It should contain any Python prep
  needed, then an IPython HTML(...) display call that renders the D3 figure.
- Replace cell ${cell.id} itself. Do not create a new cell, scratch file, or
  alternate attempt.
- Use this exact workflow once:

cat <<'PY' | gusnb set ${cell.id} - --run
# generated Python visualization code here
PY

- After replacement, the cell must behave like any other normal Python code cell.
- The code must produce rich text/html output, normally with:

from IPython.display import HTML
HTML(r'''...''')

- Prefer D3 v7 for custom interactive charts when appropriate.
- Keep visualizations static on initial render: do not use D3 transitions,
  delayed reveals, animated loading effects, CSS animations, or timed entrance
  effects unless the user explicitly asks for animation.
- Do not write .transition(), .duration(), .delay(), requestAnimationFrame,
  setInterval, or setTimeout for visual effects. Set all SVG attributes,
  styles, and text directly to their final values.
- Do not initialize marks to temporary animated states such as bar height 0,
  radius 0, opacity 0, or off-canvas positions. Create bars, points, paths,
  labels, and axes directly in their final visible state.
- Size charts for slide/readout embedding from the start. For a full-width
  single plot, use margins close to {top: 40, right: 160, bottom: 50, left: 60}
  with plot area width 520 and height 340, keeping the total SVG under about
  700 by 430. For two side-by-side plots, use plot area width 440 and height
  320, keeping each total under about 600 by 400.
- Put the generated chart inside a root element with class "viz-root", and
  include local CSS in the HTML output so .viz-root has background:none,
  margin:0, and padding:0.
- Use a unique root element id in the HTML.
- Keep all JavaScript scoped inside an IIFE.
- Make the visualization responsive to the output width.
- If the prompt refers to notebook data, use the obvious nearby variables/files.
  Otherwise generate the requested visualization directly.
- Verify only: same cell id exists, cell is normal code, rendered output exists.
- The command above automatically preserves undo metadata and the agent prompt in GusNotebook.`;
}

async function generateVisCell(id) {
  const c = getCell(id);
  if (!c) return;
  const ed = document.getElementById('ed-' + id);
  const promptText = (ed ? ed.value : c.source || '').trim();
  if (!promptText) {
    flash('Describe the visualization first.');
    if (ed) ed.focus();
    return;
  }
  await saveCell(id);
  const btn = document.getElementById('visgo-' + id);
  if (btn) { btn.disabled = true; btn.textContent = 'Sending...'; }
  try {
    const t = await sendPromptToAgent(visAgentPrompt(c, promptText));
    flash(`Sent visualization replacement request to ${t.kind}`);
  } catch (err) {
    if (btn) { btn.disabled = false; btn.textContent = 'Generate'; }
    flash('Vis agent: ' + errText(err));
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Generate'; }
  }
}

// ---------- Settings ----------
let settingsData = null;

/* Restrictions: one render/collect pair, shared by ⚙ Settings (the app-wide set)
 * and by a session's own. The preset list comes from the server rather than
 * being spelled out here as well — a label that disagrees with the rules it
 * stands for is worse than no label at all. */
let restrictionPresets = null;

async function loadRestrictionPresets() {
  if (restrictionPresets) return restrictionPresets;
  try {
    restrictionPresets = (await api('/api/settings')).restrictions || [];
  } catch (err) {
    restrictionPresets = [];       // the editor degrades to its extra-rules box
  }
  return restrictionPresets;
}

function renderRestrictions(el, current) {
  const r = current || {};
  el.innerHTML = (restrictionPresets || []).map(p => `
    <label>
      <input type="checkbox" value="${escapeAttr(p.key)}" ${r[p.key] ? 'checked' : ''}>
      <span>${escapeHtml(p.label)}
        <span class="rn">${p.rules} rule${p.rules === 1 ? '' : 's'}</span></span>
    </label>`).join('') ||
    '<div class="fhelp" style="margin:0">No presets available — use the box below.</div>';
}

/** What a pair of editors is asking for. Unticked keys are left out rather than
 *  written `false`: an empty dict is what "nothing restricted" means everywhere
 *  else, and {no_execute: false} would read as deliberately configured. */
function collectRestrictions(el, extraEl) {
  const out = {};
  el.querySelectorAll('input[type="checkbox"]').forEach(cb => {
    if (cb.checked) out[cb.value] = true;
  });
  const extra = (extraEl.value || '').trim();
  if (extra) out.deny_extra = extra;
  return out;
}

/** Whether a stored set actually blocks anything — what the ▤ marker reads. */
function hasRestrictions(r) {
  return !!r && Object.keys(r).some(k => k === 'deny_extra' ? !!r[k] : r[k]);
}

// The inline LLM and gateway fields are hidden in the settings modal — no API
// is in use right now. They stay in the DOM (see the .llm-hidden wrappers in
// templates/index.html), so their values still round-trip on save; this flag
// only keeps the (now unreachable) model picker from blocking a save of the
// visible fields.
const LLM_SETTINGS_HIDDEN = true;

async function openSettings() {
  const back = document.getElementById('settings-back');
  // Cleared first so "are the fields filled in yet?" has one honest answer,
  // rather than the previous open's data standing in for this one.
  settingsData = null;
  let data;
  try {
    data = await api('/api/settings');
  } catch (err) {
    // Nothing is shown on failure: an empty modal looks broken and gives the
    // user nothing to act on, so say what went wrong instead.
    flash('Cannot load settings: ' + errText(err));
    return;
  }
  // Fill the fields, *then* reveal — the modal is never visible while empty.
  settingsData = data;
  const s = settingsData.settings || {};
  const models = settingsData.models || [];
  const sel = document.getElementById('set-model');
  const known = models.includes(s.inline_llm_model);
  sel.innerHTML = models.map(m =>
    `<option value="${escapeAttr(m)}" ${m === s.inline_llm_model ? 'selected' : ''}>
       ${escapeHtml(m)}</option>`).join('') +
    `<option value="" ${known ? '' : 'selected'}>Other…</option>`;

  document.getElementById('set-model-custom').value = known ? '' : (s.inline_llm_model || '');
  document.getElementById('set-instructions').value = s.inline_llm_instructions || '';
  document.getElementById('set-max-tokens').value = s.inline_llm_max_tokens || 1200;
  document.getElementById('set-fold-focus').checked = foldOpenOnFocus;
  document.getElementById('set-fold-lines').value = codeFoldLines;
  document.getElementById('set-claude').value = s.claude_instructions || '';

  // The presets come on this same response, so no second round trip here.
  restrictionPresets = settingsData.restrictions || [];
  const restrict = s.claude_restrictions || {};
  renderRestrictions(document.getElementById('set-restrict'), restrict);
  document.getElementById('set-restrict-extra').value = restrict.deny_extra || '';

  // Credentials. The key comes back masked, never in the clear; leaving the
  // mask alone on save means "keep the stored one".
  document.getElementById('set-gw-url').value = s.gateway_url || '';
  document.getElementById('set-gw-key').value = s.gateway_key || '';
  document.getElementById('set-gw-store').value = s.gateway_key_store || 'session';
  document.getElementById('set-gw-key').placeholder =
    settingsData.has_key && !s.gateway_key
      ? `using the ${settingsData.key_source || 'environment'} — type a key to override`
      : 'AI_GATEWAY_KEY';

  const where = settingsData.key_source
    ? ({session: 'held for this session only, not written to disk',
        settings: 'saved in settings.json',
        environment: 'from the environment',
        '.env': 'from .env'}[settingsData.key_source] || settingsData.key_source)
    : null;
  document.getElementById('set-gateway').innerHTML =
    `In use: <code>${escapeHtml(settingsData.gateway || '—')}</code><br>` +
    (settingsData.has_key
      ? `<span class="good">API key found</span> — ${escapeHtml(where)}`
      : '<span class="bad">No API key</span> — add one above, or set ' +
        'AI_GATEWAY_KEY in .env') +
    '<br>Used by <b>+ AI</b> cells, the <b>Help</b> button, and Claude Code ' +
    'terminals (new sessions). Codex uses your existing Codex login.' +
    // Where everything on this modal is written. Otherwise unanswerable from the
    // UI once the app is installed as a tool rather than run from a checkout —
    // "settings.json" is no help if you can't find it.
    (settingsData.state_dir
      ? `<br>Settings, sessions and skills: <code>${
           escapeHtml(settingsData.state_dir)}</code>`
      : '');

  back.classList.add('on');
}

/** Selecting a listed model clears the free-text box, and vice versa. */
function pickModel(value) {
  if (value) document.getElementById('set-model-custom').value = '';
}

function closeSettings() {
  document.getElementById('settings-back').classList.remove('on');
}

async function saveSettings() {
  const custom = document.getElementById('set-model-custom').value.trim();
  const foldLines = parseInt(document.getElementById('set-fold-lines').value, 10);
  codeFoldLines = Number.isFinite(foldLines) ? Math.min(60, Math.max(1, foldLines)) : 10;
  foldOpenOnFocus = document.getElementById('set-fold-focus').checked;
  writeFoldPreferences();
  const body = {
    inline_llm_model: custom || document.getElementById('set-model').value,
    inline_llm_instructions: document.getElementById('set-instructions').value,
    inline_llm_max_tokens:
      parseInt(document.getElementById('set-max-tokens').value, 10) || 1200,
    claude_instructions: document.getElementById('set-claude').value,
    claude_restrictions: collectRestrictions(
      document.getElementById('set-restrict'),
      document.getElementById('set-restrict-extra')),
    gateway_url: document.getElementById('set-gw-url').value.trim(),
    // Sent as-is: if it's still the mask, the server reads that as "unchanged".
    gateway_key: document.getElementById('set-gw-key').value.trim(),
    gateway_key_store: document.getElementById('set-gw-store').value,
  };
  if (!body.inline_llm_model && !LLM_SETTINGS_HIDDEN) {
    flash('Pick a model, or type a deployment name.');
    return;
  }
  try {
    settingsData = await api('/api/settings',
                             {method: 'POST', body: JSON.stringify(body)});
    closeSettings();
    if (isNotebookTab()) render();
    // Say out loud that a restriction isn't retroactive. A user who thinks a
    // guardrail is live on a terminal already running is the failure mode.
    flash(hasRestrictions(body.claude_restrictions)
      ? 'Saved — restrictions apply to new Claude terminals, not ones already open'
      : body.claude_instructions.trim()
        ? 'Saved — new Claude and Codex agents will use these instructions'
        : LLM_SETTINGS_HIDDEN
          ? 'Saved'
          : 'Inline LLM → ' + body.inline_llm_model);
  } catch (err) {
    flash('Cannot save settings: ' + errText(err));
  }
}

// ---------- Error help (one model call) ----------
async function getHelp(id) {
  const panel = document.getElementById('help-' + id);
  if (!panel) return;

  panel.innerHTML = '<div class="help-loading">asking the model…</div>';
  try {
    const r = await fetch(BASE + `/api/cells/${id}/help${nbq()}`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json',
                ...(currentSession ? {'X-Session-Id': currentSession} : {})},
      body: '{}',
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);

    const u = data.usage || {};
    const meta = [data.model, u.total_tokens ? u.total_tokens + ' tokens' : null]
      .filter(Boolean).join(' · ');
    panel.innerHTML = `
      <div class="help-box">
        <div class="help-head">
          <span class="help-tag">Help</span>
          <span class="help-meta">${escapeHtml(meta)}</span>
          <button class="help-close" onclick="event.stopPropagation();closeHelp('${id}')">✕</button>
        </div>
        <div class="help-body">${DOMPurify.sanitize(marked.parse(data.markdown || ''))}</div>
      </div>`;
  } catch (err) {
    panel.innerHTML = `<div class="help-box error-box">
      <div class="help-head"><span class="help-tag">Help failed</span>
      <button class="help-close" onclick="event.stopPropagation();closeHelp('${id}')">✕</button></div>
      <div class="help-body"><p>${escapeHtml(err.message)}</p></div></div>`;
  }
}

function closeHelp(id) {
  const panel = document.getElementById('help-' + id);
  if (panel) panel.innerHTML = '';
}

// Toolbar wrapper — operates on the cell the caret is in.
function deleteSelected() {
  if (selected) deleteCell(selected);
}

async function clearOutputs() {
  await api('/api/clear-outputs' + nbq(), {method: 'POST', body: JSON.stringify({})});
  await load();
}

const restartKernel = () => api('/api/kernel/restart' + nbq(), {method: 'POST'});
async function interruptKernel() {
  const t = activeTab();
  if (!t || t.kind !== 'notebook') return;
  const previousStatus = t.status || 'busy';
  setKernelStatus('stopping');
  try {
    await api('/api/kernel/interrupt' + nbq(), {
      method: 'POST',
      body: JSON.stringify({run_id: activeRuns.get(active) || null}),
    });
  } catch (err) {
    setKernelStatus(previousStatus);
    flash('Stop failed: ' + errText(err));
  }
}

// ---------- Kernel status ----------
function setKernelStatus(status) {
  const t = activeTab();
  if (t && t.kind === 'notebook') t.status = status;
  document.getElementById('k-dot').className = 'dot ' + status;
  const el = document.getElementById('k-status');
  el.textContent = status;
  el.title = (t && t.python ? t.python : 'python') + ' — ' + status;
}
