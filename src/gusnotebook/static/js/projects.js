/* Ordinary report/notebook tabs, with versioning in the existing Git panel. */
window.ProjectWorkspace = (() => {
  const $ = id => document.getElementById('project-' + id);
  let projects = [], project = null, timer = null, loading = null, opening = null;
  const post = (path, body = {}) => api(path, {method:'POST', body:JSON.stringify(body)});
  const endpoint = suffix => `/api/projects/${project.id}${suffix}`;
  const act = async fn => { try { return await fn(); } catch (error) { flash(errText(error)); } };
  const notebookPath = id => project.notebooks[id] || project.notebook;
  function closeCreate() { $('create-back').classList.remove('on'); }
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
  async function openNotebook(id) { await openFile(notebookPath(id)); }
  async function openProducingNotebook(run) {
    await openFile(`${project.path}/results/runs/${run.id}/notebook.ipynb`);
  }
  function render() {
    $('links').hidden = false; $('label').textContent = project.name;
    $('job').textContent = project.job?.status === 'running' ? 'Preparing environment…'
      : project.data_access?.status === 'invalid' ? 'Check input policy'
      : project.data_access?.status === 'empty' ? 'No approved inputs' : 'Linked Jupyter notebook';
    $('job').classList.toggle('failed', project.job?.status === 'failed');
    $('job').title = project.job?.error || project.data_access?.error || project.path;
    $('cancel').hidden = project.job?.status !== 'running';
    clearTimeout(timer);
    timer = setTimeout(() => act(refresh), project.job?.status === 'running' ? 1200 : 5000);
  }
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
  $('data').onclick = () => act(() => openFile(project.data_readme));
  $('inputs').onclick = () => act(() => openFile(project.data_access.path));
  $('notebook').onclick = event => {
    const ids = Object.keys(project.notebooks);
    if (ids.length <= 1) return act(() => openNotebook(ids[0]));
    event.stopPropagation();
    const box = event.currentTarget.getBoundingClientRect();
    showFileCtx(box.left,box.bottom,ids.map(id => ({label:id,action:() => act(() => openNotebook(id))})), 'Linked notebooks');
  };
  $('cancel').onclick = () => act(async () => { await post(endpoint('/cancel')); await refresh(); });
  window.addEventListener('message', event => {
    const frame = document.getElementById('html-preview-frame');
    if (!project || event.source !== frame.contentWindow || event.origin !== activeTab()?.previewOrigin || event.data?.type !== 'gusproject-analysis') return;
    const run = project.runs.find(r => r.id === event.data.run && r.analysis_id === event.data.analysis);
    if (run) act(() => openProducingNotebook(run));
  });
  const init = () => act(async () => { await catalog(); await sync(); });
  if (booted) init(); else window.addEventListener('workspace-ready', init, {once:true});
  return {open:() => act(open), closeCreate, sync:() => act(sync),
    ownsDocument:(id,path) => projects.some(p => p.id === id && path?.startsWith(p.path + '/')),
    openProject:(id,terminal) => act(() => openProject(id,terminal)),
    changed:id => { if (project?.id === id) { clearTimeout(timer); timer = setTimeout(() => act(refresh),150); } }};
})();
