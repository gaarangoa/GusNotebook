/* Small authoring controls for HTML previews. The bridge supplies the save
 * and authored-DOM hooks; this module never reloads the page to apply an edit. */
export function initMarkupTools(bridge) {
  const {authoredMutation, isAuthoredNode, runtimeAttr} = bridge;
  const host = document.createElement('div');
  host.setAttribute(runtimeAttr, 'authoring');
  host.contentEditable = 'false';
  host.style.cssText = 'all:initial!important;position:fixed!important;inset:0!important;pointer-events:none!important;z-index:2147483647!important;';
  const shadow = host.attachShadow({mode: 'open'});
  shadow.innerHTML = `
    <style>
      :host { --bg:#fff; --fg:#263244; --line:#cbd5e1; --hover:#eef2f6; --accent:#830051; --size:12px; color-scheme:light; }
      :host([data-dark]) { --bg:#222832; --fg:#e5eaf0; --line:#46515f; --hover:#343e4b; --accent:#f1a9d3; color-scheme:dark; }
      * { box-sizing:border-box; }
      [hidden] { display:none!important; }
      .panel { position:fixed; pointer-events:auto; display:flex; align-items:center; gap:5px;
        max-width:calc(100vw - 12px); padding:5px; border:1px solid var(--line); border-radius:6px;
        background:var(--bg); color:var(--fg); box-shadow:0 4px 14px #0002;
        font:var(--size)/1.4 system-ui,sans-serif; }
      button,input { font:inherit; color:inherit; background:var(--bg); border:1px solid var(--line); border-radius:3px; height:26px; }
      button { padding:2px 7px; cursor:pointer; }
      button:hover, button[aria-pressed=true] { background:var(--hover); color:var(--accent); }
      button:disabled { opacity:.4; cursor:default; }
      :focus-visible { outline:2px solid var(--accent); outline-offset:1px; }
      input[type=number] { width:53px; padding:2px 4px; }
      input[type=color] { width:28px; padding:2px; cursor:pointer; }
      label { display:flex; align-items:center; justify-content:space-between; gap:6px; }
      #text { flex-wrap:wrap; }
      #card { display:grid; grid-template-columns:1fr 1fr; width:248px; padding:8px; gap:8px; }
      .heading { grid-column:1/-1; display:flex; gap:5px; align-items:center; }
      .heading strong { flex:1; font-weight:600; }
      .outline { position:fixed; pointer-events:none; outline:1px solid var(--accent); outline-offset:2px; border-radius:4px; }
      .sr { position:absolute; width:1px; height:1px; overflow:hidden; clip-path:inset(50%); }
    </style>
    <div id="text" class="panel" role="toolbar" aria-label="Text formatting" hidden>
      <input id="font" type="number" min="6" max="144" step="1" aria-label="Font size" title="Font size in pixels">
      <input id="color" type="color" aria-label="Text color" title="Text color">
      <button id="bold" aria-label="Bold" aria-pressed="false"><b>B</b></button>
      <button id="italic" aria-label="Italic" aria-pressed="false"><i>I</i></button>
      <button data-action="undo" aria-label="Undo" title="Undo (⌘/Ctrl Z)">↶</button>
      <button data-action="redo" aria-label="Redo" title="Redo (⌘/Ctrl Shift Z)">↷</button>
    </div>
    <div id="outline" class="outline" hidden></div>
    <div id="card" class="panel" role="group" aria-label="Card settings" hidden>
      <div class="heading"><strong>Card</strong>
        <button data-action="undo" aria-label="Undo">↶</button><button data-action="redo" aria-label="Redo">↷</button>
        <button id="delete" aria-label="Delete card" title="Delete card">Delete</button>
        <button id="close" aria-label="Close card settings">×</button>
      </div>
      <label>Background<input id="background" type="color" aria-label="Card background"></label>
      <label>Border<input id="border-color" type="color" aria-label="Card border color"></label>
      <label>Width %<input id="width" type="number" min="10" max="100" aria-label="Card width percent"></label>
      <label>Padding<input id="padding" type="number" min="0" max="80" aria-label="Card padding"></label>
      <label>Border px<input id="border-width" type="number" min="0" max="12" aria-label="Card border width"></label>
    </div>
    <div id="image" class="panel" role="toolbar" aria-label="Image size" hidden>
      <button id="image-smaller" aria-label="Make image smaller">−</button>
      <label><input id="image-width" type="number" min="24" max="2400" step="1" aria-label="Image width">px</label>
      <button id="image-larger" aria-label="Make image larger">+</button>
      <button data-action="undo" aria-label="Undo">↶</button>
      <button data-action="redo" aria-label="Redo">↷</button>
      <button id="image-delete" aria-label="Delete image">Delete</button>
    </div>
    <span id="notice" class="sr" role="status"></span>`;
  document.documentElement.appendChild(host);
  const ui = id => shadow.getElementById(id);
  let savedRange = null, selectedCard = null, selectedImage = null, nativeEdit = null;
  let imagesBeforePaste = null;
  const history = [], future = [];
  const blocked = 'script,style,svg,canvas,iframe,object,input,textarea,select,button,[contenteditable="false"],[data-gusnb-viz],[' + runtimeAttr + ']';

  function element(node) { return node?.nodeType === Node.ELEMENT_NODE ? node : node?.parentElement; }
  function editable(node) {
    return !!node && document.body.contains(node) && !element(node)?.closest(blocked) && isAuthoredNode(node);
  }
  function currentRange() {
    const selection = document.getSelection();
    return selection?.rangeCount ? selection.getRangeAt(0) : null;
  }
  function validRange(range) {
    return range && editable(range.startContainer) && editable(range.endContainer);
  }
  function rememberRange() {
    const range = currentRange();
    if (validRange(range)) savedRange = range.cloneRange();
  }
  function bookmark() {
    const range = currentRange();
    return validRange(range) ? {start: range.startContainer, from: range.startOffset,
      end: range.endContainer, to: range.endOffset} : null;
  }
  function restore(mark) {
    if (!mark || !mark.start.isConnected || !mark.end.isConnected) return;
    const length = node => node.nodeType === Node.TEXT_NODE ? node.length : node.childNodes.length;
    const range = document.createRange();
    range.setStart(mark.start, Math.min(mark.from, length(mark.start)));
    range.setEnd(mark.end, Math.min(mark.to, length(mark.end)));
    const selection = document.getSelection();
    selection.removeAllRanges(); selection.addRange(range);
    savedRange = range.cloneRange();
  }
  function focusRange() {
    if (!validRange(savedRange)) return false;
    window.focus();
    document.body.focus({preventScroll:true});
    const selection = document.getSelection();
    selection.removeAllRanges(); selection.addRange(savedRange);
    return true;
  }

  // Record only synchronous user edits, keeping the actual nodes and their
  // listeners. Restoring body.innerHTML would restart charts and lose handlers.
  function begin() {
    const pending = [];
    const observer = new MutationObserver(records => pending.push(...records));
    observer.observe(document.body, {subtree:true, childList:true, characterData:true,
      characterDataOldValue:true, attributes:true, attributeOldValue:true});
    return {observer, pending, before:bookmark()};
  }
  function finish(transaction, group = '') {
    const records = [...transaction.pending, ...transaction.observer.takeRecords()];
    transaction.observer.disconnect();
    const edits = records.filter(record => editable(record.target) ||
      (!record.target.isConnected && isAuthoredNode(record.target))).map(record => {
      if (record.type === 'attributes') return {type:'attribute', node:record.target,
        name:record.attributeName, before:record.oldValue, after:record.target.getAttribute(record.attributeName)};
      if (record.type === 'characterData') return {type:'text', node:record.target,
        before:record.oldValue, after:record.target.data};
      return {type:'children', node:record.target, added:[...record.addedNodes],
        removed:[...record.removedNodes], next:record.nextSibling};
    }).filter(edit => edit.type === 'children' || edit.before !== edit.after);
    if (!edits.length) return;
    const after = bookmark(), previous = history.at(-1), now = Date.now();
    const adjacent = previous?.after && transaction.before && previous.after.start === transaction.before.start &&
      previous.after.from === transaction.before.from && previous.after.end === transaction.before.end &&
      previous.after.to === transaction.before.to;
    if (group && previous?.group === group && adjacent && now - previous.time < 900 && !future.length) {
      previous.edits.push(...edits); previous.after = after; previous.time = now;
    } else {
      history.push({edits, before:transaction.before, after, group, time:now});
      if (history.length > 80) history.shift();
    }
    future.length = 0;
    bridge.changed();
    updateUndo();
  }
  function edit(callback) {
    const transaction = begin();
    try { authoredMutation(callback); }
    finally { finish(transaction); }
  }
  function updateUndo() {
    shadow.querySelectorAll('[data-action="undo"]').forEach(button => { button.disabled = !history.length; });
    shadow.querySelectorAll('[data-action="redo"]').forEach(button => { button.disabled = !future.length; });
  }
  function replay(redo) {
    const from = redo ? future : history, to = redo ? history : future;
    const entry = from.pop();
    if (!entry) return;
    authoredMutation(() => {
      for (const operation of redo ? entry.edits : [...entry.edits].reverse()) {
        const value = redo ? operation.after : operation.before;
        if (operation.type === 'attribute') {
          if (value === null) operation.node.removeAttribute(operation.name);
          else operation.node.setAttribute(operation.name, value);
        } else if (operation.type === 'text') operation.node.data = value;
        else {
          const remove = redo ? operation.removed : operation.added;
          const insert = redo ? operation.added : operation.removed;
          remove.forEach(node => { if (node.parentNode === operation.node) node.remove(); });
          const next = operation.next?.parentNode === operation.node ? operation.next : null;
          insert.forEach(node => operation.node.insertBefore(node, next));
        }
      }
    });
    to.push(entry);
    window.focus(); document.body.focus({preventScroll:true});
    restore(redo ? entry.after : entry.before);
    if (!selectedCard?.isConnected) selectedCard = null;
    if (!selectedImage?.isConnected) selectedImage = null;
    bridge.changed(); updateUndo(); showSelection();
    if (selectedCard) cardSettings(selectedCard);
    if (selectedImage) imageSettings(selectedImage);
  }
  document.addEventListener('beforeinput', event => {
    if (event.composedPath().includes(host) || !editable(event.target)) return;
    if (event.inputType === 'historyUndo' || event.inputType === 'historyRedo') {
      event.preventDefault(); replay(event.inputType === 'historyRedo'); return;
    }
    nativeEdit = begin();
    // A canceled beforeinput has no input event and must not capture scripts
    // that update the page later in the same event loop.
    const transaction = nativeEdit;
    setTimeout(() => {
      if (nativeEdit === transaction) { transaction.observer.disconnect(); nativeEdit = null; }
    }, 0);
  }, true);
  document.addEventListener('input', event => {
    if (!nativeEdit || event.composedPath().includes(host)) return;
    const transaction = nativeEdit; nativeEdit = null;
    if (imagesBeforePaste && event.inputType === 'insertFromPaste') {
      authoredMutation(() => {
        for (const image of document.images) {
          if (imagesBeforePaste.has(image) || !editable(image)) continue;
          image.style.setProperty('width', 'auto', 'important');
          image.style.setProperty('max-width', 'min(240px, 100%)', 'important');
          image.style.setProperty('height', 'auto', 'important');
          image.removeAttribute('height');
        }
      });
      imagesBeforePaste = null;
    }
    finish(transaction, ['insertText','deleteContentBackward','deleteContentForward'].includes(event.inputType) ? event.inputType : '');
    hide();
  }, true);
  document.addEventListener('keydown', event => {
    if (event.composedPath().includes(host)) return;
    if ((event.metaKey || event.ctrlKey) && !event.altKey &&
        (event.key.toLowerCase() === 'z' || event.key.toLowerCase() === 'y')) {
      if (!editable(event.target)) return;
      event.preventDefault(); event.stopImmediatePropagation();
      replay(event.shiftKey || event.key.toLowerCase() === 'y');
    } else if (event.key === 'Escape') hide();
  }, true);

  function selectedTexts() {
    if (!validRange(savedRange) || savedRange.collapsed) return [];
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    const selected = [];
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      if (!editable(node) || !savedRange.intersectsNode(node)) continue;
      const start = node === savedRange.startContainer ? savedRange.startOffset : 0;
      const end = node === savedRange.endContainer ? savedRange.endOffset : node.length;
      if (end > start) selected.push({node, start, end});
    }
    return selected;
  }
  function format(property, value) {
    const texts = selectedTexts();
    if (!texts.length || !focusRange()) return;
    edit(() => {
      const results = texts.map(({node, start, end}) => {
        if (end < node.length) node.splitText(end);
        if (start) node = node.splitText(start);
        let span = node.parentElement;
        if (span.tagName !== 'SPAN' || span.childNodes.length !== 1) {
          span = document.createElement('span');
          node.before(span); span.appendChild(node);
        }
        span.style.setProperty(property, value, 'important');
        return node;
      });
      restore({start:results[0], from:0, end:results.at(-1), to:results.at(-1).length});
    });
    showSelection();
  }
  function colorHex(color, fallback = '#ffffff') {
    const parts = color.match(/[\d.]+/g);
    if (!parts || parts.length < 3 || (parts.length > 3 && Number(parts[3]) === 0)) return fallback;
    return '#' + parts.slice(0,3).map(value => Math.round(Number(value)).toString(16).padStart(2,'0')).join('');
  }
  function number(id, callback) {
    ui(id).addEventListener('change', () => {
      const input = ui(id), value = input.valueAsNumber;
      if (!Number.isFinite(value) || !input.checkValidity()) { input.reportValidity(); return; }
      callback(value);
    });
  }
  number('font', size => format('font-size', size + 'px'));
  ui('color').addEventListener('change', () => format('color', ui('color').value));
  ui('bold').addEventListener('click', () => format('font-weight', ui('bold').getAttribute('aria-pressed') === 'true' ? '400' : '700'));
  ui('italic').addEventListener('click', () => format('font-style', ui('italic').getAttribute('aria-pressed') === 'true' ? 'normal' : 'italic'));
  shadow.querySelectorAll('[data-action]').forEach(button => button.addEventListener('click', () => replay(button.dataset.action === 'redo')));
  // Clicking a control must not replace the document selection with its label.
  shadow.addEventListener('mousedown', event => { if (event.target.closest('button')) event.preventDefault(); });
  shadow.addEventListener('keydown', event => {
    if (event.key === 'Escape') { event.preventDefault(); hide(); focusRange(); }
    if (event.key === 'Enter' && event.target.matches('input')) { event.preventDefault(); event.target.blur(); }
  });

  function position(panel, rect) {
    panel.hidden = false;
    const width = panel.offsetWidth, height = panel.offsetHeight;
    panel.style.left = Math.max(6, Math.min(innerWidth - width - 6, rect.left)) + 'px';
    const above = rect.top - height - 8;
    panel.style.top = Math.max(6, Math.min(innerHeight - height - 6, above >= 6 ? above : rect.bottom + 8)) + 'px';
  }
  function showSelection() {
    if (shadow.activeElement) return;
    rememberRange();
    const texts = selectedTexts();
    if (!texts.length) { ui('text').hidden = true; return; }
    selectedCard = null; ui('card').hidden = true; ui('outline').hidden = true;
    selectedImage = null; ui('image').hidden = true;
    const style = getComputedStyle(texts[0].node.parentElement);
    ui('font').value = Math.round(parseFloat(style.fontSize));
    ui('color').value = colorHex(style.color, '#263244');
    ui('bold').setAttribute('aria-pressed', String(texts.every(({node}) => parseInt(getComputedStyle(node.parentElement).fontWeight,10) >= 600)));
    ui('italic').setAttribute('aria-pressed', String(texts.every(({node}) => getComputedStyle(node.parentElement).fontStyle === 'italic')));
    position(ui('text'), savedRange.getBoundingClientRect());
  }
  function hide() {
    ui('text').hidden = true; ui('card').hidden = true; ui('outline').hidden = true;
    ui('image').hidden = true; selectedImage = null; selectedCard = null;
  }
  function cardSettings(card) {
    if (!editable(card)) return;
    selectedImage = null; ui('image').hidden = true;
    selectedCard = card; bridge.deselectViz(); ui('text').hidden = true;
    const style = getComputedStyle(card);
    ui('background').value = colorHex(style.backgroundColor);
    ui('border-color').value = colorHex(style.borderTopColor, '#cbd5e1');
    ui('border-width').value = Math.round(parseFloat(style.borderTopWidth));
    ui('padding').value = Math.round(parseFloat(style.paddingTop));
    ui('width').value = card.style.width.endsWith('%') ? parseFloat(card.style.width) : 100;
    positionCard();
  }
  function positionCard() {
    if (!selectedCard?.isConnected) { ui('card').hidden = true; ui('outline').hidden = true; return; }
    const rect = selectedCard.getBoundingClientRect();
    Object.assign(ui('outline').style, {left:rect.left+'px', top:rect.top+'px', width:rect.width+'px', height:rect.height+'px'});
    ui('outline').hidden = false;
    position(ui('card'), rect);
  }
  function cardStyle(property, value) {
    if (!selectedCard?.isConnected) return;
    edit(() => selectedCard.style.setProperty(property, value, 'important'));
    positionCard();
  }
  ui('background').addEventListener('change', () => cardStyle('background-color', ui('background').value));
  ui('border-color').addEventListener('change', () => cardStyle('border-color', ui('border-color').value));
  number('width', value => cardStyle('width', value + '%'));
  number('padding', value => cardStyle('padding', value + 'px'));
  number('border-width', value => cardStyle('border-width', value + 'px'));
  ui('delete').addEventListener('click', () => {
    if (!selectedCard?.isConnected) return;
    edit(() => selectedCard.remove()); hide();
  });
  ui('close').addEventListener('click', () => { hide(); focusRange(); });

  function imageSettings(image) {
    if (!editable(image)) return;
    selectedImage = image; selectedCard = null;
    bridge.deselectViz(); ui('card').hidden = true; ui('text').hidden = true;
    const rect = image.getBoundingClientRect();
    ui('image-width').value = Math.round(rect.width);
    Object.assign(ui('outline').style, {left:rect.left+'px', top:rect.top+'px', width:rect.width+'px', height:rect.height+'px'});
    ui('outline').hidden = false;
    position(ui('image'), rect);
  }
  function resizeImage(width) {
    if (!selectedImage?.isConnected) return;
    width = Math.max(24, Math.min(2400, Math.round(width)));
    edit(() => {
      selectedImage.style.setProperty('width', width + 'px', 'important');
      selectedImage.style.setProperty('max-width', '100%', 'important');
      selectedImage.style.setProperty('height', 'auto', 'important');
      selectedImage.removeAttribute('height');
    });
    imageSettings(selectedImage);
  }
  number('image-width', resizeImage);
  ui('image-smaller').addEventListener('click', () => resizeImage(Number(ui('image-width').value) / 1.25));
  ui('image-larger').addEventListener('click', () => resizeImage(Number(ui('image-width').value) * 1.25));
  ui('image-delete').addEventListener('click', () => {
    if (!selectedImage?.isConnected) return;
    edit(() => selectedImage.remove()); hide();
  });
  document.addEventListener('paste', async event => {
    if (event.composedPath().includes(host) || !editable(event.target)) return;
    const clipboard = event.clipboardData;
    if (!clipboard) return;
    // Provenance pastes belong to the existing visualization handler.
    if (/data-gusnb-(viz|provenance)/.test(clipboard.getData('text/html') + clipboard.getData('text/plain'))) return;
    const files = [...clipboard.files].filter(file => /^image\/(png|jpeg|webp|gif)$/.test(file.type));
    if (!files.length) {
      if (/<img[\s>]/i.test(clipboard.getData('text/html'))) {
        imagesBeforePaste = new Set(document.images);
        setTimeout(() => { imagesBeforePaste = null; }, 0);
      }
      return;
    }
    event.preventDefault();
    rememberRange();
    const insertion = validRange(savedRange) ? savedRange.cloneRange() : null;
    try {
      ui('notice').textContent = 'Pasting image…';
      const uploaded = await bridge.uploadImages(files);
      const images = await Promise.all(uploaded.map(async file => {
        const image = new Image();
        image.setAttribute('src', file.url);
        image.alt = file.name && !/^image\./i.test(file.name) ? file.name : 'Pasted image';
        await image.decode();
        image.style.cssText = `display:block;width:${Math.min(240, image.naturalWidth)}px;max-width:100%;height:auto;margin:12px 0;`;
        return image;
      }));
      edit(() => {
        const fragment = document.createDocumentFragment();
        images.forEach(image => fragment.appendChild(image));
        if (validRange(insertion)) {
          insertion.deleteContents(); insertion.insertNode(fragment);
        } else document.body.appendChild(fragment);
        const last = images.at(-1), parent = last.parentNode, offset = [...parent.childNodes].indexOf(last) + 1;
        restore({start:parent, from:offset, end:parent, to:offset});
      });
      const last = images.at(-1);
      last.scrollIntoView({block:'nearest'}); imageSettings(last);
      ui('notice').textContent = 'Image pasted at a small size. Use minus or plus to resize.';
    } catch (_) {
      ui('notice').textContent = 'This image could not be pasted.';
      bridge.notice('This image could not be pasted. Check that it is a valid image and the folder is writable.');
    }
  }, true);

  function insertCard() {
    rememberRange(); bridge.deselectViz();
    let reference = validRange(savedRange) ? element(savedRange.endContainer) : null;
    // Insert after a complete block, preserving paragraphs, lists and tables.
    // Never put a section inside a heading, paragraph, cell, or another card.
    reference = reference?.closest('[data-gusnb-card],table,ul,ol,blockquote,pre,p,h1,h2,h3,h4,h5,h6,div,section,article') || null;
    const container = reference?.closest('[data-gusnb-card],table,ul,ol');
    if (container) reference = container;
    if (reference === document.body || !editable(reference)) reference = null;
    let card;
    edit(() => {
      card = document.createElement('section');
      card.setAttribute('data-gusnb-card', '1');
      card.style.cssText = 'box-sizing:border-box;width:100%;max-width:100%;margin:16px 0;padding:20px;border:1px solid #cbd5e1;border-radius:8px;background:#ffffff;color:#263244;overflow-wrap:anywhere;';
      card.innerHTML = '<h3 style="font-size:20px;line-height:1.3;margin:0 0 6px;color:inherit">Card title</h3>' +
        '<p style="font-size:14px;line-height:1.5;margin:0 0 12px;color:#64748b">Subtitle</p>' +
        '<p style="font-size:14px;line-height:1.6;margin:0;color:inherit">Write your content here.</p>';
      if (reference) reference.after(card);
      else document.body.appendChild(card);
      const title = card.querySelector('h3').firstChild;
      restore({start:title, from:0, end:title, to:title.length});
    });
    card.scrollIntoView({block:'nearest'});
    window.focus(); document.body.focus({preventScroll:true});
    focusRange(); showSelection();
    ui('notice').textContent = 'Card inserted. Type a title; click its border for card settings.';
  }

  document.addEventListener('selectionchange', () => {
    if (shadow.activeElement) return;
    const range = currentRange();
    if (validRange(range)) rememberRange();
    else { ui('text').hidden = true; return; }
    if (range.collapsed) ui('text').hidden = true;
  });
  document.addEventListener('mouseup', event => {
    if (event.composedPath().includes(host)) return;
    if (event.target.tagName === 'IMG' && editable(event.target)) { hide(); imageSettings(event.target); return; }
    const card = event.target.closest?.('[data-gusnb-card]');
    if (card && event.target === card && currentRange()?.collapsed) cardSettings(card);
    else { hide(); showSelection(); }
  });
  document.addEventListener('keyup', event => {
    if (!event.composedPath().includes(host) && (event.shiftKey || event.key.startsWith('Arrow'))) showSelection();
  });
  const reposition = () => {
    if (!ui('text').hidden && validRange(savedRange)) position(ui('text'), savedRange.getBoundingClientRect());
    if (!ui('card').hidden) positionCard();
    if (!ui('image').hidden && selectedImage?.isConnected) imageSettings(selectedImage);
  };
  window.addEventListener('scroll', reposition, {passive:true, capture:true});
  window.addEventListener('resize', reposition);
  updateUndo();
  return {insertCard, appearance({dark, fontSize}) {
    host.toggleAttribute('data-dark', !!dark);
    if (Number.isFinite(fontSize) && fontSize >= 10 && fontSize <= 22) host.style.setProperty('--size', fontSize + 'px');
    reposition();
  }};
}
