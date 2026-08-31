(() => {
  const sidebar = document.getElementById('app-sidebar');
  const openButton = document.querySelector('[data-sidebar-open]');
  const closeButton = document.querySelector('[data-sidebar-close]');
  const backdrop = document.querySelector('[data-sidebar-backdrop]');
  if (!sidebar || !openButton || !backdrop) return;

  let previousFocus = null;
  const focusableSelector = 'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])';
  const isMobile = () => window.matchMedia('(max-width: 899px)').matches;

  function focusables() {
    return [...sidebar.querySelectorAll(focusableSelector)].filter((element) => !element.hidden && element.offsetParent !== null);
  }

  function openDrawer() {
    if (!isMobile()) return;
    previousFocus = document.activeElement;
    sidebar.classList.add('is-open');
    backdrop.hidden = false;
    document.body.classList.add('app-drawer-open');
    openButton.setAttribute('aria-expanded', 'true');
    requestAnimationFrame(() => (focusables()[0] || sidebar).focus());
  }

  function closeDrawer({ restoreFocus = true } = {}) {
    sidebar.classList.remove('is-open');
    backdrop.hidden = true;
    document.body.classList.remove('app-drawer-open');
    openButton.setAttribute('aria-expanded', 'false');
    if (restoreFocus && previousFocus instanceof HTMLElement) previousFocus.focus();
  }

  openButton.addEventListener('click', openDrawer);
  closeButton?.addEventListener('click', () => closeDrawer());
  backdrop.addEventListener('click', () => closeDrawer());
  sidebar.querySelectorAll('.app-nav-link').forEach((link) => link.addEventListener('click', () => {
    if (isMobile()) closeDrawer({ restoreFocus: false });
  }));

  document.addEventListener('keydown', (event) => {
    if (!sidebar.classList.contains('is-open')) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      closeDrawer();
      return;
    }
    if (event.key !== 'Tab') return;
    const items = focusables();
    if (!items.length) return;
    const first = items[0];
    const last = items[items.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  });

  window.addEventListener('resize', () => {
    if (!isMobile() && sidebar.classList.contains('is-open')) closeDrawer({ restoreFocus: false });
  });
})();

// Attach the session CSRF token to same-origin mutating fetch requests so the
// legacy inline page scripts (manuscripts, config) stay CSRF-protected without
// each having to read the token themselves.
(() => {
  const meta = document.querySelector('meta[name="csrf-token"]');
  const token = meta ? meta.content : '';
  if (!token || !window.fetch) return;
  const origFetch = window.fetch;
  window.fetch = function (input, init) {
    init = init || {};
    const method = (init.method || (typeof input !== 'string' && input && input.method) || 'GET').toUpperCase();
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    const sameOrigin = url.startsWith('/') || url.startsWith(location.origin);
    if (sameOrigin && !['GET', 'HEAD', 'OPTIONS'].includes(method)) {
      const headers = new Headers(init.headers || (typeof input !== 'string' && input ? input.headers : undefined) || {});
      if (!headers.has('X-CSRF-Token')) headers.set('X-CSRF-Token', token);
      init = Object.assign({}, init, { headers });
    }
    return origFetch.call(this, input, init);
  };
})();
