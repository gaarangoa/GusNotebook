/* View-only navigation. Scale the isolated iframe, never the authored HTML. */
const MARKUP_ZOOM_STEPS = [.1, .25, .33, .5, .67, .75, .9, 1, 1.1, 1.25, 1.5, 1.75, 2, 2.5, 3];
let markupPanDrag = null;
let markupFitSerial = 0;

function rememberMarkupCanvas(t = activeTab()) {
  const viewport = document.getElementById('html-view-viewport');
  if (isMarkupTab(t) && viewport.dataset.path === t.path) {
    t.markupCanvasScroll = {x: viewport.scrollLeft, y: viewport.scrollTop};
  }
}

function syncMarkupViewControls() {
  const t = activeTab();
  const markup = isMarkupTab(t);
  document.getElementById('html-view-controls').hidden = !markup;
  const zoom = markup ? t.markupZoom || 1 : 1;
  const preview = document.getElementById('html-preview');
  const viewport = document.getElementById('html-view-viewport');
  const stage = document.getElementById('html-view-stage');
  const frame = document.getElementById('html-preview-frame');
  const switched = viewport.dataset.path !== (markup ? t.path : '');
  if (switched) rememberMarkupCanvas(tabs.find(item => item.path === viewport.dataset.path));
  viewport.dataset.path = markup ? t.path : '';
  // Keep the iframe's layout viewport independent of zoom. Resizing it by
  // 1/zoom makes vw/vh units, percentage widths, and responsive charts cancel
  // the visual scaling. Only the outer scrollable canvas changes with zoom.
  const width = (markup && t.markupCanvasSize?.width) || Math.max(1, preview.clientWidth);
  const height = (markup && t.markupCanvasSize?.height) || Math.max(1, preview.clientHeight);
  preview.style.setProperty('--markup-zoom', zoom);
  frame.style.width = width + 'px'; frame.style.height = height + 'px';
  stage.style.width = width * zoom + 'px'; stage.style.height = height * zoom + 'px';
  if (switched) {
    viewport.scrollLeft = t?.markupCanvasScroll?.x || 0;
    viewport.scrollTop = t?.markupCanvasScroll?.y || 0;
  }
  document.getElementById('html-zoom-reset').textContent = Math.round(zoom * 100) + '%';
  document.getElementById('html-zoom-out').disabled = zoom <= MARKUP_ZOOM_STEPS[0];
  document.getElementById('html-zoom-in').disabled = zoom >= MARKUP_ZOOM_STEPS.at(-1);
  const pan = markup && !!t.markupPan;
  document.getElementById('html-pan-toggle').setAttribute('aria-pressed', String(pan));
  document.getElementById('html-pan-surface').hidden = !pan;
  if (!pan || markupPanDrag?.path !== t?.path) endMarkupPan();
}

function setMarkupZoom(value) {
  const t = activeTab();
  if (!isMarkupTab(t) || !Number.isFinite(value)) return;
  const viewport = document.getElementById('html-view-viewport');
  const previous = t.markupZoom || 1;
  const center = {x: (viewport.scrollLeft + viewport.clientWidth / 2) / previous,
    y: (viewport.scrollTop + viewport.clientHeight / 2) / previous};
  t.markupFitRequest = null;
  t.markupZoom = Math.max(MARKUP_ZOOM_STEPS[0], Math.min(MARKUP_ZOOM_STEPS.at(-1), value));
  syncMarkupViewControls();
  viewport.scrollLeft = center.x * t.markupZoom - viewport.clientWidth / 2;
  viewport.scrollTop = center.y * t.markupZoom - viewport.clientHeight / 2;
  rememberMarkupCanvas(t);
}

function resetMarkupZoom() {
  const t = activeTab();
  if (!isMarkupTab(t)) return;
  t.markupCanvasSize = null;
  setMarkupZoom(1);
}

function stepMarkupZoom(direction) {
  const zoom = activeTab()?.markupZoom || 1;
  const steps = direction > 0 ? MARKUP_ZOOM_STEPS : [...MARKUP_ZOOM_STEPS].reverse();
  const next = steps.find(value => direction > 0 ? value > zoom + .001 : value < zoom - .001);
  if (next !== undefined) setMarkupZoom(next);
}

function fitMarkupWidth() {
  const t = activeTab();
  if (!isMarkupTab(t)) return;
  // Fit can explicitly expand the canvas for a fixed-size document. Regular
  // zooming then scales that canvas without reflowing the page again.
  resetMarkupZoom();
  t.markupFitRequest = ++markupFitSerial;
  markupCommand('measure-view', {request: t.markupFitRequest});
}

function acceptMarkupViewSize(t, data) {
  if (!t.markupFitRequest || data.request !== t.markupFitRequest ||
      !Number.isFinite(data.width) || data.width <= 0 ||
      !Number.isFinite(data.height) || data.height <= 0) return;
  const preview = document.getElementById('html-preview');
  const width = preview.clientWidth;
  t.markupCanvasSize = {width: Math.max(width, data.width), height: Math.max(preview.clientHeight, data.height)};
  setMarkupZoom(Math.min(1, Math.max(1, width - 16) / data.width));
  const viewport = document.getElementById('html-view-viewport');
  viewport.scrollLeft = 0;
  rememberMarkupCanvas(t);
  markupCommand('pan-view', {dx: -data.width, dy: 0, root: true});
}

function toggleMarkupPan(enabled) {
  const t = activeTab();
  if (!isMarkupTab(t)) return;
  t.markupPan = enabled === undefined ? !t.markupPan : enabled;
  syncMarkupViewControls();
  if (t.markupPan) document.getElementById('html-pan-surface').focus({preventScroll: true});
}

function endMarkupPan() {
  const surface = document.getElementById('html-pan-surface');
  if (markupPanDrag && surface.hasPointerCapture(markupPanDrag.id)) {
    surface.releasePointerCapture(markupPanDrag.id);
  }
  markupPanDrag = null;
  surface.classList.remove('dragging');
}

const markupPanSurface = document.getElementById('html-pan-surface');
function markupPanPoint(event) {
  const rect = markupPanSurface.getBoundingClientRect();
  const viewport = document.getElementById('html-view-viewport');
  const zoom = activeTab()?.markupZoom || 1;
  return {x: (event.clientX - rect.left + viewport.scrollLeft) / zoom,
    y: (event.clientY - rect.top + viewport.scrollTop) / zoom};
}

function panMarkupView(data) {
  const viewport = document.getElementById('html-view-viewport');
  const zoom = activeTab()?.markupZoom || 1;
  const x = viewport.scrollLeft, y = viewport.scrollTop;
  viewport.scrollLeft += data.dx * zoom;
  viewport.scrollTop += data.dy * zoom;
  // Move the magnified canvas first, then any remaining distance inside the
  // document (including its nested scrollers).
  markupCommand('pan-view', {...data, dx: data.dx - (viewport.scrollLeft - x) / zoom,
    dy: data.dy - (viewport.scrollTop - y) / zoom});
  rememberMarkupCanvas();
}

markupPanSurface.addEventListener('pointerdown', event => {
  if (event.button !== 0) return;
  event.preventDefault();
  markupPanSurface.focus({preventScroll: true});
  markupPanDrag = {id: event.pointerId, x: event.clientX, y: event.clientY, dx: 0, dy: 0, path: active};
  markupPanSurface.setPointerCapture(event.pointerId);
  markupPanSurface.classList.add('dragging');
  panMarkupView({...markupPanPoint(event), start: true, dx: 0, dy: 0});
});
markupPanSurface.addEventListener('pointermove', event => {
  if (!markupPanDrag || event.pointerId !== markupPanDrag.id) return;
  const zoom = activeTab()?.markupZoom || 1;
  // Round the total displacement, avoiding cumulative subpixel scroll drift.
  const dx = Math.round((markupPanDrag.x - event.clientX) / zoom);
  const dy = Math.round((markupPanDrag.y - event.clientY) / zoom);
  panMarkupView({dx: dx - markupPanDrag.dx, dy: dy - markupPanDrag.dy});
  markupPanDrag.dx = dx;
  markupPanDrag.dy = dy;
});
['pointerup', 'pointercancel', 'lostpointercapture'].forEach(kind => {
  markupPanSurface.addEventListener(kind, endMarkupPan);
});
markupPanSurface.addEventListener('wheel', event => {
  // Leave browser zoom shortcuts available.
  if (event.ctrlKey || event.metaKey) return;
  event.preventDefault();
  const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? markupPanSurface.clientHeight : 1;
  const zoom = activeTab()?.markupZoom || 1;
  panMarkupView({...markupPanPoint(event), start: true,
    dx: (event.shiftKey && !event.deltaX ? event.deltaY : event.deltaX) * unit / zoom,
    dy: (event.shiftKey && !event.deltaX ? 0 : event.deltaY) * unit / zoom});
}, {passive: false});
markupPanSurface.addEventListener('keydown', event => {
  if (event.key === 'Escape') {
    event.preventDefault();
    toggleMarkupPan(false);
    document.getElementById('html-pan-toggle').focus();
    return;
  }
  const moves = {ArrowLeft: [-80, 0], ArrowRight: [80, 0], ArrowUp: [0, -80], ArrowDown: [0, 80]};
  const move = moves[event.key];
  if (!move || event.ctrlKey || event.metaKey || event.altKey) return;
  event.preventDefault();
  const zoom = activeTab()?.markupZoom || 1;
  const rect = markupPanSurface.getBoundingClientRect();
  panMarkupView({start: true, ...markupPanPoint({clientX: rect.left + rect.width / 2,
    clientY: rect.top + rect.height / 2}), dx: move[0] / zoom, dy: move[1] / zoom});
});

document.getElementById('html-view-viewport').addEventListener('scroll', () => rememberMarkupCanvas(), {passive:true});
new ResizeObserver(() => {
  if (isMarkupTab(activeTab())) syncMarkupViewControls();
}).observe(document.getElementById('html-preview'));
