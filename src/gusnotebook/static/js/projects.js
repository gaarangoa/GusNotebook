/* Report-first projects live beside ordinary notebooks, with their own broker. */
const ProjectWorkspace = (() => {
  const $ = id => document.getElementById('project-' + id);
  let project = null, poll = null, reportRevision = null, generation = 0, selectedRun = '', dirty = false;
  const endpoint = suffix => `/api/projects/${project.id}${suffix}`;
  const post = (path, data = {}) => api(path, {method: 'POST', body: JSON.stringify(data)});
  const sourceIds = () => $('analysis-sources').value.split(',').map(s => s.trim()).filter(Boolean);
  const analysisId = () => $('analysis-id').value.trim();
  function status(text, error = false) { $('status').textContent = text; $('status').classList.toggle('project-error', error); }
  async function act(action) {
    try { await action(); } catch (error) {
      let message = String(error); try { message = JSON.parse(message).error || message; } catch (_) {}
      status(message, true);
    }
  }
  function preview(html, label) {
    // No host cookies, same-origin access, network, nested frames, forms or popups.
    // Workers cannot supply markup: their output passes a JSON-only renderer.
    const policy = `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; font-src data:; connect-src 'none'; frame-src 'none'; form-action 'none'; base-uri 'none'">`;
    $('preview').srcdoc = policy + html;
    $('preview-label').textContent = label;
  }
  async function showReport() {
    const id = project.id, data = await api(endpoint('/report'));
    if (project?.id !== id) return;
    reportRevision = data.revision;
    preview(data.html, 'Accepted results');
  }
  function fillAnalysis() {
    const key = $('analysis-picker').value, analysis = project?.analyses[key];
    $('analysis-id').value = key || 'analysis';
    $('analysis-sources').value = (analysis?.source_ids || project.sources.map(s => s.id)).join(', ');
    $('parameters').value = JSON.stringify(analysis?.parameters || {}, null, 2);
    $('code').value = analysis?.code || '';
    dirty = false; renderRuns();
  }
  function renderRuns() {
    const key = analysisId(), analysis = project.analyses[key];
    const runs = project.runs.filter(r => r.analysis_id === key).slice().reverse();
    $('analysis-state').textContent = analysis?.report_modified ? 'Generated report content was edited or removed. Its displayed values are no longer verified by the accepted run.' : analysis?.stale ? 'Changed since the accepted run. Rerun and review.' : analysis?.accepted_run ? 'Accepted result has saved provenance.' : 'No accepted result yet.';
    $('run-picker').replaceChildren(...runs.map(run => new Option(`${run.started_at.slice(11,19)} · ${run.status}${run.id === analysis?.accepted_run ? ' · accepted' : ''} · ${run.id.slice(-6)}`, run.id)));
    if (runs.some(r => r.id === selectedRun)) $('run-picker').value = selectedRun;
    else selectedRun = $('run-picker').value;
    inspectRun();
  }
  function inspectRun() {
    selectedRun = $('run-picker').value;
    const run = project.runs.find(r => r.id === selectedRun);
    $('producing-code').hidden = !run;
    $('producing-notebook').hidden = run?.status !== 'completed';
    if (run) {
      $('producing-code').href = BASE + endpoint(`/runs/${run.id}/analysis.py`);
      $('producing-notebook').href = BASE + endpoint(`/runs/${run.id}/notebook.ipynb`);
    }
    $('receipt').textContent = run ? JSON.stringify(run, null, 2) : 'Run an analysis to create a receipt.';
    const comparison = run?.comparison;
    $('comparison').textContent = run?.error || (comparison ?
      `${comparison.passed ? 'Results match' : comparison.difference_count + ' result differences'} within rtol ${comparison.rtol}, atol ${comparison.atol}. ${comparison.lineage_changes.length ? 'Changed: ' + comparison.lineage_changes.join(', ') : 'Same recorded lineage.'}` : 'The next run will compare with the accepted result.');
    if (run?.package) $('comparison').textContent += ` Package verified: ${run.package.name}. Same-schema fixture passed; scientific validation remains your responsibility.`;
  }
  function render() {
    $('body').hidden = false; $('create').hidden = true;
    $('picker').value = project.id;
    const current = $('analysis-picker').value;
    $('analysis-picker').replaceChildren(new Option('New analysis', ''), ...Object.keys(project.analyses).map(id => new Option(id, id)));
    $('analysis-picker').value = current in project.analyses ? current : Object.keys(project.analyses)[0] || '';
    if (!dirty) fillAnalysis(); else renderRuns();
    $('sources').replaceChildren(...project.sources.map(source => {
      const row = document.createElement('div'), name = document.createElement('strong'), info = document.createElement('p'), remove = document.createElement('button');
      name.textContent = source.id; info.textContent = `${source.schema.map(c => c.name).join(', ')} · ${fmtSize(source.size)} · ${source.sha256.slice(0, 12)}`;
      remove.textContent = 'Revoke'; remove.disabled = project.job?.status === 'running';
      remove.onclick = () => act(async () => { await api(endpoint('/sources/' + source.id), {method:'DELETE'}); await refresh(); });
      row.append(name, info, remove); return row;
    }));
    $('env-info').textContent = project.environment ? `Locked runtime ready · ${project.environment.image.slice(0, 22)}. Package versions are recorded in each receipt.` : 'Prepare a locked uv environment in Docker. Dependency preparation has network access; analysis execution does not.';
    const messages = project.events.filter(e => ['user_message', 'assistant_message', 'analysis_ready'].includes(e.kind));
    $('messages').replaceChildren(...messages.map(event => {
      const item = document.createElement('article'); item.dataset.role = event.kind === 'user_message' ? 'user' : 'assistant';
      item.textContent = event.text; return item;
    }));
    $('history').replaceChildren(...project.events.slice().reverse().map(event => {
      const item = document.createElement('details'), title = document.createElement('summary'), content = document.createElement('pre');
      title.textContent = `${event.time.slice(11,19)} · ${event.kind.replaceAll('_', ' ')}`;
      content.textContent = JSON.stringify(event, null, 2); item.append(title, content); return item;
    }));
    $('history-download').href = BASE + endpoint('/history');
    const running = project.job?.status === 'running';
    document.querySelectorAll('[data-project-idle]').forEach(button => button.disabled = running);
    $('cancel').hidden = !running;
    if (project.job) status(`${project.job.kind}: ${project.job.status}${project.job.error ? ' · ' + project.job.error : ''}`, project.job.status === 'failed');
    clearTimeout(poll);
    if (running && $('workspace').open) poll = setTimeout(() => act(refresh), 1500);
  }
  async function refresh() {
    const id = project.id, serial = generation;
    const latest = await api(endpoint(''));
    if (generation !== serial || project?.id !== id) return;
    const finishedChat = project.job?.kind === 'chat' && project.job.status === 'running' && latest.job?.status === 'completed';
    const changedReport = latest.report_revision !== reportRevision && $('preview-label').textContent === 'Accepted results';
    project = latest; render();
    if ((finishedChat || changedReport) && $('html-editor').hidden) await showReport();
  }
  async function select(id) {
    const serial = ++generation; clearTimeout(poll); dirty = false; selectedRun = '';
    if (!id) { project = null; $('create').hidden = false; $('body').hidden = true; status(''); return; }
    const latest = await api('/api/projects/' + id);
    if (generation !== serial) return;
    project = latest; render(); await showReport();
  }
  async function list() {
    const data = await api('/api/projects');
    $('picker').replaceChildren(new Option('New project', ''), ...data.projects.map(p => new Option(p.name, p.id)));
  }
  async function open() {
    if (!$('workspace').open) $('workspace').showModal();
    $('location').value ||= fileState.path || '';
    await list();
    if (project) { await refresh(); $('picker').value = project.id; }
    else await select($('picker').options[1]?.value || '');
    const runtime = await api('/api/projects/runner'); $('runtime').textContent = runtime.message;
  }
  function close() { $('workspace').close(); clearTimeout(poll); }
  $('close').onclick = close;
  $('chat-settings').onclick = () => act(async () => { close(); await openSettings(); settingsSection('projects'); });
  $('workspace').addEventListener('close', () => clearTimeout(poll));
  $('picker').onchange = () => act(() => select($('picker').value));
  $('new').onclick = () => act(() => select(''));
  $('create').onsubmit = e => { e.preventDefault(); act(async () => {
    const created = await post('/api/projects', {name: $('name').value.trim(), location: $('location').value.trim()});
    await list(); await select(created.id); status('Project created. Approve data and prepare the environment.');
  }); };
  $('source-form').onsubmit = e => { e.preventDefault(); act(async () => {
    await post(endpoint('/sources'), {sources:[{id:$('source-id').value.trim(), path:$('source-path').value.trim()}]});
    await refresh(); status('File snapshot approved.');
  }); };
  $('source-list-file').onchange = () => act(async () => {
    const file = $('source-list-file').files[0]; if (!file) return;
    if (file.size > 100000) throw Error('Source list exceeds 100 KB');
    $('source-list').value = await file.text();
  });
  $('source-list-add').onclick = () => act(async () => { await post(endpoint('/sources'), JSON.parse($('source-list').value)); await refresh(); });
  $('prepare').onclick = () => act(async () => { await post(endpoint('/prepare')); await refresh(); });
  $('analysis-picker').onchange = fillAnalysis;
  $('analysis-form').oninput = () => { dirty = true; };
  $('analysis-form').onsubmit = e => { e.preventDefault(); act(async () => {
    const key = analysisId();
    await post(endpoint('/analyses/' + encodeURIComponent(key)), {parameters: JSON.parse($('parameters').value), source_ids: sourceIds(),
      code: $('code').value || null, revision: project.analyses[key]?.code_revision || 'new'});
    dirty = false; await refresh(); $('analysis-picker').value = key; fillAnalysis(); status('Analysis saved.');
  }); };
  $('pca').onclick = () => {
    $('code').value = ''; dirty = true;
    const source = project.sources.find(s => s.id === sourceIds()[0]);
    $('parameters').value = JSON.stringify({source: source?.id || '', columns: [], standardize: true}, null, 2);
    status('Fill columns with the numeric features to analyze, then Save analysis.');
  };
  async function run(options = {}) {
    if (dirty) throw Error('Save your analysis changes first.');
    await post(endpoint('/analyses/' + encodeURIComponent(analysisId()) + '/run'), options);
    selectedRun = ''; await refresh();
  }
  $('run').onclick = () => act(() => run());
  $('package').onclick = () => act(() => run({package:true}));
  $('replay').onclick = () => act(() => { if (!selectedRun) throw Error('Select a run.'); return run({replay:selectedRun}); });
  $('cancel').onclick = () => act(async () => { await post(endpoint('/cancel')); status('Cancelling…'); });
  $('chat-form').onsubmit = e => { e.preventDefault(); act(async () => {
    const text = $('prompt').value;
    await post(endpoint('/chat'), {prompt:text, analysis_id:analysisId(), source_ids:sourceIds()});
    $('prompt').value = ''; dirty = false; selectedRun = ''; await refresh();
  }); };
  $('run-picker').onchange = inspectRun;
  $('candidate').onclick = () => act(async () => {
    if (!selectedRun) throw Error('Select a completed run.');
    const data = await api(endpoint(`/runs/${selectedRun}/view.html`)); preview(data.html, 'Candidate · ' + selectedRun.slice(-6));
  });
  $('accept').onclick = () => act(async () => {
    if (!selectedRun) throw Error('Select a completed run.');
    project = await post(endpoint('/accept'), {run_id:selectedRun, revision:reportRevision}); render(); await showReport(); status('Result accepted into report.');
  });
  $('report-show').onclick = () => act(showReport);
  $('edit-report').onclick = () => act(async () => {
    const data = await api(endpoint('/report')); reportRevision = data.revision;
    $('html').value = data.html; $('html-editor').hidden = false; $('preview').hidden = true;
  });
  $('discard-report').onclick = () => { $('html-editor').hidden = true; $('preview').hidden = false; };
  $('save-report').onclick = () => act(async () => {
    await post(endpoint('/report'), {html:$('html').value, revision:reportRevision});
    $('discard-report').click(); await showReport(); await refresh(); status('Report saved.');
  });
  $('open-html').onclick = () => { const path = project.path + '/reports/deck.html'; close(); openFile(path); };
  $('notebook').onclick = () => {
    const path = project.path + '/notebooks/' + analysisId() + '.ipynb'; close(); openFile(path);
  };
  window.addEventListener('message', event => {
    if (event.source !== $('preview').contentWindow || event.data?.type !== 'gusproject-analysis' || !project) return;
    const run = project.runs.find(r => r.id === event.data.run && r.analysis_id === event.data.analysis);
    if (!run) return;
    $('analysis-picker').value = run.analysis_id; selectedRun = run.id; fillAnalysis(); $('receipt').parentElement.open = true;
  });
  return {open: () => act(open)};
})();
