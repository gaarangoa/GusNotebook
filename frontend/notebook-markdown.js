import {Marked} from 'marked';
import katex from 'katex';
import DOMPurify from 'dompurify';

// Tokenize before Markdown can consume TeX backslashes, underscores or HTML.
// A separate parser keeps notebook syntax out of file previews and app help.
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

const notebookMarkdown = new Marked({extensions: [
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
    renderer: renderMath,
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
    renderer: renderMath,
  },
]});

export function renderNotebookMarkdown(source) {
  // Sanitize the complete output, including math, before it enters the page.
  return DOMPurify.sanitize(notebookMarkdown.parse(source || ''), {
    ADD_TAGS: ['semantics', 'annotation'],
  });
}
