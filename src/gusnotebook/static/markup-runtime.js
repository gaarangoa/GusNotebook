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
  var selectedViz = null;
  var vizToolbar = null;
  var vizMoveMode = false;
  var vizDrag = null;
  var vizResizeHandle = null;
  var vizResize = null;
  var snapshotTimer = null;
  var selectionTimer = null;
  var changeRevision = 0;
  var snapshotRevision = -1;
  var lastChangeSentRevision = -1;
  var lastSnapshot = null;

  // The bridge is injected before the document's own scripts. Remember nodes
  // those scripts create so saving the editable page does not turn a rendered
  // chart into source markup as well as retaining the script that renders it.
  // Authored SVG/HTML and iframe srcdoc are parser-created and remain intact.
  var generatedNodes = new WeakSet();
  var generatedContents = new WeakMap();
  var generatedReplacements = new WeakMap();
  var authoredMutationDepth = 0;

  function isRuntimeNode(node) {
    return !!(node && node.nodeType === 1 && node.hasAttribute(runtimeAttr));
  }

  function markGenerated(node) {
    if (!node || isRuntimeNode(node)) return;
    if (node.nodeType === 11) {
      Array.prototype.forEach.call(node.childNodes, markGenerated);
      return;
    }
    generatedNodes.add(node);
    Array.prototype.forEach.call(node.childNodes || [], markGenerated);
  }

  function authoredMutation(callback) {
    authoredMutationDepth += 1;
    try { return callback(); }
    finally { authoredMutationDepth -= 1; }
  }

  function rememberGeneratedContent(element) {
    if (authoredMutationDepth || !element || !element.isConnected ||
        isRuntimeNode(element) || generatedNodes.has(element) ||
        generatedContents.has(element)) return;
    generatedContents.set(element,
      Array.prototype.map.call(element.childNodes, function (node) {
        return node.cloneNode(true);
      }));
  }

  function patchGeneratedDom() {
    function freshNodes(node) {
      var nodes = node && node.nodeType === 11
        ? Array.prototype.slice.call(node.childNodes) : [node];
      return nodes.filter(function (candidate) {
        return candidate && !candidate.isConnected;
      });
    }

    var appendChild = Node.prototype.appendChild;
    Node.prototype.appendChild = function (child) {
      var fresh = freshNodes(child);
      var result = appendChild.call(this, child);
      if (!authoredMutationDepth && !isRuntimeNode(this)) fresh.forEach(markGenerated);
      return result;
    };

    var insertBefore = Node.prototype.insertBefore;
    Node.prototype.insertBefore = function (child, reference) {
      var fresh = freshNodes(child);
      var result = insertBefore.call(this, child, reference);
      if (!authoredMutationDepth && !isRuntimeNode(this)) fresh.forEach(markGenerated);
      return result;
    };

    var replaceChild = Node.prototype.replaceChild;
    Node.prototype.replaceChild = function (child, previous) {
      var fresh = freshNodes(child);
      var original = !authoredMutationDepth && previous &&
        !generatedNodes.has(previous) && !isRuntimeNode(previous)
        ? previous.cloneNode(true) : null;
      var result = replaceChild.call(this, child, previous);
      if (!authoredMutationDepth && !isRuntimeNode(this)) {
        fresh.forEach(markGenerated);
        if (original && fresh.length) generatedReplacements.set(fresh[0], original);
      }
      return result;
    };

    var replaceWith = Element.prototype.replaceWith;
    if (replaceWith) {
      Element.prototype.replaceWith = function () {
        var nodes = Array.prototype.slice.call(arguments);
        var fresh = [];
        nodes.forEach(function (node) {
          if (node && typeof node === 'object') fresh = fresh.concat(freshNodes(node));
        });
        var original = !authoredMutationDepth && !generatedNodes.has(this) &&
          !isRuntimeNode(this) ? this.cloneNode(true) : null;
        var result = replaceWith.apply(this, nodes);
        if (!authoredMutationDepth) {
          fresh.forEach(markGenerated);
          if (original && fresh.length) generatedReplacements.set(fresh[0], original);
        }
        return result;
      };
    }

    var inner = Object.getOwnPropertyDescriptor(Element.prototype, 'innerHTML');
    if (inner && inner.get && inner.set) {
      Object.defineProperty(Element.prototype, 'innerHTML', {
        configurable: inner.configurable,
        enumerable: inner.enumerable,
        get: inner.get,
        set: function (value) {
          var track = !authoredMutationDepth && !isRuntimeNode(this) &&
            !generatedNodes.has(this);
          if (track) rememberGeneratedContent(this);
          inner.set.call(this, value);
          if (track) Array.prototype.forEach.call(this.childNodes, markGenerated);
        }
      });
    }

    var insertHtml = Element.prototype.insertAdjacentHTML;
    if (insertHtml) {
      Element.prototype.insertAdjacentHTML = function (position, html) {
        var parent = /beforebegin|afterend/i.test(position) ? this.parentNode : this;
        var before = parent ? Array.prototype.slice.call(parent.childNodes) : [];
        var result = insertHtml.call(this, position, html);
        if (!authoredMutationDepth && parent && !isRuntimeNode(parent)) {
          Array.prototype.forEach.call(parent.childNodes, function (node) {
            if (before.indexOf(node) === -1) markGenerated(node);
          });
        }
        return result;
      };
    }

    ['append', 'prepend'].forEach(function (name) {
      var native = Element.prototype[name];
      if (!native) return;
      Element.prototype[name] = function () {
        var nodes = Array.prototype.slice.call(arguments);
        var before = Array.prototype.slice.call(this.childNodes);
        var fresh = [];
        nodes.forEach(function (node) {
          if (node && typeof node === 'object') fresh = fresh.concat(freshNodes(node));
        });
        var result = native.apply(this, nodes);
        if (!authoredMutationDepth && !isRuntimeNode(this)) {
          fresh.forEach(markGenerated);
          Array.prototype.forEach.call(this.childNodes, function (node) {
            if (before.indexOf(node) === -1 && nodes.indexOf(node) === -1) markGenerated(node);
          });
        }
        return result;
      };
    });
  }

  patchGeneratedDom();

  function scrubGenerated(live, clone) {
    var original = generatedContents.get(live);
    if (original) {
      while (clone.firstChild) clone.removeChild(clone.firstChild);
      original.forEach(function (node) { clone.appendChild(node.cloneNode(true)); });
      return;
    }
    var liveChildren = Array.prototype.slice.call(live.childNodes || []);
    var cloneChildren = Array.prototype.slice.call(clone.childNodes || []);
    liveChildren.forEach(function (child, index) {
      var copy = cloneChildren[index];
      if (!copy) return;
      if (generatedNodes.has(child)) {
        var replacement = generatedReplacements.get(child);
        if (replacement) copy.replaceWith(replacement.cloneNode(true));
        else copy.remove();
      }
      else scrubGenerated(child, copy);
    });
  }

  function cleanClone(node) {
    var clone = node.cloneNode(true);
    scrubGenerated(node, clone);
    clone.querySelectorAll('[' + runtimeAttr + ']').forEach(function (el) {
      el.remove();
    });
    clone.querySelectorAll('[data-gusnotebook-edit-root]').forEach(function (el) {
      el.removeAttribute('data-gusnotebook-edit-root');
      el.removeAttribute('contenteditable');
      el.removeAttribute('spellcheck');
    });
    clone.querySelectorAll('.gusnb-viz-selected').forEach(function (el) {
      el.classList.remove('gusnb-viz-selected');
      el.classList.remove('gusnb-viz-moving');
    });
    clone.querySelectorAll('[data-gusnb-applied-height]').forEach(function (el) {
      el.removeAttribute('data-gusnb-applied-height');
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

  function snapshot() {
    if (lastSnapshot === null || snapshotRevision !== changeRevision) {
      lastSnapshot = serialize();
      snapshotRevision = changeRevision;
    }
    return lastSnapshot;
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
    lastSnapshot = documentText;
    snapshotRevision = changeRevision;
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

  function queueSelectionReport() {
    clearTimeout(selectionTimer);
    selectionTimer = setTimeout(function () {
      selectionTimer = null;
      reportSelection();
    }, 120);
  }

  function sendSnapshot(kind) {
    clearTimeout(snapshotTimer);
    snapshotTimer = null;
    if (kind === 'change' && lastChangeSentRevision === changeRevision) return;
    sendToParent({channel: config.channel, nonce: config.nonce, kind: kind,
                  text: snapshot()});
    if (kind === 'change') lastChangeSentRevision = changeRevision;
  }

  function markChanged() {
    changed = true;
    changeRevision += 1;
    sendToParent({channel: config.channel, nonce: config.nonce, kind: 'dirty'});
    clearTimeout(snapshotTimer);
    snapshotTimer = setTimeout(function () { sendSnapshot('change'); }, 450);
  }

  function send(kind) {
    if (kind === 'change') markChanged();
    else if (kind === 'save') sendSnapshot('save');
  }

  function wireVizSource(root) {
    (root || document).querySelectorAll('[data-gusnb-viz-info]').forEach(function (button) {
      if (button.getAttribute('onmouseenter') || button.getAttribute('onclick')) return;
      if (button._gusnbVizInfoWired) return;
      button._gusnbVizInfoWired = true;
      var show = function (event) {
        event.preventDefault();
        event.stopPropagation();
        var block = button.closest('[data-gusnb-viz]');
        var panel = block && block.querySelector('[data-gusnb-viz-panel]');
        if (panel) panel.hidden = false;
      };
      var hide = function (event) {
        event.preventDefault();
        event.stopPropagation();
        var block = button.closest('[data-gusnb-viz]');
        var panel = block && block.querySelector('[data-gusnb-viz-panel]');
        if (panel) panel.hidden = true;
      };
      button.addEventListener('mouseenter', show);
      button.addEventListener('focus', show);
      button.addEventListener('mouseleave', hide);
      button.addEventListener('blur', hide);
    });
  }

  function ensureEditorVizStyle() {
    if (document.querySelector('style[' + runtimeAttr + '="viz-style"]')) return;
    var style = document.createElement('style');
    style.setAttribute(runtimeAttr, 'viz-style');
    style.textContent = [
      '.gusnb-viz{position:relative;margin:0;}',
      '.gusnb-viz.gusnb-viz-selected{outline:1px solid rgba(131,0,81,.45);outline-offset:3px;}',
      '.gusnb-viz.gusnb-viz-moving{cursor:move;}',
      '.gusnb-viz-render{max-width:100%;overflow:visible;}',
      '.gusnb-viz-render svg{max-width:100%;height:auto;}',
      '.gusnb-viz-frame{display:block;width:100%;min-height:140px;border:0;background:transparent;pointer-events:auto;}',
      '.gusnb-viz [data-gusnb-viz-panel]{position:absolute;top:14px;left:0;z-index:7;max-width:min(520px,100%);white-space:pre-wrap;margin:0;padding:9px 10px;border:1px solid #cbd5e1;border-radius:6px;background:rgba(255,255,255,.98);color:#0f172a;font:11.5px/1.45 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;box-shadow:0 10px 30px rgba(15,23,42,.16);}'
    ].join('');
    document.head.appendChild(style);
  }

  function vizIcon(name) {
    var attrs = ' width="15" height="15" viewBox="0 0 24 24" fill="none" ' +
      'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"';
    if (name === 'source') {
      return '<svg' + attrs + ' aria-hidden="true"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>';
    }
    if (name === 'move') {
      return '<svg' + attrs + ' aria-hidden="true"><path d="M12 2v20"/><path d="m15 5-3-3-3 3"/><path d="m15 19-3 3-3-3"/><path d="M2 12h20"/><path d="m5 9-3 3 3 3"/><path d="m19 9 3 3-3 3"/></svg>';
    }
    if (name === 'delete') {
      return '<svg' + attrs + ' aria-hidden="true"><path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="m19 6-1 14H6L5 6"/><path d="M10 11v5"/><path d="M14 11v5"/></svg>';
    }
    return '';
  }

  function ensureVizToolbar() {
    if (vizToolbar) return vizToolbar;
    vizToolbar = document.createElement('div');
    vizToolbar.setAttribute(runtimeAttr, 'viz-toolbar');
    vizToolbar.innerHTML =
      '<button type="button" data-viz-cmd="source" title="Source" aria-label="Source">' + vizIcon('source') + '</button>' +
      '<button type="button" data-viz-cmd="move" title="Move freely" aria-label="Move freely">' + vizIcon('move') + '</button>' +
      '<button type="button" data-viz-cmd="w-">W-</button>' +
      '<button type="button" data-viz-cmd="w+">W+</button>' +
      '<button type="button" data-viz-cmd="h-">H-</button>' +
      '<button type="button" data-viz-cmd="h+">H+</button>' +
      '<button type="button" data-viz-cmd="delete" title="Delete" aria-label="Delete">' + vizIcon('delete') + '</button>';
    Object.assign(vizToolbar.style, {
      position: 'fixed', zIndex: '2147483646', display: 'none',
      alignItems: 'center', gap: '4px', padding: '4px',
      border: '1px solid rgba(15,23,42,.16)', borderRadius: '6px',
      background: 'rgba(255,255,255,.96)',
      boxShadow: '0 8px 24px rgba(15,23,42,.16)'
    });
    Array.prototype.forEach.call(vizToolbar.querySelectorAll('button'), function (button) {
      Object.assign(button.style, {
        border: '1px solid rgba(15,23,42,.14)', borderRadius: '4px',
        background: '#fff', color: '#334155', padding: '3px 6px',
        minWidth: '28px', height: '28px', display: 'inline-flex',
        alignItems: 'center', justifyContent: 'center',
        font: '11px/1.2 system-ui,-apple-system,Segoe UI,sans-serif',
        cursor: 'pointer'
      });
    });
    vizToolbar.addEventListener('click', function (event) {
      var button = event.target.closest && event.target.closest('[data-viz-cmd]');
      if (!button || !selectedViz) return;
      event.preventDefault();
      event.stopPropagation();
      if (button.getAttribute('data-viz-cmd') === 'source') {
        toggleVizSource();
        return;
      }
      if (button.getAttribute('data-viz-cmd') === 'move') {
        toggleVizMoveMode();
        return;
      }
      editSelectedViz(button.getAttribute('data-viz-cmd'));
    });
    document.body.appendChild(vizToolbar);
    return vizToolbar;
  }

  function ensureVizResizeHandle() {
    if (vizResizeHandle) return vizResizeHandle;
    vizResizeHandle = document.createElement('div');
    vizResizeHandle.setAttribute(runtimeAttr, 'viz-resize-handle');
    vizResizeHandle.setAttribute('title', 'Resize');
    Object.assign(vizResizeHandle.style, {
      position: 'fixed',
      zIndex: '2147483647',
      display: 'none',
      width: '13px',
      height: '13px',
      borderRadius: '3px',
      border: '1px solid rgba(131,0,81,.55)',
      background: '#fff',
      boxShadow: '0 1px 5px rgba(15,23,42,.25)',
      cursor: 'nwse-resize'
    });
    vizResizeHandle.addEventListener('pointerdown', startVizResize, true);
    document.body.appendChild(vizResizeHandle);
    return vizResizeHandle;
  }

  function positionVizToolbar() {
    if (!selectedViz || !vizToolbar) return;
    var rect = selectedViz.getBoundingClientRect();
    vizToolbar.style.left = Math.max(6, Math.min(window.innerWidth - 220, rect.left)) + 'px';
    vizToolbar.style.top = Math.max(6, rect.top - 34) + 'px';
    vizToolbar.style.display = 'flex';
    positionVizResizeHandle();
  }

  function positionVizResizeHandle() {
    if (!selectedViz || !vizResizeHandle) return;
    var rect = selectedViz.getBoundingClientRect();
    vizResizeHandle.style.left = Math.round(rect.right - 7) + 'px';
    vizResizeHandle.style.top = Math.round(rect.bottom - 7) + 'px';
    vizResizeHandle.style.display = 'block';
  }

  function selectViz(block) {
    if (selectedViz && selectedViz !== block) {
      selectedViz.classList.remove('gusnb-viz-selected');
      selectedViz.classList.remove('gusnb-viz-moving');
      setVizMoveMode(false);
    }
    showVizSource(false);
    setVizTextEdit(false);
    selectedViz = block;
    if (!selectedViz) {
      setVizMoveMode(false);
      if (vizToolbar) vizToolbar.style.display = 'none';
      if (vizResizeHandle) vizResizeHandle.style.display = 'none';
      return;
    }
    selectedViz.classList.add('gusnb-viz-selected');
    requestVizHeight(selectedViz);
    ensureVizToolbar();
    ensureVizResizeHandle();
    setVizMoveMode(true);
    positionVizToolbar();
  }

  function requestVizHeight(block) {
    var frame = block && block.querySelector('.gusnb-viz-frame');
    if (!frame || !frame.contentWindow) return;
    frame.contentWindow.postMessage({type: 'gusnotebook-viz-report-height'}, '*');
  }

  function applyVizFrameHeight(frame, height) {
    height = Math.max(24, Math.min(4000, Number(height) || 0));
    if (!frame || !height) return;
    if (frame.getAttribute('data-gusnb-applied-height') === String(height)) return;
    frame.setAttribute('data-gusnb-applied-height', String(height));
    var scale = vizScale(frame);
    var render = frame.closest('[data-gusnb-viz-render]');
    frame.style.height = height + 'px';
    frame.setAttribute('height', String(height));
    frame.setAttribute('data-gusnb-base-height', String(height));
    if (render) {
      render.style.height = Math.round(height * scale) + 'px';
      render.style.minHeight = '0';
      render.style.overflow = 'visible';
    }
    var block = frame.closest('[data-gusnb-viz]');
    if (block) {
      block.style.minHeight = '0';
      block.style.overflow = 'visible';
    }
    positionVizToolbar();
  }

  function showVizSource(show) {
    if (!selectedViz) return;
    var panel = selectedViz.querySelector('[data-gusnb-viz-panel]');
    if (panel) panel.hidden = !show;
  }

  function toggleVizSource() {
    if (!selectedViz) return;
    var panel = selectedViz.querySelector('[data-gusnb-viz-panel]');
    if (panel) panel.hidden = !panel.hidden;
  }

  function setVizTextEdit(enabled) {
    if (!selectedViz) return;
    var frame = selectedViz.querySelector('.gusnb-viz-frame');
    selectedViz.classList.toggle('gusnb-viz-text-edit', !!enabled);
    if (frame) {
      frame.style.pointerEvents = enabled ? 'auto' : '';
      if (frame.contentWindow) {
        frame.contentWindow.postMessage({
          type: 'gusnotebook-viz-text-edit',
          enabled: !!enabled
        }, '*');
      }
    }
    if (vizToolbar) {
      var button = vizToolbar.querySelector('[data-viz-cmd="text"]');
      if (button) button.style.background = enabled ? '#fdf2f8' : '#fff';
    }
  }

  function toggleVizTextEdit() {
    if (!selectedViz) return;
    setVizTextEdit(!selectedViz.classList.contains('gusnb-viz-text-edit'));
  }

  function setVizMoveMode(enabled) {
    vizMoveMode = !!enabled;
    if (selectedViz) selectedViz.classList.toggle('gusnb-viz-moving', vizMoveMode);
    if (vizToolbar) {
      var button = vizToolbar.querySelector('[data-viz-cmd="move"]');
      if (button) button.style.background = vizMoveMode ? '#fdf2f8' : '#fff';
    }
  }

  function toggleVizMoveMode() {
    if (!selectedViz) return;
    if (!vizMoveMode) prepareVizFreePlacement(selectedViz);
    setVizMoveMode(!vizMoveMode);
  }

  function prepareVizFreePlacement(block) {
    if (!block) return;
    var rect = block.getBoundingClientRect();
    var width = parseFloat(block.style.width) || rect.width;
    var parent = block.parentElement || document.body;
    if (parent && parent !== document.body && getComputedStyle(parent).position === 'static') {
      parent.style.position = 'relative';
    }
    var parentRect = parent && parent !== document.body
      ? parent.getBoundingClientRect()
      : {left: 0, top: 0};
    var left = rect.left - parentRect.left + (parent ? parent.scrollLeft || 0 : 0);
    var top = rect.top - parentRect.top + (parent ? parent.scrollTop || 0 : 0);
    block.style.position = 'absolute';
    block.style.left = Math.round(left) + 'px';
    block.style.top = Math.round(top) + 'px';
    block.style.width = Math.round(width) + 'px';
    block.style.maxWidth = 'none';
    block.style.margin = '0';
    block.style.zIndex = block.style.zIndex || '1';
    block.setAttribute('data-gusnb-free-placement', '1');
    changed = true;
    send('change');
    positionVizToolbar();
  }

  function startVizDrag(event, block) {
    if (!vizMoveMode || !block || block !== selectedViz || event.button !== 0) return false;
    if (event.target.closest && event.target.closest('[data-gusnb-viz-panel]')) return false;
    startVizDragAt(block, event.clientX, event.clientY);
    event.preventDefault();
    event.stopImmediatePropagation();
    return true;
  }

  function startVizDragAt(block, clientX, clientY) {
    prepareVizFreePlacement(block);
    vizDrag = {
      block: block,
      startX: clientX,
      startY: clientY,
      left: parseFloat(block.style.left) || 0,
      top: parseFloat(block.style.top) || 0
    };
  }

  function moveVizDragTo(clientX, clientY) {
    if (!vizDrag) return;
    var nextLeft = vizDrag.left + clientX - vizDrag.startX;
    var nextTop = vizDrag.top + clientY - vizDrag.startY;
    vizDrag.block.style.left = Math.round(nextLeft) + 'px';
    vizDrag.block.style.top = Math.round(nextTop) + 'px';
    positionVizToolbar();
  }

  function moveVizDrag(event) {
    if (!vizDrag) return;
    moveVizDragTo(event.clientX, event.clientY);
    event.preventDefault();
  }

  function endVizDragAt(clientX, clientY) {
    if (!vizDrag) return;
    moveVizDragTo(clientX, clientY);
    vizDrag = null;
    changed = true;
    send('change');
  }

  function endVizDrag(event) {
    if (!vizDrag) return;
    endVizDragAt(event.clientX, event.clientY);
    event.preventDefault();
  }

  function startVizResize(event) {
    if (!selectedViz || event.button !== 0) return;
    var block = selectedViz;
    var frame = block.querySelector('.gusnb-viz-frame');
    var render = frame && frame.closest('[data-gusnb-viz-render]');
    var rect = block.getBoundingClientRect();
    var baseHeight = frame ? (
      parseFloat(frame.getAttribute('data-gusnb-base-height')) ||
      parseFloat(frame.getAttribute('height')) ||
      parseFloat(frame.style.height) || rect.height || 240
    ) : rect.height || 240;
    prepareVizFreePlacement(block);
    vizResize = {
      block: block,
      frame: frame,
      render: render,
      startX: event.clientX,
      startY: event.clientY,
      width: rect.width,
      height: rect.height,
      baseHeight: baseHeight
    };
    event.preventDefault();
    event.stopImmediatePropagation();
  }

  function moveVizResize(event) {
    if (!vizResize) return;
    var width = Math.max(140, vizResize.width + event.clientX - vizResize.startX);
    var height = Math.max(90, vizResize.height + event.clientY - vizResize.startY);
    vizResize.block.style.width = Math.round(width) + 'px';
    vizResize.block.style.maxWidth = 'none';
    if (vizResize.frame) {
      applyVizScale(vizResize.frame, height / Math.max(1, vizResize.baseHeight));
    } else {
      vizResize.block.style.height = Math.round(height) + 'px';
    }
    positionVizToolbar();
    event.preventDefault();
  }

  function endVizResize(event) {
    if (!vizResize) return;
    moveVizResize(event);
    vizResize = null;
    changed = true;
    send('change');
  }

  function vizFrameForSource(source) {
    var frames = document.querySelectorAll('.gusnb-viz-frame');
    for (var i = 0; i < frames.length; i++) {
      if (frames[i].contentWindow === source) return frames[i];
    }
    return null;
  }

  function iframeVizClientPoint(frame, data) {
    var rect = frame.getBoundingClientRect();
    return {
      x: rect.left + (Number(data.x) || 0),
      y: rect.top + (Number(data.y) || 0)
    };
  }

  function handleIframeVizPointer(event, data) {
    var frame = vizFrameForSource(event.source);
    var block = frame && frame.closest('[data-gusnb-viz]');
    if (!frame || !block) return false;
    selectViz(block);
    var point = iframeVizClientPoint(frame, data);
    if (data.phase === 'down') {
      startVizDragAt(block, point.x, point.y);
      return true;
    }
    if (data.phase === 'move') {
      moveVizDragTo(point.x, point.y);
      return true;
    }
    if (data.phase === 'up' || data.phase === 'cancel') {
      endVizDragAt(point.x, point.y);
      return true;
    }
    return false;
  }

  function vizScale(frame) {
    var scale = parseFloat(frame.getAttribute('data-gusnb-scale')) ||
      parseFloat(String(frame.style.transform || '').replace(/.*scale\(([^)]+)\).*/, '$1')) || 1;
    return Math.max(.35, Math.min(1.8, scale));
  }

  function applyVizScale(frame, scale) {
    scale = Math.max(.35, Math.min(1.8, scale));
    var render = frame.closest('[data-gusnb-viz-render]');
    var baseHeight = parseFloat(frame.getAttribute('data-gusnb-base-height')) ||
      parseFloat(frame.getAttribute('height')) ||
      parseFloat(frame.style.height) || 460;
    frame.setAttribute('data-gusnb-scale', String(Number(scale.toFixed(3))));
    frame.setAttribute('data-gusnb-base-height', String(Math.round(baseHeight)));
    frame.setAttribute('height', String(Math.round(baseHeight)));
    frame.style.height = Math.round(baseHeight) + 'px';
    frame.style.width = '100%';
    frame.style.zoom = '';
    frame.style.transform = `scale(${Number(scale.toFixed(3))})`;
    frame.style.transformOrigin = 'top left';
    if (render) {
      render.style.height = Math.round(baseHeight * scale) + 'px';
      render.style.overflow = 'visible';
    }
  }

  function editSelectedViz(command) {
    var block = selectedViz;
    if (!block) return;
    if (command === 'delete') {
      block.remove();
      setVizMoveMode(false);
      selectViz(null);
      changed = true;
      send('change');
      return;
    }
    var frame = block.querySelector('.gusnb-viz-frame');
    var rect = block.getBoundingClientRect();
    if (command === 'w-' || command === 'w+') {
      var parentWidth = block.parentElement ? block.parentElement.clientWidth : window.innerWidth;
      var width = parseFloat(block.style.width) || rect.width || parentWidth;
      width += command === 'w+' ? 40 : -40;
      width = Math.max(180, Math.min(parentWidth, width));
      block.style.width = Math.round(width) + 'px';
      block.style.maxWidth = '100%';
    } else if (frame && (command === 'h-' || command === 'h+')) {
      var scale = vizScale(frame) + (command === 'h+' ? .08 : -.08);
      applyVizScale(frame, scale);
    }
    changed = true;
    send('change');
    positionVizToolbar();
  }

  function scrubVizFragment(fragment) {
    fragment.querySelectorAll('style[data-gusnb-viz-style]').forEach(function (style) {
      style.remove();
    });
    fragment.querySelectorAll('script').forEach(function (script) {
      if (script.matches('[type="application/json"][data-gusnb-viz-source]')) return;
      script.remove();
    });
    fragment.querySelectorAll('*').forEach(function (el) {
      Array.from(el.attributes).forEach(function (attr) {
        var name = attr.name.toLowerCase();
        if (name.startsWith('on')) {
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
    if (!html || (html.indexOf('data-gusnb-viz="1"') === -1 &&
                  html.indexOf('data-gusnb-provenance="1"') === -1)) return;
    var template = document.createElement('template');
    var fragment;
    authoredMutation(function () {
      template.innerHTML = html;
      fragment = scrubVizFragment(template.content);
    });
    if (!fragment.querySelector('[data-gusnb-viz="1"], [data-gusnb-provenance="1"]')) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    if (!authoredMutation(function () { return insertFragmentAtSelection(fragment); })) return;
    wireVizSource(document);
    changed = true;
    send('change');
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
    send('change');
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
      send('change');
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

  document.addEventListener('click', function (event) {
    if (vizToolbar && vizToolbar.contains(event.target)) return;
    var block = event.target.closest && event.target.closest('[data-gusnb-viz]');
    if (!block) {
      if (selectedViz) {
        selectedViz.classList.remove('gusnb-viz-selected');
        selectViz(null);
      }
      return;
    }
    event.preventDefault();
    event.stopImmediatePropagation();
    selectViz(block);
  }, true);

  document.addEventListener('pointerdown', function (event) {
    var block = event.target.closest && event.target.closest('[data-gusnb-viz]');
    if (startVizDrag(event, block)) return;
  }, true);
  document.addEventListener('pointermove', moveVizDrag, true);
  document.addEventListener('pointerup', endVizDrag, true);
  document.addEventListener('pointercancel', endVizDrag, true);
  document.addEventListener('pointermove', moveVizResize, true);
  document.addEventListener('pointerup', endVizResize, true);
  document.addEventListener('pointercancel', endVizResize, true);

  document.addEventListener('mouseup', function () {
    queueSelectionReport();
  });
  document.addEventListener('keyup', function (event) {
    if (event.key === 'Meta' || event.key === 'Control') return;
    queueSelectionReport();
  });
  document.addEventListener('input', function (event) {
    if (svgEditor && event.target === svgEditor.input) return;
    send('change');
    queueViewReport();
  }, true);
  document.addEventListener('paste', handleVizPaste, true);
  document.addEventListener('keydown', function (event) {
    if (event.key.toLowerCase() !== 's' || (!event.metaKey && !event.ctrlKey)) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    if (svgEditor) closeSvgEditor(false);
    send('save');
  }, true);

  document.addEventListener('submit', function (event) { event.preventDefault(); }, true);
  document.addEventListener('click', function (event) {
    if (event.target.closest && event.target.closest('a')) event.preventDefault();
  }, true);
  window.addEventListener('message', function (event) {
    var data = event.data || {};
    if (data.type === 'gusnotebook-viz-pointer') {
      if (handleIframeVizPointer(event, data)) return;
    }
    if (data.type === 'gusnotebook-html-output-height') {
      var frame = document.querySelector(
        'iframe.gusnb-viz-frame[data-output-frame="' +
        String(data.id || '').replace(/["\\]/g, '\\$&') + '"]');
      applyVizFrameHeight(frame, data.height);
      return;
    }
    if (data.type === 'gusnotebook-viz-srcdoc-updated') {
      var frames = document.querySelectorAll('.gusnb-viz-frame');
      for (var i = 0; i < frames.length; i++) {
        if (frames[i].contentWindow !== event.source) continue;
        frames[i].setAttribute('srcdoc', data.html || '');
        frames[i].srcdoc = data.html || '';
        changed = true;
        send('change');
        break;
      }
      return;
    }
    if (event.source !== parent ||
        (config.parentOrigin !== '*' && event.origin !== config.parentOrigin) ||
        data.channel !== config.channel || data.nonce !== config.nonce) return;
    if (data.command === 'save') {
      if (svgEditor) closeSvgEditor(false);
      send('save');
    } else if (data.command === 'saved') {
      changed = false;
    } else if (data.command === 'report-view') {
      reportView();
    } else if (data.command === 'restore-view') {
      restoreView(data.view);
    }
  });

  window.addEventListener('scroll', queueViewReport, {passive: true});
  window.addEventListener('scroll', positionVizToolbar, {passive: true});
  window.addEventListener('resize', queueViewReport, {passive: true});
  window.addEventListener('resize', positionVizToolbar, {passive: true});
  window.addEventListener('blur', function () {
    reportView();
    if (changed) sendSnapshot('change');
  });
  window.addEventListener('pagehide', function () {
    reportView();
    if (changed) sendSnapshot('change');
  });
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
    ensureEditorVizStyle();
    wireVizSource(document);
    sendToParent({channel: config.channel, nonce: config.nonce, kind: 'ready'});
  }
  if (document.readyState === 'loading')
    document.addEventListener('DOMContentLoaded', enableEditing, {once: true});
  else enableEditing();
})();
