'use strict';
(() => {
  const key = 'veil-theme';
  const system = window.matchMedia('(prefers-color-scheme: dark)');
  const readPreference = () => {
    try { const value = localStorage.getItem(key); return ['light', 'dark'].includes(value) ? value : null; }
    catch { return null; }
  };
  let preference = readPreference();
  const paths = {
    dark: 'M20.5 13.2A8.5 8.5 0 0 1 10.8 3.5 8.5 8.5 0 1 0 20.5 13.2Z',
    light: 'M12 8a4 4 0 1 0 0 8a4 4 0 0 0 0-8ZM12 2v2m0 16v2M2 12h2m16 0h2M4.9 4.9l1.4 1.4m11.4 11.4 1.4 1.4M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4'
  };
  function apply() {
    const theme = preference || (system.matches ? 'dark' : 'light');
    document.documentElement.dataset.theme = theme;
    const next = theme === 'dark' ? 'light' : 'dark';
    document.querySelectorAll('.theme-toggle').forEach(button => {
      const action = 'Switch to '+next+' mode';
      button.setAttribute('aria-label', action);
      button.title = action;
      button.querySelector('span').textContent = next === 'dark' ? 'Dark mode' : 'Light mode';
      button.querySelector('path').setAttribute('d', paths[next]);
    });
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.content = theme === 'dark' ? '#101b18' : '#f3f6f2';
  }
  // Runs before styles load, so a saved dark preference does not flash white.
  apply();
  document.addEventListener('DOMContentLoaded', () => {
    apply();
    document.querySelectorAll('.theme-toggle').forEach(button => button.addEventListener('click', () => {
      preference = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
      try { localStorage.setItem(key, preference); } catch { /* Use the choice for this page when storage is unavailable. */ }
      apply();
    }));
  }, {once: true});
  system.addEventListener('change', () => { if (!preference) apply(); });
  window.addEventListener('storage', event => { if (event.key === key || event.key === null) { preference = readPreference(); apply(); } });
})();
