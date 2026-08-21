/* The CodeMirror cell editor, its textarea fallback, and folding.
 *
 * The fallback is the reason this can't simply be a module: with esm.sh
 * unreachable there is no CM at all, and `#ed-<id>.value` has to keep working
 * against the plain textarea underneath. */

/* ---------- The cell editor ----------
 * Two implementations behind one interface. With CodeMirror loaded, each cell
 * gets an EditorView: Python colouring and a per-cell undo history, which is
 * JupyterLab's model — history belongs to the editor, so undoing here says
 * nothing about the cell below. Without it (offline, a blocked CDN, an esm.sh
 * outage) the plain <textarea> that cellHtml() emitted stays exactly where it
 * is. That fallback is load-bearing rather than decorative: CM is ESM from a
 * CDN, and if it fails there is no editor at all, so a notebook that can't be
 * typed in is the alternative.
 *
 * Both paths answer to `#ed-<id>.value`, because eight call sites and both
 * Playwright suites read and write it. See shimHost() for how CM gets one.
 */
const cmViews = new Map();       // cell id -> EditorView, mounted cells only

/**
 * Give a mounted CM host a `value` property, so `document.getElementById('ed-X')
 * .value` keeps meaning what it always did. The alternative was rewriting every
 * caller (saveCell, runCell, generateCell, insertSkill, advanceFrom) plus both
 * suites; one shim keeps the two editors interchangeable instead.
 *
 * `focus()`, `blur()` and `setSelectionRange()` are shimmed for the same reason:
 * the callers that reach for the element reach for those too.
 */
function shimHost(host, id, view) {
  Object.defineProperty(host, 'value', {
    configurable: true,
    get: () => view.state.doc.toString(),
    set: (text) => {
      const s = String(text == null ? '' : text);
      if (s === view.state.doc.toString()) return;
      view.dispatch({changes: {from: 0, to: view.state.doc.length, insert: s}});
    },
  });
  host.focus = () => view.focus();
  host.blur = () => view.contentDOM.blur();
  host.setSelectionRange = (from, to) => {
    const n = view.state.doc.length;
    view.dispatch({selection: {anchor: Math.min(from, n), head: Math.min(to, n)}});
  };
  Object.defineProperty(host, 'selectionStart',
    {configurable: true, get: () => view.state.selection.main.from});
  Object.defineProperty(host, 'selectionEnd',
    {configurable: true, get: () => view.state.selection.main.to});
}

/** Enable/disable a cell's ↶/↷ from CM's own history depth. */
function refreshHist(id) {
  const bar = document.getElementById('hist-' + id);
  const view = cmViews.get(id);
  if (!bar || !view) return;
  const [u, r] = [CM.undoDepth(view.state), CM.redoDepth(view.state)];
  const [ub, rb] = bar.querySelectorAll('.hist-btn');
  ub.disabled = !u;
  rb.disabled = !r;
  bar.classList.toggle('on', !!(u || r));
}

function cellUndo(id) {
  const view = cmViews.get(id);
  if (!view) return;
  CM.undo(view);
  view.focus();
  queueSave(id);
}

function cellRedo(id) {
  const view = cmViews.get(id);
  if (!view) return;
  CM.redo(view);
  view.focus();
  queueSave(id);
}

/* ---------- Path completion ----------
 * Triggered by Tab when the cursor is inside a string literal that looks like
 * a path (contains / or starts with . or /). Falls through to indentMore when
 * outside a string or when no completions come back, so ordinary Tab still
 * indents. */

/** True when `pos` is inside a string node in the CM syntax tree. */
function insideString(state, pos) {
  if (!window.CM || !CM.syntaxTree) return false;
  // resolveInner finds the innermost node that covers pos — faster and more
  // correct than iterating the whole tree, and works with lezer's lazy parsing.
  const node = CM.syntaxTree(state).resolveInner(pos, -1);
  // Walk up: the node itself or any ancestor named String means we're inside one.
  let n = node;
  while (n) {
    if (n.name === 'String') return true;
    n = n.parent;
  }
  return false;
}

/**
 * The partial path the user has typed so far, or null if the cursor isn't
 * in a path-looking string.  Returns {path, from, to} where from/to mark the
 * slice of the document the accepted completion should replace.
 */
function pathContext(state) {
  const cur = state.selection.main;
  if (!insideString(state, cur.head)) return null;
  const line = state.doc.lineAt(cur.head);
  const txt  = line.text;
  // Scan left from the cursor position to the nearest opening quote.
  let start = cur.head - line.from;
  while (start > 0 && txt[start - 1] !== '"' && txt[start - 1] !== "'") start--;
  const typed = txt.slice(start, cur.head - line.from);
  // Only activate when the text looks like a path.
  if (!typed.includes('/') && !typed.startsWith('.') && !typed.startsWith('/')) return null;
  return {path: typed, from: line.from + start, to: cur.head};
}

/**
 * CM completion source — fetches matching filesystem entries from the server
 * and returns them as CM completions.
 */
async function pathCompletionSource(context) {
  const pc = pathContext(context.state);
  if (!pc) return null;
  const nb = active || '';
  try {
    const data = await api('/api/completions?' +
      new URLSearchParams({path: pc.path, base: nb}));
    if (!data.completions.length) return null;
    return {
      from: pc.from,
      to:   pc.to,
      options: data.completions.map(c => ({
        label:  c.label,
        type:   c.type === 'dir' ? 'namespace' : 'variable',
        boost:  c.type === 'dir' ? 1 : 0,
      })),
      validFor: /^[^"']*$/,
    };
  } catch (_) { return null; }
}

/**
 * A Tab command for a cell that triggers path completion when the cursor is
 * inside a path string, and falls through to indentMore otherwise.
 *
 * `startCompletion` is synchronous — it kicks off the async source and opens
 * the dropdown if results arrive. We check pathContext first so a Tab press
 * outside a path string never triggers the UI.
 */
function pathCompleteOrIndent(id) {
  return (view) => {
    if (pathContext(view.state) && window.CM && CM.startCompletion) {
      return CM.startCompletion(view);
    }
    return CM.indentMore(view);
  };
}

/** Every keybinding onEditorKey() has, as CM commands. */
function cmKeymap(id) {
  const stop = (fn) => (view) => { fn(view); return true; };
  return [
    {key: 'Shift-Enter', run: stop(() => runCell(id, true))},
    {key: 'Mod-Shift-ArrowUp', run: stop(() => moveCell(id, -1))},
    {key: 'Mod-Shift-ArrowDown', run: stop(() => moveCell(id, 1))},
    {key: 'Mod-Shift-m', run: stop(() => {
      const c = getCell(id);
      if (c) changeType(id, c.cell_type === 'markdown' ? 'code' : 'markdown');
    })},
    {key: 'Mod-/', run: stop((view) => {
      const c = getCell(id);
      cmToggleComment(view, c ? c.cell_type : 'code');
      queueSave(id);
    })},
    {key: 'Mod-s', run: stop(() => saveCell(id))},
    {key: 'Escape', run: stop((view) => {
      view.contentDOM.blur();
      const c = getCell(id);
      if (c && c.cell_type === 'markdown') renderMarkdown(id);
    })},
    // Tab indents rather than moving focus, as the textarea path does.
    // indentMore, not insertTab: insertTab inserts a literal \t when there's no
    // selection, and a tab character in Python source is exactly what nobody
    // wants. indentMore always uses the indentUnit set below — four spaces.
    // Exception: inside a string literal that looks like a path, Tab triggers
    // path completion instead (pathCompleteOrIndent falls through to indentMore
    // when no completions are available or the cursor isn't in a path string).
    {key: 'Tab', run: pathCompleteOrIndent(id), shift: CM.indentLess},
  ];
}

/**
 * Cmd/Ctrl+/ against a CM document. Same rule as toggleComment(): uncomment
 * only when every non-blank line already is, otherwise comment the lot at the
 * shallowest indent. Written against CM's transaction API rather than sharing
 * the textarea version's body, because that one mutates `.value` and moves
 * `selectionStart` — through the shim that would replace the whole document and
 * lose the caret, and it would land in the undo history as one wholesale edit.
 */
function cmToggleComment(view, cellType) {
  const marker = cellType === 'markdown' ? null : '#';
  const doc = view.state.doc;
  const sel = view.state.selection.main;
  const first = doc.lineAt(sel.from), last = doc.lineAt(sel.to);
  const from = first.from, to = last.to;
  const lines = doc.sliceString(from, to).split('\n');

  let out;
  if (marker === null) {
    const block = lines.join('\n');
    const m = /^\s*<!--\s?([\s\S]*?)\s?-->\s*$/.exec(block);
    out = (m ? m[1] : `<!-- ${block} -->`).split('\n');
  } else {
    const filled = lines.filter(l => l.trim());
    const commented = filled.length > 0 &&
      filled.every(l => l.trimStart().startsWith(marker));
    if (commented) {
      out = lines.map(l => l.replace(new RegExp(`^(\\s*)${marker} ?`), '$1'));
    } else {
      const indent = Math.min(...(filled.length ? filled : lines)
        .map(l => l.length - l.trimStart().length));
      out = lines.map(l => l.trim()
        ? l.slice(0, indent) + marker + ' ' + l.slice(indent)
        : l);
    }
  }

  const replacement = out.join('\n');
  view.dispatch({
    changes: {from, to, insert: replacement},
    // Keep the same lines selected so repeated presses toggle the same block.
    selection: {anchor: from, head: from + replacement.length},
  });
}

/**
 * Attach a CM editor over one cell's fallback textarea.
 *
 * A view already built for this cell is **moved** into the new host rather than
 * rebuilt, because render() replaces the notebook's innerHTML wholesale and a
 * rebuild would drop the cell's undo history every time anything re-rendered —
 * adding a cell, switching a type, finishing a run. JupyterLab's history
 * survives those, so ours does too.
 */
function mountEditor(id) {
  const host = document.getElementById('ed-host-' + id);
  if (!host || !window.CM) return;
  const area = host.querySelector('textarea');
  if (!area) return;
  const text = area.value;
  const hint = area.placeholder || '';
  const isPython = host.dataset.lang === 'python';

  const existing = cmViews.get(id);
  // A view is reused only when nothing about it has changed: same language and
  // placeholder (both fixed at construction), and the same text it already
  // holds — either because nothing moved, or because the user has an unsaved
  // edit, which outranks the stale `cells[].source` a re-render renders from.
  //
  // When the text *has* changed under the editor — an agent's `gusnb here
  // - --run`, a skill's snippet, an external file edit — the view is rebuilt,
  // not patched. Dispatching the replacement instead leaves CM rebasing its
  // history through a whole-document change, and undo then produces a
  // half-and-half document that is neither what the user typed nor what the
  // agent wrote. A rebuild resets the typing history honestly: walking back
  // someone else's write is `nb_undo`'s job, and the ↶ Undo replace strip sits
  // right above the editor for it.
  const reusable = existing &&
    existing.nbLang === host.dataset.lang &&
    existing.nbHint === hint &&
    (unsaved.has(id) || existing.state.doc.toString() === text);
  if (reusable) {
    area.remove();
    host.id = 'ed-' + id;
    host.classList.add('editor-cm');
    shimHost(host, id, existing);
    host.appendChild(existing.dom);
    refreshHist(id);
    return;
  }
  if (existing) { existing.destroy(); cmViews.delete(id); }

  const ext = [
    CM.history(),
    CM.highlightActiveLine(),
    CM.syntaxHighlighting(CM.style),
    CM.indentUnit.of('    '),
    // Ours before CM's defaults, so ⇧⏎ and Mod-/ win over anything the base
    // keymap claims for the same chord.
    CM.Prec.high(CM.keymap.of(cmKeymap(id))),
    CM.keymap.of([...CM.historyKeymap, ...CM.defaultKeymap]),
    CM.EditorView.updateListener.of((u) => {
      if (u.focusChanged && u.view.hasFocus) selectCell(id);
      if (!u.docChanged) return;
      queueSave(id);
      refreshHist(id);
    }),
  ];
  // Code scrolls sideways rather than wrapping, so indentation still reads;
  // prose wraps, as the textarea's `pre-wrap` did for markdown and raw cells.
  if (isPython) {
    ext.push(CM.python());
    ext.push(CM.lineNumbers());
    // Path completion: Tab inside a string literal that looks like a path
    // fetches filesystem entries from the server.  autocompletion() provides
    // the dropdown UI; the source is activated only by the explicit Tab binding
    // above (activateOnTyping: false), so normal typing is unaffected.
    if (CM.autocompletion && CM.autocompletionConfig) {
      ext.push(CM.autocompletion({
        override: [pathCompletionSource],
        activateOnTyping: false,
        maxRenderedOptions: 30,
        closeOnBlur: true,
      }));
    }
  } else {
    ext.push(CM.EditorView.lineWrapping);
  }
  if (hint) ext.push(CM.placeholder(hint));

  const view = new CM.EditorView({doc: text, extensions: ext});
  view.nbLang = host.dataset.lang;
  view.nbHint = hint;
  // The textarea goes, and the host inherits its id — so `#ed-<id>` still
  // resolves, now to something the shim has given a `.value`. The placeholder
  // moves with the id: it's the AI cell's "describe what you want" prompt, and
  // it's read off the element by attribute as well as shown.
  if (hint) host.setAttribute('placeholder', hint);
  area.remove();
  host.id = 'ed-' + id;
  host.classList.add('editor-cm');
  shimHost(host, id, view);
  host.appendChild(view.dom);
  cmViews.set(id, view);
  refreshHist(id);
}

/**
 * Mount every unmounted host after a render, and forget the views whose cells
 * are gone. render() replaces the notebook's innerHTML wholesale, so a view
 * left in the map would point at a detached DOM node and its `.value` would be
 * read by nobody — and its history would be resurrected onto the next cell that
 * happened to reuse the id.
 */
function mountEditors() {
  for (const [id, view] of [...cmViews]) {
    if (!document.querySelector(`.ed-host[data-cell="${id}"]`)) {
      view.destroy();
      cmViews.delete(id);
    }
  }
  if (!window.CM) return;
  for (const host of document.querySelectorAll('.ed-host')) mountEditor(host.dataset.cell);
}

// A module runs deferred, so the first render() may well beat CM's import. Then
// this fires and mounts what was rendered as a textarea; if CM was already in
// hand, mountEditors() did it and there is nothing here to do.
window.addEventListener('cm-ready', () => {
  if (window.CM && isNotebookTab()) render();
});

/**
 * Track which cell is active — for the toolbar Run button, for `gusnb here`,
 * and for the highlight.
 *
 * The highlight is a **class on the cell**, not `:focus` on its editor, and that
 * is the whole point: focus leaves the moment you click into a Claude terminal,
 * which is precisely when you most need to see which cell Claude is about to
 * act on. `.editor:focus` stays as well, for the narrower "the caret is here
 * right now" cue.
 */
function selectCell(id) {
  if (selected === id) return;
  selected = id;
  paintSelection();
  reportFocus(id);
}

/** Move `.is-current` to the selected cell. Cheap enough to call on any repaint. */
function paintSelection() {
  for (const el of document.querySelectorAll('.cell.is-current')) {
    if (el.dataset.id !== selected) el.classList.remove('is-current');
  }
  if (!selected) return;
  const el = document.querySelector(`.cell[data-id="${selected}"]`);
  if (el) el.classList.add('is-current');
}

// Tell the server where the caret is, so `nb.py here` can answer "the cell I'm
// on" and Claude can work on it without being told an id. Debounced and
// fire-and-forget: arrow-keying down a notebook would otherwise be one request
// per cell, and nothing on screen depends on the reply.
let focusTimer = null;
function reportFocus(id) {
  if (!isNotebookTab()) return;
  clearTimeout(focusTimer);
  focusTimer = setTimeout(() => {
    api('/api/focus' + nbq(), {method: 'POST', body: JSON.stringify({cell_id: id})})
      .catch(() => {});
  }, 150);
}

/* ---------- Folding: code, and output ----------
 * Both are class toggles on nodes already in the DOM, never a render(). A
 * re-render would rebuild the notebook's innerHTML, and while mountEditor()
 * carries CM views across that, it can't carry the caret or the scroll position
 * within an editor — and folding is something you do *while* reading a cell.
 */

/** Clip a long code cell, or open it. */
function toggleFold(id) {
  const c = getCell(id);
  const wrap = document.getElementById('fold-' + id);
  if (!wrap || !c) return;
  // Off what's on screen, not off `codeOpen`: a cell the caret is in renders
  // unfolded whether or not it's in the set (see isFolded), and reading the set
  // there would make the first click on ⌃ open an already-open cell.
  const fold = !wrap.classList.contains('folded');
  if (fold) codeOpen.delete(id); else codeOpen.add(id);
  const t = activeTab();
  if (t && t.kind === 'notebook') {
    t.codeOpen = codeOpen;
    rememberNotebookView(t);
  }
  wrap.classList.toggle('folded', fold);
  // The veil is a click target over hidden code, so it belongs to the folded
  // state alone — left in place it would swallow clicks on code now visible, and
  // its "N more lines" is only right for a given fold point. Removed and remade
  // rather than hidden, for that count.
  const veil = wrap.querySelector('.fold-veil');
  if (veil) veil.remove();
  if (fold) wrap.insertAdjacentHTML('beforeend', veilHtml(c));
  refreshViewBtns(id);
}

/** Hide a cell's output, or show it. */
function toggleOutput(id) {
  const c = getCell(id);
  const hide = !outsHidden.has(id);
  if (hide) outsHidden.add(id); else outsHidden.delete(id);
  const t = activeTab();
  if (t && t.kind === 'notebook') {
    t.outsHidden = outsHidden;
    rememberNotebookView(t);
  }
  const out = document.getElementById('out-' + id);
  if (out && c) {
    out.classList.toggle('output-hidden', hide);
    out.innerHTML = hide ? outNoteHtml(c) : outputSlotHtml(c);
    if (!hide) pinStreams(out);
  }
  refreshViewBtns(id);
}

/**
 * Repaint one cell's fold/output toggles in place.
 *
 * Needed beyond render() because a cell that had neither when it was rendered
 * grows both by running: the SSE handlers write straight into `#out-<id>` and
 * never rebuild the gutter, so without this a cell you just ran would show its
 * output with no ▾ to collapse it until something else forced a repaint.
 */
function refreshViewBtns(id) {
  const gutter = document.querySelector(`.cell[data-id="${id}"] .gutter`);
  const c = getCell(id);
  if (!gutter || !c) return;
  const html = viewBtnsHtml(c);
  const existing = gutter.querySelector('.gutter-out');
  if (existing) existing.outerHTML = html;
  else if (html) gutter.querySelector('.gutter-label').insertAdjacentHTML('afterend', html);
}

/**
 * Reconcile a cell's output chrome with the output it now has, after a run wrote
 * into `#out-<id>` behind render()'s back — keeping the ▾ honest and the hidden
 * count matching what's behind it.
 *
 * A collapsed cell **stays** collapsed through a re-run, as it does in
 * JupyterLab: you closed that output on purpose, and a cell you re-run in a loop
 * would otherwise reopen every time and undo the thing you asked for. The note is
 * what tells you it ran (see showRunning).
 */
function syncOutputView(id) {
  const out = document.getElementById('out-' + id);
  const c = getCell(id);
  if (!out || !c) return;
  const hidden = outsHidden.has(id);
  out.classList.toggle('output-hidden', hidden);
  if (hidden) {
    out.innerHTML = (c.outputs || []).length ? outNoteHtml(c) : '';
  } else {
    out.innerHTML = outputSlotHtml(c);
    pinStreams(out);
  }
  refreshViewBtns(id);
}

/**
 * Say a collapsed cell is running. The "running…" spinner is written inside
 * `#out-<id>`, which is display:none while the output is hidden — so without
 * this, running a collapsed cell shows nothing at all happening.
 */
function showRunning(id) {
  if (!outsHidden.has(id)) return;
  const out = document.getElementById('out-' + id);
  const note = out && out.querySelector('.out-note');
  if (note) {
    note.classList.add('running-dots');
    note.textContent = '▸ running';
  }
  else if (out) {
    out.innerHTML =
      `<button class="out-note running-dots"
         onclick="event.stopPropagation();toggleOutput('${id}')"
         title="Show this cell's output">▸ running</button>`;
  }
}

/** Bring a cell into view if it's outside the scroll viewport. */
function scrollToCell(id, block = 'nearest') {
  const el = document.querySelector(`.cell[data-id="${id}"]`);
  if (el) el.scrollIntoView({behavior: 'smooth', block});
}

function getCell(id) { return cells.find(c => c.id === id); }
