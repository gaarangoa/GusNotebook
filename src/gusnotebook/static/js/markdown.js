/* Markdown files share the text editor's buffer, save and conflict handling. */

function isMarkdownTab(t) {
  return !!t && t.kind === 'text' && ['md', 'markdown'].includes(t.language);
}

function markdownFileUrl(t, value) {
  try {
    if (value.startsWith('//')) return new URL(value, location.href);
    const base = new URL('file://' + t.path.split('/').map(encodeURIComponent).join('/'));
    return new URL(value, base);
  } catch (_) { return null; }
}

function renderMarkdownFile(t) {
  const preview = document.getElementById('markdown-preview');
  const fragment = DOMPurify.sanitize(marked.parse(t.text || ''), {
    RETURN_DOM_FRAGMENT: true, USE_PROFILES: {html: true}, ALLOW_DATA_ATTR: false,
    FORBID_TAGS: ['style', 'form', 'button', 'textarea', 'select', 'iframe', 'video', 'audio', 'picture', 'source'],
    FORBID_ATTR: ['style', 'class', 'id', 'name', 'srcset'],
  });
  // Raw Markdown HTML must not take over app IDs, styles, or controls.
  fragment.querySelectorAll('input').forEach(input => { input.disabled = true; });
  const anchors = new Set();
  fragment.querySelectorAll('h1,h2,h3,h4,h5,h6').forEach(heading => {
    const base = heading.textContent.trim().toLowerCase().replace(/[^\p{L}\p{N}_\s-]/gu, '').replace(/\s+/g, '-') || 'section';
    let slug = base, index = 1;
    while (anchors.has(slug)) slug = `${base}-${index++}`;
    anchors.add(slug);
    heading.dataset.mdAnchor = slug;
  });
  fragment.querySelectorAll('img[src]').forEach(img => {
    const url = markdownFileUrl(t, img.getAttribute('src'));
    if (!url) { img.removeAttribute('src'); return; }
    if (url.protocol === 'file:') {
      try { img.src = BASE + '/api/raw?' + new URLSearchParams({path: decodeURIComponent(url.pathname)}); }
      catch (_) { img.removeAttribute('src'); }
    } else if (!['https:', 'http:', 'data:'].includes(url.protocol)) {
      img.removeAttribute('src');
    }
    img.loading = 'lazy';
    img.referrerPolicy = 'no-referrer';
  });
  fragment.querySelectorAll('a[href]').forEach(link => {
    const url = markdownFileUrl(t, link.getAttribute('href'));
    if (!url) { link.removeAttribute('href'); return; }
    if (url.protocol === 'file:') {
      try {
        link.dataset.mdPath = decodeURIComponent(url.pathname);
        link.dataset.mdFragment = decodeURIComponent(url.hash.slice(1));
        // A normal click opens a document tab. The fallback supports copying
        // the link or downloading it with the browser's context menu.
        link.href = BASE + '/api/files/download?' + new URLSearchParams({path: link.dataset.mdPath});
      } catch (_) { link.removeAttribute('href'); }
    } else if (['https:', 'http:', 'mailto:', 'tel:'].includes(url.protocol)) {
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
    } else {
      link.removeAttribute('href');
    }
  });
  preview.replaceChildren(fragment);
  preview.scrollTop = t.markdownScroll || 0;
}

function showMarkdownFile(t) {
  const enabled = isMarkdownTab(t);
  const source = enabled && t.markdownMode === 'source';
  const pane = document.getElementById('textpane');
  pane.classList.toggle('markdown', enabled);
  pane.classList.toggle('markdown-source', source);
  document.getElementById('markdown-view').hidden = !enabled;
  document.getElementById('markdown-preview-button').setAttribute('aria-pressed', String(!source));
  document.getElementById('markdown-source-button').setAttribute('aria-pressed', String(source));
  if (enabled && !source) renderMarkdownFile(t);
}

function setMarkdownMode(mode) {
  const t = activeTab();
  if (!isMarkdownTab(t) || !['source', 'preview'].includes(mode)) return;
  t.markdownMode = mode;
  showMarkdownFile(t);
  if (mode === 'source') document.getElementById('text-editor').focus({preventScroll: true});
}

document.getElementById('markdown-preview').addEventListener('scroll', event => {
  const t = activeTab();
  if (isMarkdownTab(t) && t.markdownMode !== 'source') t.markdownScroll = event.currentTarget.scrollTop;
});

document.getElementById('markdown-preview').addEventListener('click', async event => {
  const link = event.target.closest('a[data-md-path]');
  if (!link || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  const path = link.dataset.mdPath, anchor = link.dataset.mdFragment;
  const opened = active === path ? activeTab() : await openFile(path);
  if (!opened || active !== opened.path || !isMarkdownTab(opened)) return;
  setMarkdownMode('preview');
  const preview = document.getElementById('markdown-preview');
  if (!anchor) { preview.scrollTop = 0; return; }
  const heading = [...preview.querySelectorAll('[data-md-anchor]')].find(el => el.dataset.mdAnchor === anchor);
  heading?.scrollIntoView({block: 'start'});
});
