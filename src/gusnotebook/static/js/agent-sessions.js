/* One folder-scoped catalog of native agent conversations. */
window.AgentHistory = (() => {
  const el = name => document.getElementById('agent-history-' + name);
  let folder, workspace, serial = 0, next = null, timer, opener, busy = false;
  const isOpen = () => el('back').classList.contains('on');
  const error = message => { el('error').textContent = message; el('error').hidden = !message; };
  const date = seconds => seconds ? new Date(seconds * 1000).toLocaleString() : 'Date unavailable';
  const providerName = row => row.provider === 'codex' ? 'Codex' : row.provider === 'claude' ? 'Claude' : row.provider;

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
    el('catalog').hidden = false;
    el('detail').hidden = true;
    error('');
  }

  async function resume(row) {
    if (busy || workspace !== currentSession) return;
    busy = true;
    el('resume').disabled = true;
    try {
      const t = await openTerminal(row.cwd, row.provider, null, row.id);
      if (t) { close(); focusTerm(t.id); }
    } catch (err) {
      error('Cannot resume: ' + errText(err));
    } finally {
      busy = false;
      el('resume').disabled = false;
    }
  }

  async function preview(row) {
    const request = ++serial;
    el('catalog').hidden = true;
    el('detail').hidden = false;
    el('conversation-title').textContent = row.title;
    el('conversation-meta').textContent = `${providerName(row)} · ${date(row.updated)} · ${row.id}`;
    el('preview-help').textContent = 'Loading conversation…';
    el('messages').replaceChildren();
    el('resume').disabled = !row.available;
    el('resume').title = row.available ? 'Continue this saved conversation' : `${providerName(row)} CLI is not installed on this computer`;
    el('resume').onclick = () => resume(row);
    error('');
    const query = new URLSearchParams({folder: row.cwd, provider: row.provider, id: row.id});
    try {
      const data = await api('/api/agent-sessions/preview?' + query);
      if (request !== serial || !isOpen() || workspace !== currentSession) return;
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
        meta.textContent = `${providerName(row)} · ${date(row.updated)}${row.project ? ' · Project agent' : ''}`;
        const path = document.createElement('span');
        path.textContent = row.cwd;
        view.append(title, meta, path);
        view.onclick = () => preview(row);
        const launch = document.createElement('button');
        launch.className = 'tb';
        launch.textContent = 'Resume';
        launch.disabled = !row.available;
        launch.title = row.available ? 'Continue in the terminal' : `${providerName(row)} CLI is not installed on this computer`;
        launch.onclick = async () => { launch.disabled = true; await resume(row); launch.disabled = !row.available; };
        item.append(view, launch);
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
  return {open, close, refresh: () => { back(); return refresh(); }, more: () => refresh(true), back};
})();
