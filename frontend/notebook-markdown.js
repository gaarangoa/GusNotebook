import {Marked} from 'marked';
import katex from 'katex';
import DOMPurify from 'dompurify';

// Tokenize before Markdown can consume TeX backslashes, underscores or HTML.
// A separate parser keeps math syntax out of app help.
function mathToken(source) {
  const open = ['$$', '\\[', '\\(', '$'].find(value => source.startsWith(value));
  if (!open) return;
  const close = {'$$': '$$', '\\[': '\\]', '\\(': '\\)', '$': '$'}[open];
  const displayMode = open === '$$' || open === '\\[';
  if (open === '$' && /\s/.test(source[1] || ' ')) return;
  for (let end = open.length; end < source.length; end++) {
    if (!displayMode && source[end] === '\n') return;
    if (source.startsWith(close, end)) {
      if (open === '$' && (/\s/.test(source[end - 1]) || /\d/.test(source[end + 1] || ''))) continue;
      const text = source.slice(open.length, end);
      if (!text.trim()) return;
      return {raw: source.slice(0, end + close.length), text, displayMode};
    }
    // Escaped dollars and paired backslashes cannot close a formula.
    if (source[end] === '\\') end++;
  }
}

function renderMath(token) {
  return katex.renderToString(token.text, {
    displayMode: token.displayMode,
    throwOnError: false,
    trust: false,
    maxSize: 20,
    maxExpand: 1000,
  });
}

function mathMarkdown(renderer) {
  return new Marked({extensions: [
    {
      name: 'mathBlock',
      level: 'block',
      start(source) { return source.match(/(?:^|\n) {0,3}(?:\$\$|\\\[)/)?.index; },
      tokenizer(source) {
        const indent = source.match(/^ {0,3}/)[0];
        const token = mathToken(source.slice(indent.length));
        if (!token?.displayMode) return;
        const trailing = source.slice(indent.length + token.raw.length).match(/^[ \t]*(?:\n|$)/);
        if (!trailing) return;
        return {...token, type: 'mathBlock', raw: indent + token.raw + trailing[0]};
      },
      renderer,
    },
    {
      name: 'mathInline',
      level: 'inline',
      start(source) { return source.match(/\$|\\[([]/)?.index; },
      tokenizer(source) {
        if (this.lexer.state.inRawBlock) return;
        const token = mathToken(source);
        if (token) return {...token, type: 'mathInline'};
      },
      renderer,
    },
  ]});
}

const notebookMarkdown = mathMarkdown(renderMath);

export function renderNotebookMarkdown(source) {
  // Sanitize the complete output, including math, before it enters the page.
  return DOMPurify.sanitize(notebookMarkdown.parse(source || ''), {
    ADD_TAGS: ['semantics', 'annotation'],
  });
}

export function renderMarkdownFileFragment(source) {
  // Keep generated math separate so file HTML cannot supply KaTeX styles,
  // classes, or MathML. Unpredictable markers cannot be forged by the source.
  const prefix = `gusnotebook-math-${crypto.getRandomValues(new Uint32Array(4)).join('-')}-`;
  const formulas = new Map();
  const parser = mathMarkdown(token => {
    const marker = prefix + formulas.size;
    formulas.set(marker, renderMath(token));
    return `<span>${marker}</span>`;
  });
  const fragment = DOMPurify.sanitize(parser.parse(source || ''), {
    RETURN_DOM_FRAGMENT: true, USE_PROFILES: {html: true}, ALLOW_DATA_ATTR: false,
    FORBID_TAGS: ['style', 'form', 'button', 'textarea', 'select', 'iframe', 'video', 'audio', 'picture', 'source'],
    FORBID_ATTR: ['style', 'class', 'id', 'name', 'srcset'],
  });
  fragment.querySelectorAll('span').forEach(placeholder => {
    const formula = formulas.get(placeholder.textContent);
    if (!formula || placeholder.childElementCount) return;
    placeholder.replaceWith(DOMPurify.sanitize(formula, {
      RETURN_DOM_FRAGMENT: true, ADD_TAGS: ['semantics', 'annotation'],
    }));
  });
  return fragment;
}
