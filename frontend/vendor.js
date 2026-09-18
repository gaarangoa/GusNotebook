import {Terminal} from '@xterm/xterm';
import {FitAddon} from '@xterm/addon-fit';
import {WebLinksAddon} from '@xterm/addon-web-links';
import {marked} from 'marked';
import DOMPurify from 'dompurify';
import {renderNotebookMarkdown} from './notebook-markdown.js';

Object.assign(window, {Terminal, FitAddon: {FitAddon},
                      WebLinksAddon: {WebLinksAddon}, marked, DOMPurify, renderNotebookMarkdown});
