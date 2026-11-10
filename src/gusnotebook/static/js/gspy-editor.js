/* A real Quill document. Python embeds retain the notebook's ordered execution
 * model; prose is edited in place, and code lives in the selected-step inspector. */
const GspyDelta = GspyQuill.import('delta');
const GspyBlockEmbed = GspyQuill.import('blots/block/embed');
const GspyInlineEmbed = GspyQuill.import('blots/embed');
const GspyParchment = GspyQuill.import('parchment');
GspyQuill.register(new GspyParchment.Attributor('gspyId', 'data-gspy-text-id', {scope: GspyParchment.Scope.BLOCK}), true);

class GspyPython extends GspyBlockEmbed {
  static blotName = 'gspyPython';
  static tagName = 'DIV';
  static className = 'gspy-python';
  static create(id) {
    const node = super.create();
    node.dataset.pythonId = id;
    node.contentEditable = 'false';
    node.innerHTML = '<span class="gspy-step-label">Python · not run</span>';
    return node;
  }
  static value(node) { return node.dataset.pythonId; }
}

class GspyValue extends GspyInlineEmbed {
  static blotName = 'gspyValue';
  static tagName = 'SPAN';
  static className = 'gspy-value-embed';
  static create(value) {
    const node = super.create();
    node.dataset.owner = value.owner; node.dataset.name = value.name;
    node.contentEditable = 'false';
    node.setAttribute('role', 'button'); node.tabIndex = 0;
    node.textContent = '{{' + value.name + '}}';
    return node;
  }
  static value(node) { return {owner: node.dataset.owner, name: node.dataset.name}; }
  html() { return '{{' + escapeHtml(this.domNode.dataset.name) + '}}'; }
}
GspyQuill.register(GspyPython, true);
GspyQuill.register(GspyValue, true);

// getRandomValues also works on remote HTTP/IP origins, where randomUUID
// requires a secure context and is unavailable.
const gspyId = () => 'd_' + Array.from(crypto.getRandomValues(new Uint8Array(16)), b => b.toString(16).padStart(2, '0')).join('');
const gspyFormats = ['gspyId', 'gspyPython', 'gspyValue', 'bold', 'italic', 'underline', 'strike', 'code',
  'header', 'list', 'indent', 'align', 'blockquote', 'link', 'color', 'background', 'size'];

function gspyTextDelta(state, cell, source = cell.source, id = cell.id) {
  let text = cell.rich_text && source === cell.source ? new GspyDelta(cell.rich_text)
    : state.quill.clipboard.convert({html: DOMPurify.sanitize(renderNotebookMarkdown(source))});
  if (!text.ops.length || !String(text.ops.at(-1).insert).endsWith('\n')) text = text.concat(new GspyDelta().insert('\n'));
  let result = new GspyDelta();
  for (const op of text.ops) {
    if (typeof op.insert !== 'string') continue;
    const parts = op.insert.split(/(\n|\{\{[\p{L}_][\p{L}\p{N}_]*\}\})/u);
    for (const part of parts) {
      if (!part) continue;
      const name = part.startsWith('{{') && part.endsWith('}}') ? part.slice(2, -2) : null;
      if (name && !op.attributes?.code && cell.content?.definition.bindings[name]) result.insert({gspyValue: {owner: cell.id, name}}, op.attributes);
      else result.insert(part, part === '\n' ? {...op.attributes, gspyId: id} : op.attributes);
    }
  }
  return result;
}

function gspyDocumentDelta(state, documentCells) {
  let delta = new GspyDelta();
  state.origins = new Map(); state.extract = new Set();
  for (const c of documentCells) {
    state.origins.set(c.id, c);
    if (c.cell_type === 'markdown') {
      delta = delta.concat(gspyTextDelta(state, c));
    } else {
      delta.insert({gspyPython: c.id});
      // Preserve first-generation content as editable prose. On the next edit
      // its extraction and the original component revision are saved atomically.
      for (const component of c.content?.definition.components || []) {
        if (component.kind !== 'markdown') continue;
        const id = gspyId(); state.origins.set(id, c); state.extract.add(c.id);
        delta = delta.concat(gspyTextDelta(state, c, component.source, id));
      }
    }
  }
  if (!delta.ops.length || typeof delta.ops.at(-1).insert !== 'string' || !delta.ops.at(-1).insert.endsWith('\n')) {
    delta.insert('\n', {gspyId: gspyId()});
  }
  return delta;
}

function gspyStatus(state) {
  const status = state.root.querySelector('.gspy-editor-status');
  status.textContent = state.error || (state.saving ? 'Saving…' : state.dirty ? 'Unsaved changes' : 'Saved');
  status.classList.toggle('error', !!state.error);
  state.root.querySelector('[data-document-retry]').hidden = !state.error;
  state.tab.dirty = state.dirty || notebookDrafts(state.tab).size > 0;
}

function createGspyEditor(t) {
  const root = document.createElement('div'); root.className = 'gspy-editor-shell';
  root.innerHTML = '<div class="gspy-editor-paper"><div class="gspy-quill"></div></div><aside class="gspy-inspector" hidden></aside><div class="gspy-editor-footer"><span class="gspy-editor-status" role="status">Saved</span><button data-document-retry hidden>Retry save</button><span class="gspy-prose-context"></span><button data-document-history>History</button></div>';
  const state = {root, tab: t, base: t.editorVersion, dirty: false, revision: 0, timer: null, saving: null, error: '', inspector: null};
  state.quill = new GspyQuill(root.querySelector('.gspy-quill'), {
    placeholder: 'Start writing. Insert Python wherever your analysis needs it.',
    formats: gspyFormats, modules: {toolbar: false, history: {userOnly: true, delay: 700}},
  });
  state.quill.root.setAttribute('aria-label', 'Scientific document');
  state.quill.root.setAttribute('role', 'textbox'); state.quill.root.setAttribute('aria-multiline', 'true');
  state.quill.clipboard.addMatcher('.gspy-python', () => new GspyDelta());
  // A pasted result is a text snapshot. Cross-document copies must not retain
  // IDs that could silently link to a different producer in this document.
  state.quill.clipboard.addMatcher('.gspy-value-embed', node => new GspyDelta().insert(node.textContent.replaceAll('\uFEFF', '')));
  state.quill.setContents(gspyDocumentDelta(state, t.cells || []), 'silent');
  state.quill.history.clear();
  state.quill.on('text-change', (_delta, _old, source) => {
    if (source !== 'user') return;
    resolveTypedGspyValues(state);
    state.dirty = true; state.revision++; state.error = '';
    clearTimeout(state.timer);
    state.timer = setTimeout(() => saveGspyDocument(t).catch(() => {}), 600);
    gspyStatus(state); renderTabs();
  });
  state.quill.on('selection-change', range => {
    if (!range) return;
    state.selection = range;
    const [line] = state.quill.getLine(range.index);
    const id = line?.domNode.dataset.pythonId || line?.formats()?.gspyId;
    if (id && getCell(id)) selectCell(id);
    refreshGspyProseContext(state);
  });
  root.querySelector('[data-document-retry]').onclick = () => saveGspyDocument(t).catch(error => flash(errText(error)));
  root.querySelector('[data-document-history]').onclick = () => openDocumentHistory();
  root.addEventListener('click', event => {
    const python = event.target.closest('[data-open-python]');
    if (python) { event.preventDefault(); openGspyPython(python.dataset.openPython).catch(error => flash(errText(error))); }
    const live = event.target.closest('.gspy-value-embed');
    if (live) {
      const owner = getCell(live.dataset.owner) || state.origins.get(live.dataset.owner);
      const value = owner?.content?.bindings[live.dataset.name];
      if (value) inspectGspyResult(value.producer, value.run_id).catch(error => flash(errText(error)));
    }
  });
  root.addEventListener('keydown', event => {
    if (event.target.matches('.gspy-value-embed') && ['Enter', ' '].includes(event.key)) {
      event.preventDefault(); event.target.click();
    }
  });
  return state;
}

function resolveTypedGspyValues(state) {
  if (state.resolvingValues) return;
  const q = state.quill;
  let index = 0, consumed = 0, change = new GspyDelta();
  for (const op of q.getContents().ops) {
    if (typeof op.insert === 'string' && !op.attributes?.code) {
      for (const match of op.insert.matchAll(/\{\{([\p{L}_][\p{L}\p{N}_]*)\}\}/gu)) {
        const start = index + match.index;
        const [line] = q.getLine(start), id = line?.formats()?.gspyId;
        const owner = (state.tab.cells || []).find(c => c.id === id) || state.origins.get(id);
        if (!owner?.content?.definition.bindings[match[1]]) continue;
        change.retain(start - consumed).delete(match[0].length)
          .insert({gspyValue: {owner: owner.id, name: match[1]}}, op.attributes);
        consumed = start + match[0].length;
      }
    }
    index += typeof op.insert === 'string' ? op.insert.length : 1;
  }
  if (!change.ops.length) return;
  state.resolvingValues = true;
  try {
    // Record the replacement in the same undo group as the user's typing.
    // Converting after an asynchronous save would separate those two actions.
    const range = q.getSelection();
    q.updateContents(change, 'user');
    if (range) q.setSelection(change.transformPosition(range.index), 0, 'silent');
    refreshGspyEmbeds(state);
  } finally { state.resolvingValues = false; }
}

function serializeGspyDocument(state) {
  const q = state.quill, sections = [], seen = new Set();
  let current = null;
  for (const line of q.getLines()) {
    const index = q.getIndex(line), length = line.length();
    if (line instanceof GspyPython) {
      const id = line.domNode.dataset.pythonId;
      if (seen.has(id)) throw new Error('A Python step cannot occur twice. Insert a new step instead.');
      seen.add(id); current = null;
      const existing = (state.tab.cells || []).find(c => c.id === id) || state.origins.get(id);
      sections.push({id, cell_type: existing?.cell_type === 'raw' ? 'raw' : 'code', extract_text: state.extract.has(id)});
      continue;
    }
    const originId = line.formats().gspyId;
    let id = originId || gspyId();
    if (!current || current.origin !== originId) {
      if (seen.has(id)) id = gspyId();
      seen.add(id);
      const origin = state.origins.get(originId) || (state.tab.cells || []).find(c => c.id === originId);
      current = {id, origin: originId, cell_type: 'markdown', start: index, length: 0, bindings: structuredClone(origin?.content?.definition.bindings || {})};
      sections.push(current);
    }
    q.formatLine(index, length, 'gspyId', current.id, 'silent');
    current.length += length;
  }
  return sections.map(section => {
    if (section.cell_type !== 'markdown') return section;
    const ops = q.getContents(section.start, section.length).ops.map(op => {
      const attributes = {...op.attributes}; delete attributes.gspyId;
      const value = op.insert?.gspyValue;
      return {insert: value ? '{{' + value.name + '}}' : op.insert, ...(Object.keys(attributes).length ? {attributes} : {})};
    });
    // Quill's HTML exporter is not a trust boundary. Sanitize after export as
    // well as before import; arbitrary HTML outputs stay in sandboxed frames.
    const html = DOMPurify.sanitize(q.getSemanticHTML(section.start, section.length), {
      ALLOWED_TAGS: ['p', 'br', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'strong', 'b', 'em', 'i', 'u', 's', 'span', 'a', 'ol', 'ul', 'li', 'blockquote', 'code', 'pre'],
      ALLOWED_ATTR: ['href', 'target', 'rel', 'style', 'class'],
    });
    return {id: section.id, cell_type: 'markdown', source: html, delta: ops, bindings: section.bindings};
  });
}

async function saveGspyDocument(t = activeTab()) {
  const state = t?.gspyEditor;
  if (!state || t.closing) return;
  clearTimeout(state.timer);
  if (state.saving) { await state.saving; if (state.dirty) return saveGspyDocument(t); return; }
  if (!state.dirty) return;
  const revision = state.revision;
  let blocks;
  try { blocks = serializeGspyDocument(state); }
  catch (error) { state.error = 'Save failed: ' + errText(error); gspyStatus(state); throw error; }
  state.saving = (async () => {
    // Archive the latest typed code before a document deletion removes it.
    const retained = new Set(blocks.map(block => block.id));
    for (const id of notebookDrafts(t).keys()) if (!retained.has(id)) await saveCell(id, t);
    return api('/api/notebook/document?' + new URLSearchParams({notebook: t.path}), {
      method: 'PUT', body: JSON.stringify({blocks, expected_version: state.base}),
    });
  })().then(data => {
    state.base = t.editorVersion = data.editor_version;
    state.error = ''; state.extract.clear();
    const drafts = notebookDrafts(t);
    const live = new Map((t.cells || []).filter(c => c._running).map(c => [c.id, c]));
    t.cells = data.cells.map(c => ({...c,
      ...(live.has(c.id) ? {_running: true, outputs: live.get(c.id).outputs} : {}),
      ...(drafts.has(c.id) ? {source: drafts.get(c.id).source} : {})}));
    for (const c of t.cells) state.origins.set(c.id, c);
    if (revision === state.revision) state.dirty = false;
    if (active === t.path) { cells = t.cells; refreshGspyEmbeds(state); }
  }).catch(error => {
    state.error = 'Save failed: ' + errText(error);
    state.dirty = true;
    throw error;
  }).finally(() => { state.saving = null; gspyStatus(state); renderTabs(); });
  gspyStatus(state);
  await state.saving;
  if (state.dirty) return saveGspyDocument(t);
}

function renderGspyDocument() {
  const t = activeTab(), container = document.getElementById('notebook');
  container.classList.add('gspy-document'); container.dataset.notebook = active;
  const state = t.gspyEditor || (t.gspyEditor = createGspyEditor(t));
  if (container.firstElementChild !== state.root) container.replaceChildren(state.root);
  if (state.base !== t.editorVersion && !state.saving) {
    if (state.dirty) state.error = 'Document changed elsewhere. Your draft is kept; save reports the conflict. Reload to discard the draft and review the saved document.';
    else {
      const range = state.quill.getSelection();
      state.quill.setContents(gspyDocumentDelta(state, t.cells), 'silent');
      state.quill.history.clear(); state.base = t.editorVersion;
      if (range) state.quill.setSelection(Math.min(range.index, state.quill.getLength() - 1), 0, 'silent');
    }
  }
  refreshGspyEmbeds(state); renderGspyInspector(state); mountEditors(); gspyStatus(state);
}

function refreshGspyEmbeds(state) {
  const documentCells = state.tab.cells || [];
  for (const node of state.quill.root.querySelectorAll('.gspy-python')) {
    const c = documentCells.find(c => c.id === node.dataset.pythonId);
    const id = node.dataset.pythonId;
    const status = c?._running ? 'running' : c?.content?.run?.state || 'not run';
    const mode = c?.display_mode || 'output';
    const caption = c?.cell_type === 'raw' ? '<span class="gspy-step-label">Raw text · not executed</span>'
      : `<button class="gspy-step-label" data-open-python="${escapeAttr(id)}" aria-label="Inspect Python step">Python · ${mode === 'background' ? 'background · ' : ''}${escapeHtml(status)}</button>`;
    const output = c?.cell_type === 'raw' ? `<pre>${escapeHtml(c.source)}</pre>` : c && mode === 'output' ? (c.content?.components || []).filter(item => item.kind === 'output').map(item =>
      `<figure class="gspy-figure">${renderOutputs(item.outputs, item.producer, 'doc-' + c.id + '-' + item.id)}<button class="gspy-output-source" data-gspy-producer="${escapeAttr(item.producer)}" data-gspy-run="${escapeAttr(item.run_id || '')}">Source · ${escapeHtml(item.state)}</button></figure>`).join('') : '';
    const warning = c?.content?.run?.error ? `<span class="gspy-step-error">${escapeHtml(c.content.run.error)}</span>` : '';
    const html = `<div class="gspy-step-content"><div class="gspy-content gspy-${escapeAttr(c?.content?.definition.layout || 'flow')}">${output}</div>${caption}${warning}</div>`;
    if (node._gspyHtml !== html) { node.innerHTML = html; node._gspyHtml = html; }
    node.classList.toggle('background', mode === 'background');
  }
  for (const node of state.quill.root.querySelectorAll('.gspy-value-embed')) {
    const owner = documentCells.find(c => c.id === node.dataset.owner) || state.origins.get(node.dataset.owner);
    const value = owner?.content?.bindings[node.dataset.name];
    const inner = node.querySelector('[contenteditable="false"]') || node;
    const text = value?.text ?? 'missing';
    if (inner.textContent !== text) inner.textContent = text;
    node.title = `${node.dataset.name} · ${value?.state || 'missing'} · Inspect producing code`;
    node.classList.toggle('stale', ['stale', 'missing'].includes(value?.state));
  }
  state.quill.update('silent');
  refreshGspyProseContext(state);
}

function refreshGspyProseContext(state) {
  if (active !== state.tab.path) return;
  const format = state.quill.getFormat(state.selection || {index: 0, length: 0});
  document.querySelector('#gspy-format-tools select').value = format.header || '';
  for (const button of document.querySelectorAll('[data-gspy-format]')) {
    button.setAttribute('aria-pressed', String(!!format[button.dataset.gspyFormat]));
  }
  const [line] = state.quill.getLine(state.selection?.index || 0);
  const c = (state.tab.cells || []).find(c => c.id === line?.formats()?.gspyId);
  state.root.querySelector('.gspy-prose-context').innerHTML = c?.content?.review_pending
    ? `Supporting results changed. <button data-gspy-review="${escapeAttr(c.id)}">Mark reviewed</button>` : '';
}

async function gspyEditValues() {
  try {
    const state = activeTab()?.gspyEditor; if (!state) return;
    await saveGspyDocument();
    const [line] = state.quill.getLine(state.selection?.index || 0);
    const id = line?.formats()?.gspyId;
    if (id && getCell(id)?.cell_type === 'markdown') await openGspyContent(id);
    else flash('Place the cursor in a paragraph to link its live values.');
  } catch (error) { flash(errText(error)); }
}

function gspyFormat(name, value) {
  const state = activeTab()?.gspyEditor; if (!state) return;
  const range = state.quill.getSelection() || state.selection || {index: 0, length: 0};
  state.quill.setSelection(range, 'silent');
  if (name === 'list' && state.quill.getFormat(range).list === value) value = false;
  state.quill.format(name, value === undefined ? !state.quill.getFormat(range)[name] : value, 'user');
  state.quill.focus();
}

async function gspyInsertPython() {
  const t = activeTab(), state = t?.gspyEditor; if (!state) return;
  try {
    const range = state.quill.getSelection() || state.selection || {index: state.quill.getLength() - 1, length: 0};
    const id = gspyId();
    state.quill.history.cutoff();
    state.quill.insertEmbed(range.index, 'gspyPython', id, 'user');
    state.quill.history.cutoff();
    const inserted = state.quill.getLines().find(line => line.domNode.dataset.pythonId === id);
    if (inserted) {
      state.selection = {index: state.quill.getIndex(inserted) + 1, length: 0};
      state.quill.setSelection(state.selection, 'silent');
    }
    await saveGspyDocument(t);
    await openGspyPython(id);
  } catch (error) { flash(errText(error)); }
}

async function openGspyPython(id) {
  const state = activeTab()?.gspyEditor; if (!state) return;
  if (state.inspector && state.inspector !== id) await saveCell(state.inspector);
  state.inspector = id; selectCell(id); renderGspyInspector(state); focusCellEditor(id);
}

function gspyContinueWriting(id) {
  const state = activeTab()?.gspyEditor; if (!state) return;
  const step = state.quill.getLines().find(line => line.domNode.dataset.pythonId === id);
  state.inspector = null; state.root.querySelector('.gspy-inspector').hidden = true;
  if (step) state.quill.setSelection(Math.min(state.quill.getIndex(step) + 1, state.quill.getLength() - 1));
  state.quill.focus();
}

function renderGspyInspector(state) {
  const panel = state.root.querySelector('.gspy-inspector');
  const c = (state.tab.cells || []).find(cell => cell.id === state.inspector);
  if (!c) { panel.hidden = true; return; }
  panel.hidden = false;
  const editor = document.getElementById('ed-' + c.id);
  if (panel.dataset.cell !== c.id || (window.CM && !cmViews.has(c.id)) || (editor && !unsaved.has(c.id) && editor.value !== c.source)) {
    panel.dataset.cell = c.id;
    panel.innerHTML = `<div class="gspy-inspector-bar"><strong>Python</strong>
      <select aria-label="Python output mode"><option value="output">Show output in document</option><option value="background">Background step</option></select>
      <button data-run-python>Run</button><button data-run-through>Run through here</button>
      <button data-python-history>History</button><button data-python-bindings>Results</button><button data-python-copy>Copy result</button><button data-close-python aria-label="Close Python inspector">×</button></div>
      <div class="cell gspy-code-inspector" id="cell-${c.id}" data-id="${c.id}" data-type="code"><div class="cell-body">
        <div class="ed-host" id="ed-host-${c.id}" data-cell="${c.id}" data-lang="python"><textarea class="editor" id="ed-${c.id}"
          oninput="queueSave('${c.id}')" onkeydown="return onEditorKey(event, '${c.id}')" spellcheck="false">${escapeHtml(c.source)}</textarea></div>
        <div class="gspy-inspector-state" role="status"></div>
        <details class="gspy-execution-log"><summary>Execution output and errors</summary><div class="output-area"><div id="out-${c.id}"></div></div></details>
        <div class="help-panel" id="help-${c.id}"></div></div></div>`;
    panel.querySelector('[data-close-python]').onclick = async () => {
      try {
        await saveCell(c.id); state.inspector = null; panel.hidden = true;
        if (state.selection) state.quill.setSelection(state.selection, 'silent');
        state.quill.focus();
      }
      catch (error) { flash(errText(error)); }
    };
    panel.querySelector('[data-run-python]').onclick = () => runCell(c.id);
    panel.querySelector('[data-run-through]').onclick = () => gspyRunThrough(c.id);
    panel.querySelector('[data-python-history]').onclick = () => openCellHistory(c.id);
    panel.querySelector('[data-python-bindings]').onclick = () => openGspyContent(c.id).catch(error => flash(errText(error)));
    panel.querySelector('[data-python-copy]').onclick = () => copyCellProvenance(c.id);
    panel.querySelector('select').onchange = async event => {
      try { await api(`/api/cells/${c.id}/display${nbq()}`, {method: 'PATCH', body: JSON.stringify({mode: event.target.value})}); await load(); }
      catch (error) { flash(errText(error)); }
    };
    mountEditors();
  }
  panel.querySelector('select').value = c.display_mode || 'output';
  const run = c.content?.run;
  panel.querySelector('.gspy-inspector-state').innerHTML = escapeHtml(c._running ? 'Running…' : run?.reason || 'Ready to run')
    + (run?.pending_run_id ? ` <button data-gspy-retry="${escapeAttr(run.pending_run_id)}">Retry saving result</button>` : '')
    + Object.entries(c.content?.capture_errors || {}).map(([name, why]) => `<div class="gspy-step-error">${escapeHtml(name + ': ' + why)}</div>`).join('');
  const output = panel.querySelector('.output-area'); output.hidden = false;
  output.firstElementChild.innerHTML = renderCellOutput(c);
  if (c._running || c.outputs?.some(o => o.output_type === 'error')) panel.querySelector('details').open = true;
}
