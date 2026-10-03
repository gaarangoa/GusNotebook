/* Optional local connection manager; account controls live in Accounts. */
let tunnelState = null, tunnelPanelActive = false, tunnelPollTimer = null;
let tunnelLoaded = false, tunnelPolling = false, tunnelRenderKey = '';
let tunnelDetailsId = null;
const tunnelGitRetryPending = new Set();

function syncTunnelPanel() {
  const visible = !!document.getElementById('tunnels') && layoutPrefs.sidebarSection === 'tunnels' && filesVisible();
  if (visible === tunnelPanelActive) return;
  tunnelPanelActive = visible;
  clearTimeout(tunnelPollTimer);
  if (visible) pollTunnels();
}
async function pollTunnels() {
  if (tunnelPolling) return;
  tunnelPolling = true;
  try {
    tunnelState = await api('/api/tunnels');
    renderTunnels();
    if (!tunnelLoaded) {
      tunnelLoaded = true;
      if (!tunnelState.cli_error) await refreshTunnels();
    }
  } catch (error) {
    const notice = document.getElementById('tunnel-error');
    notice.hidden = false; notice.textContent = errText(error);
  } finally {
    tunnelPolling = false;
    clearTimeout(tunnelPollTimer);
    if (tunnelPanelActive) tunnelPollTimer = setTimeout(pollTunnels, 1000);
  }
}
async function tunnelRequest(path, body = {}, method = 'POST') {
  try {
    tunnelState = await api('/api/tunnels' + path, {method, body:JSON.stringify(body)});
    renderTunnels();
  } catch (error) { flash(errText(error)); }
}
function refreshTunnels() { return tunnelRequest('/refresh'); }

function renderTunnels() {
  if (!tunnelState || !document.getElementById('tunnels')) return;
  if (tunnelDetailsId) renderTunnelDetails(tunnelState.saved?.find(row => row.id === tunnelDetailsId));
  const key = JSON.stringify(tunnelState);
  if (key === tunnelRenderKey) return;
  tunnelRenderKey = key;
  const {activity, saved = [], discovered = []} = tunnelState;
  const running = activity?.state === 'running';
  document.getElementById('tunnel-refresh').disabled = running || !!tunnelState.cli_error;
  const notice = document.getElementById('tunnel-error');
  notice.textContent = tunnelState.error || tunnelState.cli_error || activity?.error || '';
  notice.hidden = !notice.textContent;
  const list = document.getElementById('tunnel-list');
  const focus = document.activeElement?.closest('[data-tunnel-key]');
  const focusedKey = focus?.dataset.tunnelKey, focusedMenu = document.activeElement?.classList.contains('tunnel-more');
  list.replaceChildren();
  function section(title, rows, registered) {
    if (!rows.length) return;
    const heading = document.createElement('div'); heading.className = 'tunnel-group'; heading.textContent = title;
    list.append(heading);
    for (const row of rows) {
      const container = document.createElement('div'); container.className = 'tunnel-row';
      container.dataset.tunnelKey = row.id || row.tunnel;
      const button = document.createElement('button'); button.className = 'tunnel-open';
      button.title = row.error || row.tunnel;
      button.innerHTML = `<span class="tunnel-dot ${escapeAttr(row.state)}"></span><span class="tunnel-label">${escapeHtml(row.name)}<small>${escapeHtml(row.state === 'unknown' ? 'Not checked' : row.state[0].toUpperCase()+row.state.slice(1))}</small></span>`;
      button.addEventListener('click', () => openTunnelWindow(row));
      container.append(button);
      const more = document.createElement('button'); more.className = 'tunnel-more files-btn';
      more.setAttribute('aria-label', (registered ? 'Actions for ' : 'Save ') + row.name);
      more.innerHTML = registered ? icon('more') : icon('plus');
      more.addEventListener('click', event => registered ? tunnelMenu(event, row) : saveDiscoveredTunnel(row));
      container.append(more); list.append(container);
      if (container.dataset.tunnelKey === focusedKey) (focusedMenu ? more : button).focus({preventScroll:true});
    }
  }
  section('Saved', saved, true);
  section('On your account', discovered, false);
  if (!saved.length && !discovered.length) {
    const empty = document.createElement('p'); empty.className = 'tunnel-help';
    empty.textContent = 'No tunnels yet. Add a tunnel ID or sign in and refresh.'; list.append(empty);
  }
}
function openTunnelWindow(row) {
  const url = new URL(BASE + '/tunnels/connect', location.origin);
  if (row.id) url.searchParams.set('id', row.id);
  else url.searchParams.set('tunnel', row.tunnel);
  url.searchParams.set('name', row.name);
  window.open(url.href, '_blank', 'noopener');
}
async function saveDiscoveredTunnel(row) {
  try {
    await api('/api/tunnels', {method:'POST', body:JSON.stringify({name:row.name, tunnel:row.tunnel})});
    await pollTunnels();
  } catch (error) { flash(errText(error)); }
}
function tunnelMenu(event, row) {
  event.stopPropagation();
  const rect = event.currentTarget.getBoundingClientRect();
  showFileCtx(rect.left, rect.bottom, [
    {label:'Open in new window', action:() => openTunnelWindow(row)},
    ...(['connecting','connected'].includes(row.state) ? [{label:row.state === 'connecting' ? 'Cancel connection' : 'Disconnect',
      action:() => tunnelRequest('/' + row.id + '/disconnect')}] : []),
    {label:'Rename…', action:async () => {
      const name = await askName('Rename tunnel', row.name, row.tunnel, 'Rename');
      if (name === null || !name.trim()) return;
      try { await api('/api/tunnels/' + row.id, {method:'PATCH', body:JSON.stringify({name:name.trim()})}); await pollTunnels(); }
      catch (error) { flash(errText(error)); }
    }},
    {label:'Details', action:() => openTunnelEditor(row)},
    {label:'Remove from list', action:async () => {
      if (!await askConfirm(`Remove ${row.name}?`, 'Hides this tunnel from the list and disconnects locally. Add its tunnel ID again to restore it. The remote workspace keeps running.', 'Remove')) return;
      try { await api('/api/tunnels/' + row.id, {method:'DELETE'}); await pollTunnels(); }
      catch (error) { flash(errText(error)); }
    }},
  ], 'Tunnel actions');
}
function openTunnelEditor(row = null) {
  tunnelDetailsId = row?.id || null;
  document.getElementById('tunnel-title').textContent = row ? 'Tunnel details' : 'Add tunnel';
  for (const [id, value] of [['tunnel-name', row?.name || ''], ['tunnel-id', row?.tunnel || '']]) {
    const input = document.getElementById(id); input.value = value; input.readOnly = !!row;
  }
  document.getElementById('tunnel-save').hidden = !!row;
  document.getElementById('tunnel-form-error').textContent = '';
  renderTunnelDetails(row);
  document.getElementById('tunnel-back').classList.add('on');
  document.getElementById(row ? 'tunnel-id' : 'tunnel-name').focus();
}
function renderTunnelDetails(row) {
  const pending = tunnelGitRetryPending.has(row?.id) || row?.git_retrying;
  const text = row
    ? [row.state, row.url, pending ? 'Retrying Git credential sharing…'
       : row.git_sharing ? 'GitHub credential sharing connected' : row.git_error,
       row.last_connected ? 'Last connected: ' + new Date(row.last_connected * 1000).toLocaleString() : '', row.error].filter(Boolean).join('\n')
    : tunnelDetailsId ? 'This saved tunnel is no longer available.'
    : 'Use the full tunnel ID printed by GusNotebook on the remote computer.';
  const details = document.getElementById('tunnel-details');
  if (details.textContent !== text) details.textContent = text;
  const retry = document.getElementById('tunnel-git-retry');
  retry.hidden = !row || row.state !== 'connected' || (!row.git_error && !pending);
  retry.disabled = !!pending;
  retry.textContent = pending ? 'Retrying…' : 'Retry Git sharing';
}
async function retryTunnelGitSharing() {
  if (!tunnelDetailsId || tunnelGitRetryPending.has(tunnelDetailsId)) return;
  const identifier = tunnelDetailsId;
  tunnelGitRetryPending.add(identifier);
  document.getElementById('tunnel-form-error').textContent = '';
  renderTunnelDetails(tunnelState.saved.find(row => row.id === identifier));
  try {
    tunnelState = await api('/api/tunnels/' + encodeURIComponent(identifier) + '/git/retry', {method:'POST', body:'{}'});
    renderTunnels();
  } catch (error) {
    if (tunnelDetailsId === identifier) document.getElementById('tunnel-form-error').textContent = errText(error);
  } finally {
    tunnelGitRetryPending.delete(identifier);
    if (tunnelDetailsId) renderTunnelDetails(tunnelState.saved.find(row => row.id === tunnelDetailsId));
  }
}
function closeTunnelEditor() {
  tunnelDetailsId = null;
  document.getElementById('tunnel-back').classList.remove('on');
}
async function saveTunnelEntry() {
  const button = document.getElementById('tunnel-save'); button.disabled = true;
  try {
    await api('/api/tunnels', {method:'POST', body:JSON.stringify({
      name:document.getElementById('tunnel-name').value.trim(), tunnel:document.getElementById('tunnel-id').value.trim(),
    })});
    closeTunnelEditor(); await pollTunnels();
  } catch (error) { document.getElementById('tunnel-form-error').textContent = errText(error); }
  finally { button.disabled = false; }
}
document.addEventListener('DOMContentLoaded', syncTunnelPanel);
