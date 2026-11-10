/* Clipboard actions belong to the browser, including for remote terminals. */
const terminalClipboardMac = /Mac|iPhone|iPad|iPod/.test(navigator.platform);
let terminalClipboardDialog = null;

function pasteTerminalText(t, text) {
  if (findTerm(t.id) !== t || activeTerm !== t.id || t.ws?.readyState !== WebSocket.OPEN) {
    flash('Select a connected terminal before pasting.');
    return;
  }
  t.term.focus();
  // xterm normalizes line endings and honors the application's bracketed
  // paste mode. Never add Enter or send clipboard text directly to the socket.
  if (text) t.term.paste(text);
}

function openTerminalClipboard(t, mode, text = '') {
  if (findTerm(t.id) !== t || activeTerm !== t.id) return;
  terminalClipboardDialog = {terminal: t, mode};
  document.getElementById('terminal-clipboard-title').textContent = mode === 'paste' ? 'Paste into terminal' : 'Copy terminal selection';
  document.getElementById('terminal-clipboard-hint').textContent = mode === 'paste'
    ? 'Paste your text below using the keyboard or right-click menu, then choose Paste.'
    : 'Copy the selected text using the keyboard or right-click menu.';
  document.getElementById('terminal-clipboard-ok').textContent = mode === 'paste' ? 'Paste' : 'Done';
  const input = document.getElementById('terminal-clipboard-text');
  input.value = text;
  input.readOnly = mode === 'copy';
  document.getElementById('terminal-clipboard-back').classList.add('on');
  input.focus();
  if (mode === 'copy') input.select();
}

function closeTerminalClipboard(accept = false) {
  const dialog = terminalClipboardDialog;
  if (!dialog) return;
  terminalClipboardDialog = null;
  const input = document.getElementById('terminal-clipboard-text');
  const text = input.value;
  input.value = '';
  document.getElementById('terminal-clipboard-back').classList.remove('on');
  // Let modal focus management remove inert before restoring terminal focus.
  queueMicrotask(() => {
    if (accept && dialog.mode === 'paste') pasteTerminalText(dialog.terminal, text);
    else if (findTerm(dialog.terminal.id) === dialog.terminal && activeTerm === dialog.terminal.id) {
      dialog.terminal.term.focus();
    }
  });
}

async function copyTerminalSelection(t) {
  const text = t.term.getSelection();
  if (!text) { flash('Select terminal text to copy.'); return; }
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      flash('Copied');
      return;
    }
  } catch (_) { /* HTTP and denied clipboard access use native copy below. */ }
  const previous = document.activeElement;
  const input = document.createElement('textarea');
  input.value = text;
  input.readOnly = true;
  input.style.cssText = 'position:fixed;left:0;top:0;width:1px;height:1px;opacity:0';
  document.body.appendChild(input);
  let copied = false;
  try {
    input.focus({preventScroll: true});
    input.select();
    copied = document.execCommand('copy');
  } catch (_) { /* A selectable dialog also works when programmatic copy fails. */ }
  finally {
    input.remove();
    if (previous?.isConnected) previous.focus({preventScroll: true});
  }
  if (copied) flash('Copied');
  else openTerminalClipboard(t, 'copy', text);
}

async function pasteTerminalClipboard(t) {
  if (t.clipboardReadPending) return;
  t.clipboardReadPending = true;
  try {
    if (navigator.clipboard?.readText) {
      try {
        const text = await navigator.clipboard.readText();
        pasteTerminalText(t, text);
        return;
      } catch (_) { /* Native paste into a textarea needs no clipboard API. */ }
    }
    openTerminalClipboard(t, 'paste');
  } finally { t.clipboardReadPending = false; }
}

function terminalClipboardMenu(t, x, y) {
  t.term.focus();
  showFileCtx(x, y, [
    {label: 'Copy', action: () => copyTerminalSelection(t)},
    {label: 'Paste', action: () => pasteTerminalClipboard(t)},
    {label: 'Select all', action: () => { t.term.selectAll(); t.term.focus(); }},
  ], 'Terminal actions');
}

function wireTerminalClipboard(t) {
  t.term.attachCustomKeyEventHandler(event => {
    if (event.altKey || event.isComposing) return true;
    const key = event.key.toLowerCase();
    const nativeModifier = terminalClipboardMac
      ? event.metaKey && !event.ctrlKey : event.ctrlKey && !event.metaKey;
    const modified = event.ctrlKey || event.metaKey;
    const copy = key === 'c' && (event.metaKey || (event.ctrlKey && event.shiftKey));
    const paste = key === 'v' && (event.metaKey || (event.ctrlKey && event.shiftKey));
    const menu = key === 'contextmenu' || (key === 'f10' && event.shiftKey && !modified);
    if (!copy && !paste && !menu) return true;  // Ctrl+C always remains the interrupt.
    // Native paste emits a clipboard event that xterm already handles. Let
    // the browser deliver it even when direct clipboard access is denied.
    // Copy explicitly uses xterm's selection, not the page's DOM selection.
    if (paste && nativeModifier && !event.shiftKey) return false;
    event.preventDefault();
    if (event.type === 'keydown' && !event.repeat) {
      if (copy) copyTerminalSelection(t);
      else if (paste) pasteTerminalClipboard(t);
      else {
        const box = t.host.getBoundingClientRect();
        terminalClipboardMenu(t, box.left + 16, box.top + 16);
      }
    }
    return false;
  });
  t.term.element.addEventListener('contextmenu', event => {
    // Shift+right-click remains available for the browser's own menu.
    if (event.shiftKey) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    terminalClipboardMenu(t, event.clientX, event.clientY);
  }, true);
}
