/* Render in one isolated iframe, then display the SVG as a static image.
 * The graph's markup and styles never become part of the application DOM. */
let markdownMermaidRuntime = null;
let markdownMermaidQueue = Promise.resolve();
let markdownMermaidSerial = 0;
const markdownMermaidState = new WeakMap();

function getMarkdownMermaidRuntime() {
  if (markdownMermaidRuntime) return markdownMermaidRuntime.ready;
  const state = {pending: new Map()};
  markdownMermaidRuntime = state;
  state.ready = (async () => {
    const response = await fetch(BASE + '/static/vendor/mermaid.js', {credentials: 'same-origin'});
    if (!response.ok) throw new Error('Could not load the diagram renderer. Reload to try again.');
    // Opaque sandbox origins cannot load a blob owned by the parent. Embed the
    // trusted local bundle and escape HTML script terminators in its JS strings.
    const script = (await response.text()).replace(/<\/script/gi, '<\\/script');
    const nonce = crypto.getRandomValues(new Uint32Array(4)).join('_');
    const frame = document.createElement('iframe');
    frame.className = 'mermaid-renderer';
    frame.title = 'Diagram renderer';
    frame.setAttribute('aria-hidden', 'true');
    frame.tabIndex = -1;
    frame.setAttribute('sandbox', 'allow-scripts');
    state.frame = frame;
    state.nonce = nonce;
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        window.removeEventListener('message', state.listener);
        frame.remove();
        reject(new Error('Diagram renderer timed out. Reload to try again.'));
      }, 20000);
      state.listener = event => {
        const data = event.data;
        if (event.source !== frame.contentWindow || data?.channel !== 'gusnotebook-mermaid' ||
            data.nonce !== nonce) return;
        if (data.type === 'ready') {
          clearTimeout(timer);
          resolve();
        } else if (data.type === 'result') {
          const job = state.pending.get(data.id);
          if (!job) return;
          state.pending.delete(data.id);
          clearTimeout(job.timer);
          data.error ? job.reject(new Error(data.error)) : job.resolve(data.svg);
        }
      };
      window.addEventListener('message', state.listener);
      frame.srcdoc = `<!doctype html><html data-nonce="${nonce}"><head>
        <meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; font-src data:">
        <style>body{margin:0;background:transparent}</style></head><body>
        <script>${script}</script></body></html>`;
      document.body.append(frame);
    });
    return state;
  })().catch(error => {
    if (markdownMermaidRuntime === state) markdownMermaidRuntime = null;
    throw error;
  });
  return state.ready;
}

function markdownMermaidSvg(runtime, source, appearance) {
  const id = ++markdownMermaidSerial;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      runtime.pending.delete(id);
      reject(new Error('Diagram rendering timed out. Showing source.'));
    }, 20000);
    runtime.pending.set(id, {resolve, reject, timer});
    runtime.frame.contentWindow.postMessage({channel: 'gusnotebook-mermaid', nonce: runtime.nonce,
      type: 'render', id, source, appearance}, '*');
  });
}

function renderMarkdownMermaid(preview) {
  const style = getComputedStyle(preview);
  const appearance = {colors: AppAppearance.colors(), dark: AppAppearance.isDark(),
    fontFamily: style.fontFamily, fontSize: parseFloat(style.fontSize)};
  const key = JSON.stringify(appearance);
  preview.querySelectorAll('.markdown-mermaid').forEach(block => {
    if (markdownMermaidState.get(block)?.key === key) return;
    const state = {key};
    markdownMermaidState.set(block, state);
    const current = () => block.isConnected && markdownMermaidState.get(block) === state;
    const diagram = block.querySelector('.mermaid-diagram');
    const status = block.querySelector('.mermaid-status');
    const details = block.querySelector('details');
    diagram.setAttribute('aria-busy', 'true');
    status.textContent = 'Rendering diagram…';
    status.hidden = false;
    markdownMermaidQueue = markdownMermaidQueue.then(async () => {
      if (!current()) return;
      try {
        const source = block.dataset.mermaidSource;
        if (source.length > 50000) throw new Error('Diagram exceeds 50,000 characters. Showing source.');
        const runtime = await getMarkdownMermaidRuntime();
        if (!current()) return;
        const svg = await markdownMermaidSvg(runtime, source, appearance);
        if (!current()) return;
        const safe = DOMPurify.sanitize(svg, {USE_PROFILES: {svg: true, svgFilters: true},
          FORBID_TAGS: ['foreignObject', 'script', 'a', 'image']});
        const image = document.createElement('img');
        const drawing = new DOMParser().parseFromString(safe, 'image/svg+xml').documentElement;
        image.alt = drawing.querySelector('title')?.textContent || 'Mermaid diagram';
        const width = Number(drawing.getAttribute('viewBox')?.split(/[ ,]+/)[2]);
        if (Number.isFinite(width) && width > 0) image.width = Math.ceil(Math.min(width, 10000));
        image.dataset.theme = appearance.dark ? 'dark' : 'light';
        // SVGs used as images cannot execute scripts or affect the page styles.
        image.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(safe);
        await image.decode();
        if (!current()) return;
        diagram.replaceChildren(image);
        status.hidden = true;
        block.classList.remove('mermaid-error');
      } catch (error) {
        if (!current()) return;
        diagram.replaceChildren();
        block.classList.add('mermaid-error');
        status.textContent = `Could not render Mermaid diagram: ${error.message || error}`;
        status.hidden = false;
        details.open = true;
      } finally {
        if (current()) diagram.setAttribute('aria-busy', 'false');
      }
    });
  });
}

let markdownMermaidAppearanceTimer;
document.addEventListener('appearance-change', () => {
  clearTimeout(markdownMermaidAppearanceTimer);
  markdownMermaidAppearanceTimer = setTimeout(() => {
    const t = activeTab();
    if (isMarkdownTab(t) && t.markdownMode !== 'source') {
      renderMarkdownMermaid(document.getElementById('markdown-preview'));
    }
  }, 150);
});
