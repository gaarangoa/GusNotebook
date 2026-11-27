/* One folder-scoped catalog of native agent conversations. */
window.AgentHistory = (() => {
  const el = name => document.getElementById('agent-history-' + name);
  let folder, workspace, serial = 0, next = null, timer, opener, busy = false, selected = null;
  const isOpen = () => el('back').classList.contains('on');
  const error = message => { el('error').textContent = message; el('error').hidden = !message; };
  const date = seconds => seconds ? new Date(seconds * 1000).toLocaleString() : 'Date unavailable';
  const providerName = row => row.provider === 'codex' ? 'Codex' : row.provider === 'claude' ? 'Claude' : row.provider;

  function setBusy(value) {
    busy = value;
    el('back').setAttribute('aria-busy', String(value));
    for (const button of el('back').querySelectorAll('[data-agent-action]')) {
      button.disabled = value || button.dataset.disabled === 'true';
    }
  }

  function configure(button, disabled, title, action) {
    button.dataset.agentAction = 'true';
    button.dataset.disabled = String(disabled);
    button.disabled = busy || disabled;
    button.title = title;
    button.onclick = action;
  }

  function updateTermLabels(change) {
    if (!change.title) return;
    for (const terminal of terms) {
      if (change.terminals?.includes(terminal.id) ||
          (terminal.kind === change.provider && terminal.agent_session === change.id)) {
        terminal.agent_session = change.id;
        terminal.label = change.title.slice(0, 60);
      }
    }
    renderTermTabs();
  }

  function close() {
    ++serial;
    clearTimeout(timer);
    el('back').classList.remove('on');
    syncModals();
    opener?.focus();
  }

  async function open() {
    if (!fileState.path) { flash('Choose a folder in Files first.'); return; }
    opener = document.activeElement;
    if (folder !== fileState.path) {
      el('query').value = '';
      el('provider').value = '';
      el('children').checked = false;
    }
    folder = fileState.path;
    workspace = currentSession;
    el('folder').textContent = folder;
    el('back').classList.add('on');
    syncModals();
    back();
    el('query').focus();
    await refresh();
  }

  function back() {
    ++serial;
    selected = null;
    el('catalog').hidden = false;
    el('detail').hidden = true;
    error('');
  }

  async function resume(row) {
    if (busy || workspace !== currentSession) return;
    setBusy(true);
    error('');
    try {
      const t = await openTerminal(row.cwd, row.provider, null, row.id);
      if (t) { close(); focusTerm(t.id); }
    } catch (err) {
      error('Cannot resume: ' + errText(err));
    } finally {
      setBusy(false);
    }
  }

  async function rename(row) {
    if (busy || workspace !== currentSession) return;
    setBusy(true);
    error('');
    try {
      const answer = askName('Rename agent session', row.title, 'Saved title in ' + providerName(row) + ' · up to 180 characters', 'Save');
      document.getElementById('ask-input').select();
      const title = await answer;
      if (title === null || workspace !== currentSession) return;
      const data = await api('/api/agent-sessions', {method:'PATCH', body:JSON.stringify({
        folder:row.cwd, provider:row.provider, id:row.id, title,
      })});
      updateTermLabels(data);
      if (!isOpen() || workspace !== currentSession) return;
      if (selected?.id === row.id && selected.provider === row.provider) await preview(data.session);
      else await refresh();
    } catch (err) {
      error('Cannot rename: ' + errText(err));
    } finally { setBusy(false); }
  }

  async function remove(row) {
    if (busy || workspace !== currentSession) return;
    setBusy(true);
    error('');
    try {
      const confirmed = await askConfirm('Delete “' + row.title + '”?',
        'Permanently deletes this ' + providerName(row) + ' conversation and its subagent history from this computer. Your notebooks and project files are kept.', 'Delete session');
      if (!confirmed || workspace !== currentSession) return;
      await api('/api/agent-sessions', {method:'DELETE', body:JSON.stringify({
        folder:row.cwd, provider:row.provider, id:row.id,
      })});
      if (!isOpen() || workspace !== currentSession) return;
      back();
      await refresh();
    } catch (err) {
      error('Cannot delete: ' + errText(err));
    } finally { setBusy(false); }
  }

  function detailsActions(row) {
    configure(el('resume'), !row.available, row.available ? 'Continue this saved conversation' : `${providerName(row)} CLI is not installed on this computer`, () => resume(row));
    configure(el('rename'), !row.manageable, row.manageable ? 'Rename this saved conversation' : 'Install Codex CLI to rename its sessions', () => rename(row));
    configure(el('delete'), !row.manageable || row.running, row.running ? 'Close the running agent terminal before deleting this conversation' : 'Delete this saved conversation', () => remove(row));
  }

  async function preview(row) {
    const request = ++serial;
    selected = row;
    el('catalog').hidden = true;
    el('detail').hidden = false;
    el('conversation-title').textContent = row.title;
    el('conversation-meta').textContent = `${providerName(row)} · ${date(row.updated)} · ${row.id}`;
    el('preview-help').textContent = 'Loading conversation…';
    el('messages').replaceChildren();
    detailsActions(row);
    error('');
    const query = new URLSearchParams({folder: row.cwd, provider: row.provider, id: row.id});
    try {
      const data = await api('/api/agent-sessions/preview?' + query);
      if (request !== serial || !isOpen() || workspace !== currentSession) return;
      selected = data.session;
      detailsActions(selected);
      el('preview-help').textContent = data.truncated
        ? 'Recent conversation excerpt. Resume loads the saved session in its agent. Tool output and images are omitted from this preview.'
        : 'Conversation text. Tool output and images are omitted from this preview.';
      for (const message of data.messages) {
        const section = document.createElement('section');
        section.className = 'agent-history-message';
        const heading = document.createElement('strong');
        heading.textContent = message.role === 'user' ? 'You' : providerName(row);
        const content = document.createElement('pre');
        content.textContent = message.text + (message.truncated ? '\n[… shortened for preview]' : '');
        section.append(heading, content);
        el('messages').append(section);
      }
      if (!data.messages.length) el('messages').textContent = 'No conversation text in the available excerpt. Resume to view the session in its agent.';
    } catch (err) {
      if (request === serial) { el('preview-help').textContent = ''; error(errText(err)); }
    }
  }

  async function refresh(append = false) {
    const request = ++serial;
    error('');
    el('status').textContent = 'Loading agent sessions…';
    el('more').hidden = true;
    if (!append) el('list').replaceChildren();
    const query = new URLSearchParams({folder, provider: el('provider').value, q: el('query').value,
      children: el('children').checked ? '1' : '0', offset: append && next !== null ? next : 0});
    try {
      const data = await api('/api/agent-sessions?' + query);
      if (request !== serial || !isOpen() || workspace !== currentSession) return;
      const selected = el('provider').value;
      el('provider').replaceChildren(new Option('All agents', ''), ...data.providers.map(p => new Option(p.name, p.id)));
      el('provider').value = selected;
      el('status').textContent = `${data.total} saved session${data.total === 1 ? '' : 's'}${data.truncated ? ' · Discovery limit reached; some sessions may be omitted.' : ''}`;
      for (const row of data.sessions) {
        const item = document.createElement('div');
        item.className = 'agent-history-row';
        const view = document.createElement('button');
        view.className = 'agent-history-view';
        const title = document.createElement('strong');
        title.textContent = row.title;
        const meta = document.createElement('span');
        meta.textContent = `${providerName(row)} · ${date(row.updated)}${row.project ? ' · Project context' : ''}${row.running ? ' · Open in terminal' : ''}`;
        const path = document.createElement('span');
        path.textContent = row.cwd;
        view.append(title, meta, path);
        view.onclick = () => preview(row);
        const launch = document.createElement('button');
        launch.className = 'tb';
        launch.textContent = 'Resume';
        configure(launch, !row.available, row.available ? 'Continue in the terminal' : `${providerName(row)} CLI is not installed on this computer`, () => resume(row));
        const actions = document.createElement('div');
        actions.className = 'agent-history-actions';
        actions.append(launch);
        for (const [label, glyph, disabled, hint, action] of [
          ['Rename session', 'edit', !row.manageable, 'Rename this saved conversation', () => rename(row)],
          ['Delete session', 'trash', !row.manageable || row.running, row.running ? 'Close the running agent terminal before deleting this conversation' : 'Delete this saved conversation', () => remove(row)],
        ]) {
          const button = document.createElement('button');
          button.className = 'tb agent-history-action';
          button.innerHTML = icon(glyph);
          button.setAttribute('aria-label', label);
          configure(button, disabled, row.manageable ? hint : 'Install Codex CLI to manage its sessions', action);
          actions.append(button);
        }
        item.append(view, actions);
        el('list').append(item);
      }
      if (!data.total) el('list').textContent = 'No matching saved Claude or Codex sessions. Try including subfolders or choosing another folder in Files.';
      next = data.next_offset;
      el('more').hidden = next === null;
    } catch (err) {
      if (request === serial) { el('status').textContent = ''; error(errText(err)); }
    }
  }

  el('query').oninput = () => { clearTimeout(timer); timer = setTimeout(() => refresh(), 200); };
  el('provider').onchange = () => refresh();
  el('children').onchange = () => refresh();
  function changed(change) {
    updateTermLabels(change);
    if (busy || !isOpen()) return;
    if (selected?.id === change.id && selected.provider === change.provider) {
      if (change.deleted) { back(); refresh(); }
      else { selected.title = change.title; el('conversation-title').textContent = change.title; }
    } else if (!selected) refresh();
  }
  return {open, close, changed, refresh: () => { back(); return refresh(); }, more: () => refresh(true),
    back: () => { back(); return refresh(); }};
})();
