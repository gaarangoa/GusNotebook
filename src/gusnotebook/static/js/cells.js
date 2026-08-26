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
  if (data.kind === 'view-state') {
    if (data.view && typeof data.view.y === 'number') t.markupView = data.view;
    return;
  }
  if (data.kind === 'ready') {
    if (t.markupView) {
      frame.contentWindow.postMessage({
        channel: MARKUP_EDITOR_CHANNEL, nonce: t.previewNonce,
        command: 'restore-view', view: t.markupView,
      }, t.previewOrigin);
    }
    return;
  }
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
    if (active !== t.path) return;
    // A restarted GusNotebook process gives each document a new random preview
    // port. Its generation starts at "1" again, so the generation alone cannot
    // distinguish that live server from the dead origin retained by this page.
    // Re-render the current browser buffer on the new origin; this is safe even
    // while dirty because no disk content is read or discarded here.
    if (Object.prototype.hasOwnProperty.call(data, 'preview_origin') &&
        data.preview_origin !== t.previewOrigin) {
      await renderMarkupEditor();
      if (active === t.path) flash(`${t.name} preview reconnected`);
      return;
    }
    if (data.disk_version === t.diskVersion &&
        data.preview_version === t.previewVersion) return;
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
  pathEl.dataset.path = t ? t.path : '';
  pathEl.dataset.name = t ? t.name : '';
  pathEl.contentEditable = kind === 'notebook' ? 'true' : 'false';
  pathEl.classList.toggle('editable', kind === 'notebook');
  const nbReload = document.getElementById('nb-reload');
  if (nbReload) nbReload.style.display = kind === 'notebook' ? '' : 'none';

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

function notebookNameKey(event) {
  const t = activeTab();
  if (!t || t.kind !== 'notebook') return;
  if (event.key === 'Enter') {
    event.preventDefault();
    event.currentTarget.blur();
  } else if (event.key === 'Escape') {
    event.preventDefault();
    event.currentTarget.textContent = event.currentTarget.dataset.name || t.name;
    event.currentTarget.blur();
  }
}

async function commitNotebookNameEdit() {
  const el = document.getElementById('nb-path');
  const t = activeTab();
  if (!el || !t || t.kind !== 'notebook') return;
  const oldPath = t.path;
  const oldName = t.name;
  let name = el.textContent.trim();
  if (!name || name === oldName) {
    el.textContent = oldName;
    return;
  }
  if (!name.endsWith('.ipynb')) name += '.ipynb';
  if (name.includes('/') || name.includes('\\')) {
    flash('Notebook name must not contain slashes');
    el.textContent = oldName;
    return;
  }
  try {
    await Promise.all([...unsaved].map(id => saveCell(id)));
    const data = await api('/api/files/rename', {method: 'POST',
      body: JSON.stringify({path: oldPath, name})});
    const newPath = data.path;
    const oldView = notebookViewState.get(oldPath);
    if (oldView) {
      notebookViewState.delete(oldPath);
      notebookViewState.set(newPath, oldView);
      try {
        sessionStorage.removeItem('gusnotebook:view:' + oldPath);
        sessionStorage.setItem('gusnotebook:view:' + newPath, JSON.stringify({
          codeOpen: [...oldView.codeOpen],
          outsHidden: [...oldView.outsHidden],
          headingsCollapsed: [...oldView.headingsCollapsed],
        }));
      } catch (e) {}
    }
    t.path = newPath;
    t.name = name;
    active = newPath;
    rememberActive(newPath);
    renderTabs();
    showActive();
    if (fileState.path) browse(fileState.path);
    loadSessions();
    flash(`Renamed notebook to ${name}`);
  } catch (err) {
    el.textContent = oldName;
    flash('Rename failed: ' + errText(err));
  }
}

/** Open any file in a tab (or focus the tab that already has it).
 *
 * `restore` reads a tab already recorded in the workspace without recording it
 * again. `cached` carries only this window's view state while the authoritative
 * document content still comes from the server. */
async function openFile(path, options = {}) {
  if (tab(path)) { if (!options.quiet) switchTab(path); return tab(path); }
  let data;
  try {
    data = await api('/api/open', {method: 'POST',
      body: JSON.stringify({path, restore: !!options.restore})});
  } catch (err) {
    flash('Cannot open: ' + errText(err));
    return;
  }
  const p = data.path || path;
  if (tab(p)) {
    if (!options.quiet) switchTab(p);
    return tab(p);
  }                                         // server normalized to an open tab

  const cached = options.cached;
  let t = {path: p, name: p.split('/').pop(), kind: data.kind || 'text', dirty: false};
  if (t.kind === 'notebook') {
    const oldCells = new Map((cached && cached.cells || []).map(c => [c.id, c]));
    const running = data.running_cells || {};
    const freshCells = (data.cells || []).map(c => {
      const old = oldCells.get(c.id);
      // A server snapshot taken in the middle of execution can lag the SSE
      // stream. Keep the live output this window already saw only if the server
      // still says that cell is running.
      return old && old._running && Object.prototype.hasOwnProperty.call(running, c.id)
        ? {...c, outputs: old.outputs, _running: true}
        : {...c, _running: Object.prototype.hasOwnProperty.call(running, c.id)};
    });
    Object.assign(t, {cells: freshCells,
                      selected: cached && cached.selected,
                      editing: cached && cached.editing || new Set(),
                      codeOpen: cached && cached.codeOpen || new Set(),
                      outsHidden: cached && cached.outsHidden || new Set(),
                      headingsCollapsed: cached && cached.headingsCollapsed || new Set(),
                      scroll: cached && cached.scroll || 0,
                      python: data.kernel_python || data.python,
                      status: data.kernel_status || 'stopped'});
    restoreNotebookView(t);
  } else if (t.kind === 'image') {
    t.url = data.url;
  } else {
    if (cached && (cached.dirty || cached.saveInFlight)) {
      // Keep an unsaved buffer and its optimistic disk revision. If disk changed
      // while away, polling/save will surface the conflict instead of replacing
      // the user's text.
      t = cached;
      Object.assign(t, {path: p, name: p.split('/').pop(), kind: data.kind || 'text',
                        language: data.language,
                        previewOrigin: data.preview_origin,
                        previewVersion: data.preview_version});
    } else {
      Object.assign(t, {text: data.text || '', language: data.language,
                        previewOrigin: data.preview_origin,
                        previewVersion: data.preview_version,
                        diskVersion: data.disk_version,
                        markupView: cached && cached.markupView});
    }
  }

  if (!options.quiet) stashActive();
  tabs.push(t);
  if (options.quiet) return t;
  active = p;
  if (t.kind === 'notebook') {
    restoreNotebookView(t);
    cells = t.cells; selected = null; editing = t.editing;
    codeOpen = t.codeOpen; outsHidden = t.outsHidden;
    headingsCollapsed = t.headingsCollapsed || new Set();
  }
  renderTabs();
  showActive();
  if (fileState.path) browse(fileState.path);   // re-mark which rows are open
  loadSessions();          // the tab joined this session; update its count
  return t;
}

const activeTimers = new Map();

function rememberActive(path) {
  if (!currentSession) return;
  const sid = currentSession;
  clearTimeout(activeTimers.get(sid));
  activeTimers.set(sid, setTimeout(() => {
    activeTimers.delete(sid);
    api('/api/sessions/' + encodeURIComponent(sid), {method: 'POST',
      body: JSON.stringify({active: path || null})}).catch(() => {});
  }, 100));
}

function switchTab(path, remember = true) {
  if (path === active) return;
  const t = tab(path);
  if (!t) return;
  stashActive();
  active = path;
  if (t.kind === 'notebook') {
    restoreNotebookView(t);
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
  if (remember) rememberActive(path);
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
      restoreNotebookView(next);
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
    rememberActive(active);
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
  if (t.saveInFlight) {
    // Double-clicking Save used to race two writes carrying the same disk
    // revision: the first succeeded and the second looked like an external
    // edit. Keep only the newest requested buffer and write it after this one
    // if the document is still dirty.
    t.queuedSaveText = text;
    return;
  }
  const revision = t.editRevision || 0;
  const st = document.getElementById('text-status');
  if (active === t.path) st.textContent = 'saving…';
  t.saveInFlight = true;
  const controller = new AbortController();
  const requestTimer = setTimeout(() => controller.abort(), 30000);
  try {
    const saved = await api('/api/text', {method: 'POST',
      body: JSON.stringify({path: t.path, text, disk_version: t.diskVersion}),
      signal: controller.signal});
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
    clearTimeout(requestTimer);
    t.saveInFlight = false;
    const queued = t.queuedSaveText;
    delete t.queuedSaveText;
    if (typeof queued === 'string' && t.dirty && !t.externalConflict) {
      persistText(t, typeof t.text === 'string' ? t.text : queued);
    }
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
  // A malformed document can prevent the bridge from starting. Saving the last
  // markup received is still better than leaving the toolbar stuck on saving.
  clearTimeout(t.markupSaveTimer);
  t.markupSaveTimer = setTimeout(() => {
    t.markupSaveTimer = null;
    persistText(t, t.text || '');
  }, 750);
  if (!frame || !frame.contentWindow) return;
  try {
    frame.contentWindow.postMessage(
      {channel: MARKUP_EDITOR_CHANNEL, nonce: t.previewNonce, command: 'save'},
      t.previewOrigin || '*');
  } catch (err) {
    clearTimeout(t.markupSaveTimer);
    t.markupSaveTimer = null;
    if (active === t.path) document.getElementById('text-status').textContent = 'save failed';
    flash('Save failed: ' + errText(err));
  }
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
    data = await api('/api/dirlist?' + new URLSearchParams({path}));
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

  // The server inspects pyvenv.cfg/conda-meta and supplies the interpreter.
  // Names are presentation only: MusicAI is as valid as .venv.
  const dirs = data.entries || [];

  let html = '';
  if (data.parent) {
    html += `<div class="dpick-row" onclick="dirPickNav('${escapeAttr(data.parent)}')">
      <span class="dp-ic">↑</span><span class="dp-nm">..</span></div>`;
  }
  if (!dirs.length && !data.parent) {
    html += '<div class="files-msg">No subdirectories</div>';
  }
  dirs.forEach(e => {
    const isVenv = !!e.is_venv;
    const python = e.python;
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

/* A nested scroll box is useful for a long log, but browsers do not reliably
 * chain a wheel gesture from it to our overflowed notebook pane (especially
 * with the app itself fixed to the viewport). At the stream's boundary, move
 * that same vertical delta into the notebook. Inside the boundary, native
 * scrolling remains untouched. */
function handoffOutputScroll(event) {
  const target = event.target;
  const stream = target && target.closest
    ? target.closest('.output.stream') : null;
  if (!stream || !event.deltaY) return;
  const atTop = stream.scrollTop <= 0;
  const atBottom = stream.scrollTop + stream.clientHeight >= stream.scrollHeight - 1;
  if ((event.deltaY < 0 && !atTop) || (event.deltaY > 0 && !atBottom)) return;

  const pane = document.getElementById('notebook-pane');
  if (!pane) return;
  const scale = event.deltaMode === WheelEvent.DOM_DELTA_LINE ? 16
    : event.deltaMode === WheelEvent.DOM_DELTA_PAGE ? pane.clientHeight : 1;
  event.preventDefault();
  pane.scrollTop += event.deltaY * scale;
}
// Capture so editor widgets inside an output cannot stop the event before it
// reaches the notebook handoff.
document.addEventListener('wheel', handoffOutputScroll,
                          {passive: false, capture: true});

function richMimeText(value) {
  return Array.isArray(value) ? value.join('\n') : String(value == null ? '' : value);
}

function htmlOutputSrcdoc(body, outputId) {
  const safeId = JSON.stringify(outputId);
  const d3Patch = `<script data-gusnb-d3-static>
(() => {
  const nativeSetTimeout = window.setTimeout.bind(window);
  const patch = d3 => {
    if (!d3 || d3.__gusnbStaticTransitions || !d3.selection) return d3;
    d3.__gusnbStaticTransitions = true;
    const proto = d3.selection.prototype;
    proto.transition = function () { return this; };
    proto.duration = function () { return this; };
    proto.delay = function () { return this; };
    proto.ease = function () { return this; };
    proto.interrupt = function () { return this; };
    d3.transition = function () { return d3.selection(); };
    d3.timer = function (callback) {
      if (typeof callback === 'function') nativeSetTimeout(() => callback(Infinity), 0);
      return {restart() {}, stop() {}};
    };
    d3.timeout = function (callback) {
      if (typeof callback === 'function') nativeSetTimeout(() => callback(Infinity), 0);
      return {restart() {}, stop() {}};
    };
    d3.interval = function (callback) {
      if (typeof callback === 'function') nativeSetTimeout(() => callback(Infinity), 0);
      return {restart() {}, stop() {}};
    };
    return d3;
  };
  let current;
  try {
    Object.defineProperty(window, 'd3', {
      configurable: true,
      get: () => current,
      set: value => { current = patch(value); }
    });
  } catch (err) {
    if (window.d3) patch(window.d3);
  }
})();
</script>`;
  const resize = `<script>
(() => {
  let textEdit = false;
  let editor = null;
  let draggingViz = false;
  const measuredHeight = () => {
    const root = document.body || document.documentElement;
    if (!root) return 24;
    const selectors = [
      '.viz-root', '[data-gusnb-viz-root]', 'svg', 'canvas', 'img', 'video', 'table'
    ];
    const targets = Array.from(root.querySelectorAll(selectors.join(','))).filter(el => {
      const rect = el.getBoundingClientRect();
      const style = getComputedStyle(el);
      return rect.width > 0 && rect.height > 0 && style.display !== 'none' &&
        style.visibility !== 'hidden';
    });
    const nodes = targets.length ? targets : Array.from(root.children).filter(el => {
      if (el.matches('script,style,link,base')) return false;
      const rect = el.getBoundingClientRect();
      return rect.width > 0 && rect.height > 0;
    });
    if (nodes.length) {
      let top = Infinity;
      let bottom = -Infinity;
      nodes.forEach(el => {
        const rect = el.getBoundingClientRect();
        top = Math.min(top, rect.top);
        bottom = Math.max(bottom, rect.bottom);
      });
      if (Number.isFinite(top) && Number.isFinite(bottom)) {
        return Math.ceil(bottom - Math.min(top, 0));
      }
    }
    return Math.max(
      root.scrollHeight || 0,
      document.documentElement ? document.documentElement.scrollHeight : 0,
      24
    );
  };
  const send = () => parent.postMessage({
    type: 'gusnotebook-html-output-height',
    id: ${safeId},
    height: measuredHeight()
  }, '*');
  addEventListener('load', send);
  addEventListener('resize', send);
  if (window.ResizeObserver) new ResizeObserver(send).observe(document.documentElement);
  setTimeout(send, 0);
  setTimeout(send, 250);
  const svgTextTarget = node => {
    while (node && node !== document) {
      if (node.namespaceURI === 'http://www.w3.org/2000/svg' &&
          (node.localName === 'text' || node.localName === 'tspan')) return node;
      node = node.parentNode;
    }
    return null;
  };
  const serialized = () => {
    const clone = document.documentElement.cloneNode(true);
    clone.querySelectorAll('script:not([data-gusnb-frame-runtime])').forEach(s => s.remove());
    return '<!doctype html>\\n' + clone.outerHTML;
  };
  const commit = cancel => {
    if (!editor) return;
    const edit = editor;
    editor = null;
    if (cancel) edit.target.textContent = edit.original;
    else edit.target.textContent = edit.input.value;
    edit.input.remove();
    parent.postMessage({
      type: 'gusnotebook-viz-srcdoc-updated',
      id: ${safeId},
      html: serialized()
    }, '*');
    setTimeout(send, 0);
  };
  const postVizPointer = (phase, event) => {
    parent.postMessage({
      type: 'gusnotebook-viz-pointer',
      phase,
      id: ${safeId},
      x: event.clientX,
      y: event.clientY
    }, '*');
  };
  const selectOutputCell = () => parent.postMessage({
    type: 'gusnotebook-output-interaction',
    id: ${safeId}
  }, '*');
  const openEditor = target => {
    commit(false);
    const rect = target.getBoundingClientRect();
    const style = getComputedStyle(target);
    const input = document.createElement('input');
    input.value = target.textContent || '';
    input.setAttribute('aria-label', 'Edit chart text');
    Object.assign(input.style, {
      position: 'fixed',
      zIndex: '2147483647',
      left: Math.max(4, rect.left) + 'px',
      top: Math.max(4, rect.top) + 'px',
      width: Math.max(120, rect.width + 48) + 'px',
      height: Math.max(26, rect.height + 10) + 'px',
      padding: '3px 6px',
      border: '2px solid #830051',
      borderRadius: '4px',
      background: '#fff',
      color: style.fill === 'none' ? '#0f172a' : style.fill,
      font: style.font,
      boxShadow: '0 3px 14px rgba(15,23,42,.24)'
    });
    document.body.appendChild(input);
    editor = {input, target, original: target.textContent || ''};
    input.addEventListener('input', () => { target.textContent = input.value; send(); });
    input.addEventListener('blur', () => commit(false));
    input.addEventListener('keydown', event => {
      if (event.key === 'Enter') { event.preventDefault(); commit(false); }
      if (event.key === 'Escape') { event.preventDefault(); commit(true); }
    });
    input.focus();
    input.select();
  };
  addEventListener('message', event => {
    const data = event.data || {};
    if (data.type === 'gusnotebook-viz-report-height') {
      send();
      return;
    }
    if (data.type !== 'gusnotebook-viz-text-edit') return;
    textEdit = !!data.enabled;
    document.body.style.cursor = textEdit ? 'text' : '';
    if (!textEdit) commit(false);
  });
  document.addEventListener('click', event => {
    selectOutputCell();
    const target = svgTextTarget(event.target);
    if (!target) return;
    event.preventDefault();
    event.stopPropagation();
    openEditor(target);
  }, true);
  document.addEventListener('pointerdown', event => {
    selectOutputCell();
    if (event.button !== 0 || editor || svgTextTarget(event.target)) return;
    draggingViz = true;
    if (event.target.setPointerCapture) {
      try { event.target.setPointerCapture(event.pointerId); } catch (_err) {}
    }
    postVizPointer('down', event);
    event.preventDefault();
    event.stopPropagation();
  }, true);
  document.addEventListener('pointermove', event => {
    if (!draggingViz) return;
    postVizPointer('move', event);
    event.preventDefault();
    event.stopPropagation();
  }, true);
  const endVizPointer = event => {
    if (!draggingViz) return;
    draggingViz = false;
    postVizPointer('up', event);
    event.preventDefault();
    event.stopPropagation();
  };
  document.addEventListener('pointerup', endVizPointer, true);
  document.addEventListener('pointercancel', endVizPointer, true);
})();
</script>`;
  return `<!doctype html><html><head><base target="_blank">
<style>
html,body{margin:0;padding:0;background:transparent;color:#1e293b;font:13px/1.45 -apple-system,BlinkMacSystemFont,"Inter",sans-serif;}
body{overflow-x:auto;overflow-y:hidden;}
*,*::before,*::after{animation:none!important;transition:none!important;}
img,svg,canvas,video{max-width:100%;}
table{border-collapse:collapse;font:11.5px/1.45 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;margin:3px 0;}
th,td{border:1px solid #e2e8f0;padding:4px 7px;text-align:right;vertical-align:top;white-space:nowrap;}
th{background:#f8fafc;color:#475569;font-weight:600;}
tbody th{text-align:left;background:#fff;color:#64748b;font-weight:500;}
tbody tr:hover td,tbody tr:hover th{background:#f8fafc;}
table[border]{border:none;}
</style>
</head><body>${d3Patch}${body}${resize.replace('<script>', '<script data-gusnb-frame-runtime>')}</body></html>`;
}

function htmlOutputFrame(source, outputId, plain) {
  const raw = plain ? `
    <button class="output-view-btn" onclick="event.stopPropagation();toggleHtmlOutputRaw('${outputId}')"
            title="Show raw text output">raw</button>
    <pre class="output-raw" id="${outputId}-raw">${escapeHtml(plain)}</pre>` : '';
  return `<div class="output-html-frame" id="${outputId}">
    <iframe sandbox="allow-scripts" referrerpolicy="no-referrer"
            data-output-frame="${outputId}"
            srcdoc="${escapeAttr(htmlOutputSrcdoc(source, outputId))}"></iframe>
    ${raw}
  </div>`;
}

function toggleHtmlOutputRaw(id) {
  const box = document.getElementById(id);
  if (!box) return;
  box.classList.toggle('show-raw');
  const btn = box.querySelector('.output-view-btn');
  if (btn) btn.textContent = box.classList.contains('show-raw') ? 'html' : 'raw';
}

window.addEventListener('message', (event) => {
  const data = event.data || {};
  if (data.type === 'gusnotebook-output-interaction') {
    const frame = document.querySelector(`iframe[data-output-frame="${CSS.escape(data.id || '')}"]`);
    const cell = frame && frame.closest('.cell');
    if (cell && cell.dataset.id) selectCell(cell.dataset.id);
    return;
  }
  if (data.type !== 'gusnotebook-html-output-height') return;
  const frame = document.querySelector(`iframe[data-output-frame="${CSS.escape(data.id || '')}"]`);
  const height = Math.max(24, Math.min(4000, Number(data.height) || 0));
  if (frame && height) frame.style.height = height + 'px';
});

function renderOutputs(outputs, cellId) {
  if (!outputs || !outputs.length) return '';
  let html = '<div class="outputs">';
  outputs.forEach((o, outputIndex) => {
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
      const outputId = `htmlout-${cellId || 'cell'}-${outputIndex}`;
      if (d['text/html']) {
        html += htmlOutputFrame(richMimeText(d['text/html']), outputId,
                                d['text/plain'] ? richMimeText(d['text/plain']) : '');
      } else if (d['image/png']) {
        html += `<div class="output"><img src="data:image/png;base64,${richMimeText(d['image/png']).replace(/\s+/g, '')}"></div>`;
      } else if (d['image/svg+xml']) {
        html += `<div class="output"><img src="data:image/svg+xml;charset=utf-8,${encodeURIComponent(richMimeText(d['image/svg+xml']))}"></div>`;
      } else if (d['image/jpeg']) {
        html += `<div class="output"><img src="data:image/jpeg;base64,${richMimeText(d['image/jpeg']).replace(/\s+/g, '')}"></div>`;
      } else if (d['text/markdown']) {
        html += `<div class="output-html">${DOMPurify.sanitize(marked.parse(richMimeText(d['text/markdown'])))}</div>`;
      } else if (d['text/plain']) {
        html += `<div class="output result">${escapeHtml(richMimeText(d['text/plain']))}</div>`;
      }
    }
  });
  return html + '</div>';
}

function mimeText(value) {
  return Array.isArray(value) ? value.join('\n') : String(value == null ? '' : value);
}

function htmlFromDisplaySource(source) {
  const text = String(source || '');
  const marker = 'HTML(';
  const start = text.indexOf(marker);
  if (start < 0) return '';
  let i = start + marker.length;
  while (/\s/.test(text[i] || '')) i++;
  if ((text[i] === 'r' || text[i] === 'R') && /['"]/.test(text[i + 1] || '')) i++;
  const quote = text.slice(i, i + 3);
  if (quote === "'''" || quote === '"""') {
    const end = text.indexOf(quote, i + 3);
    return end > i ? text.slice(i + 3, end) : '';
  }
  if (text[i] === "'" || text[i] === '"') {
    const q = text[i];
    let out = '';
    for (let j = i + 1; j < text.length; j++) {
      if (text[j] === '\\') {
        out += text[j] + (text[j + 1] || '');
        j++;
      } else if (text[j] === q) {
        return out;
      } else {
        out += text[j];
      }
    }
  }
  return '';
}

function visualOutput(c) {
  for (const o of (c && c.outputs) || []) {
    if (o.output_type !== 'execute_result' && o.output_type !== 'display_data') continue;
    const d = o.data || {};
    if (d['image/svg+xml']) {
      const source = mimeText(d['image/svg+xml']);
      return {mime: 'image/svg+xml', source,
              render: DOMPurify.sanitize(source)};
    }
    if (d['text/html']) {
      const source = mimeText(d['text/html']);
      return {mime: 'text/html', source,
              render: source};
    }
    if (d['image/png']) {
      const source = mimeText(d['image/png']).replace(/\s+/g, '');
      return {mime: 'image/png', source, dataUrl: `data:image/png;base64,${source}`,
              render: `<img src="data:image/png;base64,${source}" alt="">`};
    }
    if (d['image/jpeg']) {
      const source = mimeText(d['image/jpeg']).replace(/\s+/g, '');
      return {mime: 'image/jpeg', source, dataUrl: `data:image/jpeg;base64,${source}`,
              render: `<img src="data:image/jpeg;base64,${source}" alt="">`};
    }
  }
  const sourceHtml = htmlFromDisplaySource(c && c.source);
  if (sourceHtml) {
    return {mime: 'text/html', source: sourceHtml, render: sourceHtml};
  }
  return null;
}

function notebookName(path) {
  return String(path || '').split('/').filter(Boolean).pop() || String(path || '');
}

function jsonForHtml(value) {
  return JSON.stringify(value, null, 2)
    .replace(/</g, '\\u003c')
    .replace(/>/g, '\\u003e')
    .replace(/&/g, '\\u0026')
    .replace(/\u2028/g, '\\u2028')
    .replace(/\u2029/g, '\\u2029');
}

function provenanceSummary(payload) {
  const lines = [
    `Notebook: ${payload.notebook || '(unsaved)'}`,
    `Cell: ${payload.cell_index} (${payload.cell_id})`,
    `Executed: ${payload.execution_count == null ? 'not recorded' : payload.execution_count}`,
    `Captured: ${payload.timestamp}`,
  ];
  if (payload.output_mime) lines.push(`Output: ${payload.output_mime}`);
  if (!payload.output_mime) lines.push('Output: none captured');
  if (payload.comment) {
    lines.push('Comment:');
    lines.push(payload.comment);
  }
  if (payload.attachments && payload.attachments.length) {
    lines.push('Attachments:');
    for (const attachment of payload.attachments) {
      lines.push(`- ${attachment.role || 'file'}: ${attachment.path}`);
    }
  }
  return lines.join('\n');
}

let provenanceResolve = null;
let provenancePicker = {path: null, parent: null, entries: []};

function askProvenanceDetails(defaultPath, hint) {
  const back = document.getElementById('prov-back');
  const path = document.getElementById('prov-path');
  const comment = document.getElementById('prov-comment');
  const hintEl = document.getElementById('prov-hint');
  if (!back || !path || !comment) return Promise.resolve(null);
  path.value = defaultPath || '';
  comment.value = '';
  if (hintEl) hintEl.textContent = hint || '';
  back.classList.add('on');
  provenanceClosePicker();
  path.focus();
  path.setSelectionRange(path.value.length, path.value.length);
  return new Promise(resolve => { provenanceResolve = resolve; });
}

function provenanceDone(ok) {
  const back = document.getElementById('prov-back');
  const path = document.getElementById('prov-path');
  const comment = document.getElementById('prov-comment');
  if (back) back.classList.remove('on');
  provenanceClosePicker();
  const resolve = provenanceResolve;
  provenanceResolve = null;
  if (!resolve) return;
  resolve(ok ? {
    path: path ? path.value.trim() : '',
    comment: comment ? comment.value.trim() : '',
  } : null);
}

function provenancePickerStartPath() {
  if (active) {
    const parts = active.split('/');
    parts.pop();
    return parts.join('/') || '/';
  }
  return fileState.path || '/';
}

function provenanceClosePicker() {
  const picker = document.getElementById('prov-picker');
  if (picker) picker.classList.remove('on');
}

async function provenanceTogglePicker() {
  const picker = document.getElementById('prov-picker');
  if (!picker) return;
  if (picker.classList.contains('on')) {
    provenanceClosePicker();
    return;
  }
  picker.classList.add('on');
  await provenanceBrowse(provenancePicker.path || provenancePickerStartPath());
}

async function provenanceBrowse(path) {
  const files = document.getElementById('prov-files');
  if (files) files.innerHTML = '<div class="prov-file-msg">loading...</div>';
  try {
    const q = new URLSearchParams();
    if (path) q.set('path', path);
    q.set('hidden', fileState.hidden ? '1' : '0');
    const data = await api('/api/files?' + q);
    provenancePicker = {path: data.path, parent: data.parent, entries: data.entries || []};
    const search = document.getElementById('prov-search');
    if (search) search.value = '';
    provenanceRenderPicker();
  } catch (err) {
    if (files) files.innerHTML =
      `<div class="prov-file-msg">Cannot list files: ${escapeHtml(errText(err))}</div>`;
  }
}

function provenanceRenderCrumbs(path) {
  const el = document.getElementById('prov-crumbs');
  if (!el) return;
  const parts = String(path || '/').split('/').filter(Boolean);
  let html = `<span class="prov-crumb" onclick="provenanceBrowse('/')">/</span>`;
  let cur = '';
  const start = Math.max(0, parts.length - 3);
  if (start > 0) {
    cur = '/' + parts.slice(0, start).join('/');
    html += ` <span class="prov-crumb" onclick="provenanceBrowse('${escapeAttr(cur)}')">...</span>`;
  }
  for (let i = start; i < parts.length; i++) {
    cur = '/' + parts.slice(0, i + 1).join('/');
    html += ` / <span class="prov-crumb" onclick="provenanceBrowse('${escapeAttr(cur)}')">${escapeHtml(parts[i])}</span>`;
  }
  el.innerHTML = html;
  el.title = path || '/';
}

function provenanceRenderPicker() {
  provenanceRenderCrumbs(provenancePicker.path);
  const files = document.getElementById('prov-files');
  if (!files) return;
  const q = (document.getElementById('prov-search') || {}).value || '';
  const needle = q.trim().toLowerCase();
  let entries = provenancePicker.entries || [];
  if (needle) entries = entries.filter(e =>
    String(e.name || '').toLowerCase().includes(needle) ||
    String(e.path || '').toLowerCase().includes(needle));
  const rows = [];
  if (provenancePicker.parent && !needle) {
    rows.push(`<div class="prov-file-row dir" onclick="provenanceBrowse('${escapeAttr(provenancePicker.parent)}')">
      <span class="ic">↰</span><span class="nm">..</span><span class="sz"></span>
    </div>`);
  }
  for (const e of entries) {
    const isDir = e.kind === 'dir';
    const action = isDir
      ? `provenanceBrowse('${escapeAttr(e.path)}')`
      : `provenancePickFile('${escapeAttr(e.path)}')`;
    rows.push(`<div class="prov-file-row ${isDir ? 'dir' : ''}" onclick="${action}" title="${escapeAttr(e.path)}">
      <span class="ic">${isDir ? '▸' : '·'}</span>
      <span class="nm">${escapeHtml(e.name)}</span>
      <span class="sz">${isDir ? '' : fmtSize(e.size)}</span>
    </div>`);
  }
  files.innerHTML = rows.length ? rows.join('') : '<div class="prov-file-msg">No matches</div>';
}

function provenancePickFile(path) {
  const input = document.getElementById('prov-path');
  if (input) input.value = path;
  provenanceClosePicker();
  const comment = document.getElementById('prov-comment');
  if (comment) comment.focus();
}

function provenanceKey(event) {
  if (event.key === 'Escape') {
    event.preventDefault();
    provenanceDone(false);
  } else if (event.key === 'Enter' && event.target && event.target.id === 'prov-path') {
    event.preventDefault();
    provenanceDone(true);
  }
}

function attachmentFromPath(path) {
  const clean = String(path || '').trim();
  if (!clean) return [];
  return [{
    path: clean,
    name: notebookName(clean),
    role: 'data',
    timestamp: new Date().toISOString(),
    source: 'notebook-linked',
  }];
}

function provenancePlaceholder(payload) {
  const title = payload.attachments && payload.attachments.length
    ? 'Notebook-linked data source' : 'Notebook provenance source';
  const detail = payload.attachments && payload.attachments.length
    ? payload.attachments.map(a => a.path).join(', ')
    : payload.comment || `${payload.notebook_name || 'Notebook'} cell ${payload.cell_index}`;
  return `<div class="gusnb-viz-placeholder" data-gusnb-viz-placeholder>
    <strong>${escapeHtml(title)}</strong>
    <span>${escapeHtml(detail)}</span>
  </div>`;
}

function provenanceRenderHtml(visual, outputId) {
  if (!visual) return '';
  if (visual.mime === 'text/html') {
    return `<iframe class="gusnb-viz-frame" title="Notebook visualization"
      sandbox="allow-scripts" referrerpolicy="no-referrer"
      data-gusnb-scale="1" data-gusnb-base-height="460"
      data-output-frame="${escapeAttr(outputId)}"
      height="460" style="display:block;width:100%;height:460px;border:0;background:transparent;"
      srcdoc="${escapeAttr(htmlOutputSrcdoc(visual.source || '', outputId))}"></iframe>`;
  }
  return visual.render || '';
}

function provenanceResizeRuntime() {
  return `<script data-gusnb-viz-runtime>
(() => {
  if (window.__gusnbVizResizeRuntime) return;
  window.__gusnbVizResizeRuntime = true;
  addEventListener('message', event => {
    const data = event.data || {};
    if (data.type !== 'gusnotebook-html-output-height') return;
    const id = String(data.id || '').replace(/["\\\\]/g, '\\\\$&');
    const frame = document.querySelector('iframe[data-output-frame="' + id + '"]');
    if (!frame) return;
    const height = Math.max(24, Math.min(4000, Number(data.height) || 0));
    const scale = Math.max(.35, Math.min(1.8, Number(frame.getAttribute('data-gusnb-scale')) || 1));
    const render = frame.closest('[data-gusnb-viz-render]');
    frame.style.height = height + 'px';
    frame.setAttribute('height', String(height));
    frame.setAttribute('data-gusnb-base-height', String(height));
    if (render) {
      render.style.height = Math.round(height * scale) + 'px';
      render.style.minHeight = '0';
      render.style.overflow = 'visible';
    }
  });
})();
</script>`;
}

function provenanceHtml(c, visual, details) {
  details = details || {};
  const payload = {
    kind: 'gusnotebook-provenance-snapshot',
    version: 2,
    notebook: active || '',
    notebook_name: notebookName(active),
    cell_id: c.id,
    cell_index: cells.findIndex(x => x.id === c.id) + 1,
    execution_count: c.execution_count == null ? null : c.execution_count,
    timestamp: new Date().toISOString(),
    code: c.source || '',
    output_mime: visual ? visual.mime : null,
    output_source: visual ? visual.source : null,
    output_data_url: visual ? visual.dataUrl || null : null,
    attachments: attachmentFromPath(details.path),
    comment: details.comment || '',
  };
  const summary = provenanceSummary(payload);
  const outputId = `gusnb-viz-${c.id}-${Date.now().toString(36)}`;
  const render = visual ? provenanceRenderHtml(visual, outputId) : provenancePlaceholder(payload);
  const html = `<figure class="gusnb-viz" data-gusnb-viz="1" data-gusnb-provenance="1" data-gusnb-notebook="${escapeAttr(payload.notebook)}" data-gusnb-cell-id="${escapeAttr(c.id)}">
  <div class="gusnb-viz-render" data-gusnb-viz-render>${render}</div>
  <pre data-gusnb-viz-panel contenteditable="false" hidden>${escapeHtml(summary)}</pre>
  <script type="application/json" data-gusnb-viz-source>${jsonForHtml(payload)}</script>
</figure>${visual && visual.mime === 'text/html' ? provenanceResizeRuntime() : ''}`;
  return {
    html,
    text: html,
    plain: summary,
    payload,
  };
}

async function copyRichHtml(html, plain) {
  if (navigator.clipboard && window.ClipboardItem && window.isSecureContext) {
    await navigator.clipboard.write([
      new ClipboardItem({
        'text/html': new Blob([html], {type: 'text/html'}),
        'text/plain': new Blob([plain], {type: 'text/plain'}),
      })
    ]);
    return;
  }
  const div = document.createElement('div');
  div.contentEditable = 'true';
  div.style.position = 'fixed';
  div.style.left = '-9999px';
  div.innerHTML = html;
  document.body.appendChild(div);
  const range = document.createRange();
  range.selectNodeContents(div);
  const sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(range);
  const ok = document.execCommand('copy');
  sel.removeAllRanges();
  div.remove();
  if (!ok) throw new Error('clipboard unavailable');
}

async function copyCellProvenance(id, event) {
  const c = getCell(id);
  if (!c) return;
  const visual = visualOutput(c);
  const wantsAttachment = !visual || (event && (event.altKey || event.metaKey || event.ctrlKey));
  const defaultPath = active ? active.split('/').slice(0, -1).join('/') + '/' : '';
  const details = wantsAttachment
    ? await askProvenanceDetails(defaultPath, visual
      ? 'Attach a file and/or comment to this visual snapshot.'
      : 'No rendered output was found. Link a data or figure file, add context, or copy as source-only.')
    : {};
  if (details == null) return;
  const block = provenanceHtml(c, visual, details);
  try {
    await copyRichHtml(block.html, block.text);
    flash(visual ? 'Copied provenance snapshot' : 'Copied source provenance');
  } catch (err) {
    flash('Copy failed: ' + errText(err));
  }
}

/** The execution state stays separate from output so a streamed message cannot
 * replace it. It deliberately comes after the output: on a long run the newest
 * lines and the still-running signal remain together at the bottom of the cell. */
function runningStatusHtml() {
  return '<div class="spin cell-run-status" role="status">running</div>';
}

function runningLabelHtml() {
  return '[<span class="run-symbol" role="img" aria-label="running"></span>]';
}

function executionLabel(c) {
  return `[${c && c.execution_count != null ? c.execution_count : ' '}]`;
}

/** Update both the in-memory cell and the two small DOM affordances in place. */
function setCellRunning(id, running) {
  const c = getCell(id);
  if (c) c._running = !!running;
  const cellEl = document.querySelector(`.cell[data-id="${id}"]`);
  if (!cellEl) return;
  cellEl.classList.toggle('is-running', !!running);
  const label = cellEl.querySelector('.gutter-label');
  if (!label) return;
  if (running) {
    label.innerHTML = runningLabelHtml();
    label.title = 'Cell is running';
  } else {
    label.textContent = executionLabel(c);
    label.removeAttribute('title');
  }
}

function renderCellOutput(c) {
  return renderOutputs(c && c.outputs, c && c.id) +
    (c && c._running ? runningStatusHtml() : '');
}

/* How many lines a folded cell shows, as a max-height for the clip. In em so it
 * tracks the editor's line-height rather than a pixel count that breaks when the
 * font size changes; the extra covers the editor's vertical padding. */
function foldHeight() { return (codeFoldLines * 1.55 + 1.4).toFixed(2) + 'em'; }

function cellLines(c) { return (c.source || '').split('\n').length; }
function isFoldable(c) {
  return c.cell_type === 'code' && cellLines(c) > codeFoldLines;
}

/** Is the caret inside this cell's editor right now? */
function hasCaret(id) {
  const a = document.activeElement;
  const cell = a && a.closest ? a.closest('.cell') : null;
  return !!cell && cell.dataset.id === id;
}

/**
 * Whether a long cell should be drawn clipped.
 *
 * The focus exemption is optional. By default a click on folded code keeps it
 * folded, so the user opens it deliberately from the "N more lines" veil.
 */
function isFolded(c) {
  return isFoldable(c) && !codeOpen.has(c.id) && !(foldOpenOnFocus && hasCaret(c.id));
}

/** The clipped-code veil, with a small explicit click target to open it. */
function veilHtml(c) {
  const hidden = cellLines(c) - codeFoldLines;
  return `<div class="fold-veil">
      <span onclick="event.stopPropagation();toggleFold('${c.id}')"
            title="Show the whole cell">⌄ ${hidden} more line${
              hidden > 1 ? 's' : ''}</span></div>`;
}

/**
 * The gutter's fold toggle. Output hide/show lives inside the output area, so
 * the control is in the same place for both visible and hidden output.
 *
 * Shown only when there's something to fold: a short cell has no fold state
 * worth a button.
 */
function viewBtnsHtml(c) {
  const long = isFoldable(c);
  if (!long) return '';
  // What's on screen when there's something on screen to ask, since the caret
  // exemption in isFolded() means the set alone doesn't decide it. Only the
  // initial render has no wrapper to ask.
  const wrap = document.getElementById('fold-' + c.id);
  const folded = wrap ? wrap.classList.contains('folded') : isFolded(c);
  return `<div class="gutter-out">${long ? `
        <button class="out-btn" id="foldb-${c.id}"
                title="${folded ? 'Show the whole cell' : "Fold this cell's code"}"
                onclick="event.stopPropagation();toggleFold('${c.id}')">${
                  folded ? '⌄' : '⌃'}</button>` : ''}
      </div>`;
}

function outHideHtml(c) {
  if (!(c.outputs || []).length) return '';
  return `<button class="out-note" onclick="event.stopPropagation();toggleOutput('${c.id}')"
      title="Hide this cell's output">▾ Hide output</button>`;
}

/** The "N outputs hidden" stand-in shown in place of collapsed output. */
function outNoteHtml(c) {
  return `<button class="out-note" onclick="event.stopPropagation();toggleOutput('${c.id}')"
      title="Show this cell's output">▸ Show output</button>`;
}

function outputSlotHtml(c) {
  if (outsHidden.has(c.id)) {
    if (!(c.outputs || []).length && !c._running) return '';
    return c._running
      ? `<button class="out-note running-dots"
           onclick="event.stopPropagation();toggleOutput('${c.id}')"
           title="Show this cell's output">▸ running</button>`
      : outNoteHtml(c);
  }
  return outHideHtml(c) + renderCellOutput(c);
}

function cellHtml(c) {
  const isMd = c.cell_type === 'markdown';
  const isCode = c.cell_type === 'code';
  const isAi = c.cell_type === 'ai';
  const isVis = c.cell_type === 'vis';
  const isEditing = editing.has(c.id) || !c.source.trim();
  // Code cells show [n]; markdown/raw/ai/vis show a marker instead.
  const label = isCode
    ? executionLabel(c)
    : (isAi ? 'AI' : (isVis ? 'VIS' : ''));
  const labelHtml = isCode && c._running ? runningLabelHtml() : escapeHtml(label);

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
      : isVis
      ? 'Describe the visualization you want, then press ⇧⏎'
      : '';
    // A host for CodeMirror, with the textarea inside it as the fallback. The
    // textarea is what's in the DOM until mountEditor() replaces it, so a cell
    // is editable from the first paint and stays editable if CM never loads.
    bodyInner = `<div class="ed-host" id="ed-host-${c.id}" data-cell="${c.id}"
      data-lang="${isCode || isAi ? 'python' : 'text'}"><textarea class="editor" id="ed-${c.id}"
      oninput="autosize(this); queueSave('${c.id}'); refreshCodeFoldForEdit('${c.id}', this.value)"
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
  } else if (isVis) {
    bodyInner += `
      <div class="ai-bar vis-bar">
        <button class="ai-go" id="visgo-${c.id}" onclick="event.stopPropagation();generateVisCell('${c.id}')">
          Generate</button>
        <span class="ai-hint">⇧⏎ sends to the active agent · replaces this cell</span>
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
          <button class="act-btn" title="Copy provenance snapshot (⌥/Ctrl/⌘ click to attach file)"
                  onclick="event.stopPropagation();copyCellProvenance('${c.id}', event)">⧉</button>
          <button class="act-btn danger" title="Delete this cell"
                  onclick="event.stopPropagation();deleteCell('${c.id}')">✕</button>
        </div>
      </div>`;

  return `
  <div class="cell ${c._running ? 'is-running' : ''}" data-id="${c.id}" data-type="${c.cell_type}"
       onclick="selectCell('${c.id}')">
    <div class="gutter">
      ${hdBtn}<span class="gutter-label"${c._running ? ' title="Cell is running"' : ''}>${labelHtml}</span>${viewBtns}${histBtns}${cellBtns}
    </div>
    <div class="cell-body">
      ${promptStrip}${claudeStrip}${undoStrip}${bodyInner}
      <div class="output-area">
        <div id="out-${c.id}" class="${outHidden ? 'output-hidden' : ''}">${
          outputSlotHtml(c)}</div>
      </div>
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
  const t = activeTab();
  if (t && t.kind === 'notebook') {
    t.headingsCollapsed = headingsCollapsed;
    rememberNotebookView(t);
  }
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
  resetFoldedPreviewScrolls();
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
