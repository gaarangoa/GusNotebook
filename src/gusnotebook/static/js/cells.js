/* A notebook's cells, from output rendering to the markup of the cell itself.
 *
 * Also the env picker and the text-tab editor, which live here rather than in
 * their own files because both are per-tab state that render() reads. */

// ---------- A cell's type, from the ⌥ in its gutter ----------

let typeMenuCell = null;      // which cell the shared menu is open for

function closeTypeMenu() {
  document.getElementById('type-menu').classList.remove('on');
  typeMenuCell = null;
}

function toggleTypeMenu(ev, id) {
  if (ev) ev.stopPropagation();
  const menu = document.getElementById('type-menu');
  if (typeMenuCell === id) { closeTypeMenu(); return; }
  // Anchored to the button, and pulled back inside the window: the gutter is at
  // the left edge, so only the bottom needs guarding against.
  const r = ev.currentTarget.getBoundingClientRect();
  menu.style.top = Math.min(r.bottom + 4, window.innerHeight - 130) + 'px';
  menu.style.left = r.left + 'px';
  menu.classList.add('on');
  typeMenuCell = id;
  selectCell(id);       // acting on a cell parks you on it, as clicking it does
}

function typeFromMenu(type) {
  const id = typeMenuCell;
  closeTypeMenu();
  if (!id) return;
  const c = getCell(id);
  if (!c || c.cell_type === type) return;    // no round-trip to change nothing
  changeType(id, type);
}

document.addEventListener('click', (e) => {
  if (!e.target.closest('#type-menu')) closeTypeMenu();
});

// ---------- Visual HTML / SVG editor ----------

const MARKUP_EDITOR_CHANNEL = 'gusnotebook-markup-editor';
let markupPreviewSerial = 0;
let markupFocusTimer = null;
let reportedMarkupPath = null;
let markupDiskPollBusy = false;

function isMarkupTab(t) {
  return !!t && t.kind === 'text' &&
    (t.language === 'html' || t.language === 'htm' || t.language === 'svg');
}

function markTextExternalConflict(t) {
  if (!t || t.kind !== 'text') return;
  if (!t.dirty) {
    reloadTextFromDisk(t, true).then(ok => {
      if (ok) flash(`${t.name} reloaded after an external change`);
    });
    return;
  }
  t.externalConflict = true;
  clearTimeout(markupFocusTimer);
  reportedMarkupPath = null;
  if (active === t.path) {
    setMarkupSelectionBadge(false);
    document.getElementById('text-status').textContent = 'changed on disk · reload';
  }
  flash(`${t.name} changed on disk — reload before editing or saving`);
}

function setMarkupSelectionBadge(selected) {
  const badge = document.getElementById('html-live');
  badge.textContent = selected
    ? 'selection ready for agent'
    : 'edit directly · select a region for the agent';
}

function publishMarkupFocus(t, selection) {
  clearTimeout(markupFocusTimer);
  reportedMarkupPath = selection ? t.path : null;
  setMarkupSelectionBadge(!!selection);
  markupFocusTimer = setTimeout(() => {
    api('/api/markup-focus', {method: 'POST',
      body: JSON.stringify({path: t.path, source: t.text || '',
                            disk_version: t.diskVersion,
                            selection: selection})}).catch(err => {
      if (errCode(err) === 'external_change') markTextExternalConflict(t);
    });
  }, 80);
}

function clearMarkupFocus() {
  clearTimeout(markupFocusTimer);
  const path = reportedMarkupPath;
  reportedMarkupPath = null;
  setMarkupSelectionBadge(false);
  if (!path) return;
  api('/api/markup-focus', {method: 'POST',
    body: JSON.stringify({path: path, selection: null})}).catch(() => {});
}

async function renderMarkupEditor() {
  const t = activeTab();
  if (!isMarkupTab(t)) return;
  clearMarkupFocus();
  t.previewNonce = `${Date.now()}-${++markupPreviewSerial}`;
  const nonce = t.previewNonce;
  const frame = document.getElementById('html-preview-frame');
  try {
    const data = await api('/api/preview', {method: 'POST', body: JSON.stringify({
      path: t.path, source: t.text || '', nonce: nonce,
      parent_origin: window.location.origin,
    })});
    if (active !== t.path || t.previewNonce !== nonce) return;
    t.previewOrigin = data.origin;
    t.previewUrl = data.url;
    t.previewVersion = data.preview_version;
    frame.src = data.url;
  } catch (err) {
    if (active === t.path) {
      document.getElementById('text-status').textContent = 'preview failed';
      flash('Preview failed: ' + errText(err));
    }
  }
}

function acceptMarkupEdit(t, text) {
  if (typeof text !== 'string' || text === t.text) return;
  clearMarkupFocus();
  t.text = text;
  t.editRevision = (t.editRevision || 0) + 1;
  document.getElementById('text-editor').value = text;
  if (active === t.path) document.getElementById('text-status').textContent = 'unsaved';
  if (!t.dirty) { t.dirty = true; renderTabs(); }
}

window.addEventListener('message', event => {
  const frame = document.getElementById('html-preview-frame');
  const data = event.data || {};
  const t = activeTab();
  if (event.source !== frame.contentWindow || !isMarkupTab(t) ||
      event.origin !== t.previewOrigin ||
      data.channel !== MARKUP_EDITOR_CHANNEL || data.nonce !== t.previewNonce) return;
  if (data.kind === 'selection') {
    publishMarkupFocus(t, data.selection || null);
    return;
  }
  if (typeof data.text === 'string') acceptMarkupEdit(t, data.text);
  if (data.kind === 'save') {
    clearTimeout(t.markupSaveTimer);
    t.markupSaveTimer = null;
    persistText(t, t.text || '');
  }
});

async function reloadTextFromDisk(target, force) {
  const t = target || activeTab();
  if (!t || t.kind !== 'text' || t.reloadInFlight) return false;
  t.reloadInFlight = true;
  try {
    if (t.dirty && !force) {
      const discard = await askConfirm(
        `${t.name} has unsaved changes and also changed on disk.`,
        'Reloading keeps the external file and discards this browser buffer.',
        'Reload and discard');
      if (!discard) return false;
    }
    const data = await api('/api/open', {
      method: 'POST', body: JSON.stringify({path: t.path})});
    t.text = data.text || '';
    t.diskVersion = data.disk_version;
    t.previewOrigin = data.preview_origin || t.previewOrigin;
    t.previewVersion = data.preview_version;
    t.dirty = false;
    t.externalConflict = false;
    t.editRevision = (t.editRevision || 0) + 1;
    if (active === t.path) {
      document.getElementById('text-editor').value = t.text;
      showActive();
      document.getElementById('text-status').textContent = 'reloaded from disk';
    }
    renderTabs();
    return true;
  } catch (err) {
    if (active === t.path) document.getElementById('text-status').textContent = 'reload failed';
    flash('Reload failed: ' + errText(err));
    return false;
  } finally {
    t.reloadInFlight = false;
  }
}

/** Follow normal agent/file-tool saves that happen outside GusNotebook.
 * A clean canvas can safely repaint immediately. A dirty canvas keeps the
 * browser edit and advertises the conflict until the user explicitly reloads.
 */
async function pollMarkupDisk() {
  const t = activeTab();
  if (!isMarkupTab(t) || !t.diskVersion || t.saveInFlight || t.reloadInFlight ||
      markupDiskPollBusy) return;
  markupDiskPollBusy = true;
  try {
    const query = new URLSearchParams({path: t.path});
    const data = await api('/api/text-version?' + query.toString());
    if (active !== t.path ||
        (data.disk_version === t.diskVersion &&
         data.preview_version === t.previewVersion)) return;
    if (t.dirty) {
      if (!t.externalConflict) markTextExternalConflict(t);
      return;
    }
    const reloaded = await reloadTextFromDisk(t, true);
    if (reloaded) flash(`${t.name} reloaded after an agent changed it`);
  } catch (err) {
    // Polling is best-effort. Open, save, and reload still report their errors.
  } finally {
    markupDiskPollBusy = false;
  }
}

setInterval(pollMarkupDisk, 800);

/** Show whichever pane the active tab needs, and fill it. */
function showActive() {
  const t = activeTab();
  const kind = t ? t.kind : 'none';
  const isNb = kind === 'notebook' || kind === 'none';

  document.getElementById('notebook-pane').style.display = isNb ? '' : 'none';
  document.getElementById('toolbar').style.display = kind === 'notebook' ? '' : 'none';
  const textPane = document.getElementById('textpane');
  textPane.classList.toggle('on', kind === 'text');
  textPane.classList.toggle('markup', isMarkupTab(t));
  document.getElementById('imgpane').classList.toggle('on', kind === 'image');
  if (!isMarkupTab(t)) {
    clearMarkupFocus();
    // Stop animations, timers and media belonging to a preview that is no
    // longer visible. It is rebuilt from the tab's source when the user returns.
    const frame = document.getElementById('html-preview-frame');
    frame.src = 'about:blank';
  }

  // The app's name on a notebook tab — the same text the markup ships with, so
  // rendering a tab doesn't quietly rename the app back to something generic.
  document.getElementById('nb-label').textContent =
    kind === 'notebook' ? 'GusNotebook' : (t ? t.name.split('.').pop() : '');
  const pathEl = document.getElementById('nb-path');
  pathEl.textContent = t ? t.name : '';
  pathEl.title = t ? t.path : '';

  if (kind === 'notebook') {
    render();
    document.getElementById('notebook-pane').scrollTop = t.scroll || 0;
    setKernelStatus(t.status || 'stopped');
    refreshVenvBtn();
  } else if (kind === 'text') {
    const ed = document.getElementById('text-editor');
    ed.value = t.text || '';
    document.getElementById('text-lang').textContent = t.language || 'text';
    document.getElementById('text-status').textContent = t.externalConflict
      ? 'changed on disk · reload' : (t.dirty ? 'unsaved' : 'saved');
    if (isMarkupTab(t)) renderMarkupEditor();
  } else if (kind === 'image') {
    document.getElementById('imgview').src = BASE + t.url;
  } else {
    document.getElementById('notebook').innerHTML =
      '<div class="files-msg">No file open — pick one in the browser on the left.</div>';
  }
}

/** Open any file in a tab (or focus the tab that already has it). */
async function openFile(path) {
  if (tab(path)) { switchTab(path); return; }
  let data;
  try {
    data = await api('/api/open', {method: 'POST', body: JSON.stringify({path})});
  } catch (err) {
    flash('Cannot open: ' + errText(err));
    return;
  }
  const p = data.path || path;
  if (tab(p)) { switchTab(p); return; }      // server normalized to an open tab

  const t = {path: p, name: p.split('/').pop(), kind: data.kind || 'text', dirty: false};
  if (t.kind === 'notebook') {
    Object.assign(t, {cells: data.cells || [], selected: null, editing: new Set(),
                      codeOpen: new Set(), outsHidden: new Set(),
                      python: data.kernel_python || data.python,
                      status: data.kernel_status || 'stopped'});
  } else if (t.kind === 'image') {
    t.url = data.url;
  } else {
    Object.assign(t, {text: data.text || '', language: data.language,
                      previewOrigin: data.preview_origin,
                      previewVersion: data.preview_version,
                      diskVersion: data.disk_version});
  }

  stashActive();
  tabs.push(t);
  active = p;
  if (t.kind === 'notebook') {
    cells = t.cells; selected = null; editing = t.editing;
    codeOpen = t.codeOpen; outsHidden = t.outsHidden;
    headingsCollapsed = t.headingsCollapsed || new Set();
  }
  renderTabs();
  showActive();
  if (fileState.path) browse(fileState.path);   // re-mark which rows are open
  loadSessions();          // the tab joined this session; update its count
}

function switchTab(path) {
  if (path === active) return;
  const t = tab(path);
  if (!t) return;
  stashActive();
  active = path;
  if (t.kind === 'notebook') {
    cells = t.cells || [];
    selected = t.selected || null;
    editing = t.editing || new Set();
    codeOpen = t.codeOpen || new Set();
    outsHidden = t.outsHidden || new Set();
    headingsCollapsed = t.headingsCollapsed || new Set();
  }
  closeVenvMenu();
  renderTabs();
  showActive();
  if (t.kind === 'notebook') load();     // pick up anything changed while away
  if (fileState.path) browse(fileState.path);
}

async function closeTab(path, ev) {
  if (ev) ev.stopPropagation();
  const i = tabs.findIndex(t => t.path === path);
  if (i === -1) return;
  const t = tabs[i];
  // askConfirm, not window.confirm: a suppressed confirm() returns false, which
  // would make closing a dirty tab silently refuse with nothing on screen.
  if (t.dirty && !await askConfirm(`${t.name} has unsaved changes.`,
                                   'Closing it discards the edit.',
                                   'Close anyway')) return;

  if (path === active) stashActive();
  tabs.splice(i, 1);
  try {
    await api('/api/close', {method: 'POST', body: JSON.stringify({path})});
  } catch (err) { /* the tab is gone either way */ }

  if (path === active) {
    const next = tabs[Math.min(i, tabs.length - 1)];
    active = next ? next.path : null;
    if (next && next.kind === 'notebook') {
      cells = next.cells || [];
      selected = next.selected || null;
      editing = next.editing || new Set();
      codeOpen = next.codeOpen || new Set();
      outsHidden = next.outsHidden || new Set();
      headingsCollapsed = next.headingsCollapsed || new Set();
    } else {
      cells = []; selected = null; editing = new Set();
      codeOpen = new Set(); outsHidden = new Set(); headingsCollapsed = new Set();
    }
    showActive();
    if (next && next.kind === 'notebook') load();
  }
  renderTabs();
  if (fileState.path) browse(fileState.path);
  loadSessions();
}

// ---------- Text tabs ----------
function markTextDirty() {
  const t = activeTab();
  if (!t || t.kind !== 'text') return;
  t.text = document.getElementById('text-editor').value;
  t.editRevision = (t.editRevision || 0) + 1;
  document.getElementById('text-status').textContent = 'unsaved';
  if (!t.dirty) { t.dirty = true; renderTabs(); }
}

async function persistText(t, text) {
  if (!t || t.kind !== 'text') return;
  const revision = t.editRevision || 0;
  const st = document.getElementById('text-status');
  if (active === t.path) st.textContent = 'saving…';
  t.saveInFlight = true;
  try {
    const saved = await api('/api/text', {method: 'POST',
      body: JSON.stringify({path: t.path, text, disk_version: t.diskVersion})});
    if ((t.editRevision || 0) === revision) {
      t.text = text;
      t.diskVersion = saved.disk_version;
      t.dirty = false;
      t.externalConflict = false;
      if (active === t.path) st.textContent = 'saved';
      if (isMarkupTab(t)) {
        document.getElementById('html-preview-frame').contentWindow.postMessage(
          {channel: MARKUP_EDITOR_CHANNEL, nonce: t.previewNonce, command: 'saved'},
          t.previewOrigin || '*');
      }
    } else if (active === t.path) {
      st.textContent = 'unsaved';
    }
    renderTabs();
  } catch (err) {
    if (errCode(err) === 'external_change') {
      markTextExternalConflict(t);
      await reloadTextFromDisk(t, false);
      return;
    }
    if (active === t.path) st.textContent = 'save failed';
    flash('Save failed: ' + errText(err));
  } finally {
    t.saveInFlight = false;
  }
}

function saveText() {
  const t = activeTab();
  if (!t || t.kind !== 'text') return;
  if (!isMarkupTab(t)) {
    persistText(t, document.getElementById('text-editor').value);
    return;
  }

  document.getElementById('text-status').textContent = 'saving…';
  const frame = document.getElementById('html-preview-frame');
  frame.contentWindow.postMessage(
    {channel: MARKUP_EDITOR_CHANNEL, nonce: t.previewNonce, command: 'save'},
    t.previewOrigin || '*');
  // A malformed document can prevent the bridge from starting. Saving the last
  // markup received is still better than leaving the toolbar stuck on saving.
  clearTimeout(t.markupSaveTimer);
  t.markupSaveTimer = setTimeout(() => {
    t.markupSaveTimer = null;
    persistText(t, t.text || '');
  }, 750);
}

function onTextKey(e) {
  if (e.key === 's' && (e.metaKey || e.ctrlKey)) {
    e.preventDefault();
    saveText();
    return false;
  }
  if (e.key === 'Tab') {
    e.preventDefault();
    const el = e.target, s = el.selectionStart, en = el.selectionEnd;
    el.value = el.value.slice(0, s) + '    ' + el.value.slice(en);
    el.selectionStart = el.selectionEnd = s + 4;
    markTextDirty();
    return false;
  }
  return true;
}

// ---------- Environment picker (per notebook) ----------
/** ".venv" out of "/proj/.venv/bin/python" — what the button shows. */
function venvLabel(python) {
  if (!python) return 'env…';
  const parts = String(python).split('/');
  return parts.length >= 3 ? parts[parts.length - 3] : parts.pop();
}

function refreshVenvBtn() {
  const t = activeTab();
  const py = t && t.python;
  document.getElementById('venv-name').textContent = py ? venvLabel(py) : 'env…';
  document.getElementById('venv-btn').title = py
    ? `${py}\nClick to switch this notebook's environment`
    : 'Pick a Python environment for this notebook';
}

function closeVenvMenu() {
  document.getElementById('venv-menu').classList.remove('on');
}

async function toggleVenvMenu(ev) {
  if (ev) ev.stopPropagation();
  const menu = document.getElementById('venv-menu');
  if (menu.classList.contains('on')) { closeVenvMenu(); return; }
  if (!active || !isNotebookTab()) return;

  // Anchor under the badge's right edge. The menu is position: fixed, so it
  // isn't clipped by the toolbar's horizontal scroll.
  const r = document.getElementById('venv-btn').getBoundingClientRect();
  menu.style.top = (r.bottom + 5) + 'px';
  menu.style.right = (window.innerWidth - r.right) + 'px';
  menu.classList.add('on');
  menu.innerHTML = '<div class="venv-note">looking for environments…</div>';
  try {
    renderVenvMenu(await api('/api/venvs' + nbq()));
  } catch (err) {
    menu.innerHTML = `<div class="venv-note">${escapeHtml(errText(err))}</div>`;
  }
}

function renderVenvMenu(data) {
  const rows = (data.venvs || []).map(v => {
    const cls = ['venv-item'];
    if (v.python === data.current) cls.push('current');
    if (!v.ipykernel) cls.push('off');
    const note = v.ipykernel ? (v.origin || '') : 'no ipykernel';
    return `<div class="${cls.join(' ')}" onclick="setVenv('${escapeAttr(v.python)}')"
                 title="${escapeAttr(v.python)}">
      <span class="vl">${escapeHtml(v.label)}</span>
      <span class="vv">${escapeHtml(v.version || '?')}</span>
      <span class="vo">${escapeHtml(note)}</span>
    </div>`;
  }).join('');

  document.getElementById('venv-menu').innerHTML = rows +
    '<div class="venv-sep"></div>' +
    `<div class="venv-item" onclick="browseVenv()">
       <span class="vl">Browse…</span>
       <span class="vo">type a venv or python path</span>
     </div>` +
    '<div class="venv-note">picking one restarts this notebook\'s kernel</div>' +
    // The kernel menu is also where the less-used run/clear actions live, now
    // that the toolbar is down to the essentials.
    '<div class="venv-sep"></div>' +
    `<div class="venv-item" onclick="closeVenvMenu(); runAll()">
       <span class="vl">Run all cells</span>
     </div>
     <div class="venv-item" onclick="closeVenvMenu(); clearOutputs()">
       <span class="vl">Clear all outputs</span>
     </div>`;
}

// ---------- Directory picker for venv selection ----------
let dirPickResolve = null;
let dirPickSelected = null;

async function browseVenv() {
  const start = (activeTab() || {}).python
    ? require_path_parent((activeTab() || {}).python)
    : null;
  // Fall back to the active notebook's directory, then the browsed directory —
  // both are already accessible so we avoid triggering macOS TCC on a parent.
  const fallback = active ? active.split('/').slice(0, -1).join('/') : fileState.path;
  const chosen = await openDirPicker(start || fallback);
  if (chosen) setVenv(chosen);
}

function require_path_parent(p) {
  // Given a python binary path, return the grandparent (the venv root's parent)
  // as a sensible starting point for browsing.
  try { return p.split('/').slice(0, -3).join('/') || null; } catch (_) { return null; }
}

function openDirPicker(startPath) {
  document.getElementById('dirpick-back').classList.add('on');
  dirPickSelected = null;
  document.getElementById('dirpick-ok').disabled = true;
  document.getElementById('dirpick-sel').textContent = '';
  dirPickNav(startPath || (fileState.home || '/'));
  return new Promise(resolve => { dirPickResolve = resolve; });
}

async function dirPickNav(path) {
  document.getElementById('dirpick-list').innerHTML =
    '<div class="files-msg">Loading…</div>';
  let data;
  try {
    // Reuse the existing file browser API — it already has the right OS permissions.
    data = await api('/api/files?' + new URLSearchParams({path, hidden: '1'}));
  } catch (err) {
    document.getElementById('dirpick-list').innerHTML =
      `<div class="files-msg err">${escapeHtml(String(err))}</div>`;
    return;
  }

  // Crumbs
  const parts = data.path.split('/').filter(Boolean);
  const full = parts.map((p, i) => '/' + parts.slice(0, i + 1).join('/'));
  let crumbs = `<span class="crumb" onclick="dirPickNav('/')">/</span>`;
  parts.forEach((p, i) => {
    crumbs += `<span class="crumb-sep">/</span>
      <span class="crumb" onclick="dirPickNav('${escapeAttr(full[i])}')">${escapeHtml(p)}</span>`;
  });
  document.getElementById('dirpick-crumbs').innerHTML = crumbs;

  // Only show dirs and .venv entries; detect venvs by pyvenv.cfg presence
  const dirs = (data.entries || []).filter(e => e.kind === 'dir');

  let html = '';
  if (data.parent) {
    html += `<div class="dpick-row" onclick="dirPickNav('${escapeAttr(data.parent)}')">
      <span class="dp-ic">↑</span><span class="dp-nm">..</span></div>`;
  }
  if (!dirs.length && !data.parent) {
    html += '<div class="files-msg">No subdirectories</div>';
  }
  dirs.forEach(e => {
    const isVenv = e.name === '.venv' || e.name.endsWith('.venv');
    const python = isVenv ? e.path + '/bin/python' : null;
    html += `<div class="dpick-row${isVenv ? ' dp-venv' : ''}"
      onclick="${isVenv
        ? `dirPickSelect('${escapeAttr(python)}','${escapeAttr(e.path)}')`
        : `dirPickNav('${escapeAttr(e.path)}')`}">
      <span class="dp-ic">${isVenv ? '🐍' : '▸'}</span>
      <span class="dp-nm">${escapeHtml(e.name)}${isVenv ? '' : '/'}</span>
      ${isVenv ? '<span class="dp-tag">venv</span>' : ''}
    </div>`;
  });
  document.getElementById('dirpick-list').innerHTML = html;
}

function dirPickSelect(python, label) {
  dirPickSelected = python;
  document.getElementById('dirpick-sel').textContent = 'Selected: ' + label;
  document.getElementById('dirpick-ok').disabled = false;
}

function dirPickManual(val) {
  const v = val.trim();
  if (v) {
    dirPickSelected = v;
    document.getElementById('dirpick-sel').textContent = 'Selected: ' + v;
    document.getElementById('dirpick-ok').disabled = false;
  } else {
    if (!document.querySelector('.dpick-row.dp-venv.active')) {
      dirPickSelected = null;
      document.getElementById('dirpick-ok').disabled = true;
    }
  }
}

function dirPickOk() {
  document.getElementById('dirpick-back').classList.remove('on');
  document.getElementById('dirpick-manual').value = '';
  const r = dirPickResolve; dirPickResolve = null;
  if (r) r(dirPickSelected);
}

function dirPickCancel() {
  document.getElementById('dirpick-back').classList.remove('on');
  const r = dirPickResolve; dirPickResolve = null;
  if (r) r(null);
}

async function setVenv(python) {
  closeVenvMenu();
  const t = activeTab();
  if (!t) return;
  setKernelStatus('starting');
  try {
    const info = await api('/api/venv' + nbq(),
                           {method: 'POST', body: JSON.stringify({python})});
    t.python = info.python;
    refreshVenvBtn();
    flash(`${t.name} → ${venvLabel(info.python)} (Python ${info.version})`);
  } catch (err) {
    flash(errText(err));
    setKernelStatus(t.status || 'stopped');
  }
}

/** Brief bottom-of-pane notice — less jarring than alert() mid-flow. */
let flashTimer = null;
function flash(text) {
  let el = document.getElementById('flash');
  if (!el) {
    el = document.createElement('div');
    el.id = 'flash';
    el.className = 'flash';
    document.body.appendChild(el);
  }
  el.textContent = text;
  el.classList.add('on');
  clearTimeout(flashTimer);
  flashTimer = setTimeout(() => el.classList.remove('on'), 4000);
}

document.addEventListener('click', (e) => {
  if (!e.target.closest('.venv-pick')) closeVenvMenu();
});


// Strip ANSI escapes from tracebacks.
function stripAnsi(s) {
  return String(s).replace(/\x1b\[[0-9;]*[a-zA-Z]/g, '');
}

const MAX_STREAM_LINES = 400;

const ANSI_CLASS = {
  1: 'a-bold', 2: 'a-dim', 31: 'a-red', 32: 'a-green', 33: 'a-yellow',
  34: 'a-blue', 35: 'a-magenta', 36: 'a-cyan', 90: 'a-dim', 91: 'a-red',
  92: 'a-green', 93: 'a-yellow', 94: 'a-blue', 95: 'a-magenta', 96: 'a-cyan',
};

/**
 * Render console text the way a terminal would: honour \r (carriage return)
 * so progress bars collapse to their final frame, and map SGR colour codes to
 * spans. Everything else (cursor moves, \x1b[K, bracketed paste) is dropped.
 */
function ansiToHtml(text) {
  // Collapse each line's \r frames to the last one — pip's progress bar
  // redraws with \r, so this turns hundreds of frames into one.
  const lines = String(text).split('\n').map(line => {
    if (line.indexOf('\r') === -1) return line;
    const frames = line.split('\r');
    // Later frames overwrite earlier ones from column 0.
    let out = '';
    for (const f of frames) {
      out = f.length >= out.length ? f : f + out.slice(f.length);
    }
    return out;
  });

  // Very long logs (pip, training loops) get an elided middle — the box only
  // shows a few screens anyway, and rendering 10k lines per event is slow.
  if (lines.length > MAX_STREAM_LINES) {
    const dropped = lines.length - MAX_STREAM_LINES;
    lines.splice(40, dropped, `\x1b[2m… ${dropped} lines hidden …\x1b[0m`);
  }

  let html = '';
  const open = [];
  for (const line of lines) {
    let i = 0;
    while (i < line.length) {
      const esc = line.indexOf('\x1b[', i);
      if (esc === -1) { html += escapeHtml(line.slice(i)); break; }
      html += escapeHtml(line.slice(i, esc));
      const m = /^\x1b\[([0-9;?]*)([a-zA-Z])/.exec(line.slice(esc));
      if (!m) { i = esc + 2; continue; }
      if (m[2] === 'm') {
        for (const raw of (m[1] || '0').split(';')) {
          const code = parseInt(raw || '0', 10);
          if (code === 0) {
            while (open.length) { html += '</span>'; open.pop(); }
          } else if (ANSI_CLASS[code]) {
            html += `<span class="${ANSI_CLASS[code]}">`;
            open.push(code);
          }
        }
      }
      i = esc + m[0].length;
    }
    html += '\n';
  }
  while (open.length) { html += '</span>'; open.pop(); }
  return html.replace(/\n+$/, '\n');
}

/** Keep terminal boxes scrolled to the newest line, like a real terminal. */
function pinStreams(root) {
  for (const box of root.querySelectorAll('.output.stream')) {
    box.scrollTop = box.scrollHeight;
  }
}

function renderOutputs(outputs, cellId) {
  if (!outputs || !outputs.length) return '';
  let html = '<div class="outputs">';
  for (const o of outputs) {
    const t = o.output_type;
    if (t === 'stream') {
      // Terminal-style box: capped height, ANSI colours, \r frames collapsed.
      const cls = o.name === 'stderr' ? 'output stream stderr' : 'output stream';
      html += `<div class="${cls}">${ansiToHtml(o.text)}</div>`;
    } else if (t === 'error') {
      const tb = (o.traceback || []).join('\n');
      const body = tb ? stripAnsi(tb) : `${o.ename}: ${o.evalue}`;
      html += `<div class="output error">${escapeHtml(body)}`;
      if (cellId) {
        html += `<button class="help-btn" onclick="event.stopPropagation();getHelp('${cellId}')"
                  title="Ask the model what went wrong">Help</button>`;
      }
      html += `</div>`;
    } else if (t === 'execute_result' || t === 'display_data') {
      const d = o.data || {};
      if (d['image/png']) {
        html += `<div class="output"><img src="data:image/png;base64,${d['image/png']}"></div>`;
      } else if (d['image/jpeg']) {
        html += `<div class="output"><img src="data:image/jpeg;base64,${d['image/jpeg']}"></div>`;
      } else if (d['image/svg+xml']) {
        html += `<div class="output">${DOMPurify.sanitize(d['image/svg+xml'])}</div>`;
      } else if (d['text/html']) {
        html += `<div class="output-html">${DOMPurify.sanitize(d['text/html'])}</div>`;
      } else if (d['text/markdown']) {
        html += `<div class="output-html">${DOMPurify.sanitize(marked.parse(d['text/markdown']))}</div>`;
      } else if (d['text/plain']) {
        html += `<div class="output result">${escapeHtml(d['text/plain'])}</div>`;
      }
    }
  }
  return html + '</div>';
}

/* How many lines a folded cell shows, as a max-height for the clip. In em so it
 * tracks the editor's line-height rather than a pixel count that breaks when the
 * font size changes; the extra covers the editor's vertical padding. */
function foldHeight() { return (CODE_FOLD_LINES * 1.55 + 1.4).toFixed(2) + 'em'; }

function cellLines(c) { return (c.source || '').split('\n').length; }
function isFoldable(c) { return c.cell_type === 'code' && cellLines(c) > CODE_FOLD_LINES; }

/** Is the caret inside this cell's editor right now? */
function hasCaret(id) {
  const a = document.activeElement;
  const cell = a && a.closest ? a.closest('.cell') : null;
  return !!cell && cell.dataset.id === id;
}

/**
 * Whether a long cell should be drawn clipped.
 *
 * The caret check is what keeps this from being infuriating: a cell grows past
 * CODE_FOLD_LINES *while you are typing in it*, and the next re-render — a run
 * finishing elsewhere, a kernel event — would otherwise fold the code out from
 * under you with your caret in the hidden part.
 */
function isFolded(c) {
  return isFoldable(c) && !codeOpen.has(c.id) && !hasCaret(c.id);
}

/** The click target over clipped code, with the count of what's behind it. */
function veilHtml(c) {
  const hidden = cellLines(c) - CODE_FOLD_LINES;
  return `<div class="fold-veil" onclick="event.stopPropagation();toggleFold('${c.id}')"
      title="Show the whole cell"><span>⌄ ${hidden} more line${
        hidden > 1 ? 's' : ''}</span></div>`;
}

/**
 * The gutter's fold and output toggles. Its own function because a cell that had
 * no output when it was rendered grows one when it runs, and the ▾ has to appear
 * without re-rendering the notebook — see refreshViewBtns().
 *
 * Both are shown only when there's something to hide: a short cell has no fold
 * state worth a button, and a cell that hasn't run has no output, so a ▾ on it
 * would toggle nothing.
 */
function viewBtnsHtml(c) {
  const hasOuts = !!(c.outputs && c.outputs.length);
  const long = isFoldable(c);
  if (!long && !hasOuts) return '';
  const hidden = outsHidden.has(c.id);
  // What's on screen when there's something on screen to ask, since the caret
  // exemption in isFolded() means the set alone doesn't decide it. Only the
  // initial render has no wrapper to ask.
  const wrap = document.getElementById('fold-' + c.id);
  const folded = wrap ? wrap.classList.contains('folded') : isFolded(c);
  return `<div class="gutter-out">${long ? `
        <button class="out-btn" id="foldb-${c.id}"
                title="${folded ? 'Show the whole cell' : "Fold this cell's code"}"
                onclick="event.stopPropagation();toggleFold('${c.id}')">${
                  folded ? '⌄' : '⌃'}</button>` : ''}${hasOuts ? `
        <button class="out-btn ${hidden ? 'off' : ''}" id="outb-${c.id}"
                title="${hidden ? 'Show' : 'Hide'} this cell's output"
                onclick="event.stopPropagation();toggleOutput('${c.id}')">${
                  hidden ? '▸' : '▾'}</button>` : ''}
      </div>`;
}

/** The "N outputs hidden" stand-in shown in place of collapsed output. */
function outNoteHtml(c) {
  const n = (c.outputs || []).length;
  return `<div class="out-note" onclick="event.stopPropagation();toggleOutput('${c.id}')"
      title="Show this cell's output">▸ ${n} output${n > 1 ? 's' : ''} hidden</div>`;
}

function cellHtml(c) {
  const isMd = c.cell_type === 'markdown';
  const isCode = c.cell_type === 'code';
  const isAi = c.cell_type === 'ai';
  const isEditing = editing.has(c.id) || !c.source.trim();
  // Code cells show [n]; markdown/raw/ai show a marker instead.
  const label = isCode
    ? `[${c.execution_count == null ? ' ' : c.execution_count}]`
    : (isAi ? 'AI' : '');

  let bodyInner = '';
  let hdBtn = '';
  if (isMd && !isEditing) {
    const level = headingLevel(c);
    const idx = cells.findIndex(x => x.id === c.id);
    const hasSection = level > 0 && sectionCells(idx).length > 0;
    const collapsed = headingsCollapsed.has(c.id);
    if (hasSection) {
      hdBtn = `<button class="hd-toggle" data-id="${c.id}"
           onclick="event.stopPropagation();toggleHeading('${c.id}')"
           title="${collapsed ? 'Expand section' : 'Collapse section'}"
           >${collapsed ? '▸' : '▾'}</button>`;
    }
    bodyInner = `<div class="md-rendered" ondblclick="editMarkdown('${c.id}')">${
      DOMPurify.sanitize(marked.parse(c.source || ''))}</div>`;
  } else {
    const placeholder = isAi
      ? 'Describe what you want in plain English, then press ⇧⏎'
      : '';
    // A host for CodeMirror, with the textarea inside it as the fallback. The
    // textarea is what's in the DOM until mountEditor() replaces it, so a cell
    // is editable from the first paint and stays editable if CM never loads.
    bodyInner = `<div class="ed-host" id="ed-host-${c.id}" data-cell="${c.id}"
      data-lang="${isCode || isAi ? 'python' : 'text'}"><textarea class="editor" id="ed-${c.id}"
      oninput="autosize(this); queueSave('${c.id}')"
      onfocus="selectCell('${c.id}')"
      onkeydown="return onEditorKey(event, '${c.id}')"
      placeholder="${escapeAttr(placeholder)}"
      spellcheck="false">${escapeHtml(c.source || '')}</textarea></div>`;

    // A long code cell is clipped to about a screenful, with the rest behind a
    // fade you click to open. Code only: markdown renders to prose and an AI cell
    // is a sentence you're writing, so neither gets long enough to be worth
    // hiding, and clipping a prompt you're halfway through typing would be
    // actively wrong.
    if (isFoldable(c)) {
      const folded = isFolded(c);
      // The wrapper is there either way, so folding again is a class toggle on a
      // node already in the DOM rather than a re-render.
      bodyInner = `<div class="fold ${folded ? 'folded' : ''}" id="fold-${c.id}"
        style="--fold-h:${foldHeight()}">${bodyInner}${folded ? veilHtml(c) : ''}</div>`;
    }
  }

  // An AI cell gets a Generate button; a cell that came from one keeps its
  // prompt on a strip above the code, with a re-run.
  if (isAi) {
    bodyInner += `
      <div class="ai-bar">
        <button class="ai-go" id="aigo-${c.id}" onclick="event.stopPropagation();generateCell('${c.id}')">
          Generate</button>
        <span class="ai-hint">⇧⏎ generates · becomes a code cell</span>
        <span class="ai-model" id="aimodel-${c.id}"></span>
      </div>`;
  }
  const promptStrip = (!isAi && c.prompt) ? `
    <div class="ai-prompt" title="${escapeAttr(c.prompt)}">
      <span class="tag">AI</span>
      <span class="pt">${escapeHtml(c.prompt)}</span>
      <span class="re" onclick="event.stopPropagation();regenerate('${c.id}')"
            title="Ask again with the same prompt">↻</span>
    </div>` : '';

  // What the user asked Claude, on a cell a terminal rewrote. Same shape as the
  // AI strip above, with no ↻: an inline-LLM prompt is a single self-contained
  // request the app can send again, while this one was one turn of a conversation
  // in a terminal — replaying it out of that context would mean something else.
  // The ↶ Undo replace strip below is what walks the write back.
  const claudeStrip = c.claude_prompt ? `
    <div class="ai-prompt claude" title="${escapeAttr(c.claude_prompt)}">
      <span class="tag">✳</span>
      <span class="pt">${escapeHtml(c.claude_prompt)}</span>
    </div>` : '';

  // Only on a cell whose source was replaced by something the user didn't type
  // — an agent or a snippet. Per cell, like JupyterLab: undoing here says
  // nothing about the cell below.
  const undoStrip = c.undo_depth ? `
    <div class="undo-bar">
      <button class="undo-go" onclick="event.stopPropagation();undoCell('${c.id}')"
              title="Restore the source this replaced">↶ Undo replace</button>
      <span class="undo-n">${c.undo_depth} step${c.undo_depth > 1 ? 's' : ''}</span>
    </div>` : '';

  // The hist div still exists (refreshHist writes into it) but is hidden —
  // ⌘Z/⇧⌘Z work fine and the buttons add visual noise without adding value.
  const histBtns = (isMd && !isEditing) || !window.CM ? '' : `
      <div class="gutter-hist" id="hist-${c.id}" style="display:none">
        <button class="hist-btn" onclick="event.stopPropagation();cellUndo('${c.id}')" disabled>↶</button>
        <button class="hist-btn" onclick="event.stopPropagation();cellRedo('${c.id}')" disabled>↷</button>
      </div>`;

  const hasOuts = !!(c.outputs && c.outputs.length);
  const outHidden = outsHidden.has(c.id);
  const viewBtns = viewBtnsHtml(c);

  // Delete this cell, add a code cell after it, and change what it is. Under the
  // number because that's the part of the row that belongs to the cell rather
  // than to its content. The new one is always **code** — a markdown cell is a
  // note about code you're about to write, so code is what you want next.
  const cellBtns = `
      <div class="gutter-acts">
        <div class="act-row">
          <button class="act-btn" title="Move cell up (⌘⇧↑)"
                  onclick="event.stopPropagation();moveCell('${c.id}',-1)">↑</button>
          <button class="act-btn" title="Move cell down (⌘⇧↓)"
                  onclick="event.stopPropagation();moveCell('${c.id}',1)">↓</button>
        </div>
        <div class="act-row">
          <button class="act-btn" title="Change this cell's type"
                  onclick="toggleTypeMenu(event, '${c.id}')">⌥</button>
          <button class="act-btn" title="Add a code cell below"
                  onclick="event.stopPropagation();addCell('code', '${c.id}')">+</button>
        </div>
        <div class="act-row">
          <button class="act-btn danger" title="Delete this cell"
                  onclick="event.stopPropagation();deleteCell('${c.id}')">✕</button>
        </div>
      </div>`;

  // What replaces the output when it's collapsed — the count, so the cell still
  // says it produced something. Hiding output is a view preference; leaving no
  // trace of it would make a collapsed cell look like one that never ran.
  const outNote = (hasOuts && outHidden) ? outNoteHtml(c) : '';

  return `
  <div class="cell" data-id="${c.id}" data-type="${c.cell_type}"
       onclick="selectCell('${c.id}')">
    <div class="gutter">
      ${hdBtn}<span class="gutter-label">${escapeHtml(label)}</span>${viewBtns}${histBtns}${cellBtns}
    </div>
    <div class="cell-body">
      ${promptStrip}${claudeStrip}${undoStrip}${bodyInner}${outNote}
      <div id="out-${c.id}" class="${outHidden ? 'out-off' : ''}">${
        renderOutputs(c.outputs, c.id)}</div>
      <div class="help-panel" id="help-${c.id}"></div>
    </div>
  </div>`;
}

// ---------- Heading collapse ----------

/** The heading level of a markdown cell (1-6), or 0 if it's not a heading. */
function headingLevel(c) {
  if (c.cell_type !== 'markdown') return 0;
  const m = /^(#{1,6})\s/.exec((c.source || '').trimStart());
  return m ? m[1].length : 0;
}

/**
 * All cell ids that belong to the section opened by the heading at `idx`.
 * A section ends at the next cell with a heading of equal or higher level
 * (lower number), or at the end of the notebook.
 */
function sectionCells(idx) {
  const level = headingLevel(cells[idx]);
  if (!level) return [];
  const ids = [];
  for (let i = idx + 1; i < cells.length; i++) {
    const l = headingLevel(cells[i]);
    if (l && l <= level) break;
    ids.push(cells[i].id);
  }
  return ids;
}

function toggleHeading(id) {
  const idx = cells.findIndex(c => c.id === id);
  if (idx === -1) return;
  const collapsed = headingsCollapsed.has(id);
  if (collapsed) headingsCollapsed.delete(id); else headingsCollapsed.add(id);
  applyHeadingCollapse();
  // Update just this button without a full render.
  const btn = document.querySelector(`.hd-toggle[data-id="${id}"]`);
  if (btn) btn.textContent = collapsed ? '▾' : '▸';
}

/** Show/hide cells according to headingsCollapsed, without re-rendering. */
function applyHeadingCollapse() {
  // Start with everything visible, then hide what collapsed sections cover.
  const hidden = new Set();
  cells.forEach((c, i) => {
    if (headingsCollapsed.has(c.id)) {
      sectionCells(i).forEach(id => hidden.add(id));
    }
  });
  cells.forEach(c => {
    const el = document.querySelector(`.cell[data-id="${c.id}"]`);
    if (el) el.style.display = hidden.has(c.id) ? 'none' : '';
  });
}

function render() {
  const scroll = document.getElementById('notebook-pane').scrollTop;
  document.getElementById('notebook').innerHTML = cells.map(cellHtml).join('');
  mountEditors();
  paintSelection();      // innerHTML was replaced, so the class went with it
  document.querySelectorAll('.editor').forEach(autosize);
  pinStreams(document.getElementById('notebook'));
  applyHeadingCollapse();
  document.getElementById('notebook-pane').scrollTop = scroll;
}

function autosize(el) {
  // A textarea only. CM grows with its content already, and pinning a height on
  // its host would clip the editor at whatever it measured once.
  if (!el || el.tagName !== 'TEXTAREA') return;
  el.style.height = 'auto';
  el.style.height = (el.scrollHeight + 2) + 'px';
}
