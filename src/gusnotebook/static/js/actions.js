/* What the user does to a notebook: edit, run, restart, generate, explain.
 *
 * Ends with the settings modal and the kernel-status indicator, both of which
 * are about the notebook as a whole rather than any one cell. */

// ---------- Editing ----------
const saveTimers = {};

// Cells typed into but not yet PATCHed. `cells[].source` lags by up to the save
// debounce, so it is not the answer to "what is in this editor" — and mountEditor()
// reconciles a reused CM view against it. Without this, any re-render landing
// inside that window (a run finishing, a cell added, a kernel event) would push
// the stale server text back over what the user just typed.
const unsaved = new Set();

function queueSave(id) {
  unsaved.add(id);
  clearTimeout(saveTimers[id]);
  saveTimers[id] = setTimeout(() => saveCell(id), 500);
}

async function saveCell(id) {
  const ed = document.getElementById('ed-' + id);
  if (!ed) return;
  const c = getCell(id);
  if (c) c.source = ed.value;
  unsaved.delete(id);
  clearTimeout(saveTimers[id]);
  await api(`/api/cells/${id}${nbq()}`,
            {method: 'PATCH', body: JSON.stringify({source: ed.value})});
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

function editMarkdown(id) {
  editing.add(id);
  render();
  const ed = document.getElementById('ed-' + id);
  if (ed) { ed.focus(); autosize(ed); }
}

async function renderMarkdown(id) {
  await saveCell(id);
  editing.delete(id);
  render();
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
    saveCell(id);
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

/**
 * Move to the cell after `id` — appending a new one only at the end of the
 * notebook. This is what ⇧⏎ does after running.
 */
async function advanceFrom(id) {
  const i = cells.findIndex(c => c.id === id);
  if (i === -1) return;

  if (i === cells.length - 1) { await addCell('code', id); return; }

  const next = cells[i + 1];
  selectCell(next.id);
  const ed = document.getElementById('ed-' + next.id);
  if (ed) { ed.focus(); ed.setSelectionRange(ed.value.length, ed.value.length); }
  else if (document.activeElement) document.activeElement.blur();
  scrollToCell(next.id);
}

async function runCell(id, advance = false) {
  const c = getCell(id);
  if (!c) return;

  if (c.cell_type === 'markdown') {
    await renderMarkdown(id);
    if (advance) await advanceFrom(id);
    return;
  }
  // ⇧⏎ on an AI cell generates the code — that's its "run".
  if (c.cell_type === 'ai') {
    await generateCell(id);
    return;
  }
  if (c.cell_type === 'raw') {
    await saveCell(id);
    if (advance) await advanceFrom(id);
    return;
  }

  const ed = document.getElementById('ed-' + id);
  const source = ed ? ed.value : c.source;
  c.source = source;
  clearTimeout(saveTimers[id]);

  const outEl = document.getElementById('out-' + id);
  if (outEl && source.trim()) {
    outEl.innerHTML = '<div class="spin">running…</div>';
    showRunning(id);       // the spinner is inside a hidden box on a collapsed cell
  } else if (outEl && !source.trim()) {
    // Empty cell — clear any stale output immediately, don't wait for SSE.
    outEl.innerHTML = '';
    c.outputs = [];
    syncOutputView(id);
  }

  try {
    await api(`/api/cells/${id}/run${nbq()}`, {method: 'POST', body: JSON.stringify({source})});
  } catch (err) {
    if (outEl) outEl.innerHTML = `<div class="outputs"><div class="output error">${escapeHtml(err)}</div></div>`;
    syncOutputView(id);
  }
  if (advance) await advanceFrom(id);
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

async function addCell(type = 'code', after = null) {
  const body = {cell_type: type};
  if (after) body.after = after;
  else if (selected) body.after = selected;
  const cell = await api('/api/cells' + nbq(), {method: 'POST', body: JSON.stringify(body)});
  await load();
  selectCell(cell.id);
  const ed = document.getElementById('ed-' + cell.id);
  if (ed) ed.focus();
  scrollToCell(cell.id, 'center');
  return cell;      // insertSkill() needs the id, to fill the cell it just made
}

async function deleteCell(id) {
  // Where the highlight lands afterwards, worked out before the cell is gone:
  // deleting the cell you're on should leave you on its neighbour, as it does in
  // JupyterLab, rather than on nothing.
  const i = cells.findIndex(c => c.id === id);
  const heir = selected === id
    ? (cells[i + 1] || cells[i - 1] || {}).id || null
    : selected;
  await api(`/api/cells/${id}${nbq()}`, {method: 'DELETE'});
  selected = null;                 // so selectCell() below isn't a no-op
  await load();
  // The notebook is never left empty — the server appends a cell if the last one
  // went — so re-select whatever survived at that position.
  if (heir && getCell(heir)) selectCell(heir);
  else if (cells.length) selectCell(cells[Math.min(i, cells.length - 1)].id);
}

async function changeType(id, type) {
  await saveCell(id);
  if (type === 'markdown') editing.add(id); else editing.delete(id);
  await api(`/api/cells/${id}${nbq()}`,
            {method: 'PATCH', body: JSON.stringify({cell_type: type})});
  await load();
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
  if (outEl) outEl.innerHTML = '<div class="spin">writing…</div>';
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
  if (!body.inline_llm_model) {
    flash('Pick a model, or type a deployment name.');
    return;
  }
  try {
    settingsData = await api('/api/settings',
                             {method: 'POST', body: JSON.stringify(body)});
    closeSettings();
    // Say out loud that a restriction isn't retroactive. A user who thinks a
    // guardrail is live on a terminal already running is the failure mode.
    flash(hasRestrictions(body.claude_restrictions)
      ? 'Saved — restrictions apply to new Claude terminals, not ones already open'
      : body.claude_instructions.trim()
        ? 'Saved — new Claude and Codex agents will use these instructions'
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
      headers: {'Content-Type': 'application/json'},
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
const interruptKernel = () => api('/api/kernel/interrupt' + nbq(), {method: 'POST'});

// ---------- Kernel status ----------
function setKernelStatus(status) {
  document.getElementById('k-dot').className = 'dot ' + status;
  const el = document.getElementById('k-status');
  el.textContent = status;
  const t = activeTab();
  el.title = (t && t.python ? t.python : 'python') + ' — ' + status;
}
