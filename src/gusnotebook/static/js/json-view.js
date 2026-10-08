/* Read-only JSON trees share the original text buffer and save/conflict handling.
 * Token text preserves large numbers, duplicate keys and their original order.
 * Parsing and lazy rendering are bounded; complex documents remain editable. */
const JSON_VIEW_MAX_BYTES = 2 * 1024 * 1024;
const JSON_VIEW_MAX_TOKENS = 10000;
const JSON_VIEW_MAX_DEPTH = 64;
const JSON_VIEW_PAGE_SIZE = 100;

function isJsonTab(t) {
  return !!t && t.kind === 'text' && ['json', 'jsonl', 'ndjson'].includes(t.language);
}

function parseJsonView(source) {
  if (source.length > JSON_VIEW_MAX_BYTES || new Blob([source]).size > JSON_VIEW_MAX_BYTES) {
    throw new Error('JSON preview is limited to 2 MB. Showing Source.');
  }
  source = source.replace(/^\uFEFF/, '');
  // Validate without using the decoded values: JSON.parse would round large
  // integers and discard duplicate keys if those values were used for display.
  try { JSON.parse(source); }
  catch (_) { throw new Error('Invalid JSON. Showing Source so you can edit it, then choose JSON to try again.'); }
  const tokens = /("(?:[^"\\]|\\.)*")(\s*:)?|-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?|true|false|null|[{}\[\]:,]/g;
  const root = {children: []}, stack = [];
  let parent = root, key = null, count = 0;
  for (const match of source.matchAll(tokens)) {
    if (++count > JSON_VIEW_MAX_TOKENS || match[0].length > 10000) {
      throw new Error('This JSON is too complex for the tree view. Showing the full Source instead.');
    }
    const raw = match[0];
    if (raw === ',' || raw === ':') continue;
    if (raw === '}' || raw === ']') { parent = stack.pop(); continue; }
    if (match[2]) { key = match[1]; continue; }
    const node = {key, raw};
    key = null;
    parent.children.push(node);
    if (raw === '{' || raw === '[') {
      if (stack.length >= JSON_VIEW_MAX_DEPTH) {
        throw new Error('This JSON is too deeply nested for the tree view. Showing the full Source instead.');
      }
      node.children = [];
      stack.push(parent);
      parent = node;
    }
  }
  return root.children[0];
}

function jsonSpan(className, text) {
  const span = document.createElement('span');
  span.className = className;
  span.textContent = text;
  return span;
}

function renderJsonNode(node, comma = false, expanded = false) {
  const suffix = comma ? ',' : '';
  const prefix = node.key === null ? [] : [jsonSpan('json-key', node.key), ': '];
  if (!node.children?.length) {
    const line = document.createElement('div');
    line.className = 'json-line';
    const value = node.children ? (node.raw === '{' ? '{}' : '[]') : node.raw;
    const type = node.children ? '' : value[0] === '"' ? 'json-string'
      : /^[-\d]/.test(value) ? 'json-number' : 'json-literal';
    line.append(...prefix, jsonSpan(type, value), suffix);
    return line;
  }
  const details = document.createElement('details');
  const summary = document.createElement('summary');
  const closing = node.raw === '{' ? '}' : ']';
  const count = node.children.length;
  const label = node.raw === '{' ? (count === 1 ? 'property' : 'properties') : (count === 1 ? 'item' : 'items');
  summary.append(...prefix, node.raw, jsonSpan('json-collapsed', '…' + closing + suffix),
    jsonSpan('json-count', `${count.toLocaleString()} ${label}`));
  details.append(summary);
  let loaded = false;
  function populate() {
    if (!details.open || loaded) return;
    loaded = true;
    const children = document.createElement('div');
    children.className = 'json-children';
    const end = document.createElement('div');
    end.className = 'json-line';
    end.textContent = closing + suffix;
    const more = document.createElement('button');
    more.type = 'button';
    more.className = 'files-btn json-more';
    let offset = 0;
    function nextPage() {
      more.remove();
      const limit = Math.min(offset + JSON_VIEW_PAGE_SIZE, node.children.length);
      for (; offset < limit; offset++) {
        children.append(renderJsonNode(node.children[offset], offset < node.children.length - 1));
      }
      if (offset < node.children.length) {
        more.textContent = `Show next ${Math.min(JSON_VIEW_PAGE_SIZE, node.children.length - offset)} · ${(node.children.length - offset).toLocaleString()} remaining`;
        children.append(more);
      }
    }
    more.addEventListener('click', nextPage);
    nextPage();
    details.append(children, end);
  }
  details.addEventListener('toggle', populate);
  details.open = expanded;
  populate();
  return details;
}

function showJsonFile(t) {
  const enabled = isJsonTab(t);
  const pane = document.getElementById('textpane');
  const preview = document.getElementById('json-preview');
  const notice = document.getElementById('json-notice');
  document.getElementById('json-view').hidden = !enabled || t.language !== 'json';
  pane.classList.toggle('json', enabled);
  let view = enabled ? t.jsonView : null;
  if (enabled && t.jsonMode !== 'source' && (!view || view.text !== t.text)) {
    view = t.jsonView = {text: t.text, element: null, notice: ''};
    t.jsonScroll = 0;
    try {
      if (t.language !== 'json') {
        throw new Error('JSON Lines is shown as plain text. Files are limited to 2 MB.');
      }
      view.element = renderJsonNode(parseJsonView(t.text), false, true);
    } catch (error) { view.notice = error.message; }
  }
  const source = enabled && (t.jsonMode === 'source' || !view?.element);
  pane.classList.toggle('json-source', source);
  preview.hidden = !enabled || source;
  notice.textContent = enabled ? view?.notice || '' : '';
  notice.hidden = !notice.textContent;
  document.getElementById('json-preview-button').setAttribute('aria-pressed', String(enabled && !source));
  document.getElementById('json-source-button').setAttribute('aria-pressed', String(source));
  if (!enabled) preview.replaceChildren();
  else if (!source) {
    if (preview.firstChild !== view.element) preview.replaceChildren(view.element);
    preview.scrollTop = t.jsonScroll || 0;
  }
}

function setJsonMode(mode) {
  const t = activeTab();
  if (!isJsonTab(t) || !['source', 'preview'].includes(mode)) return;
  const preview = document.getElementById('json-preview');
  if (!preview.hidden) t.jsonScroll = preview.scrollTop;
  t.jsonMode = mode;
  showJsonFile(t);
  if (document.getElementById('textpane').classList.contains('json-source')) {
    document.getElementById('text-editor').focus({preventScroll: true});
  }
}
