/* Content-first notebooks. Saved receipts power the view; opening it runs no code. */
const gspyCodeOpen = new Set();
let gspyDialog = null;

function gspyMarkdown(source, bindings) {
  const root = document.createElement('div');
  root.innerHTML = renderNotebookMarkdown(source);
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  for (const node of nodes) {
    if (node.parentElement.closest('pre, code, a, button, script, style, .katex')) continue;
    const text = node.textContent, pattern = /\{\{([\p{L}_][\p{L}\p{N}_]*)\}\}/gu;
    const fragment = document.createDocumentFragment();
    let end = 0, match;
    while ((match = pattern.exec(text))) {
      fragment.append(document.createTextNode(text.slice(end, match.index)));
      const value = bindings[match[1]];
      const button = document.createElement('button');
      button.className = 'gspy-value ' + (value?.state || 'missing');
      button.textContent = value?.text ?? 'missing';
      button.title = value?.error || `${match[1]} · ${value?.state || 'unlinked'} · Inspect producing code`;
      button.dataset.gspyProducer = value?.producer || '';
      button.dataset.gspyRun = value?.run_id || '';
      fragment.append(button);
      end = pattern.lastIndex;
    }
    if (end) { fragment.append(document.createTextNode(text.slice(end))); node.replaceWith(fragment); }
  }
  return root.innerHTML;
}

function gspyCellHtml(c) {
  const root = document.createElement('div');
  root.innerHTML = notebookCellHtml(c);
  const block = root.firstElementChild, body = block.querySelector('.cell-body');
  block.classList.add('gspy-cell');
  const content = c.content;
  if (!content) return root.innerHTML;
  const layout = content.definition.layout;
  const presented = `<div class="gspy-content gspy-${layout}">${content.components.map(component => {
    if (component.kind === 'markdown') {
      return `<div class="md-rendered" data-component="${escapeAttr(component.id)}">${gspyMarkdown(component.source, content.bindings)}</div>`;
    }
    const controls = `<button class="gspy-output-source" data-gspy-producer="${escapeAttr(component.producer)}"
      data-gspy-run="${escapeAttr(component.run_id || '')}" title="Inspect producing code">Source · ${escapeHtml(component.state)}</button>`;
    const empty = component.run_id ? (component.result === '*' ? 'No display output in this run.' : 'No saved output with this name.') : 'No saved output. Run the producing block.';
    return `<figure class="gspy-figure" data-component="${escapeAttr(component.id)}">${
      component.outputs.length ? renderOutputs(component.outputs, component.producer, c.id + '-' + component.id) : `<div class="gspy-empty">${empty}</div>`}${controls}</figure>`;
  }).join('')}</div>`;
  const toolbar = document.createElement('div');
  toolbar.className = 'gspy-tools';
  toolbar.innerHTML = `<button onclick="event.stopPropagation();openGspyContent('${c.id}')">${c.cell_type === 'code' ? 'Content' : 'Values'}</button>`;
  if (content.review_pending) {
    const review = document.createElement('div'); review.className = 'gspy-state stale';
    review.innerHTML = `Supporting results changed. Review the interpretation. <button data-gspy-review="${escapeAttr(c.id)}">Mark reviewed</button>`;
    body.prepend(review);
  }
  if (c.cell_type === 'code') {
    toolbar.innerHTML += `<button onclick="event.stopPropagation();runCell('${c.id}')">Run</button>
      <button onclick="event.stopPropagation();gspyRunThrough('${c.id}')">Run through here</button>`;
    const code = body.querySelector('.fold') || body.querySelector('.ed-host');
    if (code) {
      const details = document.createElement('details');
      const key = active + ':' + c.id;
      details.className = 'gspy-code';
      details.dataset.cell = c.id;
      if (gspyCodeOpen.has(key) || !c.source.trim()) details.open = true;
      details.innerHTML = '<summary>Code</summary>';
      code.replaceWith(details);
      details.append(code);
      details.insertAdjacentHTML('beforebegin', presented);
    }
    const output = body.querySelector('.output-area');
    output.classList.add('gspy-live');
    output.hidden = !c._running;
    output.querySelector('[id^="out-"]').innerHTML = c._running ? renderCellOutput(c) : '';
    const state = content.run;
    if (state && state.state !== 'fresh') {
      const status = document.createElement('div');
      status.className = 'gspy-state ' + state.state;
      status.textContent = state.reason;
      if (state.error) status.textContent += ' · ' + state.error;
      if (state.pending_run_id) {
        const retry = document.createElement('button');
        retry.textContent = 'Retry saving result'; retry.dataset.gspyRetry = state.pending_run_id;
        status.append(' ', retry);
      }
      body.prepend(status);
    }
    const errors = Object.entries(content.capture_errors || {});
    if (errors.length) {
      const error = document.createElement('div'); error.className = 'gspy-state stale';
      error.textContent = errors.map(([name, why]) => `${name}: ${why}`).join(' · '); body.prepend(error);
    }
  } else {
    const prose = body.querySelector('.md-rendered');
    if (prose) prose.innerHTML = gspyMarkdown(c.source, content.bindings);
  }
  body.prepend(toolbar);
  return root.innerHTML;
}

document.addEventListener('toggle', event => {
  const details = event.target;
  if (!details.matches?.('.gspy-code')) return;
  const key = active + ':' + details.dataset.cell;
  if (details.open) gspyCodeOpen.add(key); else gspyCodeOpen.delete(key);
  if (details.open) cmViews.get(details.dataset.cell)?.requestMeasure();
}, true);

document.addEventListener('click', event => {
  const link = event.target.closest('[data-gspy-producer]') || (event.target.matches('.gspy-figure img')
    ? event.target.closest('.gspy-figure').querySelector('[data-gspy-producer]') : null);
  if (link) {
    event.stopPropagation();
    inspectGspyResult(link.dataset.gspyProducer, link.dataset.gspyRun).catch(error => flash(errText(error)));
  }
  const retry = event.target.closest('[data-gspy-retry]');
  if (retry) {
    api(`/api/notebook/runs/${encodeURIComponent(retry.dataset.gspyRetry)}${nbq()}`, {method: 'POST'})
      .then(() => load()).catch(error => flash(errText(error)));
  }
  const review = event.target.closest('[data-gspy-review]');
  if (review) {
    const c = getCell(review.dataset.gspyReview);
    api(`/api/cells/${c.id}/review${nbq()}`, {method: 'POST', body: JSON.stringify({expected_version: c.content.review_version})})
      .then(() => load()).catch(error => flash(errText(error)));
  }
});

function closeGspyDialog() {
  if (!gspyDialog) return;
  gspyDialog.back.classList.remove('on');
  syncModals();
  gspyDialog.back.remove();
  gspyDialog.focus?.focus();
  gspyDialog = null;
}

function showGspyDialog(title, html) {
  closeGspyDialog();
  const back = document.createElement('div'); back.className = 'modal-back on'; back.id = 'gspy-dialog-back';
  back.innerHTML = `<section class="modal gspy-dialog" role="dialog" aria-modal="true" aria-labelledby="gspy-dialog-title">
    <h2 id="gspy-dialog-title">${escapeHtml(title)}</h2>${html}</section>`;
  gspyDialog = {back, focus: document.activeElement, notebook: active};
  document.body.append(back);
  syncModals();
  back.addEventListener('keydown', event => {
    if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); closeGspyDialog(); }
    if (event.key === 'Tab') {
      const controls = [...back.querySelectorAll('button,input,select,textarea,[tabindex="0"]')].filter(e => !e.disabled && e.getClientRects().length);
      const first = controls[0], last = controls.at(-1);
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }
  });
  back.querySelector('[data-close]')?.addEventListener('click', closeGspyDialog);
  back.querySelector('input,textarea,button,select')?.focus();
  return back;
}

async function inspectGspyResult(producer, runId) {
  if (!producer) { flash('Link this value in the Values or Content editor.'); return; }
  const key = active;
  const run = runId ? await api(`/api/notebook/runs/${encodeURIComponent(runId)}${nbq()}`) : null;
  if (active !== key) return;
  const c = getCell(producer);
  const back = showGspyDialog('Producing code', `
    <p>${run ? `Saved run ${escapeHtml(run.run_id)} · ${escapeHtml(run.status)} · ${new Date(run.finished_at * 1000).toLocaleString()}` : 'No captured run yet.'}</p>
    ${run && c && run.source !== c.source ? '<p class="gspy-state stale">The current code differs from this producing revision.</p>' : ''}
    <pre class="gspy-source" tabindex="0">${escapeHtml(run?.source ?? c?.source ?? 'The producing block was deleted.')}</pre>
    <div class="gspy-dialog-actions"><button data-edit ${c ? '' : 'disabled'}>Edit current code</button>
    <button data-run ${c?.cell_type === 'code' ? '' : 'disabled'}>Run current code</button>
    <button data-history>History</button><button data-close>Close</button></div>`);
  back.querySelector('[data-edit]').onclick = () => {
    closeGspyDialog(); gspyCodeOpen.add(active + ':' + producer); render(); selectCell(producer);
    scrollToCell(producer); focusCellEditor(producer);
  };
  back.querySelector('[data-run]').onclick = () => { closeGspyDialog(); runCell(producer); };
  back.querySelector('[data-history]').onclick = () => { closeGspyDialog(); openCellHistory(producer); };
}

async function gspyRunThrough(id = null) {
  const key = active, t = activeTab();
  await flushNotebook(t);
  for (const cell of [...cells]) {
    if (active !== key) break;
    if (cell.cell_type === 'code' && cell.source.trim()) {
      const result = await runCell(cell.id);
      if (!result || result.outputs?.some(o => o.output_type === 'error')) break;
    }
    if (cell.id === id) break;
  }
}

function gspyProducerOptions(id, value) {
  const index = cells.findIndex(c => c.id === id);
  const producers = cells.slice(0, index + 1).filter(c => c.cell_type === 'code');
  const missing = value && !producers.some(c => c.id === value)
    ? `<option value="${escapeAttr(value)}" selected>Unavailable producer · ${escapeHtml(value)}</option>` : '';
  return missing + producers.map(c =>
    `<option value="${escapeAttr(c.id)}" ${c.id === value ? 'selected' : ''}>${escapeHtml(c.source.split('\n')[0].slice(0, 65) || 'Empty code')} · ${escapeHtml(c.id)}</option>`).join('');
}

async function openGspyContent(id) {
  const c = getCell(id); if (!c?.content) return;
  await saveCell(id);
  const config = structuredClone(c.content.definition), version = c.content.version;
  const code = c.cell_type === 'code';
  const back = showGspyDialog(code ? 'Block content' : 'Live values', `
    ${code ? `<label>Publish Python variables <input id="gspy-results" value="${escapeAttr(config.results.join(', '))}" placeholder="performance, sample_count"></label>
    <p>Capture these variables with each run. Use scalar values or JSON records; place figures and tables as outputs.</p>
    <label>Layout <select id="gspy-layout"><option value="flow">Document flow</option><option value="columns">Two columns</option><option value="slide">Slide section</option></select></label>
    <div id="gspy-components"></div><div class="gspy-dialog-actions"><button id="gspy-add-paragraph">Add paragraph</button><button id="gspy-add-output">Add output</button></div>` : '<p>Write references such as {{estimate}} in your Markdown. Link each name to a published result below.</p>'}
    <h3>Live values</h3><p>Use {{name}} in a paragraph. A field path such as estimate or ci.0 selects a scalar. Use .2f for two decimal places.</p>
    <div id="gspy-bindings"></div><button id="gspy-add-binding">Add value</button>
    <p id="gspy-save-error" role="alert"></p>
    <div class="gspy-dialog-actions"><button data-close>Cancel</button><button id="gspy-save-content">Save content</button></div>`);
  if (code) back.querySelector('#gspy-layout').value = config.layout;
  const components = code && !config.components.length
    ? [{id: 'outputs', kind: 'output', producer: id, result: '*'}] : config.components;
  const componentRoot = back.querySelector('#gspy-components');
  function componentRow(item) {
    const row = document.createElement('div'); row.className = 'gspy-component-editor'; row.dataset.id = item.id; row.dataset.kind = item.kind;
    row.innerHTML = item.kind === 'markdown'
      ? `<label>Paragraph (Markdown)<textarea data-source rows="4">${escapeHtml(item.source)}</textarea></label>`
      : `<label>Output from<select data-producer>${gspyProducerOptions(id, item.producer)}</select></label>
         <label>Output name<input data-result value="${escapeAttr(item.result)}" placeholder="* for all outputs, or pca"></label>`;
    row.insertAdjacentHTML('beforeend', '<div class="gspy-dialog-actions"><button data-up aria-label="Move component up">↑</button><button data-down aria-label="Move component down">↓</button><button data-remove>Remove</button></div>');
    row.querySelector('[data-remove]').onclick = () => row.remove();
    row.querySelector('[data-up]').onclick = () => { if (row.previousElementSibling) row.previousElementSibling.before(row); };
    row.querySelector('[data-down]').onclick = () => { if (row.nextElementSibling) row.nextElementSibling.after(row); };
    componentRoot.append(row);
  }
  if (code) {
    components.forEach(componentRow);
    back.querySelector('#gspy-add-paragraph').onclick = () => componentRow({id: 'p_' + crypto.randomUUID().replaceAll('-', ''), kind: 'markdown', source: ''});
    back.querySelector('#gspy-add-output').onclick = () => componentRow({id: 'o_' + crypto.randomUUID().replaceAll('-', ''), kind: 'output', producer: id, result: '*'});
  }
  const bindingRoot = back.querySelector('#gspy-bindings');
  function bindingRow(name, binding) {
    const row = document.createElement('div'); row.className = 'gspy-binding-editor';
    row.innerHTML = `<label>Name<input data-name value="${escapeAttr(name)}" placeholder="estimate"></label>
      <label>Producer<select data-producer>${gspyProducerOptions(id, binding.producer)}</select></label>
      <label>Variable<input data-result value="${escapeAttr(binding.result)}" placeholder="performance"></label>
      <label>Field path<input data-path value="${escapeAttr((binding.path || []).join('.'))}" placeholder="estimate"></label>
      <label>Format<input data-format value="${escapeAttr(binding.format || '')}" placeholder=".2f"></label><button data-remove>Remove</button>`;
    row.querySelector('[data-remove]').onclick = () => row.remove(); bindingRoot.append(row);
  }
  Object.entries(config.bindings).forEach(([name, binding]) => bindingRow(name, binding));
  back.querySelector('#gspy-add-binding').onclick = () => bindingRow('', {
    producer: code ? id : cells.slice(0, cells.indexOf(c)).filter(cell => cell.cell_type === 'code').at(-1)?.id || '', result: ''});
  back.querySelector('#gspy-save-content').onclick = async () => {
    const button = back.querySelector('#gspy-save-content'); button.disabled = true;
    try {
      if (code) {
        config.results = back.querySelector('#gspy-results').value.split(',').map(s => s.trim()).filter(Boolean);
        config.layout = back.querySelector('#gspy-layout').value;
        config.components = [...componentRoot.children].map(row => row.dataset.kind === 'markdown'
          ? {id: row.dataset.id, kind: 'markdown', source: row.querySelector('[data-source]').value}
          : {id: row.dataset.id, kind: 'output', producer: row.querySelector('[data-producer]').value, result: row.querySelector('[data-result]').value.trim() || '*'});
      }
      config.bindings = Object.create(null);
      for (const row of bindingRoot.children) {
        const value = selector => row.querySelector(selector).value.trim();
        const name = value('[data-name]');
        if (Object.hasOwn(config.bindings, name)) throw new Error('Value names must be unique');
        config.bindings[name] = {producer: value('[data-producer]'), result: value('[data-result]'),
          path: value('[data-path]').split('.').filter(Boolean).map(p => /^\d+$/.test(p) ? Number(p) : p), format: value('[data-format]')};
      }
      await api(`/api/cells/${id}/content?${new URLSearchParams({notebook: gspyDialog.notebook})}`,
        {method: 'PATCH', body: JSON.stringify({content: config, expected_version: version})});
      closeGspyDialog(); await load();
    } catch (error) { back.querySelector('#gspy-save-error').textContent = errText(error); button.disabled = false; }
  };
}

async function refreshGspy(notebook) {
  if (active === notebook) await load();
  else {
    const data = await api(notebookUrl(notebook));
    for (const entry of workspaceTabEntries(notebook)) entry.tab.cells = data.cells;
  }
}

function gspyExecutionEvent(msg, target) {
  target ||= msg.notebook ? workspaceTabEntries(msg.notebook).find(entry => entry.tab.format === 'gspy')?.tab : null;
  if (target?.format !== 'gspy') return false;
  if (msg.type === 'kernel_status' && ['idle', 'stopped', 'dead'].includes(msg.status)) {
    target.status = msg.status;
    if (msg.python) target.python = msg.python;
    refreshSessionCounts();
    if (msg.notebook === active) setKernelStatus(msg.status);
    refreshGspy(msg.notebook).catch(error => flash(errText(error)));
    return true;
  }
  if (!['cell_running', 'cell_output', 'cell_done'].includes(msg.type)) return false;
  const c = target.cells?.find(cell => cell.id === msg.cell_id);
  if (c) { c._running = msg.type !== 'cell_done'; if (msg.outputs) c.outputs = msg.outputs; }
  if (msg.notebook === active) {
    setCellRunning(msg.cell_id, msg.type !== 'cell_done');
    const area = document.getElementById('out-' + msg.cell_id);
    if (area) {
      area.closest('.output-area').hidden = msg.type === 'cell_done';
      area.innerHTML = msg.type === 'cell_output' ? renderCellOutput(c) : runningStatusHtml();
    }
  }
  if (msg.type === 'cell_done') {
    refreshGspy(msg.notebook).then(() => {
      runWaiters.get(msg.run_id)?.(msg);
      if (activeRuns.get(msg.notebook) === msg.run_id) activeRuns.delete(msg.notebook);
    }).catch(error => { flash(errText(error)); runWaiters.get(msg.run_id)?.(msg); });
    if (msg.outputs?.some(o => o.output_type === 'error')) flash(msg.outputs.find(o => o.output_type === 'error').evalue);
  }
  return true;
}
