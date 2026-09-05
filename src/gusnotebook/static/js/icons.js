/* A single local stroke icon set. Names are constants; no user SVG is inserted. */
const ICON_PATHS = {
  more: '<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>',
  plus: '<path d="M12 5v14M5 12h14"/>', close: '<path d="m6 6 12 12M18 6 6 18"/>',
  folder: '<path d="M3 6h6l2 2h10v12H3z"/>', file: '<path d="M6 3h8l4 4v14H6zM14 3v5h4"/>',
  notebook: '<rect x="5" y="3" width="15" height="18" rx="2"/><path d="M9 3v18M3 7h4M3 12h4M3 17h4"/>',
  code: '<path d="m8 6-6 6 6 6m8-12 6 6-6 6M14 4l-4 16"/>',
  text: '<path d="M4 5h16M4 10h16M4 15h10M4 20h12"/>',
  image: '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8" cy="8" r="1.5"/><path d="m3 17 6-6 4 4 3-3 5 5"/>',
  terminal: '<path d="m5 6 6 6-6 6M13 18h6"/>',
  agent: '<path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5z"/>',
  environment: '<path d="m12 3 9 5v8l-9 5-9-5V8zM3 8l9 5 9-5M12 13v8"/>',
  settings: '<path d="M4 7h16M4 17h16"/><circle cx="9" cy="7" r="3"/><circle cx="15" cy="17" r="3"/>',
  moon: '<path d="M20 14a8 8 0 0 1-10-10A8.5 8.5 0 1 0 20 14z"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1 1m12 12 1 1M5 19l1-1M18 6l1-1"/>',
  monitor: '<rect x="3" y="4" width="18" height="13" rx="2"/><path d="M12 17v4M8 21h8"/>',
  files: '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M9 3v18"/>',
  panel: '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M15 3v18"/>',
  focus: '<path d="M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5"/>',
  history: '<path d="M3 10a9 9 0 1 1 2 8M3 4v6h6M12 7v5l3 2"/>',
  refresh: '<path d="M20 8a8 8 0 0 0-14-3L3 8m0-5v5h5M4 16a8 8 0 0 0 14 3l3-3m0 5v-5h-5"/>',
  home: '<path d="m3 10 9-7 9 7M5 9v12h14V9M9 21v-8h6v8"/>',
  up: '<path d="m5 11 7-7 7 7M12 4v16"/>', down: '<path d="m5 13 7 7 7-7M12 4v16"/>',
  upload: '<path d="m8 7 4-4 4 4M12 3v12M4 14v7h16v-7"/>',
  eye: '<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
  chevron: '<path d="m9 5 7 7-7 7"/>', chevronDown: '<path d="m5 9 7 7 7-7"/>',
  play: '<path d="m7 3 14 9-14 9z"/>', stop: '<rect x="5" y="5" width="14" height="14" rx="2"/>',
  trash: '<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7"/>',
  copy: '<rect x="8" y="8" width="13" height="13" rx="2"/><path d="M16 8V3H3v13h5"/>',
  edit: '<path d="m4 15 12-12 5 5L9 20l-6 1zM14 5l5 5"/>',
  undo: '<path d="m8 3-5 5 5 5M3 8h10a7 7 0 0 1 0 14"/>',
  redo: '<path d="m16 3 5 5-5 5M21 8h-10a7 7 0 0 0 0 14"/>',
  external: '<path d="M14 3h7v7M21 3 11 13M10 3H3v18h18v-7"/>',
  check: '<path d="m4 12 5 5L20 6"/>',
};
function icon(name, extra = '') {
  return `<svg class="icon ${extra}" viewBox="0 0 24 24" aria-hidden="true" focusable="false">${ICON_PATHS[name] || ICON_PATHS.file}</svg>`;
}
function hydrateIcons(root = document) {
  for (const holder of root.querySelectorAll('[data-icon]')) {
    holder.innerHTML = icon(holder.dataset.icon);
    holder.removeAttribute('data-icon');
  }
}
document.addEventListener('DOMContentLoaded', () => hydrateIcons());
