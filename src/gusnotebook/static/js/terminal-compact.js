/* Compact the DOM presentation, never the PTY stream or terminal buffer.
 *
 * xterm 5.5's DOM renderer uses a fixed logical grid for mouse hit testing,
 * selections and IME placement. Project those coordinates along with the rows;
 * just hiding empty DOM rows would put all three in the wrong place.
 * This adapter is deliberately tied to the pinned DOM renderer. Other renderers
 * and alternate-screen applications keep their ordinary terminal geometry. */
export class CompactTerminalRows {
  activate(term) {
    const core = term._core;
    const renderer = core?._renderService?._renderer?.value;
    const mouse = core?._mouseService;
    const rows = term.element?.querySelector('.xterm-rows');
    const selection = term.element?.querySelector('.xterm-selection');
    if (!rows || !selection || !renderer?.renderRows || !renderer?.handleSelectionChanged ||
        !mouse?.getCoords || !mouse?.getMouseReportCoords) return;

    let heights = [], tops = [0], rowHeight = 0;
    const element = term.element;
    element.classList.add('compact-agent-rows');

    const projectY = value => {
      if (!rowHeight || value < 0) return value;
      const row = Math.min(heights.length, Math.floor(value / rowHeight));
      return tops[row] + (heights[row] ? (value - row * rowHeight) * heights[row] / rowHeight : 0);
    };
    const logicalEvent = (event, target) => {
      if (!rowHeight || !heights.length) return event;
      const origin = target.getBoundingClientRect().top +
        (parseFloat(getComputedStyle(target).paddingTop) || 0);
      const y = event.clientY - origin;
      if (y < 0) return event;
      let row = heights.findIndex((height, i) => height && y < tops[i + 1]);
      if (row < 0) row = heights.length - 1;
      const offset = heights[row] ? (y - tops[row]) * rowHeight / heights[row] : 0;
      const within = Math.max(0, Math.min(rowHeight - .001, offset));
      return {clientX: event.clientX, clientY: origin + row * rowHeight + within};
    };

    const projectSelection = () => {
      // Called immediately after xterm creates fresh selection rectangles.
      for (const rect of selection.children) {
        const top = parseFloat(rect.style.top) || 0;
        const height = parseFloat(rect.style.height) || 0;
        rect.style.top = projectY(top) + 'px';
        rect.style.height = Math.max(0, projectY(top + height) - projectY(top)) + 'px';
      }
    };
    const projectRows = () => {
      rowHeight = core._renderService.dimensions.css.cell.height;
      if (!rowHeight) return;
      const buffer = term.buffer.active;
      const cursor = buffer.baseY + buffer.cursorY - buffer.viewportY;
      heights = [];
      tops = [0];
      let previousBlank = true;
      for (const [i, row] of [...rows.children].entries()) {
        // An empty input line still needs a visible caret and room for typing.
        const blank = buffer.type === 'normal' && i !== cursor && !row.textContent.trim();
        row.classList.toggle('compact-blank-row', blank);
        // One small gap per group of empty lines, regardless of its length.
        const height = blank ? (previousBlank ? 0 : 4) : rowHeight;
        row.style.setProperty('--compact-row-height', height + 'px');
        heights.push(height);
        tops.push(tops[i] + heights[i]);
        previousBlank = blank;
      }
      const shift = cursor >= 0 && cursor < heights.length
        ? tops[cursor] - buffer.cursorY * rowHeight : 0;
      element.style.setProperty('--compact-cursor-shift', shift + 'px');
    };

    // Keep originals so disposing an agent tab releases all adapter hooks.
    const originalRender = renderer.renderRows;
    const originalSelection = renderer.handleSelectionChanged;
    const originalCoords = mouse.getCoords;
    const originalReport = mouse.getMouseReportCoords;
    renderer.renderRows = function (...args) {
      originalRender.apply(this, args);
      projectRows();
    };
    renderer.handleSelectionChanged = function (...args) {
      originalSelection.apply(this, args);
      projectSelection();
    };
    mouse.getCoords = function (event, target, ...args) {
      return originalCoords.call(this, logicalEvent(event, target), target, ...args);
    };
    mouse.getMouseReportCoords = function (event, target) {
      return originalReport.call(this, logicalEvent(event, target), target);
    };
    projectRows();
    term.refresh(0, term.rows - 1);

    this.dispose = () => {
      renderer.renderRows = originalRender;
      renderer.handleSelectionChanged = originalSelection;
      mouse.getCoords = originalCoords;
      mouse.getMouseReportCoords = originalReport;
      for (const row of rows.children) {
        row.classList.remove('compact-blank-row');
        row.style.removeProperty('--compact-row-height');
      }
      element.classList.remove('compact-agent-rows');
      element.style.removeProperty('--compact-cursor-shift');
    };
  }

  dispose() {}
}
