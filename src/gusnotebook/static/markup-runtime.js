/* Runs inside a localhost preview, never in GusNotebook's own page.
 * The preview server injects the script tag and configuration into its response;
 * cleanClone removes them again before edited markup is saved. */
(function () {
  'use strict';
  var script = document.currentScript;
  var config = JSON.parse(script.dataset.config || '{}');
  var runtimeAttr = 'data-gusnotebook-runtime';
  var svgEditor = null;
  var changed = false;

  function cleanClone(node) {
    var clone = node.cloneNode(true);
    clone.querySelectorAll('[' + runtimeAttr + ']').forEach(function (el) {
      el.remove();
    });
    clone.querySelectorAll('[data-gusnotebook-edit-root]').forEach(function (el) {
      el.removeAttribute('data-gusnotebook-edit-root');
      el.removeAttribute('contenteditable');
      el.removeAttribute('spellcheck');
    });
    return clone;
  }

  function serialize() {
    if (config.mode === 'svg') {
      var svg = document.querySelector('body > svg') || document.querySelector('svg');
      return svg ? config.svgPrefix + cleanClone(svg).outerHTML + config.svgSuffix : '';
    }
    var root = cleanClone(document.documentElement);
    var doctype = document.doctype
      ? new XMLSerializer().serializeToString(document.doctype) + '\n'
      : '';
    return doctype + root.outerHTML;
  }

  function selectedRange() {
    var selection = document.getSelection();
    if (!selection || !selection.rangeCount || selection.isCollapsed || svgEditor) return null;
    var plainText = selection.toString();
    var range = selection.getRangeAt(0).cloneRange();
    if (!document.body || !document.body.contains(range.commonAncestorContainer)) return null;

    var key = config.nonce.replace(/[^a-zA-Z0-9_-]/g, '');
    var startToken = '<!--GUSNB_SELECTION_START_' + key + '-->';
    var endToken = '<!--GUSNB_SELECTION_END_' + key + '-->';
    var startMarker = document.createComment('GUSNB_SELECTION_START_' + key);
    var endMarker = document.createComment('GUSNB_SELECTION_END_' + key);
    var endRange = range.cloneRange();
    endRange.collapse(false);
    endRange.insertNode(endMarker);
    var startRange = range.cloneRange();
    startRange.collapse(true);
    startRange.insertNode(startMarker);

    var liveRange = document.createRange();
    liveRange.setStartAfter(startMarker);
    liveRange.setEndBefore(endMarker);
    var marked = serialize();
    var start = marked.indexOf(startToken);
    var markerEnd = marked.indexOf(endToken, start + startToken.length);
    startMarker.remove();
    endMarker.remove();
    selection.removeAllRanges();
    selection.addRange(liveRange);
    if (start < 0 || markerEnd < 0) return null;

    var documentText = marked.slice(0, start) +
      marked.slice(start + startToken.length, markerEnd) +
      marked.slice(markerEnd + endToken.length);
    return {document: documentText, start: start,
            end: markerEnd - startToken.length, text: plainText};
  }

  function sendToParent(message) {
    parent.postMessage(message, config.parentOrigin || '*');
  }

  function reportSelection() {
    sendToParent({channel: config.channel, nonce: config.nonce,
                  kind: 'selection', selection: selectedRange()});
  }

  function send(kind, forceText) {
    sendToParent({channel: config.channel, nonce: config.nonce, kind: kind,
                  text: (changed || forceText) ? serialize() : null});
  }

  function svgTextTarget(node) {
    while (node && node !== document) {
      if (node.namespaceURI === 'http://www.w3.org/2000/svg' &&
          (node.localName === 'text' || node.localName === 'tspan')) return node;
      node = node.parentNode;
    }
    return null;
  }

  function closeSvgEditor(cancel) {
    if (!svgEditor) return;
    var edit = svgEditor;
    svgEditor = null;
    if (cancel) edit.target.textContent = edit.original;
    else edit.target.textContent = edit.input.value;
    edit.input.remove();
    changed = true;
    send('change', true);
  }

  function openSvgEditor(target) {
    closeSvgEditor(false);
    var rect = target.getBoundingClientRect();
    var input = document.createElement('input');
    var style = getComputedStyle(target);
    input.setAttribute(runtimeAttr, 'svg-editor');
    input.setAttribute('aria-label', 'Edit SVG text');
    input.value = target.textContent || '';
    Object.assign(input.style, {
      position: 'fixed', zIndex: '2147483647',
      left: Math.max(4, rect.left) + 'px', top: Math.max(4, rect.top) + 'px',
      width: Math.max(100, rect.width + 36) + 'px',
      height: Math.max(28, rect.height + 10) + 'px',
      padding: '3px 6px', border: '2px solid #830051', borderRadius: '4px',
      background: '#fff', color: style.fill === 'none' ? style.color : style.fill,
      font: style.font, boxShadow: '0 3px 14px rgba(15,23,42,.24)'
    });
    document.body.appendChild(input);
    svgEditor = {input: input, target: target, original: target.textContent || ''};
    input.addEventListener('input', function () {
      target.textContent = input.value;
      changed = true;
      send('change', true);
    });
    input.addEventListener('blur', function () { closeSvgEditor(false); });
    input.addEventListener('keydown', function (event) {
      if (event.key === 'Enter') { event.preventDefault(); closeSvgEditor(false); }
      if (event.key === 'Escape') { event.preventDefault(); closeSvgEditor(true); }
    });
    input.focus();
    input.select();
  }

  document.addEventListener('dblclick', function (event) {
    var target = svgTextTarget(event.target);
    if (!target) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    openSvgEditor(target);
  }, true);

  document.addEventListener('mouseup', function () {
    setTimeout(reportSelection, 0);
  });
  document.addEventListener('keyup', function (event) {
    if (event.key === 'Meta' || event.key === 'Control') return;
    setTimeout(reportSelection, 0);
  });
  document.addEventListener('input', function (event) {
    if (svgEditor && event.target === svgEditor.input) return;
    changed = true;
    send('change', true);
  }, true);
  document.addEventListener('keydown', function (event) {
    if (event.key.toLowerCase() !== 's' || (!event.metaKey && !event.ctrlKey)) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    if (svgEditor) closeSvgEditor(false);
    send('save', false);
  }, true);

  document.addEventListener('submit', function (event) { event.preventDefault(); }, true);
  document.addEventListener('click', function (event) {
    if (event.target.closest && event.target.closest('a')) event.preventDefault();
  }, true);
  window.addEventListener('message', function (event) {
    var data = event.data || {};
    if (event.source !== parent ||
        (config.parentOrigin !== '*' && event.origin !== config.parentOrigin) ||
        data.channel !== config.channel || data.nonce !== config.nonce) return;
    if (data.command === 'save') {
      if (svgEditor) closeSvgEditor(false);
      send('save', false);
    } else if (data.command === 'saved') {
      changed = false;
    }
  });

  function enableEditing() {
    document.designMode = 'on';
    if (document.body) {
      document.body.contentEditable = 'true';
      document.body.spellcheck = true;
      document.body.setAttribute('data-gusnotebook-edit-root', '');
    }
  }
  if (document.readyState === 'loading')
    document.addEventListener('DOMContentLoaded', enableEditing, {once: true});
  else enableEditing();
})();
