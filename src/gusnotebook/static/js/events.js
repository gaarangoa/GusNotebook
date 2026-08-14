/* load(), the SSE stream, and the document-level keys.
 *
 * The one file that reaches into nearly every other, because a server event
 * can touch any part of the page. Kept separate for exactly that reason: it's
 * where to look when something repaints and you don't know why. */

// ---------- Load + live events ----------
/** Reload the active notebook tab's cells from the server. */
async function load() {
  if (!isNotebookTab() || !active) return;
  const key = active;
  const data = await api('/api/notebook' + nbq());
  if (key !== active) return;          // the user switched tabs mid-request

  const t = activeTab();
  cells = data.cells;
  if (t) {
    t.cells = cells;
    if (data.kernel_python) t.python = data.kernel_python;
    if (data.kernel_status) t.status = data.kernel_status;
  }
  if (data.kernel_status) setKernelStatus(data.kernel_status);
  refreshVenvBtn();

  // Preserve the caret if the user is mid-edit when a reload lands. With
  // CodeMirror the focused element is its own content div, not `#ed-<id>`, so
  // the id comes from the enclosing host — otherwise every reload during typing
  // would silently drop the caret.
  const focused = document.activeElement;
  const holder = focused && focused.closest
    ? focused.closest('.editor, .ed-host') : null;
  const focusId = holder
    ? (holder.dataset.cell || holder.id.slice(3)) : null;
  const caret = focusId ? [holder.selectionStart, holder.selectionEnd] : null;

  render();

  if (focusId) {
    const ed = document.getElementById('ed-' + focusId);
    if (ed) {
      ed.focus();
      try { ed.setSelectionRange(caret[0], caret[1]); } catch (e) {}
    }
  }
}

const es = new EventSource(BASE + '/events');
es.onmessage = (e) => {
  const msg = JSON.parse(e.data);

  // Every event names the notebook it came from. Record it on that tab, but
  // only touch the DOM when it's the tab on screen.
  // A session's Claude exited — strike its tab through, keep the scrollback.
  if (msg.type === 'terminal_closed') {
    const s = findTerm(msg.terminal);
    if (s) { s.alive = false; renderTermTabs(); }
    return;
  }
  if (msg.type === 'terminal_opened') return;   // we already have it locally
  // Another view (or nb.py) touched the session list — repaint the counts, but
  // don't switch this page's workspace out from under whoever is typing.
  if (msg.type === 'sessions_changed') { loadSessions(); return; }
  // A skill can be added by editing the markdown outside the app, so the list
  // follows the server rather than only what this page did.
  if (msg.type === 'skills_changed') { loadSkills(); return; }
  if (msg.type === 'text_external_changed') {
    const changed = tab(msg.path);
    if (changed && changed.kind === 'text') markTextExternalConflict(changed);
    return;
  }
  // An agent replaced the exact HTML/SVG range selected in the visual editor.
  // The server already saved it; repaint from that authoritative text without
  // marking the tab dirty or exposing the iframe's DOM to the parent page.
  if (msg.type === 'markup_changed') {
    const visual = tab(msg.path);
    if (!visual || !isMarkupTab(visual)) return;
    visual.text = msg.text;
    visual.diskVersion = msg.disk_version;
    visual.editRevision = (visual.editRevision || 0) + 1;
    visual.dirty = false;
    if (msg.path === active) {
      document.getElementById('text-editor').value = msg.text;
      document.getElementById('text-status').textContent = 'saved by agent';
      renderMarkupEditor();
    }
    renderTabs();
    return;
  }

  const t = msg.notebook ? tab(msg.notebook) : null;
  if (t && msg.type === 'kernel_status') {
    t.status = msg.status;
    if (msg.python) t.python = msg.python;
    if (msg.notebook === active) refreshVenvBtn();
  }
  // A kernel started or died, here or in another session — the "2k live" counts
  // are the only place that's visible, so keep them honest.
  if (msg.type === 'kernel_status') refreshSessionCounts();

  // For inactive tabs, keep the stashed cells[] up to date so switching back
  // shows the correct output without needing a load(). DOM events are skipped.
  if (msg.notebook && msg.notebook !== active) {
    if (t && t.cells) {
      if (msg.type === 'cell_output' || msg.type === 'cell_done') {
        const c = t.cells.find(c => c.id === msg.cell_id);
        if (c) {
          c.outputs = msg.outputs;
          if (msg.type === 'cell_done') c.execution_count = msg.execution_count;
        }
      }
    }
    return;
  }

  if (msg.type === 'kernel_status') {
    setKernelStatus(msg.status);
  } else if (msg.type === 'cell_output') {
    const el = document.getElementById('out-' + msg.cell_id);
    if (el) { el.innerHTML = renderOutputs(msg.outputs, msg.cell_id); pinStreams(el); }
    // Keep `cells[]` in step mid-run: the ▾ and the hidden-count are drawn from
    // it, and during a long run this is the only thing that knows there's output.
    const c = getCell(msg.cell_id);
    if (c) c.outputs = msg.outputs;
    syncOutputView(msg.cell_id);
  } else if (msg.type === 'cell_running') {
    const el = document.getElementById('out-' + msg.cell_id);
    if (el) { el.innerHTML = '<div class="spin">running…</div>'; showRunning(msg.cell_id); }
    const help = document.getElementById('help-' + msg.cell_id);
    if (help) help.innerHTML = '';        // stale advice on a re-run
  } else if (msg.type === 'cell_done') {
    const c = getCell(msg.cell_id);
    if (c) { c.outputs = msg.outputs; c.execution_count = msg.execution_count; }
    const el = document.getElementById('out-' + msg.cell_id);
    if (el) { el.innerHTML = renderOutputs(msg.outputs, msg.cell_id); pinStreams(el); }
    syncOutputView(msg.cell_id);
    const cellEl = document.querySelector(`.cell[data-id="${msg.cell_id}"] .gutter-label`);
    if (cellEl) cellEl.textContent = `[${msg.execution_count == null ? ' ' : msg.execution_count}]`;
  } else if (msg.type === 'notebook_changed') {
    // Our own write, coming back to us. Every path that mutates from this page
    // (typing, add, delete, move, undo, the AI cell) reloads locally already,
    // so reloading again here is pure waste — and it isn't cheap: load() +
    // render() rebuilds every cell's DOM and re-measures every editor, which
    // on a long notebook is a visible stall on each typing pause. The echo is
    // what made editing feel frozen.
    if (msg.origin === CLIENT_ID) return;

    // Claude added/edited cells (via nb.py or by editing notebook.ipynb).
    // If the user is parked at the bottom, follow the new cell; otherwise
    // leave their scroll position alone.
    const pane = document.getElementById('notebook-pane');
    const atBottom = pane.scrollHeight - pane.scrollTop - pane.clientHeight < 120;
    load().then(() => {
      if (msg.reason === 'add' && msg.cell_id) scrollToCell(msg.cell_id, 'center');
      else if (atBottom) pane.scrollTop = pane.scrollHeight;
    });
  }
};

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && document.activeElement === document.body) selected = null;
  // ⇧⏎ keeps working when focus sits outside an editor — e.g. we just advanced
  // onto a rendered markdown cell, which has no textarea to focus. Never steal
  // the key from a cell editor (it handles its own) or from the terminal.
  const a = document.activeElement;
  // `.cm-content` as well as `.editor`: with CodeMirror mounted the focused
  // element is CM's own content div, and treating that as unclaimed would run
  // the cell twice — once from CM's keymap and once from here.
  const claimed = a && (a.classList.contains('editor') ||
                        a.closest('.cm-editor') ||
                        a.closest('#terminal-stack'));
  if (e.key === 'Enter' && e.shiftKey && !claimed && selected && isNotebookTab()) {
    e.preventDefault();
    runCell(selected, true);
  }
  // ⌘S on a text tab saves it even if focus has drifted off the textarea.
  if (e.key === 's' && (e.metaKey || e.ctrlKey) && !isNotebookTab()) {
    e.preventDefault();
    saveText();
  }
});

// Closing the browser with unsaved text tabs shouldn't lose the edit silently.
window.addEventListener('beforeunload', (e) => {
  stashActive();
  if (tabs.some(t => t.dirty)) { e.preventDefault(); e.returnValue = ''; }
});
