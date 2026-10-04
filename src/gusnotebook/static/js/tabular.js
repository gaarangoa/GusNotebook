/* Tables share the text buffer for delimited/JSON files. Binary files are
 * read-only snapshots; only the visible page is added to the DOM. */
const TABLE_PAGE_SIZE = 50;

function isTableTab(t) {
  return !!t && (t.kind === 'table' || (t.kind === 'text' &&
    ['csv', 'tsv', 'json', 'jsonl', 'ndjson'].includes(t.language)));
}

function tableOptions(t) {
  return t.tableOptions ||= {headers: true, delimiter: 'auto', query: '', page: 0,
    sort: null, direction: 1, sheet: t.tablePreview?.sheet};
}

function showTableFile(t) {
  const enabled = isTableTab(t);
  const source = enabled && t.kind === 'text' && t.tableMode === 'source';
  document.getElementById('textpane').classList.toggle('tabular', enabled);
  document.getElementById('textpane').classList.toggle('table-source', source);
  document.getElementById('table-view').hidden = !enabled;
  document.getElementById('table-source-button').hidden = t?.kind === 'table';
  document.getElementById('table-preview-button').setAttribute('aria-pressed', String(!source));
  document.getElementById('table-source-button').setAttribute('aria-pressed', String(source));
  if (!enabled || source) return;
  const options = tableOptions(t);
  if (!t.tablePreview || (t.kind === 'text' && t.tablePreviewText !== t.text) ||
      (t.kind === 'text' && t.tablePreviewDelimiter !== options.delimiter) ||
      (t.kind === 'table' && t.tablePreview.sheet && options.sheet !== t.tablePreview.sheet)) {
    loadTablePreview(t);
  }
  renderTablePreview(t);
}

function setTableMode(mode) {
  const t = activeTab();
  if (!isTableTab(t) || !['source', 'preview'].includes(mode) ||
      (mode === 'source' && t.kind !== 'text')) return;
  t.tableMode = mode;
  showTableFile(t);
  if (mode === 'source') document.getElementById('text-editor').focus({preventScroll: true});
}

async function loadTablePreview(t, force = false) {
  const options = tableOptions(t);
  const text = t.text || '';
  const delimiter = options.delimiter;
  const sheet = options.sheet;
  const key = JSON.stringify([text, delimiter, sheet]);
  if (!force && t.tableRequestKey === key) return;
  t.tableController?.abort();
  const controller = new AbortController();
  t.tableController = controller;
  t.tableRequestKey = key;
  t.tableLoading = true;
  const timeout = setTimeout(() => controller.abort(), 15000);
  try {
    const data = await api('/api/table-preview', {method: 'POST', signal: controller.signal,
      body: JSON.stringify({path: t.path, ...(t.kind === 'text' ? {text} : {}),
        delimiter: delimiter === 'tab' ? '\t' : delimiter, sheet})});
    if (t.tableController !== controller) return;
    t.tablePreview = data;
    options.sheet = data.sheet;
  } catch (err) {
    if (t.tableController !== controller) return;
    t.tablePreview = {error: controller.signal.aborted ? 'Preview timed out. Use Reload to try again.' : errText(err)};
  } finally {
    clearTimeout(timeout);
    if (t.tableController === controller) {
      t.tableLoading = false;
      t.tableRequestKey = null;
      t.tablePreviewText = text;
      t.tablePreviewDelimiter = delimiter;
      if (activeTab() === t && t.tableMode !== 'source') renderTablePreview(t);
    }
  }
}

function tableNode(tag, text, className) {
  const node = document.createElement(tag);
  if (text != null) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function renderTablePreview(t) {
  if (!isTableTab(t) || activeTab() !== t) return;
  const options = tableOptions(t);
  const data = t.tablePreview || {};
  const delimited = ['csv', 'tsv'].includes(t.language);
  const headerControl = document.getElementById('table-header-control');
  headerControl.hidden = data.columns != null || !['csv', 'tsv', 'xlsx'].includes(t.language);
  document.getElementById('table-header').checked = options.headers;
  document.getElementById('table-delimiter-control').hidden = !delimited;
  document.getElementById('table-delimiter').value = options.delimiter;
  document.getElementById('table-search').value = options.query;
  document.getElementById('table-search').disabled = !!t.tableLoading || !!data.error;
  document.getElementById('table-sheet-control').hidden = !data.sheets;
  const sheetSelect = document.getElementById('table-sheet');
  sheetSelect.replaceChildren(...(data.sheets || []).map(name => {
    const option = tableNode('option', name);
    option.value = name;
    return option;
  }));
  sheetSelect.value = options.sheet || data.sheet || '';
  const notice = document.getElementById('table-notice');
  notice.textContent = t.tableLoading ? 'Loading table preview…' : data.error ||
    [...(data.notices || []), data.note || ''].filter(Boolean).join(' ');
  notice.hidden = !notice.textContent;

  const grid = document.getElementById('table-grid');
  const head = grid.querySelector('thead');
  const body = grid.querySelector('tbody');
  head.replaceChildren();
  body.replaceChildren();
  const raw = t.tableLoading || data.error ? [] : data.rows || [];
  const hasHeader = data.columns == null && options.headers;
  const rows = hasHeader ? raw.slice(1) : raw;
  const width = Math.min(100, Math.max(data.columns?.length || 0, ...raw.map(row => row.length), 0));
  const columns = Array.from({length: width}, (_, index) =>
    (data.columns ? data.columns[index] : hasHeader ? raw[0]?.[index] : '') || `Column ${index + 1}`);
  const headerRow = tableNode('tr');
  const corner = tableNode('th', '#', 'table-row-number');
  corner.scope = 'col';
  headerRow.append(corner);
  columns.forEach((label, index) => {
    const th = tableNode('th');
    th.scope = 'col';
    const sorted = options.sort === index;
    th.setAttribute('aria-sort', sorted ? options.direction === 1 ? 'ascending' : 'descending' : 'none');
    const button = tableNode('button', `${label}${sorted ? options.direction === 1 ? ' ↑' : ' ↓' : ''}`);
    button.type = 'button';
    button.title = `Sort by ${label}${data.types?.[index] ? ` (${data.types[index]})` : ''}`;
    button.addEventListener('click', () => {
      options.direction = sorted ? -options.direction : 1;
      options.sort = index;
      options.page = 0;
      renderTablePreview(t);
      head.querySelectorAll('button')[index]?.focus({preventScroll: true});
    });
    th.append(button);
    headerRow.append(th);
  });
  if (width) head.append(headerRow);
  const query = options.query.toLocaleLowerCase();
  const indices = rows.map((_, index) => index).filter(index => !query ||
    rows[index].some(value => String(value).toLocaleLowerCase().includes(query)));
  if (options.sort != null && options.sort < width) {
    const column = options.sort;
    const numeric = /^[-+]?(?:\d+\.?\d*|\.\d+)(?:e[-+]?\d+)?$/i;
    indices.sort((a, b) => {
      const left = String(rows[a][column] ?? ''), right = String(rows[b][column] ?? '');
      const comparison = numeric.test(left) && numeric.test(right) &&
        Number.isFinite(Number(left)) && Number.isFinite(Number(right))
        ? Number(left) - Number(right) : left.localeCompare(right, undefined, {numeric: true});
      return comparison * options.direction || a - b;
    });
  }
  const pages = Math.max(1, Math.ceil(indices.length / TABLE_PAGE_SIZE));
  options.page = Math.max(0, Math.min(options.page, pages - 1));
  const visible = indices.slice(options.page * TABLE_PAGE_SIZE, (options.page + 1) * TABLE_PAGE_SIZE);
  const fragment = document.createDocumentFragment();
  visible.forEach(index => {
    const row = tableNode('tr');
    const number = tableNode('th', String(index + 1), 'table-row-number');
    number.scope = 'row';
    row.append(number);
    columns.forEach((_, column) => {
      const value = String(rows[index][column] ?? '');
      const cell = tableNode('td', value);
      cell.title = value;
      row.append(cell);
    });
    fragment.append(row);
  });
  body.append(fragment);
  if (!visible.length && !t.tableLoading && !data.error) {
    const row = tableNode('tr');
    const cell = tableNode('td', query ? 'No matching rows in this preview.' : 'No data rows.');
    cell.colSpan = width + 1;
    row.append(cell);
    body.append(row);
  }
  const counts = `${rows.length.toLocaleString()} rows · ${width} columns`;
  document.getElementById('table-summary').textContent = data.error || t.tableLoading ? '' :
    `${data.truncated ? 'Snapshot: ' : ''}${counts}${query ? ` · ${indices.length.toLocaleString()} matches` : ''}`;
  document.getElementById('table-page').textContent = `${options.page + 1} / ${pages}`;
  document.getElementById('table-previous').disabled = options.page === 0 || !!t.tableLoading;
  document.getElementById('table-next').disabled = options.page >= pages - 1 || !!t.tableLoading;
}

function filterTablePreview(query) {
  const t = activeTab();
  if (!isTableTab(t)) return;
  Object.assign(tableOptions(t), {query, page: 0});
  renderTablePreview(t);
}

function turnTablePage(direction) {
  const t = activeTab();
  if (!isTableTab(t)) return;
  tableOptions(t).page += direction;
  renderTablePreview(t);
  document.getElementById('table-scroll').scrollTop = 0;
}

function changeTableHeader(headers) {
  const t = activeTab();
  if (!isTableTab(t)) return;
  Object.assign(tableOptions(t), {headers, page: 0, sort: null});
  renderTablePreview(t);
}

function changeTableDelimiter(delimiter) {
  const t = activeTab();
  if (!isTableTab(t)) return;
  Object.assign(tableOptions(t), {delimiter, page: 0, sort: null});
  loadTablePreview(t, true);
  renderTablePreview(t);
}

function changeTableSheet(sheet) {
  const t = activeTab();
  if (t?.kind !== 'table') return;
  Object.assign(tableOptions(t), {sheet, page: 0, sort: null, query: ''});
  loadTablePreview(t, true);
  renderTablePreview(t);
}

function reloadTableFile(t) {
  Object.assign(tableOptions(t), {page: 0, sort: null});
  loadTablePreview(t, true);
  renderTablePreview(t);
}
