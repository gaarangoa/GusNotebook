/* Environment creation, installation progress, and installed package metadata. */
const environmentState = {view: 'create', session: null, job: null, timer: null,
  info: null, available: false, generation: 0, inspection: 0, opener: null};
const envElement = name => document.getElementById('environment-' + name);
const environmentBusy = () => environmentState.job &&
  ['creating', 'installing', 'inspecting'].includes(environmentState.job.status);
const environmentHeaders = () => ({'X-Session-Id': environmentState.session});

function environmentError(message = '') {
  envElement('error').textContent = message;
  envElement('error').hidden = !message;
}

function environmentDestination() {
  const location = envElement('location').value.trim();
  const parent = location.replace(/\/+$/, '');
  const name = envElement('name').value.trim();
  envElement('destination').textContent = location && name
    ? `New environment: ${parent}/${name}` : 'Choose the parent folder for the new environment.';
}

function environmentControls() {
  const busy = !!environmentBusy();
  envElement('fields').disabled = busy || !environmentState.available;
  envElement('submit').hidden = environmentState.view !== 'create';
  envElement('submit').disabled = busy || !environmentState.available;
  envElement('submit').textContent = busy ? 'Creating…' : 'Create environment';
  envElement('cancel').hidden = !busy;
  const notebook = activeTab();
  envElement('use').hidden = environmentState.view !== 'packages' || !environmentState.info ||
    !notebook || notebook.kind !== 'notebook';
  envElement('use').disabled = !environmentState.info || !environmentState.info.ipykernel;
  envElement('use').textContent = notebook ? `Use for ${notebook.name}` : 'Use for this notebook';
  envElement('use').title = environmentState.info && !environmentState.info.ipykernel
    ? 'This environment needs ipykernel to run notebooks' : 'Switch this notebook to the selected environment';
}

function environmentView(view) {
  environmentState.view = view;
  for (const name of ['create', 'packages']) {
    envElement(name).hidden = name !== view;
    envElement(name + '-tab').setAttribute('aria-selected', String(name === view));
  }
  environmentControls();
  if (view === 'packages' && !environmentState.info && envElement('select').value) {
    inspectEnvironment(envElement('select').value);
  }
}

async function openEnvironments(view = 'create', python = null) {
  closeVenvMenu();
  const previous = environmentBusy() ? environmentState.job : null;
  const generation = ++environmentState.generation;
  environmentState.inspection++;
  environmentState.session = currentSession;
  environmentState.opener = document.activeElement;
  environmentState.available = false;
  environmentState.info = null;
  environmentState.job = null;
  clearTimeout(environmentState.timer);
  envElement('select').replaceChildren();
  envElement('package-rows').replaceChildren();
  envElement('package-info').textContent = '';
  envElement('package-count').textContent = '';
  envElement('filter').value = '';
  envElement('progress').hidden = true;
  environmentError();
  envElement('back').classList.add('on');
  environmentView(view);
  const location = fileState.path || (active && active.split('/').slice(0, -1).join('/'));
  if (location) envElement('location').value = location;
  environmentDestination();
  try {
    const data = await api('/api/environments', {headers: environmentHeaders()});
    if (generation !== environmentState.generation) return;
    environmentState.available = data.uv_available;
    if (!envElement('location').value) envElement('location').value = data.location;
    envElement('python').placeholder = 'Default: ' + data.default_python;
    for (const environment of data.environments) environmentOption(environment);
    const selected = python || (activeTab() || {}).python || data.default_python;
    if (![...envElement('select').options].some(option => option.value === selected)) {
      environmentOption({python: selected, prefix: selected, label: 'Selected environment'});
    }
    envElement('select').value = selected;
    const job = data.jobs.find(job => ['creating', 'installing', 'inspecting'].includes(job.status)) ||
      (previous && data.jobs.find(job => job.id === previous.id));
    if (job) showEnvironmentJob(job);
    if (!data.uv_available && environmentState.view === 'create') {
      environmentError('Install uv on the machine running GusNotebook to create environments. Installed packages can still be inspected.');
    }
    environmentControls();
    environmentDestination();
    if (environmentState.view === 'packages' && !environmentState.info) await inspectEnvironment(selected);
    else if (!job) envElement('name').focus();
  } catch (error) {
    if (generation === environmentState.generation) environmentError(errText(error));
  }
}

function closeEnvironments() {
  envElement('back').classList.remove('on');
  clearTimeout(environmentState.timer);
  environmentState.generation++;
  environmentState.inspection++;
  if (environmentState.opener && environmentState.opener.isConnected) environmentState.opener.focus();
}

async function browseEnvironmentLocation() {
  const path = await openDirPicker(envElement('location').value || fileState.path,
    {mode: 'directory', title: 'Choose the environment location'});
  if (path) { envElement('location').value = path; environmentDestination(); }
}

async function browseEnvironmentRepository() {
  const path = await openDirPicker(fileState.path || envElement('location').value,
    {mode: 'directory', title: 'Choose a local Python repository'});
  if (path) {
    const repositories = envElement('repositories').value.trim().split('\n').filter(Boolean);
    if (!repositories.includes(path)) repositories.push(path);
    envElement('repositories').value = repositories.join('\n');
  }
}

async function createEnvironment() {
  if (environmentBusy() || envElement('submit').disabled) return;
  environmentError();
  const generation = environmentState.generation;
  envElement('submit').disabled = true;
  envElement('fields').disabled = true;
  try {
    const job = await api('/api/environments', {method: 'POST', headers: environmentHeaders(),
      body: JSON.stringify({name: envElement('name').value.trim(),
        location: envElement('location').value.trim(), python: envElement('python').value.trim(),
        packages: envElement('requirements').value, repositories: envElement('repositories').value,
        editable: envElement('editable').checked})});
    if (generation === environmentState.generation) showEnvironmentJob(job);
  } catch (error) {
    if (generation === environmentState.generation) environmentError(errText(error));
  } finally {
    if (generation === environmentState.generation) environmentControls();
  }
}

function showEnvironmentJob(job) {
  environmentState.job = job;
  envElement('progress').hidden = false;
  const labels = {creating: 'Creating the Python environment…', installing: 'Installing packages…',
    inspecting: 'Reading installed packages…', ready: 'Environment ready', failed: 'Creation failed', cancelled: 'Creation cancelled'};
  envElement('status').textContent = `${labels[job.status]} · ${job.path}`;
  const log = envElement('log');
  const atEnd = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
  log.textContent = job.log || 'Starting uv…';
  if (atEnd) log.scrollTop = log.scrollHeight;
  envElement('log-details').open = job.status !== 'ready';
  environmentControls();
  clearTimeout(environmentState.timer);
  if (environmentBusy()) {
    const generation = environmentState.generation;
    environmentState.timer = setTimeout(() => pollEnvironmentJob(job.id, generation), 700);
  } else if (job.status === 'ready') {
    environmentState.inspection++;
    environmentError();
    showEnvironmentPackages(job.environment);
    environmentView('packages');
  } else if (job.error) {
    environmentError(job.error);
  }
}

async function pollEnvironmentJob(id, generation) {
  try {
    const job = await api('/api/environments/jobs/' + id, {headers: environmentHeaders()});
    if (generation === environmentState.generation) showEnvironmentJob(job);
  } catch (error) {
    if (generation !== environmentState.generation) return;
    environmentError('Cannot read installation progress: ' + errText(error));
    environmentState.timer = setTimeout(() => pollEnvironmentJob(id, generation), 2000);
  }
}

async function cancelEnvironmentCreation() {
  if (!environmentBusy()) return;
  try {
    await api('/api/environments/jobs/' + environmentState.job.id,
      {method: 'DELETE', headers: environmentHeaders()});
  } catch (error) { environmentError(errText(error)); }
}

function environmentOption(info) {
  const select = envElement('select');
  if (![...select.options].some(option => option.value === info.python)) {
    select.add(new Option(`${info.label} · ${info.prefix}`, info.python));
  }
}

async function inspectEnvironment(python) {
  if (!python) return;
  const inspection = ++environmentState.inspection;
  environmentState.info = null;
  environmentError();
  envElement('package-info').textContent = 'Reading installed packages…';
  envElement('package-count').textContent = '';
  envElement('package-rows').replaceChildren();
  environmentControls();
  try {
    const info = await api('/api/environments/packages?' + new URLSearchParams({python}));
    if (inspection === environmentState.inspection) showEnvironmentPackages(info);
  } catch (error) {
    if (inspection !== environmentState.inspection) return;
    envElement('package-info').textContent = '';
    environmentError(errText(error));
  }
}

function showEnvironmentPackages(info) {
  environmentState.info = info;
  environmentOption(info);
  envElement('select').value = info.python;
  envElement('package-info').textContent = `Python ${info.version}\n${info.python}` +
    (info.ipykernel ? '' : '\nipykernel is not installed in this environment.');
  renderEnvironmentPackageRows();
  environmentControls();
}

function renderEnvironmentPackageRows() {
  const packages = environmentState.info ? environmentState.info.packages : [];
  const query = envElement('filter').value.trim().toLowerCase();
  const visible = packages.filter(p => `${p.name} ${p.version} ${p.local_path || ''}`.toLowerCase().includes(query));
  envElement('package-count').textContent = query ? `${visible.length} of ${packages.length} packages` : `${packages.length} installed packages`;
  envElement('package-rows').innerHTML = visible.length ? visible.map(p => `<tr>
    <td>${escapeHtml(p.name)}</td><td>${escapeHtml(p.version)}</td>
    <td>${escapeHtml(p.local_path || '')}${p.editable ? ' (editable)' : ''}</td></tr>`).join('')
    : '<tr><td colspan="3">No packages found.</td></tr>';
}

function refreshEnvironmentPackages() { return inspectEnvironment(envElement('select').value); }

async function browseInstalledEnvironment() {
  const chosen = await openDirPicker(fileState.path);
  if (chosen) await inspectEnvironment(chosen);
}

async function useInspectedEnvironment() {
  const info = environmentState.info, notebook = activeTab();
  if (!info || !notebook || notebook.kind !== 'notebook') return;
  envElement('use').disabled = true;
  try {
    await flushNotebook(notebook);
    if (activeTab() !== notebook) throw new Error('The active notebook changed; choose it again.');
    if (await setVenv(info.python)) closeEnvironments();
  } catch (error) { environmentError(errText(error)); }
  finally { environmentControls(); }
}

document.addEventListener('keydown', event => {
  if (!envElement('back').classList.contains('on')) return;
  if (event.key === 'Escape') {
    event.preventDefault(); event.stopImmediatePropagation();
    if (document.getElementById('dirpick-back').classList.contains('on')) dirPickCancel();
    else closeEnvironments();
  }
}, true);
