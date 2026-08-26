/* Page identity, the tab model, and the fetch wrapper everything else uses.
 *
 * First in the load order because every other file reads `BASE`, `tabs`,
 * `active` or `api` — a classic script's top-level `const` lands in the global
 * lexical scope, so later files see it, but only once this one has run.
 *
 * `BASE` is the one thing these files can't define: it comes from the template's
 * BASE_URL, and a static file is served rather than rendered, so index.html
 * declares it in a one-line inline block ahead of this one. That indirection is
 * the price of the split, and it's cheap — one line, in the only file Jinja
 * touches at all. */

/* ---------- Tabs ----------
 * One entry per open file. Notebook tabs own their own cells/selection so
 * switching tabs doesn't lose your place; text tabs own their buffer.
 * `active` is the path of the tab on screen; `cells`/`selected`/`editing`
 * always mirror the active notebook tab. */
let tabs = [];
let active = null;

let cells = [];
let selected = null;
let editing = new Set();     // markdown cells currently in edit mode

/* What's folded, per cell. View preferences, held here rather than in the
 * .ipynb: how much of a cell you happen to be looking at is not part of the
 * notebook, and writing it to the file would put it in every diff.
 *
 * The two default opposite ways round, because the thing being hidden differs.
 * A long code cell arrives folded — that's the ask, and code you scroll past is
 * usually code you're not reading — so the set records which ones you opened.
 * Output arrives shown, since it's the answer you just asked for, so the set
 * records which ones you shut.
 *
 * Per tab, like `editing`: cell ids are unique within a notebook, not across
 * them, so one set for every tab would eventually fold a cell in a notebook
 * nobody touched. */
let codeOpen = new Set();           // long cells you've expanded
let outsHidden = new Set();         // cells whose output you've collapsed
let headingsCollapsed = new Set();  // heading cell ids whose section is collapsed
const notebookViewState = new Map(); // path -> fold/output/section view state
let pendingCellFocus = null;         // exact cell to focus after the next render

/* The workspace belongs to this browser window, not to the server process.
 * Keeping it in the URL means a reload—and a second window—return to the same
 * session independently. The request header is what makes every API call use
 * that identity even if another window switches the persisted default. */
let currentSession = new URLSearchParams(location.search).get('session');

function setCurrentSession(sid) {
  currentSession = sid || null;
  const url = new URL(location.href);
  if (currentSession) url.searchParams.set('session', currentSession);
  else url.searchParams.delete('session');
  history.replaceState(null, '', url);
}

// Where a code cell starts being long enough to fold, and how much stays visible
// while folded. Display preferences are local to this browser window; they do
// not belong in the notebook file.
const FOLD_PREF_KEY = 'gusnotebook.foldPreferences';
let codeFoldLines = 10;
let foldOpenOnFocus = false;

function readFoldPreferences() {
  try {
    const p = JSON.parse(localStorage.getItem(FOLD_PREF_KEY) || '{}');
    const lines = parseInt(p.preview_lines, 10);
    codeFoldLines = Number.isFinite(lines) ? Math.min(60, Math.max(1, lines)) : 10;
    foldOpenOnFocus = !!p.open_on_focus;
  } catch (err) {
    codeFoldLines = 10;
    foldOpenOnFocus = false;
  }
}

function writeFoldPreferences() {
  try {
    localStorage.setItem(FOLD_PREF_KEY, JSON.stringify({
      preview_lines: codeFoldLines,
      open_on_focus: foldOpenOnFocus,
    }));
  } catch (err) {}
}

readFoldPreferences();

function tab(path) { return tabs.find(t => t.path === path); }
function activeTab() { return tab(active); }
function isNotebookTab() {
  const t = activeTab();
  return !t || t.kind === 'notebook';
}
/** Query string that pins a request to the active notebook. */
function nbq(extra) {
  const q = new URLSearchParams();
  if (active) q.set('notebook', active);
  for (const [k, v] of Object.entries(extra || {})) q.set(k, v);
  const s = q.toString();
  return s ? '?' + s : '';
}

/* This page's identity, for the lifetime of this load. Sent on every request so
 * the events our own writes provoke come back labelled, and we can skip
 * reloading for a change we already made locally. A new id per load is right:
 * after a reload we hold nothing, so every event is news. */
const CLIENT_ID = 'c' + Math.random().toString(36).slice(2) + Date.now().toString(36);

const api = async (path, opts = {}) => {
  const r = await fetch(BASE + path, {
    ...opts,
    headers: {'Content-Type': 'application/json',
              'X-Client-Id': CLIENT_ID,
              ...(currentSession ? {'X-Session-Id': currentSession} : {}),
              ...(opts.headers || {})},
  });
  return r.ok ? r.json() : Promise.reject(await r.text());
};

// ---------- Rendering ----------
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
