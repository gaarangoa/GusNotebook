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
    <div role="tab" tabindex="${t.id === activeTerm ? 0 : -1}" aria-selected="${t.id === activeTerm}" class="tterm ${t.id === activeTerm ? 'active' : ''} ${t.alive ? '' : 'dead'}"
         onclick="focusTerm('${t.id}')" title="${escapeAttr(t.cwd)}">
      <span class="ti">${icon(t.kind === 'shell' ? 'terminal' : (t.kind === 'codex' ? 'code' : 'agent'))}</span>
      <span class="tn">${escapeHtml(t.label)}</span>
      <span class="tx" role="button" tabindex="0" aria-label="Close ${escapeAttr(t.label)} terminal" onclick="closeTerminal('${t.id}', event)">${icon('close')}</span>
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
  ensurePanel('terminal');
  const t = attachTerm(data);
  loadSessions();          // it belongs to this session now; show the count
  return t;
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
    fontSize: AppAppearance.get().fontSize,
    fontFamily: 'Menlo, Consolas, monospace',
    theme: AppAppearance.terminalTheme(),
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

function activeAgentTerm() {
  const t = findTerm(activeTerm);
  return t && (t.kind === 'claude' || t.kind === 'codex') ? t : null;
}

function waitForTermSocket(t) {
  if (!t || !t.ws) return Promise.reject(new Error('No agent terminal is open'));
  if (t.ws.readyState === WebSocket.OPEN) return Promise.resolve(t);
  return new Promise((resolve, reject) => {
    const done = () => {
      cleanup();
      resolve(t);
    };
    const fail = () => {
      cleanup();
      reject(new Error('Agent terminal is not connected'));
    };
    const cleanup = () => {
      clearTimeout(timer);
      t.ws.removeEventListener('open', done);
      t.ws.removeEventListener('close', fail);
      t.ws.removeEventListener('error', fail);
    };
    const timer = setTimeout(fail, 8000);
    t.ws.addEventListener('open', done);
    t.ws.addEventListener('close', fail);
    t.ws.addEventListener('error', fail);
  });
}

async function sendPromptToAgent(text) {
  let t = activeAgentTerm();
  if (!t) {
    const kind = document.getElementById('agent-kind').value || 'claude';
    t = await openTerminal(null, kind);
  }
  await waitForTermSocket(t);
  focusTerm(t.id);
  t.ws.send('\x1b[200~' + text + '\x1b[201~\r');
  return t;
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
  if (!t || document.getElementById('agent-pane').inert) return;
  try { t.fit.fit(); } catch (e) { return; }
  if (t.ws && t.ws.readyState === WebSocket.OPEN) {
    t.ws.send(JSON.stringify({type: 'resize', cols: t.term.cols, rows: t.term.rows}));
  }
}
window.addEventListener('resize', fitTerm);
restoreAgentKind();
if (booted) bootTerminals();
else window.addEventListener('workspace-ready', bootTerminals, {once: true});

document.addEventListener('appearance-change', () => {
  for (const t of terms) {
    t.term.options.theme = AppAppearance.terminalTheme();
    t.term.options.fontSize = AppAppearance.get().fontSize;
  }
  scheduleTerminalFit();
});
