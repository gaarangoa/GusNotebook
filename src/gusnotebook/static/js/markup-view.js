/* View-only navigation. Scale the isolated iframe, never the authored HTML. */
const MARKUP_ZOOM_STEPS = [.1, .25, .33, .5, .67, .75, .9, 1, 1.1, 1.25, 1.5, 1.75, 2, 2.5, 3];
let markupPanDrag = null;
let markupFitSerial = 0;

function syncMarkupViewControls() {
  const t = activeTab();
  const markup = isMarkupTab(t);
  document.getElementById('html-view-controls').hidden = !markup;
  const zoom = markup ? t.markupZoom || 1 : 1;
  document.getElementById('html-preview').style.setProperty('--markup-zoom', zoom);
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
  t.markupFitRequest = null;
  t.markupZoom = Math.max(MARKUP_ZOOM_STEPS[0], Math.min(MARKUP_ZOOM_STEPS.at(-1), value));
  syncMarkupViewControls();
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
  // Measure at 100% so a previously zoomed-out viewport doesn't inflate the
  // document width. The runtime answers after its resized viewport has laid out.
  setMarkupZoom(1);
  t.markupFitRequest = ++markupFitSerial;
  markupCommand('measure-view', {request: t.markupFitRequest});
}

function acceptMarkupViewSize(t, data) {
  if (!t.markupFitRequest || data.request !== t.markupFitRequest ||
      !Number.isFinite(data.width) || data.width <= 0) return;
  const width = document.getElementById('html-preview').clientWidth;
  setMarkupZoom(Math.min(1, Math.max(1, width - 16) / data.width));
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
  const zoom = activeTab()?.markupZoom || 1;
  return {x: (event.clientX - rect.left) / zoom, y: (event.clientY - rect.top) / zoom};
}

markupPanSurface.addEventListener('pointerdown', event => {
  if (event.button !== 0) return;
  event.preventDefault();
  markupPanSurface.focus({preventScroll: true});
  markupPanDrag = {id: event.pointerId, x: event.clientX, y: event.clientY, dx: 0, dy: 0, path: active};
  markupPanSurface.setPointerCapture(event.pointerId);
  markupPanSurface.classList.add('dragging');
  markupCommand('pan-view', {...markupPanPoint(event), start: true, dx: 0, dy: 0});
});
markupPanSurface.addEventListener('pointermove', event => {
  if (!markupPanDrag || event.pointerId !== markupPanDrag.id) return;
  const zoom = activeTab()?.markupZoom || 1;
  // Round the total displacement, avoiding cumulative subpixel scroll drift.
  const dx = Math.round((markupPanDrag.x - event.clientX) / zoom);
  const dy = Math.round((markupPanDrag.y - event.clientY) / zoom);
  markupCommand('pan-view', {dx: dx - markupPanDrag.dx, dy: dy - markupPanDrag.dy});
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
  markupCommand('pan-view', {...markupPanPoint(event), start: true,
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
  markupCommand('pan-view', {start: true, x: markupPanSurface.clientWidth / zoom / 2,
    y: markupPanSurface.clientHeight / zoom / 2, dx: move[0] / zoom, dy: move[1] / zoom});
});
