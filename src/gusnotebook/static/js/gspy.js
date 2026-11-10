/* Saved result inspection, configuration and ordered execution. */
let gspyDialog = null;

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
  if (!producer) { flash('Link this value using Live values.'); return; }
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
    closeGspyDialog(); openGspyPython(producer).catch(error => flash(errText(error)));
  };
  back.querySelector('[data-run]').onclick = () => { closeGspyDialog(); runCell(producer); };
  back.querySelector('[data-history]').onclick = () => { closeGspyDialog(); openCellHistory(producer); };
}

async function gspyRunThrough(id = null) {
  const key = active, t = activeTab();
  try { await flushNotebook(t); }
  catch (error) { flash('Cannot run until the document is saved: ' + errText(error)); return; }
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
  await flushNotebook();
  const c = getCell(id); if (!c?.content) return;
  const config = structuredClone(c.content.definition), version = c.content.version;
  const code = c.cell_type === 'code';
  const back = showGspyDialog(code ? 'Python results' : 'Live values', `
    ${code ? `<label>Publish Python variables <input id="gspy-results" value="${escapeAttr(config.results.join(', '))}" placeholder="performance, sample_count"></label>
    <p>Capture these variables with each run. Use scalar values or JSON records; place figures and tables as outputs.</p>
    <label>Layout <select id="gspy-layout"><option value="flow">Document flow</option><option value="columns">Two columns</option><option value="slide">Slide section</option></select></label>
    <div id="gspy-components"></div><div class="gspy-dialog-actions"><button id="gspy-add-output">Add output</button></div>` : '<p>Write references such as {{estimate}} in this paragraph. Link each name to a published result below.</p>'}
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
    back.querySelector('#gspy-add-output').onclick = () => componentRow({id: gspyId(), kind: 'output', producer: id, result: '*'});
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
    for (const entry of workspaceTabEntries(notebook)) {
      entry.tab.cells = data.cells;
      entry.tab.editorVersion = data.editor_version;
    }
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
    if (target.gspyEditor) {
      refreshGspyEmbeds(target.gspyEditor);
      renderGspyInspector(target.gspyEditor);
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
