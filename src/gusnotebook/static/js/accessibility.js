/* Keyboard navigation and modal focus management, shared by the app's controls. */
const visibleControl = element => !element.disabled && !element.closest('[inert]') &&
  element.getClientRects().length && getComputedStyle(element).visibility !== 'hidden';
const focusableControls = root => [...root.querySelectorAll('button, input, textarea, select, a[href], summary, [tabindex]')]
  .filter(element => element.tabIndex >= 0 && visibleControl(element));
const modalEntries = new Map();
let modalOrder = 0;
function topModal() {
  return [...modalEntries].filter(([back]) => back.classList.contains('on'))
    .sort(([a, av], [b, bv]) => (Number(getComputedStyle(a).zIndex) - Number(getComputedStyle(b).zIndex)) || av.order - bv.order)
    .map(([back]) => back).pop();
}
function syncModals() {
  const closed = [];
  for (const back of document.querySelectorAll('.modal-back')) {
    if (back.classList.contains('on') && !modalEntries.has(back)) {
      modalEntries.set(back, {opener: document.activeElement, order: ++modalOrder});
      const dialog = back.querySelector('.modal');
      dialog.setAttribute('role', 'dialog'); dialog.setAttribute('aria-modal', 'true');
      if (!dialog.hasAttribute('aria-labelledby')) {
        const title = dialog.querySelector('.mhead .grow');
        if (title) { title.id ||= back.id + '-title'; dialog.setAttribute('aria-labelledby', title.id); }
      }
    } else if (!back.classList.contains('on') && modalEntries.has(back)) {
      closed.push(modalEntries.get(back)); modalEntries.delete(back);
    }
  }
  const top = topModal();
  document.getElementById('app').inert = !!top;
  document.querySelector('.app-header').inert = !!top;
  for (const back of document.querySelectorAll('.modal-back')) back.inert = !!top && back !== top;
  if (top && !top.contains(document.activeElement)) {
    (focusableControls(top)[0] || top.querySelector('.modal')).focus();
  } else if (!top && document.activeElement.closest('.modal-back:not(.on)')) {
    const opener = closed.sort((a, b) => b.order - a.order)[0]?.opener;
    if (opener?.isConnected && !opener.closest('.modal-back')) opener.focus();
    else document.getElementById('settings-button').focus();
  }
}
const modalObserver = new MutationObserver(syncModals);
for (const back of document.querySelectorAll('.modal-back')) modalObserver.observe(back, {attributes: true, attributeFilter: ['class']});
const modalClosers = {
  'settings-back': closeSettings, 'environment-back': closeEnvironments, 'history-back': closeHistory,
  'dirpick-back': dirPickCancel, 'ask-back': () => askDone(false), 'prov-back': () => provenanceDone(false),
  'skill-back': closeSkill, 'sinstr-back': closeSessionInstr,
};
document.addEventListener('keydown', event => {
  const modal = topModal();
  if (modal && event.key === 'Escape') {
    event.preventDefault(); event.stopImmediatePropagation(); modalClosers[modal.id]?.(); return;
  }
  if (modal && event.key === 'Tab') {
    const controls = focusableControls(modal), first = controls[0], last = controls.at(-1);
    if (!first) { event.preventDefault(); return; }
    if (event.shiftKey && (document.activeElement === first || !modal.contains(document.activeElement))) {
      event.preventDefault(); last.focus();
    } else if (!event.shiftKey && (document.activeElement === last || !modal.contains(document.activeElement))) {
      event.preventDefault(); first.focus();
    }
  }
  const target = event.target;
  if (!(target instanceof Element)) return;
  if (['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName) || target.isContentEditable) return;
  if (target.closest('#venv-menu') && event.key === 'Escape') {
    event.preventDefault(); closeVenvMenu(); return;
  }
  if (target.matches('#tabs [role="tab"]') && (event.key === 'F2' || event.key === 'ContextMenu' || (event.shiftKey && event.key === 'F10'))) {
    event.preventDefault();
    if (event.key === 'F2') renameEntry(target.dataset.path);
    else {
      const box = target.getBoundingClientRect();
      showPathCtx(box.left, box.bottom, target.dataset.path);
    }
    return;
  }
  if (target.id === 'workspace-more' && event.key === 'ArrowDown') {
    event.preventDefault(); toggleWorkspaceMenu(event); return;
  }
  const menu = target.closest('[role="menu"]');
  if (menu && ['ArrowDown', 'ArrowUp', 'Home', 'End', 'Escape'].includes(event.key)) {
    event.preventDefault();
    if (event.key === 'Escape') {
      if (menu.id === 'new-menu') closeNewMenu();
      else if (menu.id === 'type-menu') closeTypeMenu();
      else if (menu.id === 'file-ctx') closeFileCtx();
      else if (menu.id === 'workspace-menu') closeWorkspaceMenu();
      else { menu.classList.remove('on'); menu._opener?.focus(); }
      return;
    }
    const items = [...menu.querySelectorAll('[role^="menuitem"]')].filter(visibleControl);
    const at = items.indexOf(target.closest('[role^="menuitem"]'));
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1
      : (at + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
    items[next]?.focus(); return;
  }
  const tab = target.closest('[role="tab"]');
  if (tab === target && ['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
    event.preventDefault();
    const list = tab.closest('[role="tablist"]');
    if (!list) return;
    const items = [...list.querySelectorAll('[role="tab"]')].filter(visibleControl), at = items.indexOf(tab);
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1
      : (at + (event.key === 'ArrowRight' ? 1 : -1) + items.length) % items.length;
    const chosen = items[next]; chosen?.click();
    if (list.id === 'tabs') document.querySelector('#tabs .tab.active')?.focus();
    else if (list.id === 'term-tabs') document.querySelector('#term-tabs .tterm.active')?.focus();
    else chosen?.focus();
    return;
  }
  if (['Enter', ' '].includes(event.key) && target.matches('[role="button"], [role^="menuitem"], [role="tab"]')) {
    event.preventDefault(); event.stopPropagation(); target.click();
  }
});
// Labels for icon-only controls are available to assistive technology as well as tooltips.
for (const button of document.querySelectorAll('button')) {
  if (!button.hasAttribute('aria-label') && button.title) button.setAttribute('aria-label', button.title);
  if (!button.textContent.trim() && !button.hasAttribute('aria-label')) button.setAttribute('aria-label', 'Close dialog');
}
for (const list of ['term-tabs']) {
  document.getElementById(list).setAttribute('role', 'tablist');
  document.getElementById(list).setAttribute('aria-label', 'Open terminals');
}
syncModals();
