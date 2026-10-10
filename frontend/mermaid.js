// This bundle runs only inside the Markdown renderer's sandboxed iframe.
import mermaid from 'mermaid';

const channel = 'gusnotebook-mermaid';
const nonce = document.documentElement.dataset.nonce;
let queue = Promise.resolve();

window.addEventListener('message', event => {
  const data = event.data;
  if (event.source !== parent || data?.channel !== channel || data.nonce !== nonce ||
      data.type !== 'render') return;
  queue = queue.then(async () => {
    try {
      const {colors, fontFamily, fontSize, dark} = data.appearance;
      mermaid.initialize({
        startOnLoad: false, securityLevel: 'strict', suppressErrorRendering: true,
        maxTextSize: 50000, maxEdges: 500, htmlLabels: false, fontFamily,
        theme: 'base',
        themeVariables: {darkMode: dark, fontFamily, fontSize: `${fontSize}px`,
          background: colors.panel, primaryColor: colors.surface,
          primaryTextColor: colors.text, primaryBorderColor: colors.accent,
          secondaryColor: colors.surface, secondaryTextColor: colors.text,
          secondaryBorderColor: colors.border, tertiaryColor: colors.panel,
          tertiaryTextColor: colors.text, tertiaryBorderColor: colors.border,
          textColor: colors.text, lineColor: colors.muted},
        // Diagram frontmatter/directives cannot weaken the renderer settings.
        secure: ['secure', 'securityLevel', 'startOnLoad', 'suppressErrorRendering',
          'maxTextSize', 'maxEdges', 'htmlLabels', 'fontFamily', 'theme',
          'themeVariables', 'themeCSS', 'flowchart', 'dompurifyConfig'],
        flowchart: {htmlLabels: false},
      });
      const {svg} = await mermaid.render(`diagram_${nonce}_${data.id}`, data.source);
      parent.postMessage({channel, nonce, type: 'result', id: data.id, svg}, '*');
    } catch (error) {
      parent.postMessage({channel, nonce, type: 'result', id: data.id,
        error: String(error?.message || error).slice(0, 600)}, '*');
    }
  });
});
parent.postMessage({channel, nonce, type: 'ready'}, '*');
