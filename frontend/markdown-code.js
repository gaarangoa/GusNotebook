import hljs from 'highlight.js/lib/core';
import python from 'highlight.js/lib/languages/python';
import javascript from 'highlight.js/lib/languages/javascript';
import typescript from 'highlight.js/lib/languages/typescript';
import json from 'highlight.js/lib/languages/json';
import bash from 'highlight.js/lib/languages/bash';
import css from 'highlight.js/lib/languages/css';
import xml from 'highlight.js/lib/languages/xml';
import sql from 'highlight.js/lib/languages/sql';
import yaml from 'highlight.js/lib/languages/yaml';
import latex from 'highlight.js/lib/languages/latex';
import DOMPurify from 'dompurify';

for (const [name, grammar] of Object.entries({python, javascript, typescript, json, bash, css, xml, sql, yaml, latex})) {
  hljs.registerLanguage(name, grammar);
}

export function renderMarkdownCode(text, info) {
  const language = (info || '').trim().split(/\s+/, 1)[0].toLowerCase();
  const grammar = hljs.getLanguage(language);
  const block = document.createElement('div');
  block.className = 'markdown-code';
  const header = document.createElement('div');
  header.className = 'markdown-code-header';
  const label = document.createElement('span');
  label.textContent = grammar?.name || language.slice(0, 32) || 'Text';
  const copy = document.createElement('button');
  copy.type = 'button';
  copy.className = 'markdown-code-copy';
  copy.textContent = 'Copy';
  copy.setAttribute('aria-label', 'Copy code');
  header.append(label, copy);
  const pre = document.createElement('pre');
  const code = document.createElement('code');
  // Unknown or very large blocks remain readable, without guessing a language.
  if (grammar && text.length <= 100000) {
    code.append(DOMPurify.sanitize(hljs.highlight(text, {language, ignoreIllegals: true}).value, {
      RETURN_DOM_FRAGMENT: true, ALLOWED_TAGS: ['span'], ALLOWED_ATTR: ['class'],
    }));
  } else {
    code.textContent = text;
  }
  pre.append(code);
  block.append(header, pre);
  return block;
}
