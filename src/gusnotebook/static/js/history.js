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
