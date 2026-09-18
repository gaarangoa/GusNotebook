/* Persist preferred widths, while adapting the visible layout to the window. */
const LAYOUT_KEY = 'gusnotebook.layout';
const layoutDefaults = {filesWidth: 240, termWidth: 360, files: true, terminal: true, focus: false, sidebarSection: 'files'};
const sidebarSections = {files: 'sidebar-files-pane', sessions: 'sessions', skills: 'skills'};
let layoutPrefs = {...layoutDefaults};
try {
  const saved = JSON.parse(localStorage.getItem(LAYOUT_KEY)) || {};
  if (Object.hasOwn(sidebarSections, saved.sidebarSection)) layoutPrefs.sidebarSection = saved.sidebarSection;
  for (const key of ['files', 'terminal', 'focus']) if (typeof saved[key] === 'boolean') layoutPrefs[key] = saved[key];
  for (const key of ['filesWidth', 'termWidth']) {
    if (Number.isFinite(saved[key])) layoutPrefs[key] = Math.max(200, Math.min(720, saved[key]));
  }
} catch (_) {}
let termWidth = layoutPrefs.termWidth;
let panelDrawer = null;
let layoutFrame = null;
function saveLayout() { try { localStorage.setItem(LAYOUT_KEY, JSON.stringify(layoutPrefs)); } catch (_) {} }
function filesVisible() { return !document.getElementById('files').inert; }
function sidebarRailWidth() { return document.getElementById('sidebar-rail').getBoundingClientRect().width; }
function showSidebarSection(name, toggle = false) {
  if (!Object.hasOwn(sidebarSections, name)) return;
  const button = document.getElementById('sidebar-' + name);
  const fromRail = document.activeElement === button;
  if (toggle && layoutPrefs.sidebarSection === name && filesVisible()) {
    togglePanel('files');
  } else {
    layoutPrefs.sidebarSection = name;
    ensurePanel('files');
    saveLayout(); applyLayout();
  }
  if (fromRail) button.focus();
}
function scheduleTerminalFit() {
  cancelAnimationFrame(layoutFrame);
  layoutFrame = requestAnimationFrame(() => { if (typeof fitTerm === 'function') fitTerm(); });
}
function applyLayout() {
  const app = document.getElementById('app');
  const railWidth = sidebarRailWidth();
  const width = app.clientWidth - railWidth;
  const focus = layoutPrefs.focus;
  let files = !focus && layoutPrefs.files && width >= 1060;
  let terminal = layoutPrefs.terminal && width >= 820;
  let fw = files ? Math.min(layoutPrefs.filesWidth, Math.max(200, width * .25), terminal ? width - 786 : Infinity) : 0;
  const tw = terminal ? Math.max(300, Math.min(termWidth, width - fw - 486)) : 0;
  if (width >= 1060) panelDrawer = null;
  app.style.gridTemplateColumns = `${railWidth}px ${fw}px minmax(0, 1fr) ${terminal ? 6 : 0}px ${tw}px`;
  app.classList.toggle('files-hidden', !files);
  app.classList.toggle('terminal-hidden', !terminal);
  app.classList.toggle('focus-mode', focus);
  app.classList.toggle('files-drawer', panelDrawer === 'files');
  app.classList.toggle('terminal-drawer', panelDrawer === 'terminal');
  document.getElementById('files').inert = !files && panelDrawer !== 'files';
  for (const [name, id] of Object.entries(sidebarSections)) {
    const selected = layoutPrefs.sidebarSection === name;
    document.getElementById(id).hidden = !selected;
    document.getElementById('sidebar-' + name).setAttribute('aria-pressed', String(selected && filesVisible()));
  }
  document.getElementById('agent-pane').inert = !terminal && panelDrawer !== 'terminal';
  document.getElementById('splitter').inert = !terminal;
  document.getElementById('panel-backdrop').hidden = !panelDrawer;
  const terminalOpen = terminal || panelDrawer === 'terminal';
  const terminalButton = document.getElementById('sidebar-terminal');
  terminalButton.setAttribute('aria-expanded', String(terminalOpen));
  terminalButton.setAttribute('aria-pressed', String(terminalOpen));
  terminalButton.title = terminalOpen ? 'Hide terminal panel' : 'Show terminal panel';
  document.getElementById('focus-toggle').setAttribute('aria-checked', String(focus));
  for (const [id, current, min, max] of [['file-splitter', fw, 200, Math.min(480, width * .25)],
      ['splitter', tw, 300, Math.max(300, width - fw - 486)]]) {
    const separator = document.getElementById(id);
    separator.setAttribute('aria-valuemin', Math.round(min));
    separator.setAttribute('aria-valuemax', Math.round(max));
    separator.setAttribute('aria-valuenow', Math.round(current || min));
    separator.setAttribute('aria-valuetext', Math.round(current) + ' pixels');
  }
  scheduleTerminalFit();
}
function togglePanel(name) {
  const element = document.getElementById(name === 'files' ? 'files' : 'agent-pane');
  const small = document.getElementById('app').clientWidth - sidebarRailWidth() < (name === 'files' ? 1060 : 820);
  if (small) panelDrawer = panelDrawer === name ? null : name;
  else {
    const visible = !element.inert;
    if (name === 'files') layoutPrefs.focus = false;
    layoutPrefs[name] = !visible;
  }
  saveLayout(); applyLayout();
  if (panelDrawer) document.querySelector(name === 'files'
    ? '.sidebar-pane:not([hidden]) button' : '#agent-pane button')?.focus();
}
function ensurePanel(name) {
  if (document.getElementById(name === 'files' ? 'files' : 'agent-pane').inert) togglePanel(name);
}
function closePanelDrawer() { panelDrawer = null; applyLayout(); }
function toggleNotebookFocus() {
  layoutPrefs.focus = !layoutPrefs.focus;
  panelDrawer = null;
  saveLayout(); applyLayout();
}
function resetLayout() {
  layoutPrefs = {...layoutDefaults}; termWidth = layoutPrefs.termWidth; panelDrawer = null;
  saveLayout(); applyLayout();
}
function changePanelWidth(which, value) {
  const el = document.getElementById(which === 'files' ? 'file-splitter' : 'splitter');
  const size = Math.max(Number(el.getAttribute('aria-valuemin')),
    Math.min(Number(el.getAttribute('aria-valuemax')), value));
  if (which === 'files') layoutPrefs.filesWidth = size;
  else termWidth = layoutPrefs.termWidth = size;
  applyLayout();
}
for (const [id, which] of [['file-splitter', 'files'], ['splitter', 'terminal']]) {
  const separator = document.getElementById(id);
  let drag = null;
  separator.addEventListener('pointerdown', event => {
    if (event.button !== 0) return;
    drag = event.pointerId;
    separator.setPointerCapture(drag);
    separator.classList.add('dragging');
    document.body.classList.add('resizing-panels');
    event.preventDefault();
  });
  separator.addEventListener('pointermove', event => {
    if (event.pointerId !== drag) return;
    const box = document.getElementById('app').getBoundingClientRect();
    changePanelWidth(which, which === 'files' ? event.clientX - box.left - sidebarRailWidth() : box.right - event.clientX);
  });
  const finish = () => {
    drag = null;
    separator.classList.remove('dragging');
    document.body.classList.remove('resizing-panels');
    saveLayout(); scheduleTerminalFit();
  };
  separator.addEventListener('pointerup', finish);
  separator.addEventListener('lostpointercapture', finish);
  separator.addEventListener('keydown', event => {
    const current = Number(separator.getAttribute('aria-valuenow'));
    let next;
    if (event.key === 'Home') next = Number(separator.getAttribute('aria-valuemin'));
    if (event.key === 'End') next = Number(separator.getAttribute('aria-valuemax'));
    if (['ArrowLeft', 'ArrowRight'].includes(event.key)) {
      const direction = (event.key === 'ArrowRight' ? 1 : -1) * (which === 'files' ? 1 : -1);
      next = current + direction * (event.shiftKey ? 64 : 16);
    }
    if (next === undefined) return;
    event.preventDefault(); changePanelWidth(which, next); saveLayout();
  });
  separator.addEventListener('dblclick', () => {
    changePanelWidth(which, layoutDefaults[which === 'files' ? 'filesWidth' : 'termWidth']); saveLayout();
  });
}
document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && panelDrawer && !document.querySelector('.modal-back.on')) {
    const name = panelDrawer;
    closePanelDrawer();
    document.getElementById('sidebar-' + (name === 'files' ? layoutPrefs.sidebarSection : 'terminal')).focus();
    event.preventDefault();
  }
});
document.getElementById('sidebar-rail').addEventListener('keydown', event => {
  const buttons = [...event.currentTarget.querySelectorAll('button')];
  const index = buttons.indexOf(document.activeElement);
  if (index < 0 || !['ArrowUp', 'ArrowDown', 'Home', 'End'].includes(event.key)) return;
  event.preventDefault();
  const next = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1
    : (index + (event.key === 'ArrowDown' ? 1 : -1) + buttons.length) % buttons.length;
  buttons[next].focus();
});
new ResizeObserver(applyLayout).observe(document.getElementById('app'));
document.addEventListener('appearance-change', scheduleTerminalFit);
applyLayout();
