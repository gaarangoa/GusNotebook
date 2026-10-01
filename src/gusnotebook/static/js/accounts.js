/* A persistent sign-in workflow, opened from the bottom of the activity bar. */
let accountState = null, accountTimer = null, accountPolling = false, accountLoaded = false;
let accountRequestPending = false, accountRequestError = '';

function closeAccounts(restoreFocus = false) {
  const popup = document.getElementById('accounts-popover');
  if (restoreFocus || popup.contains(document.activeElement)) document.getElementById('accounts-button').focus();
  popup.hidden = true;
  document.getElementById('accounts-button').setAttribute('aria-expanded', 'false');
}
function toggleAccounts(event) {
  event?.stopPropagation();
  const popup = document.getElementById('accounts-popover');
  if (!popup.hidden) { closeAccounts(); return; }
  closeWorkspaceMenu(); closeFileCtx(); closeNewMenu();
  popup.hidden = false;
  document.getElementById('accounts-button').setAttribute('aria-expanded', 'true');
  popup.querySelector('button').focus();
  if (!accountLoaded) { accountLoaded = true; refreshAccounts(); }
  else pollAccounts();
}
async function pollAccounts() {
  if (accountPolling) return;
  accountPolling = true;
  try {
    accountState = await api('/api/accounts');
    renderAccounts();
  } catch (error) { accountNotice(errText(error)); }
  finally { accountPolling = false; scheduleAccountPoll(); }
}
function scheduleAccountPoll() {
  clearTimeout(accountTimer);
  if (!document.getElementById('accounts-popover').hidden || accountState?.activity?.state === 'running') {
    accountTimer = setTimeout(pollAccounts, 700);
  }
}
function accountNotice(message) {
  const notice = document.getElementById('account-error');
  notice.textContent = message; notice.hidden = !message;
}
async function accountRequest(body, method = 'POST') {
  if (accountRequestPending) return;
  accountRequestPending = true; accountRequestError = '';
  for (const button of document.querySelectorAll('#git-login, #git-auth-refresh, #account-microsoft')) button.disabled = true;
  try {
    accountState = await api('/api/accounts', {method, ...(body ? {body:JSON.stringify(body)} : {})});
  } catch (error) { accountRequestError = errText(error); }
  finally { accountRequestPending = false; renderAccounts(); scheduleAccountPoll(); }
}
function refreshAccounts() { return accountRequest({}); }
function accountSignIn(provider = 'github') {
  const missing = accountState?.setup?.tools.some(tool => !tool.available && (provider === 'github' || tool.name === 'devtunnel'));
  return accountRequest({login:true, provider, setup:!!missing});
}
function cancelAccountLogin() { return accountRequest(null, 'DELETE'); }

function renderAccounts() {
  if (!accountState) {
    accountNotice(accountRequestError);
    for (const button of document.querySelectorAll('#git-login, #git-auth-refresh, #account-microsoft')) button.disabled = accountRequestPending;
    return;
  }
  const {git, tunnels, activity, setup} = accountState;
  const signedIn = git.account?.logged_in || git.shared;
  document.getElementById('git-account').textContent = git.account?.logged_in
    ? 'GitHub · ' + (git.account.login || 'Signed in')
    : git.shared ? 'GitHub credentials shared from your local app' + (git.shared_account ? ' · ' + git.shared_account : '')
    : 'GitHub · Not signed in';
  const tunnelAccount = document.getElementById('tunnel-account');
  tunnelAccount.hidden = !tunnels;
  tunnelAccount.textContent = tunnels?.account?.logged_in
    ? 'Tunnels · ' + [tunnels.account.provider, tunnels.account.username].filter(Boolean).join(' · ')
    : 'Tunnels · Not signed in';
  const button = document.getElementById('accounts-button');
  button.querySelector('.account-dot').hidden = !(signedIn || tunnels?.account?.logged_in);
  button.title = signedIn ? 'Accounts · ' + (git.account.login || git.shared_account || 'GitHub') : 'Accounts';
  const running = accountRequestPending || activity?.state === 'running' || git.activity?.state === 'running' || tunnels?.activity?.state === 'running';
  const login = document.getElementById('git-login');
  const missing = setup?.tools.filter(tool => !tool.available) || [];
  const needsSetup = missing.length > 0;
  login.disabled = running || (needsSetup ? !setup.supported : !git.gh_available);
  const connected = signedIn && (!tunnels || tunnels.cli_error ||
    (tunnels.account?.logged_in && tunnels.account.provider.toLowerCase() === 'github' &&
      (!git.account.login || tunnels.account.username.toLowerCase() === git.account.login.toLowerCase())));
  login.textContent = needsSetup ? (activity?.kind === 'setup' && activity.state === 'error' ? 'Retry setup' : 'Set up GitHub')
    : connected ? 'GitHub connected' : signedIn ? 'Connect GitHub to tunnels' : 'Sign in with GitHub';
  login.disabled ||= !!connected && !needsSetup;
  document.getElementById('git-auth-refresh').disabled = running;
  const microsoft = document.getElementById('account-microsoft');
  if (microsoft) {
    const missingTunnel = missing.some(tool => tool.name === 'devtunnel');
    microsoft.disabled = running || (missingTunnel ? !setup.supported : !!tunnels?.cli_error);
    microsoft.textContent = missingTunnel ? 'Set up tunnels with Microsoft' : 'Use Microsoft for tunnels';
  }
  document.getElementById('git-install').hidden = git.gh_available || git.shared || setup?.supported;
  const install = document.getElementById('account-tunnel-install');
  install.textContent = needsSetup && setup.supported ? 'Setup installs ' + missing.map(tool => tool.label).join(' and ') + ', then starts sign-in.'
    : (needsSetup ? setup?.error : '') || tunnels?.cli_error || '';
  install.hidden = !install.textContent;
  const progress = document.getElementById('account-tool-progress');
  progress.replaceChildren();
  progress.hidden = !activity?.tools?.length || activity.state === 'done';
  const labels = {gh:'GitHub CLI', devtunnel:'Dev Tunnels'};
  const states = {pending:'Waiting', preparing:'Preparing download', downloading:'Downloading', installing:'Installing', checking:'Checking installation', available:'Ready', failed:'Installation failed', canceled:'Canceled'};
  for (const tool of activity?.tools || []) {
    const row = document.createElement('div');
    row.textContent = labels[tool.name] + ' · ' + (states[tool.state] || tool.state);
    if (tool.state === 'downloading' && tool.total) row.textContent += ` (${Math.min(100, Math.round(100 * tool.received / tool.total))}%)`;
    progress.append(row);
  }
  const gitOutput = document.getElementById('git-login-output');
  gitOutput.textContent = git.activity?.kind === 'login' && git.activity.state === 'running' ? git.activity.output || '' : '';
  gitOutput.hidden = !gitOutput.textContent;
  document.getElementById('git-login-link').hidden = gitOutput.hidden;
  const tunnelOutput = document.getElementById('tunnel-login-output');
  tunnelOutput.textContent = tunnels?.activity?.kind === 'login' && tunnels.activity.state === 'running' ? tunnels.activity.output || '' : '';
  tunnelOutput.hidden = !tunnelOutput.textContent;
  const links = document.getElementById('tunnel-login-links');
  links.replaceChildren();
  for (const value of new Set(tunnelOutput.textContent.match(/https:\/\/[^\s<>"']+/g) || [])) {
    try {
      const url = new URL(value.replace(/[.,;)]+$/, ''));
      if (!['github.com', 'microsoft.com', 'www.microsoft.com', 'login.microsoftonline.com'].includes(url.hostname)) continue;
      const link = document.createElement('a'); link.href = url.href; link.target = '_blank'; link.rel = 'noopener noreferrer';
      link.textContent = 'Open sign-in page'; links.append(link);
    } catch (_) {}
  }
  document.getElementById('account-login-help').hidden = !running || !['login', 'setup'].includes(activity?.kind) || activity.phase === 'install' || !tunnels;
  document.getElementById('account-login-cancel').hidden = activity?.state !== 'running';
  document.getElementById('account-login-cancel').textContent = activity?.phase === 'install' ? 'Cancel setup' : 'Cancel sign-in';
  document.getElementById('account-activity').textContent = running
    ? activity?.phase === 'install' ? 'Installing account tools…'
      : ['login', 'setup'].includes(activity?.kind) ? (activity.phase === 'tunnels' ? 'Connecting tunnel account…' : 'Connecting GitHub…') : 'Checking accounts…'
    : activity?.state === 'done' && ['login', 'setup'].includes(activity.kind) ? 'Account connected'
      : activity?.state === 'canceled' ? (activity.phase === 'install' ? 'Setup canceled' : 'Sign-in canceled') : '';
  accountNotice(accountRequestError || activity?.error || '');
}
document.addEventListener('click', event => {
  if (!event.target.closest('#accounts-popover, #accounts-button')) closeAccounts();
});
document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && !document.getElementById('accounts-popover').hidden && !document.querySelector('.modal-back.on')) {
    event.preventDefault(); event.stopImmediatePropagation(); closeAccounts(true);
  }
}, true);
document.addEventListener('DOMContentLoaded', pollAccounts);
