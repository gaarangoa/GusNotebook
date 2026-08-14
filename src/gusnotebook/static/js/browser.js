/* The file column, the + tab that creates things, and the tab bar itself.
 *
 * One file because they're one gesture: you navigate to a directory, and what
 * you make there arrives as a tab. */

// ---------- File browser ----------
// hidden: true by default — .env, .gitignore and friends are working files in a
// project like this one, so hiding them by default hides things you came to edit.
let fileState = {path: null, home: null, notebook: null, hidden: true};

const FILE_ICON = {dir: '▸', notebook: '◆', file: '·'};

function fmtSize(n) {
  if (n == null) return '';
  if (n < 1024) return n + 'B';
  if (n < 1048576) return (n / 1024).toFixed(0) + 'K';
  if (n < 1073741824) return (n / 1048576).toFixed(1) + 'M';
  return (n / 1073741824).toFixed(1) + 'G';
}

async function browse(path) {
  const q = new URLSearchParams();
  if (path) q.set('path', path);
  if (fileState.hidden) q.set('hidden', '1');
  let data;
  try {
    data = await api('/api/files?' + q);
  } catch (err) {
    document.getElementById('file-list').innerHTML =
      `<div class="files-msg err">${escapeHtml(String(err).slice(0, 200))}</div>`;
    return;
  }
  Object.assign(fileState, {
    path: data.path, home: data.home, parent: data.parent,
    entries: data.entries,        // used to suggest an untaken new-file name
  });
  renderCrumbs(data.path);
  renderFileList(data.entries);
  rememberRoot(data.path);
}

/* Where you browsed becomes the session's root, so switching back lands you
 * where you left off rather than where the session was first created. Fired on
 * every navigation, so it's debounced and skips no-ops. */
let rootTimer = null;

function rememberRoot(path) {
  if (!currentSession || !path) return;
  const s = sessionList.find(x => x.id === currentSession);
  if (s && s.root === path) return;
  if (s) s.root = path;                  // locally, so the check above settles
  clearTimeout(rootTimer);
  rootTimer = setTimeout(() => {
    api('/api/sessions/' + encodeURIComponent(currentSession),
        {method: 'POST', body: JSON.stringify({root: path})}).catch(() => {});
  }, 600);
}

/* Show the deepest CRUMB_TAIL folders — the ones you're actually working in.
 * Anything above them collapses into a "…" that still navigates to the last
 * hidden level, so the row stays one fixed-width line instead of scrolling. */
const CRUMB_TAIL = 3;

function renderCrumbs(path) {
  const parts = path.split('/').filter(Boolean);
  // Absolute path for each part, so any visible level is one click away.
  const full = parts.map((p, i) => '/' + parts.slice(0, i + 1).join('/'));

  const hidden = Math.max(0, parts.length - CRUMB_TAIL);
  let html = hidden
    ? `<span class="crumb" onclick="browse('${escapeAttr(full[hidden - 1])}')"
             title="${escapeAttr(full[hidden - 1])}">…</span>`
    : `<span class="crumb" onclick="browse('/')">/</span>`;

  for (let i = hidden; i < parts.length; i++) {
    html += `<span class="crumb-sep">/</span>` +
      `<span class="crumb" onclick="browse('${escapeAttr(full[i])}')"
             title="${escapeAttr(full[i])}">${escapeHtml(parts[i])}</span>`;
  }

  const el = document.getElementById('crumbs');
  el.innerHTML = html;
  el.title = path;
}

function renderFileList(entries) {
  const list = document.getElementById('file-list');
  if (!entries.length) {
    list.innerHTML = '<div class="files-msg">empty directory</div>';
  } else {
    list.innerHTML = entries.map(e => {
      const cls = ['file-row'];
      if (tab(e.path)) cls.push('open');
      if (e.kind === 'file') cls.push('dim');
      const action = e.kind === 'dir'
        ? `browse('${escapeAttr(e.path)}')`
        : `openFile('${escapeAttr(e.path)}')`;
      return `<div class="${cls.join(' ')}" onclick="${action}"
        oncontextmenu="fileCtxEntry(event,'${escapeAttr(e.path)}','${e.kind}')"
        title="${escapeAttr(e.path)}">
        <span class="ic">${FILE_ICON[e.kind]}</span>
        <span class="nm">${escapeHtml(e.name)}</span>
        <span class="sz">${fmtSize(e.size)}</span>
      </div>`;
    }).join('');
  }
  list.oncontextmenu = (e) => {
    if (!e.target.closest('.file-row')) fileCtxDir(e);
  };
}

// ---------- File context menu ----------

let fileCtxTarget = null;

function fileCtxDir(e) {
  e.preventDefault();
  fileCtxTarget = null;
  showFileCtx(e.clientX, e.clientY, [
    {label: '+ New file',   action: () => newFile()},
    {label: '+ New folder', action: () => newFolder()},
  ]);
}

function fileCtxEntry(e, path, kind) {
  e.preventDefault();
  e.stopPropagation();
  fileCtxTarget = {path, kind};
  showFileCtx(e.clientX, e.clientY, [
    {label: 'Rename', action: () => renameEntry(path)},
    {label: 'Delete', action: () => deleteEntry(path, kind), danger: true},
  ]);
}

function showFileCtx(x, y, items) {
  closeFileCtx();
  const menu = document.createElement('div');
  menu.className = 'file-ctx';
  menu.id = 'file-ctx';
  items.forEach(({label, action, danger}) => {
    const item = document.createElement('div');
    item.className = 'file-ctx-item' + (danger ? ' danger' : '');
    item.textContent = label;
    item.onclick = (e) => { e.stopPropagation(); closeFileCtx(); action(); };
    menu.appendChild(item);
  });
  // Keep menu inside the viewport.
  document.body.appendChild(menu);
  const mw = menu.offsetWidth, mh = menu.offsetHeight;
  menu.style.left = Math.min(x, window.innerWidth  - mw - 8) + 'px';
  menu.style.top  = Math.min(y, window.innerHeight - mh - 8) + 'px';
}

function closeFileCtx() {
  const m = document.getElementById('file-ctx');
  if (m) m.remove();
}

async function renameEntry(path) {
  const old = path.split('/').pop();
  const name = await askName('Rename', old, path);
  if (!name || name === old) return;
  try {
    await api('/api/files/rename', {method: 'POST',
      body: JSON.stringify({path, name: name.trim()})});
    browse(fileState.path);
  } catch (err) { flash('Rename failed: ' + errText(err)); }
}

async function deleteEntry(path, kind) {
  const name = path.split('/').pop();
  const ok = await askConfirm(
    `Delete ${kind === 'dir' ? 'folder' : 'file'} "${name}"?`,
    kind === 'dir' ? 'This will delete the folder and all its contents.' : '');
  if (!ok) return;
  try {
    await api('/api/files/delete', {method: 'POST', body: JSON.stringify({path})});
    browse(fileState.path);
  } catch (err) { flash('Delete failed: ' + errText(err)); }
}

function escapeAttr(s) {
  return String(s).replace(/&/g, '&amp;').replace(/'/g, '&#39;')
                  .replace(/"/g, '&quot;').replace(/</g, '&lt;');
}

function refreshFiles() { browse(fileState.path); }
function browseUp() { if (fileState.parent) browse(fileState.parent); }
function browseHome() { browse(fileState.home); }

// ---------- Creating things ----------

/* Names are asked for in-page rather than with window.prompt().
 *
 * Chrome offers "prevent this page from creating additional dialogs" once a page
 * has shown a few, and some setups suppress them outright. After that prompt()
 * returns null with no UI at all, which this code reads as a cancel — so every
 * New… did nothing, silently, with no way to tell it apart from a real cancel.
 */
let askResolve = null;

function askName(title, value, hint, confirmLabel) {
  document.getElementById('ask-title').textContent = title;
  document.getElementById('ask-hint').textContent = hint || '';
  document.getElementById('ask-ok').textContent = confirmLabel || 'Create';
  const input = document.getElementById('ask-input');
  input.value = value || '';
  input.classList.remove('off');
  document.getElementById('ask-back').classList.add('on');
  input.focus();
  // Select the stem, not the extension: the part you'd retype.
  const dot = input.value.lastIndexOf('.');
  input.setSelectionRange(0, dot > 0 ? dot : input.value.length);
  return new Promise(resolve => { askResolve = resolve; });
}

/** Yes/no in the same modal — window.confirm() suppresses to a silent `false`,
 *  which would turn "close this session" into a no-op with no UI at all. */
function askConfirm(title, hint, confirmLabel) {
  document.getElementById('ask-title').textContent = title;
  document.getElementById('ask-hint').textContent = hint || '';
  document.getElementById('ask-ok').textContent = confirmLabel || 'OK';
  const input = document.getElementById('ask-input');
  input.value = '';
  input.classList.add('off');            // nothing to type; it's a decision
  document.getElementById('ask-back').classList.add('on');
  document.getElementById('ask-ok').focus();
  return new Promise(resolve => { askResolve = resolve; });
}

function askDone(ok) {
  const input = document.getElementById('ask-input');
  const asked = !input.classList.contains('off');
  document.getElementById('ask-back').classList.remove('on');
  const resolve = askResolve;
  askResolve = null;
  // A confirm resolves true/false; a name resolves the text, or null to cancel.
  if (resolve) resolve(asked ? (ok ? input.value : null) : ok);
}

function askKey(ev) {
  if (ev.key === 'Enter') { ev.preventDefault(); askDone(true); }
  else if (ev.key === 'Escape') { ev.preventDefault(); askDone(false); }
}

/** Create a file or folder in the directory the panel is showing. */
async function createIn(kind, name) {
  const directory = fileState.path;
  if (!directory) { flash('The file panel has no directory open yet.'); return null; }
  let data;
  try {
    data = await api('/api/files/new', {
      method: 'POST',
      body: JSON.stringify({directory, name, kind}),
    });
  } catch (err) {
    flash('Cannot create: ' + errText(err));
    return null;
  }
  await browse(directory);
  return data;
}

/** A new .ipynb — one click, named for you, opened as a notebook tab. */
async function newNotebook() {
  const name = await askName('New notebook', suggestName('Untitled', '.ipynb'),
                             'in ' + (fileState.path || '?'));
  if (name === null || !name.trim()) return;
  const data = await createIn('file', ensureSuffix(name.trim(), '.ipynb'));
  if (data) openFile(data.path);
}

/** Any file. The extension decides what it is — .ipynb gives a real notebook. */
async function newFile() {
  const name = await askName('New file', suggestName('untitled', '.py'),
                             'an .ipynb name creates a notebook');
  if (name === null || !name.trim()) return;
  const data = await createIn('file', name.trim());
  if (data) openFile(data.path);
}

async function newFolder() {
  const name = await askName('New folder', '', 'in ' + (fileState.path || '?'));
  if (name === null || !name.trim()) return;
  const data = await createIn('dir', name.trim());
  if (data) browse(data.path);         // step into it, like Finder
}

function ensureSuffix(name, suffix) {
  return name.toLowerCase().endsWith(suffix) ? name : name + suffix;
}

/** "Untitled1.ipynb" — the first name that isn't taken in this directory. */
function suggestName(stem, suffix) {
  const taken = new Set((fileState.entries || []).map(e => e.name.toLowerCase()));
  for (let i = 1; ; i++) {
    const candidate = `${stem}${i}${suffix}`;
    if (!taken.has(candidate.toLowerCase())) return candidate;
  }
}

function toggleHidden() {
  fileState.hidden = !fileState.hidden;
  document.getElementById('hidden-btn').classList.toggle('on', fileState.hidden);
  browse(fileState.path);
}

function toggleFiles() {
  document.getElementById('app').classList.toggle('files-hidden');
  applyLayout();
  fitTerm();
}

// ---------- Tab bar ----------
const TAB_ICON = {notebook: '◆', text: '≡', markup: '◇', image: '▣'};

function tabIcon(t) {
  return TAB_ICON[isMarkupTab(t) ? 'markup' : t.kind] || '·';
}

function errText(err) {
  const s = String(err && err.message ? err.message : err);
  try { return JSON.parse(s).error || s; } catch (e) { return s.slice(0, 300); }
}

function errCode(err) {
  const s = String(err && err.message ? err.message : err);
  try { return JSON.parse(s).code || null; } catch (e) { return null; }
}

/** Park the on-screen state back into the tab it belongs to. */
function stashActive() {
  const t = activeTab();
  if (!t) return;
  if (t.kind === 'notebook') {
    t.cells = cells;
    t.selected = selected;
    t.editing = editing;
    t.codeOpen = codeOpen;
    t.outsHidden = outsHidden;
    t.headingsCollapsed = headingsCollapsed;
    t.scroll = document.getElementById('notebook-pane').scrollTop;
  } else if (t.kind === 'text') {
    t.text = document.getElementById('text-editor').value;
  }
}

function renderTabs() {
  document.getElementById('tabs').innerHTML = tabs.map(t => `
    <div class="tab ${t.path === active ? 'active' : ''} ${t.dirty ? 'dirty' : ''}"
         onclick="switchTab('${escapeAttr(t.path)}')" title="${escapeAttr(t.path)}">
      <span class="ti">${tabIcon(t)}</span>
      <span class="tn">${escapeHtml(t.name)}</span>
      <span class="tx" onclick="closeTab('${escapeAttr(t.path)}', event)">✕</span>
    </div>`).join('') +
    // Always last, so "new" sits where the next tab would appear.
    `<div class="tab-new" id="tab-new" onclick="toggleNewMenu(event)"
          title="New notebook, file, folder, agent or terminal">+</div>`;
}

// ---------- The + tab's menu ----------

function closeNewMenu() {
  document.getElementById('new-menu').classList.remove('on');
  const plus = document.getElementById('tab-new');
  if (plus) plus.classList.remove('on');
}

function toggleNewMenu(ev) {
  if (ev) ev.stopPropagation();
  const menu = document.getElementById('new-menu');
  if (menu.classList.contains('on')) { closeNewMenu(); return; }
  // Anchored under the + tab's left edge, since the menu is wider than the tab.
  const r = document.getElementById('tab-new').getBoundingClientRect();
  menu.style.top = (r.bottom + 4) + 'px';
  menu.style.left = Math.min(r.left, window.innerWidth - 270) + 'px';
  menu.classList.add('on');
  document.getElementById('tab-new').classList.add('on');
}

/** One entry from the + menu. Everything lands in the browsed directory. */
function newFromMenu(what) {
  closeNewMenu();
  if (what === 'notebook') return newNotebook();
  if (what === 'text') return newFile();
  if (what === 'folder') return newFolder();
  if (what === 'claude') return openTerminalHere('claude');
  if (what === 'codex') return openTerminalHere('codex');
  if (what === 'terminal') return openTerminalHere('shell');
}

document.addEventListener('click', (e) => {
  if (!e.target.closest('#new-menu') && !e.target.closest('#tab-new')) closeNewMenu();
  if (!e.target.closest('#file-ctx')) closeFileCtx();
});
