/* Links and audit for ordinary report/notebook tabs. The existing terminal is the chat. */
window.ProjectWorkspace = (() => {
  const $ = id => document.getElementById('project-' + id);
  let projects = [], project = null, timer = null, loading = null, opening = null;
  const post = (path, body = {}) => api(path, {method:'POST', body:JSON.stringify(body)});
  const endpoint = suffix => `/api/projects/${project.id}${suffix}`;
  const act = async fn => { try { return await fn(); } catch (error) { flash(errText(error)); } };
  const notebookPath = id => project.notebooks[id] || project.notebook;
  function closeCreate() { $('create-back').classList.remove('on'); }
  function closeHistory() { $('history-back').classList.remove('on'); }
  async function catalog() { projects = (await api('/api/projects')).projects; }
  async function open() {
    await catalog();
    $('existing').replaceChildren(new Option('New project', ''), ...projects.map(p => new Option(p.name, p.id)));
    $('existing').value = ''; $('existing').onchange();
    $('location').value = fileState.path || '';
    $('create-error').textContent = '';
    $('create-back').classList.add('on');
  }
  async function openProject(id, terminal = null) {
    if (opening === id) return;
    opening = id;
    try {
      await catalog();
      const data = await api('/api/projects/' + id);
      if (terminal && findTerm(terminal)) {
        const t = findTerm(terminal); t.project = id; t.label = data.name;
      }
      closeCreate();
      // Existing tabs and editors are the entire workspace, including the normal
      // notebook kernel picker, HTML source, visual tools, zoom and pan.
      await openFile(data.notebook, {quiet:true});
      await openFile(data.report);
      await browse(data.path);
      project = data; render(); ensurePanel('terminal');
      if (terminal && findTerm(terminal)) focusTerm(terminal);
      else renderTermTabs();
    } finally { opening = null; }
  }
  async function sync() {
    const owner = projects.filter(p => active?.startsWith(p.path + '/')).sort((a,b) => b.path.length - a.path.length)[0];
    if (!owner) { project = null; $('links').hidden = true; clearTimeout(timer); return; }
    if (project?.id === owner.id) { $('links').hidden = !project.report; return; }
    $('links').hidden = true;
    project = {...owner, analyses:{}, runs:[], events:[], sources:[], notebooks:{}};
    await refresh();
  }
  async function refresh() {
    if (!project) return;
    const id = project.id;
    if (loading === id) return;
    loading = id;
    try {
      const data = await api('/api/projects/' + id);
      if (project?.id !== id) return;
      project = data; render();
    } finally { if (loading === id) loading = null; }
  }
  function button(text, callback) {
    const b = document.createElement('button'); b.className = 'btn'; b.textContent = text;
    b.onclick = () => act(callback); return b;
  }
  async function openNotebook(id) {
    closeHistory(); await openFile(notebookPath(id));
  }
  async function openProducingNotebook(run) {
    closeHistory(); await openFile(`${project.path}/results/runs/${run.id}/notebook.ipynb`);
  }
  function renderHistory() {
    if (!project || !$('history-back').classList.contains('on')) return;
    $('history-title').textContent = project.name + ' · History';
    $('environment').textContent = project.environment?.runner === 'uv' ? 'uv · ' + project.environment.path : 'The agent prepares the project uv environment when an analysis needs it.';
    $('sources').replaceChildren(...project.sources.map(source => {
      const p = document.createElement('p'); p.textContent = `${source.id} · ${fmtSize(source.size)} · ${source.sha256.slice(0,12)}`; return p;
    }));
    $('reviews').replaceChildren(...Object.values(project.analyses).filter(a => a.displayed_run && a.displayed_run !== a.accepted_run).map(a => {
      const run = project.runs.find(r => r.id === a.displayed_run), item = document.createElement('section'); item.className = 'project-review';
      const title = document.createElement('strong'); title.textContent = `${a.id} · Ready for your review`;
      const info = document.createElement('p'); info.textContent = run?.comparison ? (run.comparison.passed ? 'Matches the previous accepted result.' : `${run.comparison.difference_count} differences from the accepted result.`) : 'New result, linked to its producing code and input data.';
      item.append(title, info, button('Open notebook', () => openNotebook(a.id)), button('Accept result', async () => {
        await post(endpoint('/accept'), {run_id:a.displayed_run, revision:project.report_revision}); await refresh();
      }));
      return item;
    }));
    const expandedRuns = new Set([...$('runs').querySelectorAll('details[open]')].map(el => el.dataset.run));
    $('runs').replaceChildren(...project.runs.slice().reverse().map(run => {
      const row = document.createElement('div'); row.className = 'project-run';
      const label = document.createElement('span'); label.textContent = `${run.analysis_id} · ${run.status} · ${run.started_at.slice(0,19).replace('T',' ')}`;
      const details = document.createElement('details'), summary = document.createElement('summary'), receipt = document.createElement('pre'); summary.textContent = 'Run receipt'; receipt.textContent = JSON.stringify(run,null,2); details.append(summary,receipt);
      details.dataset.run = run.id; details.open = expandedRuns.has(run.id);
      row.append(label, button('Open notebook', () => openNotebook(run.analysis_id)));
      if (run.status === 'completed') row.append(button('Producing notebook', () => openProducingNotebook(run)), button('Replay', async () => { await post(endpoint(`/analyses/${run.analysis_id}/run`), {replay:run.id}); await refresh(); }));
      row.append(details); return row;
    }));
    const expanded = new Set([...$('history').querySelectorAll('details[open]')].map(el => el.dataset.event));
    $('history').replaceChildren(...project.events.slice().reverse().map(event => {
      const article = document.createElement('article'), title = document.createElement('strong');
      title.textContent = `${event.time.slice(11,19)} · ${event.kind.replaceAll('_',' ')}`; article.append(title);
      if (event.text) { const p = document.createElement('p'); p.textContent = event.text; article.append(p); }
      const details = document.createElement('details'), summary = document.createElement('summary'), json = document.createElement('pre');
      details.dataset.event = event.event_id; details.open = expanded.has(event.event_id); summary.textContent = 'Details'; json.textContent = JSON.stringify(event,null,2); details.append(summary,json); article.append(details); return article;
    }));
    $('history-download').href = BASE + endpoint('/history');
  }
  function render() {
    $('links').hidden = false; $('label').textContent = project.name;
    const pending = Object.values(project.analyses).filter(a => a.displayed_run && a.displayed_run !== a.accepted_run).length;
    $('review').hidden = !pending; $('review').textContent = `Review ${pending} result${pending === 1 ? '' : 's'}`;
    $('job').textContent = project.job ? `${project.job.kind}: ${project.job.status}` : 'Linked Jupyter notebook';
    $('job').classList.toggle('failed', project.job?.status === 'failed');
    $('job').title = project.job?.error || project.path;
    $('cancel').hidden = project.job?.status !== 'running';
    renderHistory(); clearTimeout(timer);
    timer = setTimeout(() => act(refresh), project.job?.status === 'running' ? 1200 : 5000);
  }
  async function history() { await refresh(); $('history-back').classList.add('on'); renderHistory(); }
  $('existing').onchange = () => {
    const existing = !!$('existing').value; $('create-fields').hidden = existing;
    $('name').required = $('location').required = !existing;
    $('create-submit').textContent = existing ? 'Open project' : 'Create project';
  };
  $('create').onsubmit = async event => {
    event.preventDefault(); $('create-submit').disabled = true;
    try {
      const existing = $('existing').value;
      const data = await post(existing ? `/api/projects/${existing}/open` : '/api/projects', existing ? {} : {name:$('name').value.trim(), location:$('location').value.trim()});
      await openProject(data.id);
    } catch (error) { $('create-error').textContent = errText(error); }
    finally { $('create-submit').disabled = false; }
  };
  $('report').onclick = () => act(() => openFile(project.report));
  $('notebook').onclick = event => {
    const ids = Object.keys(project.notebooks);
    if (ids.length <= 1) return act(() => openNotebook(ids[0]));
    const box = event.currentTarget.getBoundingClientRect();
    showFileCtx(box.left,box.bottom,ids.map(id => ({label:id,action:() => act(() => openNotebook(id))})), 'Linked notebooks');
  };
  $('history-button').onclick = $('review').onclick = () => act(history);
  $('cancel').onclick = () => act(async () => { await post(endpoint('/cancel')); await refresh(); });
  window.addEventListener('message', event => {
    const frame = document.getElementById('html-preview-frame');
    if (!project || event.source !== frame.contentWindow || event.origin !== activeTab()?.previewOrigin || event.data?.type !== 'gusproject-analysis') return;
    const run = project.runs.find(r => r.id === event.data.run && r.analysis_id === event.data.analysis);
    if (run) act(() => openProducingNotebook(run));
  });
  const init = () => act(async () => { await catalog(); await sync(); });
  if (booted) init(); else window.addEventListener('workspace-ready', init, {once:true});
  return {open:() => act(open), closeCreate, closeHistory, sync:() => act(sync),
    terminalProject:() => project, openProject:(id,terminal) => act(() => openProject(id,terminal)),
    changed:id => { if (project?.id === id) { clearTimeout(timer); timer = setTimeout(() => act(refresh),150); } }};
})();
