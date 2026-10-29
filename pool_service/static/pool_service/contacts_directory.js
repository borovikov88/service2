/* Server-paged directory. Rendering never authorizes access or writes records. */
(() => {
  'use strict';
  let desired = new URL(location.href), controller = null, generation = 0, timer = null;
  const tabIds = {companies: 'companies-tab', suppliers: 'suppliers-tab', people: 'contacts-tab'};
  const root = () => document.getElementById('contacts-directory');
  function invalidate() {
    generation += 1;
    clearTimeout(timer);
    if (controller) controller.abort();
    return generation;
  }
  function syncUrl(push = false) {
    history[push ? 'pushState' : 'replaceState'](null, '', desired.pathname + desired.search + desired.hash);
  }
  async function load(id, push = false) {
    controller = new AbortController();
    root().setAttribute('aria-busy', 'true');
    document.getElementById('contacts-error').hidden = true;
    const active = document.activeElement;
    const group = active?.dataset.clientSearch;
    const caret = group ? active.selectionStart : null;
    try {
      const response = await fetch(desired.pathname + desired.search, {signal: controller.signal, credentials: 'same-origin', headers: {'X-Requested-With': 'XMLHttpRequest'}});
      if (!response.ok || response.redirected) throw new Error('Directory unavailable');
      const html = await response.text();
      if (id !== generation) return;
      const replacement = new DOMParser().parseFromString(html, 'text/html').getElementById('contacts-directory');
      if (!replacement) throw new Error('Invalid directory response');
      root().replaceWith(replacement);
      syncUrl(push);
      bind();
      if (group) {
        const input = root().querySelector(`[data-client-search="${group}"]`);
        input?.focus({preventScroll: true});
        if (caret !== null) input?.setSelectionRange(caret, caret);
      }
    } catch (error) {
      if (id === generation && error.name !== 'AbortError') document.getElementById('contacts-error').hidden = false;
    } finally {
      if (id === generation) root().setAttribute('aria-busy', 'false');
    }
  }
  function bind() {
    const initial = desired.searchParams.get('clients_tab') || (desired.searchParams.has('people_q') ? 'people' : 'companies');
    const selected = Object.hasOwn(tabIds, initial) ? initial : 'companies';
    bootstrap.Tab.getOrCreateInstance(document.getElementById(tabIds[selected])).show();
    for (const [group, id] of Object.entries(tabIds)) {
      document.getElementById(id).addEventListener('shown.bs.tab', () => {
        desired.searchParams.set('clients_tab', group); syncUrl();
      });
    }
    root().querySelectorAll('[data-client-search]').forEach(input => {
      input.addEventListener('input', () => {
        const id = invalidate(), group = input.dataset.clientSearch;
        desired.searchParams.set(group + '_q', input.value);
        desired.searchParams.set(group + '_page', '1');
        if (group === 'people') desired.searchParams.set('staff_page', '1');
        desired.searchParams.set('clients_tab', group);
        root().setAttribute('aria-busy', 'true');
        timer = setTimeout(() => load(id), 300);
      });
    });
    root().querySelectorAll('[data-contact-page]').forEach(link => {
      link.addEventListener('click', event => {
        if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
        event.preventDefault();
        const group = link.dataset.contactPage, params = new URL(link.href).searchParams;
        desired.searchParams.set(group + '_page', params.get(group + '_page'));
        desired.searchParams.set('clients_tab', group === 'staff' ? 'people' : group);
        load(invalidate(), true);
      });
    });
    document.getElementById('contacts-retry').addEventListener('click', () => load(invalidate()));
    root().querySelectorAll('[data-client-delete-blocked]').forEach(button => {
      button.addEventListener('click', () => bootstrap.Modal.getOrCreateInstance(document.getElementById('clientDeleteBlockedModal')).show());
    });
  }
  window.addEventListener('popstate', () => { desired = new URL(location.href); load(invalidate()); });
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bind); else bind();
})();
