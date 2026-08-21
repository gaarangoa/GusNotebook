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
  var viewFrame = null;
  var pendingView = null;
  var userMovedAfterRestore = false;

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

  function elementPath(element) {
    var parts = [];
    while (element && element.nodeType === 1) {
      var name = (element.localName || element.tagName || '').toLowerCase();
      if (!name) break;
      var index = 1;
      var sibling = element.previousElementSibling;
      while (sibling) {
        if ((sibling.localName || '').toLowerCase() === name) index += 1;
        sibling = sibling.previousElementSibling;
      }
      parts.unshift(name + ':nth-of-type(' + index + ')');
      if (element === document.documentElement) break;
      element = element.parentElement;
    }
    return parts.join(' > ');
  }

  function currentView() {
    var x = window.scrollX || window.pageXOffset || 0;
    var y = window.scrollY || window.pageYOffset || 0;
    var pointX = Math.max(0, Math.min(window.innerWidth - 1,
                                     Math.round(window.innerWidth / 2)));
    var pointY = Math.max(0, Math.min(window.innerHeight - 1, 20));
    var element = document.elementFromPoint(pointX, pointY);
    if (element && element.hasAttribute && element.hasAttribute(runtimeAttr)) {
      element = element.parentElement;
    }
    var rect = element && element.getBoundingClientRect
      ? element.getBoundingClientRect() : null;
    return {x: x, y: y, anchor: element ? {
      id: element.id || null,
      path: elementPath(element),
      top: rect ? rect.top : 0,
      left: rect ? rect.left : 0,
    } : null};
  }

  function reportView() {
    viewFrame = null;
    sendToParent({channel: config.channel, nonce: config.nonce,
                  kind: 'view-state', view: currentView()});
  }

  function queueViewReport() {
    if (viewFrame !== null) return;
    viewFrame = requestAnimationFrame(reportView);
  }

  function findViewAnchor(anchor) {
    if (!anchor) return null;
    if (anchor.id) {
      var byId = document.getElementById(anchor.id);
      if (byId) return byId;
    }
    if (!anchor.path) return null;
    try { return document.querySelector(anchor.path); }
    catch (_error) { return null; }
  }

  function applyView(view) {
    if (!view || userMovedAfterRestore) return;
    window.scrollTo(view.x || 0, view.y || 0);
    var anchor = findViewAnchor(view.anchor);
    if (anchor) {
      var rect = anchor.getBoundingClientRect();
      window.scrollBy(rect.left - (view.anchor.left || 0),
                      rect.top - (view.anchor.top || 0));
    }
    queueViewReport();
  }

  function restoreView(view) {
    pendingView = view;
    userMovedAfterRestore = false;
    requestAnimationFrame(function () {
      requestAnimationFrame(function () { applyView(pendingView); });
    });
    // Images and late styles can change layout after DOMContentLoaded. Reapply
    // once at full load unless the user has already started navigating.
    if (document.readyState === 'complete') {
      setTimeout(function () { applyView(pendingView); }, 0);
    } else {
      window.addEventListener('load', function () { applyView(pendingView); },
                              {once: true});
    }
  }

  function reportSelection() {
    sendToParent({channel: config.channel, nonce: config.nonce,
                  kind: 'selection', selection: selectedRange()});
  }

  function send(kind, forceText) {
    sendToParent({channel: config.channel, nonce: config.nonce, kind: kind,
                  text: (changed || forceText) ? serialize() : null});
  }

  function wireVizSource(root) {
    (root || document).querySelectorAll('[data-gusnb-viz-info]').forEach(function (button) {
      if (button.getAttribute('onclick')) return;
      if (button._gusnbVizInfoWired) return;
      button._gusnbVizInfoWired = true;
      button.addEventListener('click', function (event) {
        event.preventDefault();
        event.stopPropagation();
        var block = button.closest('[data-gusnb-viz]');
        var panel = block && block.querySelector('[data-gusnb-viz-panel]');
        if (panel) panel.hidden = !panel.hidden;
      });
    });
  }

  function scrubVizFragment(fragment) {
    fragment.querySelectorAll('script').forEach(function (script) {
      if (script.matches('[type="application/json"][data-gusnb-viz-source]')) return;
      script.remove();
    });
    fragment.querySelectorAll('*').forEach(function (el) {
      Array.from(el.attributes).forEach(function (attr) {
        var name = attr.name.toLowerCase();
        if (name.startsWith('on') &&
            !(name === 'onclick' && el.matches('[data-gusnb-viz-info]'))) {
          el.removeAttribute(attr.name);
        }
      });
    });
    return fragment;
  }

  function insertFragmentAtSelection(fragment) {
    var selection = document.getSelection();
    if (!selection || !selection.rangeCount) return false;
    var range = selection.getRangeAt(0);
    if (!document.body || !document.body.contains(range.commonAncestorContainer)) return false;
    range.deleteContents();
    var marker = document.createTextNode('');
    fragment.appendChild(marker);
    range.insertNode(fragment);
    range = document.createRange();
    range.setStartAfter(marker);
    range.collapse(true);
    selection.removeAllRanges();
    selection.addRange(range);
    marker.remove();
    return true;
  }

  function handleVizPaste(event) {
    var data = event.clipboardData;
    if (!data) return;
    var html = data.getData('text/html') || '';
    if (!html || html.indexOf('data-gusnb-viz="1"') === -1) return;
    var template = document.createElement('template');
    template.innerHTML = html;
    var fragment = scrubVizFragment(template.content);
    if (!fragment.querySelector('[data-gusnb-viz="1"]')) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    if (!insertFragmentAtSelection(fragment)) return;
    wireVizSource(document);
    changed = true;
    send('change', true);
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
    queueViewReport();
  }, true);
  document.addEventListener('paste', handleVizPaste, true);
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
    } else if (data.command === 'restore-view') {
      restoreView(data.view);
    }
  });

  window.addEventListener('scroll', queueViewReport, {passive: true});
  window.addEventListener('resize', queueViewReport, {passive: true});
  ['wheel', 'touchstart', 'pointerdown', 'keydown'].forEach(function (kind) {
    window.addEventListener(kind, function () { userMovedAfterRestore = true; },
                            {passive: true});
  });

  function enableEditing() {
    document.designMode = 'on';
    if (document.body) {
      document.body.contentEditable = 'true';
      document.body.spellcheck = true;
      document.body.setAttribute('data-gusnotebook-edit-root', '');
    }
    wireVizSource(document);
    sendToParent({channel: config.channel, nonce: config.nonce, kind: 'ready'});
  }
  if (document.readyState === 'loading')
    document.addEventListener('DOMContentLoaded', enableEditing, {once: true});
  else enableEditing();
})();
