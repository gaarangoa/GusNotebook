/* The Claude Code / Codex / shell terminals, the column layout, and splitter.
 *
 * Last, because bootTerminals() runs on parse like boot() does, and the
 * splitter's mousedown handler needs #splitter to already be in the document. */

/* ---------- Agent terminals ----------
 * Several sessions, each an xterm bound to a server-side PTY rooted in a
 * directory of its choosing. None opens by default. Sessions outlive the page:
 * the server keeps draining the PTY, so a reload reattaches to a running
 * Claude rather than starting a new one. */
let terms = [];            // {id, cwd, label, alive, term, fit, ws, host}
let activeTerm = null;

const AGENT_KIND_KEY = 'gusnotebook-agent-kind';

function rememberAgentKind() {
  const kind = document.getElementById('agent-kind').value;
  try { localStorage.setItem(AGENT_KIND_KEY, kind); } catch (e) {}
}

function restoreAgentKind() {
  let kind = 'claude';
  try { kind = localStorage.getItem(AGENT_KIND_KEY) || kind; } catch (e) {}
  if (kind === 'claude' || kind === 'codex') {
    document.getElementById('agent-kind').value = kind;
  }
}

function openSelectedAgent() {
  openTerminal(null, document.getElementById('agent-kind').value);
}

const statusEl = document.getElementById('ws-status');
const wsProto = location.protocol === 'https:' ? 'wss:' : 'ws:';

function findTerm(id) { return terms.find(t => t.id === id); }

function setTermStatus() {
  const t = findTerm(activeTerm);
  if (!t) {
    statusEl.textContent = terms.length ? `${terms.length} session(s)` : 'No session';
    statusEl.className = 'status disconnected';
    return;
  }
  const live = t.alive && t.ws && t.ws.readyState === WebSocket.OPEN;
  statusEl.textContent = live ? 'Connected' : (t.alive ? 'Connecting…' : 'Exited');
  statusEl.className = 'status ' + (live ? 'connected' : 'disconnected');
}

function renderTermTabs() {
  document.getElementById('term-tabs').innerHTML = terms.map(t => `
    <div class="tterm ${t.id === activeTerm ? 'active' : ''} ${t.alive ? '' : 'dead'}"
         onclick="focusTerm('${t.id}')" title="${escapeAttr(t.cwd)}">
      <span class="ti">${t.kind === 'shell' ? '❯' : (t.kind === 'codex' ? 'C' : '✳')}</span>
      <span class="tn">${escapeHtml(t.label)}</span>
      <span class="tx" onclick="closeTerminal('${t.id}', event)">✕</span>
    </div>`).join('');
  document.getElementById('term-empty').classList.toggle('off', terms.length > 0);
  setTermStatus();
}

/** Open a session in the requested directory, or wherever Files is browsing.
 * `kind` is "shell", "codex", or "claude". */
async function openTerminal(cwd, kind) {
  const root = cwd || fileState.path;
  let data;
  try {
    data = await api('/api/terminals', {
      method: 'POST',
      body: JSON.stringify({...(root ? {cwd: root} : {}), kind}),
    });
  } catch (err) {
    flash('Cannot open a terminal: ' + errText(err));
    return;
  }
  attachTerm(data);
  loadSessions();          // it belongs to this session now; show the count
}

/** From the + menu — rooted in the folder you're browsing. */
function openTerminalHere(kind) {
  if (!fileState.path) { flash('The file panel has no directory open yet.'); return; }
  openTerminal(fileState.path, kind);
}

/** Build the xterm for a session and wire it to its PTY. */
function attachTerm(info) {
  const host = document.createElement('div');
  host.className = 'term-host';
  host.id = 'term-host-' + info.id;
  document.getElementById('terminal-stack').appendChild(host);

  const term = new window.Terminal({
    cursorBlink: true,
    fontSize: 12,
    fontFamily: 'JetBrains Mono, Menlo, Monaco, monospace',
    // Light, to match the notebook pane beside it. The ANSI colours can't just
    // be the defaults: those are chosen for a dark background, and yellow or
    // bright-white on white is invisible. These are darkened to keep Claude's
    // own colouring legible, and `brightWhite` is deliberately a grey.
    theme: {
      background: '#ffffff', foreground: '#1e293b',
      cursor: '#830051', cursorAccent: '#ffffff',
      selectionBackground: '#fbcfe8', selectionForeground: '#1e293b',
      black: '#1e293b', red: '#b91c1c', green: '#15803d', yellow: '#a16207',
      blue: '#1d4ed8', magenta: '#a21caf', cyan: '#0e7490', white: '#64748b',
      brightBlack: '#94a3b8', brightRed: '#dc2626', brightGreen: '#16a34a',
      brightYellow: '#ca8a04', brightBlue: '#2563eb', brightMagenta: '#c026d3',
      brightCyan: '#0891b2', brightWhite: '#334155',
    },
  });
  const fit = new window.FitAddon.FitAddon();
  term.loadAddon(fit);
  term.loadAddon(new window.WebLinksAddon.WebLinksAddon());
  term.open(host);

  const t = {...info, term, fit, host, ws: null};
  terms.push(t);

  const wsQuery = currentSession
    ? '?' + new URLSearchParams({session: currentSession}) : '';
  const ws = new WebSocket(
    wsProto + '//' + location.host + BASE + '/ws/' + info.id + wsQuery);
  ws.binaryType = 'arraybuffer';
  t.ws = ws;
  ws.onopen = () => {
    setTermStatus();
    fitTerm();
  };
  ws.onmessage = (ev) => term.write(
    ev.data instanceof ArrayBuffer ? new Uint8Array(ev.data) : ev.data);
  ws.onclose = () => { setTermStatus(); };
  ws.onerror = () => { setTermStatus(); };
  term.onData(d => { if (ws.readyState === WebSocket.OPEN) ws.send(d); });

  focusTerm(info.id);
  return t;
}

function focusTerm(id) {
  const t = findTerm(id);
  if (!t) return;
  activeTerm = id;
  for (const other of terms) other.host.classList.toggle('on', other.id === id);
  renderTermTabs();
  fitTerm();
  t.term.focus();
}

/** Close a session for good — this one does kill the Claude running in it. */
async function closeTerminal(id, ev) {
  if (ev) ev.stopPropagation();
  const i = terms.findIndex(t => t.id === id);
  if (i === -1) return;
  const t = terms[i];
  try { if (t.ws) t.ws.close(); } catch (e) {}
  t.term.dispose();
  t.host.remove();
  terms.splice(i, 1);
  try {
    await api('/api/terminals/' + id, {method: 'DELETE'});
  } catch (err) { /* gone either way */ }
  if (activeTerm === id) {
    const next = terms[Math.min(i, terms.length - 1)];
    activeTerm = next ? next.id : null;
    if (next) focusTerm(next.id); else renderTermTabs();
  } else {
    renderTermTabs();
  }
  loadSessions();
}

/** Reattach to sessions that were already running (page reload). */
async function bootTerminals() {
  try {
    // ?session=mine: another session's Claude keeps running server-side, but it
    // isn't this page's terminal and shouldn't appear in its tab strip.
    const data = await api('/api/terminals?session=mine');
    for (const info of data.terminals || []) attachTerm(info);
  } catch (err) { /* no terminals is the normal state */ }
  renderTermTabs();
}

/** Resize the visible terminal to its host box. */
function fitTerm() {
  const t = findTerm(activeTerm);
  if (!t) return;
  try { t.fit.fit(); } catch (e) { return; }
  if (t.ws && t.ws.readyState === WebSocket.OPEN) {
    t.ws.send(JSON.stringify({type: 'resize', cols: t.term.cols, rows: t.term.rows}));
  }
}
window.addEventListener('resize', fitTerm);
restoreAgentKind();
if (booted) bootTerminals();
else window.addEventListener('workspace-ready', bootTerminals, {once: true});

// ---------- Layout (file column | notebook | splitter | terminal) ----------
const FILES_WIDTH = 230;
let termWidth = 440;

function filesVisible() {
  return !document.getElementById('app').classList.contains('files-hidden');
}

function applyLayout() {
  document.getElementById('app').style.gridTemplateColumns =
    `${filesVisible() ? FILES_WIDTH : 0}px 1fr 6px ${termWidth}px`;
}

// ---------- Splitter ----------
const splitter = document.getElementById('splitter');
let dragging = false;
splitter.addEventListener('mousedown', () => {
  dragging = true;
  splitter.classList.add('dragging');
  document.body.style.userSelect = 'none';
});
window.addEventListener('mousemove', (e) => {
  if (!dragging) return;
  const filesW = filesVisible() ? FILES_WIDTH : 0;
  termWidth = Math.min(Math.max(window.innerWidth - e.clientX, 280),
                       window.innerWidth - 360 - filesW);
  applyLayout();
});
window.addEventListener('mouseup', () => {
  if (!dragging) return;
  dragging = false;
  splitter.classList.remove('dragging');
  document.body.style.userSelect = '';
  fitTerm();
});
