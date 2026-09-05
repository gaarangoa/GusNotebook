/* The file column, the + tab that creates things, and the tab bar itself.
 *
 * One file because they're one gesture: you navigate to a directory, and what
 * you make there arrives as a tab. */

// ---------- File browser ----------
// hidden: true by default — .env, .gitignore and friends are working files in a
// project like this one, so hiding them by default hides things you came to edit.
let fileState = {path: null, home: null, notebook: null, hidden: true};
let browseSerial = 0;

const FILE_ICON = {dir: 'folder', notebook: 'notebook', file: 'file'};

function fmtSize(n) {
  if (n == null) return '';
  if (n < 1024) return n + 'B';
  if (n < 1048576) return (n / 1024).toFixed(0) + 'K';
  if (n < 1073741824) return (n / 1048576).toFixed(1) + 'M';
  return (n / 1073741824).toFixed(1) + 'G';
}

async function browse(path) {
  const serial = ++browseSerial;
  const sessionAtStart = currentSession;
  const q = new URLSearchParams();
  if (path) q.set('path', path);
  if (fileState.hidden) q.set('hidden', '1');
  let data;
  try {
    data = await api('/api/files?' + q);
  } catch (err) {
    if (serial !== browseSerial || sessionAtStart !== currentSession) return false;
    document.getElementById('file-list').innerHTML =
      `<div class="files-msg err">${escapeHtml(String(err).slice(0, 200))}</div>`;
    return false;
  }
  if (serial !== browseSerial || sessionAtStart !== currentSession) return false;
  Object.assign(fileState, {
    path: data.path, home: data.home, parent: data.parent,
    entries: data.entries,        // used to suggest an untaken new-file name
  });
  renderCrumbs(data.path);
  renderFileList(data.entries);
  rememberRoot(data.path);
  return true;
}

/* Where you browsed becomes the session's root, so switching back lands you
 * where you left off rather than where the session was first created. Fired on
 * every navigation, so it's debounced and skips no-ops. */
const rootTimers = new Map();

function rememberRoot(path) {
  if (!currentSession || !path) return;
  const sid = currentSession;
  const s = sessionList.find(x => x.id === sid);
  if (s && s.root === path) return;
  if (s) s.root = path;                  // locally, so the check above settles
  clearTimeout(rootTimers.get(sid));
  rootTimers.set(sid, setTimeout(() => {
    rootTimers.delete(sid);
    api('/api/sessions/' + encodeURIComponent(sid),
        {method: 'POST', body: JSON.stringify({root: path})}).catch(() => {});
  }, 600));
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
    ? `<span role="button" tabindex="0" class="crumb" data-path="${escapeAttr(full[hidden - 1])}" onclick="browse(this.dataset.path)"
             title="${escapeAttr(full[hidden - 1])}">…</span>`
    : `<span role="button" tabindex="0" class="crumb" onclick="browse('/')">/</span>`;

  for (let i = hidden; i < parts.length; i++) {
    html += `<span class="crumb-sep">/</span>` +
      `<span role="button" tabindex="0" class="crumb" data-path="${escapeAttr(full[i])}" onclick="browse(this.dataset.path)"
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
      if (e.path === active) cls.push('active-file');
      if (e.kind === 'file') cls.push('dim');
      const action = e.kind === 'dir'
        ? 'browse(this.dataset.path)'
        : 'openFile(this.dataset.path)';
      return `<div class="${cls.join(' ')}" data-path="${escapeAttr(e.path)}" onclick="${action}"
        oncontextmenu="fileCtxEntry(event,this.dataset.path,'${e.kind}')"
        draggable="true"
        ondragstart="fileDragStart(event,this.dataset.path,'${e.kind}')"
        ondragover="fileDragOver(event,'${e.kind}')"
        ondragleave="fileDragLeave(event)"
        ondrop="fileDrop(event,this.dataset.path,'${e.kind}')"
        role="button" tabindex="0" title="${escapeAttr(e.path)}">
        <span class="ic">${icon(FILE_ICON[e.kind])}</span>
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
let fileClipboard = null;
let fileDragPayload = null;

function fileCtxDir(e) {
  e.preventDefault();
  fileCtxTarget = null;
  const items = [];
  if (fileState.path) {
    items.push(
      {label: 'Copy path', action: () => copyText(fileState.path, 'Path copied')},
      {label: 'Copy full name', action: () => copyText(baseName(fileState.path), 'Name copied')},
    );
    if (fileClipboard) {
      items.push({label: pasteLabel('here'), action: () => pasteEntry(fileState.path)});
    }
  }
  showFileCtx(e.clientX, e.clientY, [
    ...items,
    {label: 'Upload files', action: () => chooseUploads()},
    {label: '+ New file',   action: () => newFile()},
    {label: '+ New folder', action: () => newFolder()},
  ]);
}

function fileCtxEntry(e, path, kind) {
  e.preventDefault();
  e.stopPropagation();
  fileCtxTarget = {path, kind};
  const items = [
    {label: 'Copy path', action: () => copyText(path, 'Path copied')},
    {label: 'Copy full name', action: () => copyText(baseName(path), 'Name copied')},
    {label: 'Duplicate...', action: () => duplicateEntry(path, kind)},
    {label: 'Copy', action: () => copyEntry(path, kind)},
    {label: 'Cut', action: () => cutEntry(path, kind)},
    {label: 'Rename', action: () => renameEntry(path)},
    {label: 'Delete', action: () => deleteEntry(path, kind), danger: true},
  ];
  if (kind !== 'dir') {
    items.unshift({label: 'Download', action: () => downloadEntry(path)});
  } else if (fileClipboard) {
    items.splice(5, 0, {label: pasteLabel('inside'), action: () => pasteEntry(path)});
  }
  showFileCtx(e.clientX, e.clientY, items);
}

function showFileCtx(x, y, items, label = 'File actions') {
  closeFileCtx();
  const menu = document.createElement('div');
  menu.className = 'file-ctx';
  menu.id = 'file-ctx';
  menu.setAttribute('role', 'menu');
  menu.setAttribute('aria-label', label);
  menu._opener = document.activeElement;
  items.forEach(({label, action, danger}) => {
    const item = document.createElement('div');
    item.className = 'file-ctx-item' + (danger ? ' danger' : '');
    item.setAttribute('role', 'menuitem');
    item.tabIndex = -1;
    item.textContent = label;
    item.onclick = (e) => { e.stopPropagation(); closeFileCtx(); action(); };
    menu.appendChild(item);
  });
  // Keep menu inside the viewport.
  document.body.appendChild(menu);
  const mw = menu.offsetWidth, mh = menu.offsetHeight;
  menu.style.left = Math.min(x, window.innerWidth  - mw - 8) + 'px';
  menu.style.top  = Math.min(y, window.innerHeight - mh - 8) + 'px';
  menu.firstElementChild?.focus();
}

function tabFileCtx(e, path) {
  e.preventDefault();
  e.stopPropagation();
  const element = e.currentTarget;
  element.focus();
  const box = element.getBoundingClientRect();
  showPathCtx(e.clientX || box.left, e.clientY || box.bottom, path);
}

function showPathCtx(x, y, path) {
  showFileCtx(x, y, [
    {label: 'Rename…', action: () => renameEntry(path)},
    {label: 'Show in file tree', action: () => showInFileTree(path)},
    {label: 'Copy path', action: () => copyText(path, 'Path copied')},
    {label: 'Copy full name', action: () => copyText(baseName(path), 'Name copied')},
  ]);
}

async function showInFileTree(path) {
  const app = document.getElementById('app');
  if (app && app.classList.contains('files-hidden')) {
    ensurePanel('files');
  }
  const directory = String(path || '').split('/').slice(0, -1).join('/') || '/';
  const ok = await browse(directory);
  if (!ok) return;
  const row = [...document.querySelectorAll('.file-row')]
    .find(el => el.title === path);
  if (row) {
    row.scrollIntoView({block: 'nearest'});
    row.classList.add('located');
    setTimeout(() => row.classList.remove('located'), 1600);
  }
}

function closeFileCtx() {
  const m = document.getElementById('file-ctx');
  if (m) {
    if (m.contains(document.activeElement)) m._opener?.focus();
    m.remove();
  }
}

function baseName(path) {
  const parts = String(path || '').split('/').filter(Boolean);
  return parts.length ? parts[parts.length - 1] : '/';
}

async function copyText(text, message) {
  text = String(text || '');
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
    } else {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.left = '-9999px';
      document.body.appendChild(ta);
      ta.focus();
      ta.select();
      document.execCommand('copy');
      ta.remove();
    }
    flash(message || 'Copied');
  } catch (err) {
    flash('Copy failed: ' + errText(err));
  }
}

function copyNameFor(path) {
  const old = baseName(path);
  const dot = old.lastIndexOf('.');
  if (dot > 0) return old.slice(0, dot) + ' copy' + old.slice(dot);
  return old + ' copy';
}

function copyEntry(path, kind) {
  fileClipboard = {path, kind, mode: 'copy'};
  flash(`${kind === 'dir' ? 'Folder' : 'File'} copied`);
}

function cutEntry(path, kind) {
  fileClipboard = {path, kind, mode: 'cut'};
  flash(`${kind === 'dir' ? 'Folder' : 'File'} cut`);
}

function pasteLabel(place) {
  if (!fileClipboard) return 'Paste';
  const what = fileClipboard.mode === 'cut' ? 'move' : 'copy';
  return `Paste ${what} ${place}`;
}

async function copyEntryAs(path, directory, name, openCopied) {
  try {
    const data = await api('/api/files/copy', {method: 'POST',
      body: JSON.stringify({path, directory, name})});
    await browse(fileState.path || directory);
    if (openCopied && data.kind !== 'dir') openFile(data.path);
    flash('Copied');
    return data;
  } catch (err) {
    flash('Copy failed: ' + errText(err));
    return null;
  }
}

async function duplicateEntry(path, kind) {
  const directory = path.split('/').slice(0, -1).join('/') || '/';
  const old = baseName(path);
  const name = await askName('Duplicate', old,
                             'Pick a new name. The copy is created only after this.',
                             'Duplicate');
  if (name === null) return;
  const trimmed = name.trim();
  if (!trimmed || trimmed === old) {
    flash('Choose a different name for the duplicate.');
    return;
  }
  await copyEntryAs(path, directory, trimmed, kind !== 'dir');
}

async function pasteEntry(directory) {
  if (!fileClipboard) return;
  if (fileClipboard.mode === 'cut') {
    const sourceDir = fileClipboard.path.split('/').slice(0, -1).join('/') || '/';
    if (sourceDir === directory) {
      flash('Already in this folder');
      return;
    }
    await moveEntryTo(fileClipboard.path, directory, true);
    return;
  }
  let name = baseName(fileClipboard.path);
  const sourceDir = fileClipboard.path.split('/').slice(0, -1).join('/') || '/';
  if (sourceDir === directory) name = copyNameFor(fileClipboard.path);
  await copyEntryAs(fileClipboard.path, directory, name, false);
}

async function moveEntryTo(path, directory, fromClipboard) {
  try {
    const data = await api('/api/files/move', {method: 'POST',
      body: JSON.stringify({path, directory})});
    if (fromClipboard) fileClipboard = null;
    await browse(fileState.path || directory);
    const t = tab(path);
    if (t && data.kind !== 'dir') {
      t.path = data.path;
      t.name = baseName(data.path);
      if (active === path) {
        active = data.path;
        showActive();
      }
      renderTabs();
    }
    flash('Moved');
  } catch (err) {
    flash('Move failed: ' + errText(err));
  }
}

function fileDragStart(e, path, kind) {
  closeFileCtx();
  fileDragPayload = {path, kind};
  e.dataTransfer.effectAllowed = 'move';
  e.dataTransfer.setData('application/x-gusnotebook-file',
                         JSON.stringify(fileDragPayload));
  e.dataTransfer.setData('text/plain', path);
}

function fileDragOver(e, kind) {
  if (kind !== 'dir') return;
  e.preventDefault();
  e.currentTarget.classList.add('drop');
  e.dataTransfer.dropEffect = 'move';
}

function fileDragLeave(e) {
  e.currentTarget.classList.remove('drop');
}

async function fileDrop(e, target, kind) {
  if (kind !== 'dir') return;
  e.preventDefault();
  e.stopPropagation();
  e.currentTarget.classList.remove('drop');
  let data;
  try {
    data = JSON.parse(e.dataTransfer.getData('application/x-gusnotebook-file') || '{}');
  } catch (_) { data = {}; }
  if (!data.path && fileDragPayload) data = fileDragPayload;
  if (!data.path) {
    const plain = e.dataTransfer.getData('text/plain');
    if (plain) data = {path: plain, kind: 'file'};
  }
  if (!data.path || data.path === target) return;
  try {
    await moveEntryTo(data.path, target, false);
  } catch (err) {
    flash('Move failed: ' + errText(err));
  } finally {
    fileDragPayload = null;
  }
}

async function renameEntry(path) {
  const notebook = tab(path);
  if (notebook?.kind === 'notebook') return renameNotebook(notebook);
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

function chooseUploads() {
  if (!fileState.path) { flash('The file panel has no directory open yet.'); return; }
  const input = document.getElementById('file-upload');
  input.value = '';
  input.click();
}

async function uploadFiles(fileList) {
  const selectedFiles = Array.from(fileList || []);
  if (!selectedFiles.length || !fileState.path) return;
  const directory = fileState.path;
  const form = new FormData();
  form.append('directory', directory);
  selectedFiles.forEach(file => form.append('files', file, file.name));
  const button = document.getElementById('file-upload-btn');
  button.disabled = true;
  button.classList.add('on');
  try {
    const response = await fetch(BASE + '/api/files/upload', {
      method: 'POST', headers: {
        'X-Client-Id': CLIENT_ID,
        ...(currentSession ? {'X-Session-Id': currentSession} : {}),
      }, body: form,
    });
    if (!response.ok) throw new Error(await response.text());
    const data = await response.json();
    await browse(directory);
    const n = data.uploaded.length;
    flash(`${n} file${n === 1 ? '' : 's'} uploaded`);
  } catch (err) {
    flash('Upload failed: ' + errText(err));
  } finally {
    button.disabled = false;
    button.classList.remove('on');
    document.getElementById('file-upload').value = '';
  }
}

function downloadEntry(path) {
  const link = document.createElement('a');
  link.href = BASE + '/api/files/download?' + new URLSearchParams({path});
  link.download = path.split('/').pop();
  link.hidden = true;
  document.body.appendChild(link);
  link.click();
  link.remove();
}

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

function toggleFiles() { togglePanel('files'); }

// ---------- Tab bar ----------
const TAB_ICON = {notebook: 'notebook', text: 'text', markup: 'code', image: 'image'};

function tabIcon(t) {
  return icon(TAB_ICON[isMarkupTab(t) ? 'markup' : t.kind] || 'file');
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
function rememberNotebookView(t) {
  if (!t || t.kind !== 'notebook') return;
  const state = {
    codeOpen: new Set(t.codeOpen || codeOpen),
    outsHidden: new Set(t.outsHidden || outsHidden),
    headingsCollapsed: new Set(t.headingsCollapsed || headingsCollapsed),
  };
  notebookViewState.set(t.path, state);
  try {
    sessionStorage.setItem('gusnotebook:view:' + t.path, JSON.stringify({
      codeOpen: [...state.codeOpen],
      outsHidden: [...state.outsHidden],
      headingsCollapsed: [...state.headingsCollapsed],
    }));
  } catch (e) {}
}

function restoreNotebookView(t) {
  if (!t || t.kind !== 'notebook') return;
  let saved = notebookViewState.get(t.path);
  if (!saved) {
    try {
      const raw = sessionStorage.getItem('gusnotebook:view:' + t.path);
      if (raw) {
        const parsed = JSON.parse(raw);
        saved = {
          codeOpen: new Set(parsed.codeOpen || []),
          outsHidden: new Set(parsed.outsHidden || []),
          headingsCollapsed: new Set(parsed.headingsCollapsed || []),
        };
        notebookViewState.set(t.path, saved);
      }
    } catch (e) {}
  }
  if (!saved) return;
  t.codeOpen = new Set(saved.codeOpen || []);
  t.outsHidden = new Set(saved.outsHidden || []);
  t.headingsCollapsed = new Set(saved.headingsCollapsed || []);
}

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
    rememberNotebookView(t);
  } else if (t.kind === 'text' && !isMarkupTab(t)) {
    t.text = document.getElementById('text-editor').value;
  } else if (isMarkupTab(t)) {
    requestMarkupViewState(t);
  }
}

function renderTabs() {
  const strip = document.getElementById('tabs');
  const previousActive = strip.querySelector('.tab.active')?.dataset.path;
  const scroll = strip.scrollLeft;
  for (const row of document.querySelectorAll('.file-row[data-path]')) {
    row.classList.toggle('active-file', row.dataset.path === active);
    row.classList.toggle('open', !!tab(row.dataset.path));
  }
  document.getElementById('tabs').innerHTML = tabs.map(t => `
    <div role="tab" aria-selected="${t.path === active}" tabindex="${t.path === active ? 0 : -1}" data-path="${escapeAttr(t.path)}" class="tab ${t.path === active ? 'active' : ''} ${t.dirty ? 'dirty' : ''}"
         onclick="switchTab(this.dataset.path)"
         oncontextmenu="tabFileCtx(event,this.dataset.path)"
         draggable="true"
         ondragstart="tabDragStart(event,this.dataset.path)"
         ondragover="tabDragOver(event,this.dataset.path)"
         ondragleave="tabDragLeave(event)"
         ondrop="tabDrop(event,this.dataset.path)"
         ondragend="tabDragEnd(event)"
         title="${escapeAttr(t.path)}">
      <span class="ti">${tabIcon(t)}</span>
      <span class="tn">${escapeHtml(t.name)}</span>
      <span class="tx" role="button" tabindex="0" aria-label="Close ${escapeAttr(t.name)}" onclick="closeTab(this.closest('.tab').dataset.path, event)">${icon('close')}</span>
    </div>`).join('') +
    // Always last, so "new" sits where the next tab would appear.
    `<button class="tab-new" id="tab-new" aria-label="Create new" aria-haspopup="menu" aria-expanded="false" aria-controls="new-menu" onclick="toggleNewMenu(event)"
          title="New notebook, file, folder, environment, agent or terminal">${icon('plus')}</button>`;
  strip.scrollLeft = scroll;
  if (previousActive !== active) revealActiveTab();
}

function revealActiveTab() {
  const strip = document.getElementById('tabs');
  const chosen = strip.querySelector('.tab.active');
  if (!chosen) return;
  const box = strip.getBoundingClientRect(), tabBox = chosen.getBoundingClientRect();
  const right = box.right - (document.getElementById('tab-new')?.offsetWidth || 0);
  if (tabBox.left < box.left) strip.scrollLeft -= box.left - tabBox.left;
  else if (tabBox.right > right) strip.scrollLeft += tabBox.right - right;
}
new ResizeObserver(revealActiveTab).observe(document.getElementById('tabs'));

let tabDragPath = null;

function tabDragStart(event, path) {
  tabDragPath = path;
  event.dataTransfer.effectAllowed = 'move';
  event.dataTransfer.setData('text/plain', path);
  event.currentTarget.classList.add('dragging');
}

function tabDragOver(event, path) {
  if (!tabDragPath || tabDragPath === path) return;
  event.preventDefault();
  event.dataTransfer.dropEffect = 'move';
  document.querySelectorAll('.tab.drop-left,.tab.drop-right')
    .forEach(el => el.classList.remove('drop-left', 'drop-right'));
  const rect = event.currentTarget.getBoundingClientRect();
  event.currentTarget.classList.add(
    event.clientX < rect.left + rect.width / 2 ? 'drop-left' : 'drop-right');
}

function tabDragLeave(event) {
  event.currentTarget.classList.remove('drop-left', 'drop-right');
}

function tabDragEnd(event) {
  tabDragPath = null;
  event.currentTarget.classList.remove('dragging');
  document.querySelectorAll('.tab.drop-left,.tab.drop-right')
    .forEach(el => el.classList.remove('drop-left', 'drop-right'));
}

function tabDrop(event, path) {
  if (!tabDragPath || tabDragPath === path) return;
  event.preventDefault();
  const rect = event.currentTarget.getBoundingClientRect();
  reorderTab(tabDragPath, path, event.clientX >= rect.left + rect.width / 2);
  tabDragEnd(event);
}

function reorderTab(fromPath, toPath, after) {
  const from = tabs.findIndex(t => t.path === fromPath);
  const to = tabs.findIndex(t => t.path === toPath);
  if (from < 0 || to < 0 || from === to) return;
  const [moved] = tabs.splice(from, 1);
  let insert = tabs.findIndex(t => t.path === toPath);
  if (after) insert += 1;
  tabs.splice(insert, 0, moved);
  renderTabs();
  if (currentSession) {
    api('/api/sessions/' + encodeURIComponent(currentSession), {method: 'POST',
      body: JSON.stringify({tabs: tabs.map(t => t.path), active})}).catch(() => {});
  }
}

// ---------- The + tab's menu ----------

function closeNewMenu() {
  document.getElementById('new-menu').classList.remove('on');
  const plus = document.getElementById('tab-new');
  if (plus) {
    plus.classList.remove('on'); plus.setAttribute('aria-expanded', 'false');
    if (document.getElementById('new-menu').contains(document.activeElement)) plus.focus();
  }
}

function toggleNewMenu(ev) {
  closeWorkspaceMenu();
  if (ev) ev.stopPropagation();
  const menu = document.getElementById('new-menu');
  if (menu.classList.contains('on')) { closeNewMenu(); return; }
  // Anchored under the + tab's left edge, since the menu is wider than the tab.
  const r = document.getElementById('tab-new').getBoundingClientRect();
  menu.style.top = (r.bottom + 4) + 'px';
  menu.style.left = Math.min(r.left, window.innerWidth - 270) + 'px';
  menu.classList.add('on');
  document.getElementById('tab-new').classList.add('on');
  document.getElementById('tab-new').setAttribute('aria-expanded', 'true');
  menu.style.left = Math.max(8, Math.min(r.left, innerWidth - menu.offsetWidth - 8)) + 'px';
  menu.style.top = Math.max(8, Math.min(r.bottom + 4, innerHeight - menu.offsetHeight - 8)) + 'px';
  menu.querySelector('[role=menuitem]').focus();
}

/** One entry from the + menu. Everything lands in the browsed directory. */
function newFromMenu(what) {
  closeNewMenu();
  if (what === 'notebook') return newNotebook();
  if (what === 'text') return newFile();
  if (what === 'folder') return newFolder();
  if (what === 'environment') return openEnvironments();
  if (what === 'claude') return openTerminalHere('claude');
  if (what === 'codex') return openTerminalHere('codex');
  if (what === 'terminal') return openTerminalHere('shell');
}

document.addEventListener('click', (e) => {
  if (!e.target.closest('#new-menu') && !e.target.closest('#tab-new')) closeNewMenu();
  if (!e.target.closest('#file-ctx')) closeFileCtx();
});

// Secondary workspace controls share the tab row without taking notebook space.
function closeWorkspaceMenu() {
  const menu = document.getElementById('workspace-menu');
  if (menu.contains(document.activeElement)) document.getElementById('workspace-more').focus();
  menu.hidden = true;
  document.getElementById('workspace-more').setAttribute('aria-expanded', 'false');
}
function toggleWorkspaceMenu(event) {
  event?.stopPropagation();
  const menu = document.getElementById('workspace-menu');
  if (!menu.hidden) { closeWorkspaceMenu(); return; }
  closeNewMenu(); closeFileCtx(); closeTypeMenu(); closeVenvMenu();
  const button = document.getElementById('workspace-more');
  menu.hidden = false;
  button.setAttribute('aria-expanded', 'true');
  const box = button.getBoundingClientRect();
  menu.style.top = (box.bottom + 5) + 'px';
  menu.style.right = Math.max(6, innerWidth - box.right) + 'px';
  menu.querySelector('button').focus();
}
document.addEventListener('click', event => {
  if (!event.target.closest('#workspace-menu, #workspace-more')) closeWorkspaceMenu();
});
document.addEventListener('focusin', event => {
  if (!event.target.closest('#workspace-menu, #workspace-more')) closeWorkspaceMenu();
});
