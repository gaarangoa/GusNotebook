/* Loaded in <head> so the chosen palette is applied before the first paint. */
window.AppAppearance = (() => {
  const key = 'gusnotebook.appearance';
  const defaults = {theme: 'system', density: 'comfortable', fontSize: 14};
  const media = matchMedia('(prefers-color-scheme: dark)');
  function clean(value = {}) {
    return {theme: ['light', 'dark', 'system'].includes(value.theme) ? value.theme : defaults.theme,
      density: ['comfortable', 'compact'].includes(value.density) ? value.density : defaults.density,
      fontSize: Number.isFinite(Number(value.fontSize))
        ? Math.max(11, Math.min(22, Number(value.fontSize))) : defaults.fontSize};
  }
  function read() {
    try { return clean(JSON.parse(localStorage.getItem(key)) || defaults); }
    catch (_) { return {...defaults}; }
  }
  let preferences = read();
  const isDark = () => preferences.theme === 'dark' || (preferences.theme === 'system' && media.matches);
  function apply() {
    const root = document.documentElement;
    root.dataset.theme = isDark() ? 'dark' : 'light';
    root.dataset.density = preferences.density;
    root.style.setProperty('--editor-size', preferences.fontSize + 'px');
    root.style.colorScheme = root.dataset.theme;
    document.dispatchEvent(new CustomEvent('appearance-change', {detail: {...preferences}}));
    const toggle = document.getElementById('theme-toggle');
    if (toggle) {
      toggle.setAttribute('aria-label', `Theme: ${preferences.theme}. Switch to ${isDark() ? 'light' : 'dark'} mode`);
      toggle.title = toggle.getAttribute('aria-label');
      toggle.setAttribute('aria-pressed', String(isDark()));
    }
  }
  function update(next, persist = true) {
    preferences = clean({...preferences, ...next});
    if (persist) { try { localStorage.setItem(key, JSON.stringify(preferences)); } catch (_) {} }
    apply();
  }
  function colors() {
    const style = getComputedStyle(document.documentElement);
    return Object.fromEntries(['text', 'muted', 'panel', 'surface', 'border', 'accent', 'selection',
      'red', 'green', 'yellow', 'blue', 'magenta', 'cyan'].map(name => [name, style.getPropertyValue('--' + name).trim()]));
  }
  function terminalTheme() {
    const c = colors();
    return {background: c.panel, foreground: c.text, cursor: c.accent, cursorAccent: c.panel,
      selectionBackground: c.selection, selectionForeground: c.text,
      black: c.text, white: c.muted, brightBlack: c.muted, brightWhite: c.text,
      ...Object.fromEntries(['red', 'green', 'yellow', 'blue', 'magenta', 'cyan']
        .flatMap(name => [[name, c[name]], ['bright' + name[0].toUpperCase() + name.slice(1), c[name]]]))};
  }
  media.addEventListener('change', () => { if (preferences.theme === 'system') apply(); });
  window.addEventListener('storage', event => {
    if (event.key === key || event.key === null) { preferences = read(); apply(); }
  });
  document.addEventListener('DOMContentLoaded', apply);
  apply();
  return {get: () => ({...preferences}), update, isDark, colors, terminalTheme,
    toggle: () => update({theme: isDark() ? 'light' : 'dark'})};
})();
