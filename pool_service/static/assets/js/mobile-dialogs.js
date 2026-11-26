(() => {
  "use strict";
  const workspace = document.querySelector(".dialog-workspace");
  if (!workspace) return;
  const narrow = window.matchMedia("(max-width: 767.98px)");
  const threadOpen = workspace.dataset.pane === "chat";
  const rows = workspace.querySelector(".dialog-rows");
  const search = workspace.querySelector(".dialog-search input");
  const unread = workspace.querySelector(".dialog-unread");
  const empty = workspace.querySelector(".dialog-no-results");
  const cards = Array.from(rows.querySelectorAll(".conversation-row"));
  const active = rows.querySelector(".conversation-row.active");
  const back = workspace.querySelector(".dialog-back");
  const scrollKey = "dialogs-return:" + document.body.dataset.currentUserId + ":" + workspace.dataset.org;
  const params = new URL(location.href).searchParams;
  const fold = text => text.trim().toLocaleLowerCase("ru").replaceAll("ё", "е");
  const searches = cards.map(card => fold([
    card.querySelector(".conversation-row-name strong").textContent,
    card.querySelector(".dialog-preview").textContent
  ].join(" ")));
  const withFilters = (href, query, onlyUnread) => {
    const url = new URL(href, location.href);
    if (query) url.searchParams.set("q", query); else url.searchParams.delete("q");
    if (onlyUnread) url.searchParams.set("unread", "1"); else url.searchParams.delete("unread");
    return url;
  };
  const savedPosition = () => {
    try { return JSON.parse(sessionStorage.getItem(scrollKey)); } catch { return null; }
  };
  let pending;
  function filterList(changeUrl = false) {
    const query = search.value.trim(), normalized = fold(query);
    const onlyUnread = unread.getAttribute("aria-pressed") === "true";
    let count = 0;
    cards.forEach((card, i) => {
      card.hidden = narrow.matches && ((!searches[i].includes(normalized)) || (onlyUnread && Number(card.dataset.unread) === 0));
      if (!card.hidden) count++;
    });
    empty.hidden = !narrow.matches || count !== 0 || cards.length === 0;
    empty.textContent = query ? "По вашему запросу диалогов нет." : "Непрочитанных диалогов нет.";
    if (changeUrl) {
      history.replaceState(history.state, "", withFilters(location.href, query, onlyUnread));
      rows.scrollTop = 0;
    }
    for (const link of workspace.querySelectorAll(".dialog-folders a,.conversation-row-name,.conversation-select")) {
      link.href = withFilters(link.href, query, onlyUnread);
    }
  }
  search.value = (params.get("q") || "").slice(0, search.maxLength);
  unread.setAttribute("aria-pressed", String(params.get("unread") === "1"));
  workspace.querySelector(".dialog-search-tools").hidden = false;
  search.addEventListener("input", () => {
    clearTimeout(pending);
    pending = setTimeout(() => filterList(true), 250);
  });
  unread.addEventListener("click", () => {
    clearTimeout(pending);
    unread.setAttribute("aria-pressed", String(unread.getAttribute("aria-pressed") !== "true"));
    filterList(true);
  });
  filterList();
  const folder = workspace.querySelector(".dialog-folders [aria-current]").href;
  if (!threadOpen) {
    const saved = savedPosition();
    if (saved && saved.folder === folder) rows.scrollTop = saved.top;
    rows.addEventListener("click", event => {
      const link = event.target.closest(".conversation-row-name,.conversation-select");
      if (!link || !narrow.matches || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      clearTimeout(pending);
      const query = search.value.trim(), onlyUnread = unread.getAttribute("aria-pressed") === "true";
      link.href = withFilters(link.href, query, onlyUnread);
      history.replaceState(history.state, "", withFilters(location.href, query, onlyUnread));
      const position = {top: rows.scrollTop, folder: withFilters(folder, query, onlyUnread).href};
      try { sessionStorage.setItem(scrollKey, JSON.stringify(position)); } catch { /* Storage is optional. */ }
    });
    // A cached inbox predates the read receipt and can show stale unread counts.
    window.addEventListener("pageshow", event => { if (event.persisted) location.reload(); });
  } else if (back) {
    // Prefer the actual history entry for an inbox opened in this tab.
    // A notification, bookmark or form redirect still has a working list URL.
    const from = document.referrer && new URL(document.referrer);
    if (from && from.origin === location.origin && from.pathname === location.pathname
        && !from.searchParams.has("conversation") && history.length > 1) {
      back.addEventListener("click", event => {
        if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
        event.preventDefault();
        history.back();
      });
    }
    // The URL carries list filters through form submissions and direct links.
    back.href = withFilters(back.href, search.value, unread.getAttribute("aria-pressed") === "true");
    for (const form of workspace.querySelectorAll(".dialog-thread form[method=post]")) {
      form.action = withFilters(form.action, search.value, unread.getAttribute("aria-pressed") === "true");
    }
  }

  const menu = workspace.querySelector(".dialog-menu-toggle");
  const actions = workspace.querySelector(".conversation-actions");
  if (menu && actions.querySelector("form")) {
    menu.hidden = false;
    menu.addEventListener("click", () => menu.setAttribute("aria-expanded", String(menu.getAttribute("aria-expanded") !== "true")));
    document.addEventListener("click", event => {
      if (!menu.contains(event.target) && !actions.contains(event.target)) menu.setAttribute("aria-expanded", "false");
    });
    document.addEventListener("keydown", event => {
      if (event.key === "Escape" && menu.getAttribute("aria-expanded") === "true") {
        menu.setAttribute("aria-expanded", "false");
        menu.focus();
      }
    });
  }
  const scroll = workspace.querySelector(".dialog-scroll");
  const composer = workspace.querySelector(".conversation-composer");
  const body = composer?.querySelector("textarea");
  const file = composer?.querySelector('input[type="file"]');
  if (file) file.addEventListener("change", () => {
    composer.querySelector(".dialog-file-names").textContent = Array.from(file.files, f => f.name).join(", ");
  });
  function fitMessage() {
    if (!body) return;
    body.style.height = "";
    if (narrow.matches) body.style.height = Math.min(110, Math.max(44, body.scrollHeight + 2)) + "px";
  }
  body?.addEventListener("input", fitMessage);
  let follow = true;
  function scrollToLatest() {
    if (scroll && threadOpen && narrow.matches && follow) scroll.scrollTop = scroll.scrollHeight;
  }
  if (scroll && threadOpen) {
    scroll.addEventListener("scroll", () => { follow = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 48; }, {passive: true});
    if (window.ResizeObserver) {
      const observer = new ResizeObserver(scrollToLatest);
      for (const item of [scroll, ...scroll.children]) observer.observe(item);
    }
  }
  function adapt() {
    filterList();
    fitMessage();
    scrollToLatest();
    // On desktop the preview is visible; acknowledge it via the normal open URL.
    if (!narrow.matches && !threadOpen && cards.length && (!active || Number(active.dataset.unread) > 0)) {
      location.replace((active || cards[0]).querySelector(".conversation-row-name").href);
    }
  }
  narrow.addEventListener("change", adapt);
  adapt();
})();
