let historyGroups = [];

function closeHistory() {
  document.getElementById('history-back').classList.remove('on');
}

async function openHistory() {
  document.getElementById('history-back').classList.add('on');
  const list = document.getElementById('history-list');
  list.textContent = 'Loading changes…';
  try {
    const data = await api('/api/history');
    historyGroups = data.groups;
    list.innerHTML = historyGroups.length ? historyGroups.map(group => `
      <section class="history-group">
        <div class="history-heading"><strong>${escapeHtml(group.prompt)}</strong>
          <span>${escapeHtml(new Date(group.created * 1000).toLocaleString())}</span></div>
        <p>${group.active ? 'Recording' : (group.undone ? 'Restored' : 'Recorded')} ·
          ${group.changes.length} changed document${group.changes.length === 1 ? '' : 's'}
          ${group.interrupted ? ' · recording recovered after restart' : ''}</p>
        ${group.skipped.length ? `<p>${group.skipped.length} documents could not be recorded.</p>` : ''}
        ${group.changes.map(change => `<details><summary>${escapeHtml(change.path)}</summary>
          <pre>${escapeHtml(change.error || change.diff || 'Notebook outputs or metadata changed.')}</pre>
          ${change.truncated ? '<p>Diff shortened for display.</p>' : ''}</details>`).join('')}
        ${group.active
          ? `<button class="tb" onclick="finishHistory('${group.id}')">Finish recording</button>`
          : !group.undone && group.changes.length
            ? `<button class="tb" onclick="undoHistory('${group.id}')">Undo these changes</button>` : ''}
      </section>`).join('') : 'No recorded changes yet. Agent requests start a recording automatically.';
  } catch (error) {
    list.textContent = 'Cannot load changes: ' + errText(error);
  }
}

async function beginHistory() {
  try {
    await flushNotebook();
    await api('/api/history', {method: 'POST', body: JSON.stringify({prompt: 'Manual recording'})});
    await openHistory();
  } catch (error) { flash(errText(error)); }
}

async function finishHistory(id) {
  try {
    await api(`/api/history/${id}/finish`, {method: 'POST'});
    await openHistory();
  } catch (error) { flash(errText(error)); }
}

async function undoHistory(id) {
  const group = historyGroups.find(group => group.id === id);
  if (!group) return;
  const changed = new Set(group.changes.map(change => change.path));
  if ([...changed].some(path => workspaceTabEntries(path).some(entry => entry.tab.dirty))) {
    flash('Save or reload your pending edits before restoring these documents.');
    return;
  }
  try {
    await api(`/api/history/${id}/undo`, {method: 'POST',
      body: JSON.stringify({revision: group.revision})});
    await load();
    await openHistory();
    flash('Recorded changes restored');
  } catch (error) { flash('Cannot restore: ' + errText(error)); }
}

document.addEventListener('keydown', event => {
  if (event.key === 'Escape') closeHistory();
});

let cellHistoryTarget = null;
let cellHistoryData = null;
let cellHistoryLoad = 0;

function cellHistorySummaryText(summary = {}) {
  const parts = [['requests', 'request'], ['edits', 'edit'], ['copies', 'copy'],
    ['exports', 'report'], ['notes', 'note']].filter(([key]) => summary[key])
    .map(([key, name]) => `${summary[key]} ${summary[key] === 1 ? name : name === 'copy' ? 'copies' : name + 's'}`);
  return parts.join(' · ') || (summary.count ? `${summary.count} event${summary.count === 1 ? '' : 's'}` : 'Add context');
}

function cellHistoryUrl(target = cellHistoryTarget) {
  return `/api/cells/${encodeURIComponent(target.id)}/history?` + new URLSearchParams({notebook: target.path});
}

function updateCellHistorySummary(path, id, summary) {
  for (const entry of workspaceTabEntries(path)) {
    const cell = entry.tab.cells?.find(c => c.id === id);
    if (cell) cell.history_summary = summary;
  }
  if (active === path) {
    const cell = getCell(id);
    if (cell) cell.history_summary = summary;
    for (const node of document.querySelectorAll(`[data-cell-history-summary="${CSS.escape(id)}"]`)) {
      node.textContent = cellHistorySummaryText(summary);
    }
  }
}

function closeCellHistory() {
  cellHistoryLoad++;
  cellHistoryTarget = null;
  document.getElementById('cell-history-back').classList.remove('on');
}

async function openCellHistory(id) {
  const path = active;
  if (!path || !getCell(id)) return;
  cellHistoryTarget = {path, id};
  cellHistoryData = null;
  document.getElementById('cell-history-title').textContent = `Cell ${cells.findIndex(c => c.id === id) + 1} history`;
  document.getElementById('cell-history-note').value = '';
  document.getElementById('cell-history-back').classList.add('on');
  await refreshCellHistory();
}

function renderCellHistory(data) {
  const detail = (label, content) => content ? `<details><summary>${escapeHtml(label)}</summary><pre>${escapeHtml(content)}</pre></details>` : '';
  document.getElementById('cell-history-summary').textContent = `${cellHistorySummaryText(data.summary)} · ${cellHistoryTarget.path}`;
  document.getElementById('cell-history-list').innerHTML = data.events.length ? [...data.events].reverse().map(event => `
    <section class="history-group cell-history-event" data-event-kind="${escapeAttr(event.kind)}">
      <div class="history-heading"><strong>${escapeHtml(event.summary || event.kind)}</strong>
        <span>${event.at ? escapeHtml(new Date(event.at * 1000).toLocaleString()) : 'Date not recorded'}</span></div>
      <p>${escapeHtml(event.actor || 'GusNotebook')}${event.terminal ? ' · ' + escapeHtml(event.terminal) : ''}</p>
      ${event.kind === 'legacy_request' ? '<p>Only this previously saved request is available; earlier turns may be missing.</p>' : ''}
      ${event.prompt && ['request', 'legacy_request'].includes(event.kind) ? `<div class="cell-history-text">${escapeHtml(event.prompt)}</div>` : ''}
      ${event.note ? `<div class="cell-history-text">${escapeHtml(event.note)}</div>` : ''}
      ${event.destination ? `<p>Saved in <button class="history-file-link" data-history-path="${escapeAttr(event.destination)}">${escapeHtml(event.destination)}</button></p>` : ''}
      ${event.attachment ? `<p>Attachment: ${escapeHtml(event.attachment)}</p>` : ''}
      ${event.snapshot_id ? `<p>Snapshot: ${escapeHtml(event.snapshot_id)}</p>` : ''}
      ${event.output ? `<p>${event.output.count} output(s) · ${escapeHtml(event.output.errors?.join(', ') || 'Completed')}</p>` : ''}
      ${detail('Source changes', event.diff)}
      ${event.before && event.after && event.before.cell_type !== event.after.cell_type ? `<p>${escapeHtml(event.before.cell_type)} → ${escapeHtml(event.after.cell_type)}</p>` : ''}
      ${detail('Source at this step', event.source ?? event.after?.source)}
      ${!['request', 'legacy_request'].includes(event.kind) ? detail('Related agent request', event.prompt) : ''}
      ${event.preferences_before ? detail('Previous agent preferences', JSON.stringify(event.preferences_before, null, 2)) : ''}
      ${event.preferences ? detail(event.preferences_source || 'Agent preferences',
        [event.preferences.workspace_instructions, event.preferences.session_instructions,
          Object.keys(event.preferences.restrictions || {}).length ? 'Restrictions: ' + JSON.stringify(event.preferences.restrictions, null, 2) : '']
          .filter(Boolean).join('\n\n') || 'No additional instructions recorded.') : ''}
    </section>`).join('') : '<p>No recorded events yet. Add a note to start this cell’s timeline.</p>';
  for (const button of document.querySelectorAll('[data-history-path]')) {
    button.onclick = async () => {
      try { await openFile(button.dataset.historyPath); closeCellHistory(); }
      catch (error) { flash(errText(error)); }
    };
  }
}

async function refreshCellHistory() {
  const target = cellHistoryTarget;
  if (!target) return;
  const serial = ++cellHistoryLoad;
  const errorBox = document.getElementById('cell-history-error');
  errorBox.hidden = true;
  document.getElementById('cell-history-list').textContent = 'Loading history…';
  try {
    const data = await api(cellHistoryUrl(target));
    if (serial !== cellHistoryLoad || cellHistoryTarget !== target) return;
    cellHistoryData = data;
    updateCellHistorySummary(target.path, target.id, data.summary);
    renderCellHistory(data);
  } catch (error) {
    if (serial !== cellHistoryLoad) return;
    errorBox.textContent = 'Cannot load cell history: ' + errText(error);
    errorBox.hidden = false;
    document.getElementById('cell-history-list').textContent = '';
  }
}

async function addCellHistoryNote() {
  const target = cellHistoryTarget;
  const field = document.getElementById('cell-history-note');
  if (!target || !field.value.trim()) return;
  const button = document.getElementById('cell-history-note-save');
  button.disabled = true;
  try {
    await api(cellHistoryUrl(target), {method: 'POST', body: JSON.stringify({kind: 'note', note: field.value})});
    if (cellHistoryTarget === target) { field.value = ''; await refreshCellHistory(); }
  } catch (error) {
    const box = document.getElementById('cell-history-error');
    box.textContent = 'Note was not saved: ' + errText(error); box.hidden = false;
  } finally { button.disabled = false; }
}

function downloadCellHistory() {
  if (!cellHistoryData || !cellHistoryTarget) return;
  const data = {notebook: cellHistoryTarget.path, ...cellHistoryData};
  const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], {type: 'application/json'}));
  const link = document.createElement('a');
  link.href = url; link.download = `cell-${cellHistoryTarget.id}-history.json`; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
