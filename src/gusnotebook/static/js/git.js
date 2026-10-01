/* Git always runs on the computer that owns this workspace. */
let gitState = null, gitPanelActive = false, gitTimer = null, gitPolling = false;
let gitRenderKey = '', gitActionPending = false;
const gitMessages = new Map();
let gitMessageRoot = null;

function syncGitPanel() {
  const visible = layoutPrefs.sidebarSection === 'git' && filesVisible();
  if (visible === gitPanelActive) return;
  gitPanelActive = visible;
  clearTimeout(gitTimer);
  if (visible) refreshGit();
}
async function refreshGit() {
  if (gitPolling) return;
  gitPolling = true;
  const path = fileState.path;
  try {
    const state = await api('/api/git?' + new URLSearchParams(path ? {path} : {}));
    if (path !== fileState.path) return;
    gitState = state;
    renderGit();
  } catch (error) { gitNotice(errText(error)); }
  finally {
    gitPolling = false;
    clearTimeout(gitTimer);
    if (gitPanelActive) gitTimer = setTimeout(refreshGit, 1500);
  }
}
function gitNotice(message) {
  const element = document.getElementById('git-error');
  element.textContent = message; element.hidden = !message;
}
async function gitCancelOperation() {
  try { await api('/api/git/operation', {method:'DELETE'}); await refreshGit(); }
  catch (error) { gitNotice(errText(error)); }
}
function renderGit() {
  const state = gitState;
  if (!state) return;
  const key = JSON.stringify([state, gitActionPending]);
  if (key === gitRenderKey) return;
  gitRenderKey = key;
  const operation = state.operation;
  const busy = gitActionPending || operation?.state === 'running';
  document.getElementById('git-root').textContent = state.root || state.directory;
  document.getElementById('git-root').title = state.root || state.directory;
  document.getElementById('git-branch').textContent = state.root
    ? `${state.branch} · ${state.ahead} outgoing · ${state.behind} incoming` : 'No repository in this folder';
  gitNotice(operation?.error || (state.truncated ? 'Showing the first 3,000 changed files.' : ''));
  document.getElementById('git-operation').textContent = operation?.state === 'running'
    ? `${operation.action}…` : operation?.state === 'done' ? `${operation.action} completed` : operation?.state === 'canceled' ? 'Operation canceled' : '';
  document.getElementById('git-operation-cancel').hidden = operation?.state !== 'running';
  document.getElementById('git-init').hidden = !!state.root;
  document.getElementById('git-init').disabled = busy;
  document.getElementById('git-network').hidden = !state.root;
  document.querySelectorAll('#git-network button').forEach(button => { button.disabled = busy; });
  const message = document.getElementById('git-message');
  if (gitMessageRoot !== state.root) {
    if (gitMessageRoot) gitMessages.set(gitMessageRoot, message.value);
    gitMessageRoot = state.root; message.value = gitMessages.get(state.root) || '';
  }
  if (operation?.action === 'commit' && operation.state === 'done' && operation.root === state.root && gitSubmittedMessage?.root === state.root) {
    if (message.value === gitSubmittedMessage.message) message.value = '';
    gitSubmittedMessage = null;
  }
  document.getElementById('git-commit').hidden = !state.root;
  document.getElementById('git-commit-button').disabled = busy || !(state.changes || []).some(row => row.staged) || (state.changes || []).some(row => row.conflict);
  const container = document.getElementById('git-changes');
  const focused = document.activeElement?.dataset.gitFocus;
  container.replaceChildren();
  for (const staged of [true, false]) {
    const rows = (state.changes || []).filter(row => staged ? row.staged : row.unstaged);
    if (!rows.length) continue;
    const group = document.createElement('div'); group.className = 'git-group';
    const title = document.createElement('span'); title.textContent = `${staged ? 'Staged' : 'Changes'} (${rows.length})`;
    const all = document.createElement('button'); all.className = 'strip-new'; all.textContent = staged ? 'Unstage all' : 'Stage all'; all.disabled = busy;
    all.onclick = () => gitAction(staged ? 'unstage' : 'stage', gitFilePaths(rows, staged));
    group.append(title, all); container.append(group);
    for (const row of rows) {
      const line = document.createElement('div'); line.className = 'git-row';
      const file = document.createElement('button'); file.className = 'git-file';
      file.textContent = row.path; file.title = row.old_path ? row.old_path + ' → ' + row.path : row.path;
      file.dataset.gitFocus = `${staged}:${row.path}:diff`; file.onclick = () => showGitDiff(state.root, row.path, staged);
      const flag = document.createElement('span'); flag.className = 'git-status'; flag.textContent = row.conflict ? '!' : row.status.trim();
      const action = document.createElement('button'); action.className = 'files-btn'; action.textContent = staged ? '−' : '+';
      action.setAttribute('aria-label', (staged ? 'Unstage ' : 'Stage ') + row.path); action.disabled = busy;
      action.dataset.gitFocus = `${staged}:${row.path}:stage`; action.onclick = () => gitAction(staged ? 'unstage' : 'stage', gitFilePaths([row], staged));
      line.append(file, flag, action); container.append(line);
      for (const element of [file, action]) if (element.dataset.gitFocus === focused) element.focus({preventScroll:true});
    }
  }
  if (state.root && !state.changes.length) {
    const clean = document.createElement('p'); clean.className = 'tunnel-help'; clean.textContent = 'Working tree clean'; container.append(clean);
  }
}
function gitFilePaths(rows, unstaging) {
  return [...new Set(rows.flatMap(row => row.old_path && (unstaging || row.status[0] === ' ') ? [row.old_path, row.path] : [row.path]))];
}
let gitSubmittedMessage = null;
async function gitAction(action, files) {
  if (!gitState || gitActionPending) return;
  const path = gitState.root || gitState.directory;
  const message = document.getElementById('git-message').value;
  gitActionPending = true; renderGit();
  try {
    await api('/api/git/operation', {method:'POST', body:JSON.stringify({action, path, files, message})});
    if (action === 'commit') gitSubmittedMessage = {root:path, message};
    await refreshGit();
  } catch (error) { gitNotice(errText(error)); }
  finally { gitActionPending = false; gitRenderKey = ''; }
}
async function showGitDiff(path, file, staged) {
  try {
    const data = await api('/api/git/diff?' + new URLSearchParams({path, file, staged:staged ? '1' : '0'}));
    document.getElementById('git-diff-title').textContent = `${staged ? 'Staged' : 'Changes'} · ${file}`;
    document.getElementById('git-diff-text').textContent = (data.text || 'No text changes') + (data.truncated ? '\n\n[Diff truncated]' : '');
    document.getElementById('git-diff-back').classList.add('on');
    document.getElementById('git-diff-text').focus();
  } catch (error) { gitNotice(errText(error)); }
}
function closeGitDiff() { document.getElementById('git-diff-back').classList.remove('on'); }
document.addEventListener('DOMContentLoaded', syncGitPanel);
