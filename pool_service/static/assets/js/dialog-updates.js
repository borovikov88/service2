(() => {
  "use strict";
  const root = document.querySelector(".dialog-workspace[data-updates-url]");
  if (!root) return;
  const entries = root.querySelector("[data-dialog-entries]");
  const messages = root.querySelector("[data-dialog-messages]");
  const thread = root.querySelector(".dialog-thread");
  const mobile = matchMedia("(max-width: 767.98px)");
  const delay = 5000;
  let version = root.dataset.listVersion;
  let cursor = 0, count = 0, latestIncoming = 0, acknowledged = 0;
  let retry = delay, timer, current;

  function track(node) {
    const id = Number(node.dataset.messageId);
    cursor = Math.max(cursor, id);
    if (node.closest(".bubble.in")) latestIncoming = Math.max(latestIncoming, id);
    count++;
  }
  messages?.querySelectorAll("[data-message-id]").forEach(track);
  let pendingOffset = 0;

  function updateRows(html) {
    const viewport = mobile.matches ? entries.parentElement : entries.closest(".conversation-list");
    const focused = document.activeElement;
    const focusedRow = focused.closest("[data-dialog-id]");
    const rowLink = focusedRow && focused.closest("a");
    const oldTop = viewport.scrollTop;
    const anchor = oldTop > 0 && Array.from(entries.children).find(row =>
      !row.hidden && row.getBoundingClientRect().bottom > viewport.getBoundingClientRect().top);
    const anchorTop = anchor && anchor.getBoundingClientRect().top;
    entries.innerHTML = html; // Escaped, same-origin Django fragments.
    root.dispatchEvent(new Event("dialogrowsupdated"));
    if (anchor?.dataset?.dialogId) {
      const restored = entries.querySelector('[data-dialog-id="' + anchor.dataset.dialogId + '"]');
      if (restored && !restored.hidden) viewport.scrollTop += restored.getBoundingClientRect().top - anchorTop;
      else viewport.scrollTop = oldTop;
    }
    if (rowLink) {
      const restored = entries.querySelector('[data-dialog-id="' + focusedRow.dataset.dialogId + '"]');
      if (restored && !restored.hidden) {
        const links = Array.from(restored.querySelectorAll("a"));
        (links.find(link => link.className === rowLink.className) || links[0])?.focus({preventScroll: true});
      }
    }
  }

  function updateMessages(data) {
    if (data.delivery) {
      for (const [id, html] of Object.entries(data.delivery)) {
        const state = messages.querySelector('[data-message-id="' + id + '"] .delivery-state');
        if (!state || state.outerHTML === html.trim()) continue;
        const badge = document.createElement("template");
        badge.innerHTML = html;
        state.replaceWith(badge.content);
      }
    }
    if (data.reset) {
      delete messages.dataset.lastDay;
      messages.replaceChildren();
      cursor = count = latestIncoming = acknowledged = 0;
    }
    if (!data.messages_html) return;
    const batch = document.createElement("template");
    batch.innerHTML = data.messages_html;
    const firstDay = batch.content.querySelector(".dialog-day");
    if (firstDay && firstDay.querySelector("time").dateTime === messages.dataset.lastDay) firstDay.remove();
    const days = batch.content.querySelectorAll(".dialog-day time");
    if (days.length) messages.dataset.lastDay = days[days.length - 1].dateTime;
    messages.querySelector(".dialog-empty-history")?.remove();
    batch.content.querySelectorAll("[data-message-id]").forEach(track);
    messages.append(batch.content);
    root.dispatchEvent(new Event("dialogmessagesupdated"));
  }
  if (messages) {
    const days = messages.querySelectorAll(".dialog-day time");
    if (days.length) messages.dataset.lastDay = days[days.length - 1].dateTime;
  }

  function schedule(ms) {
    clearTimeout(timer);
    if (!document.hidden) timer = setTimeout(refresh, ms);
  }
  async function refresh() {
    if (current || document.hidden) return;
    const request = new AbortController();
    current = request;
    const url = new URL(root.dataset.updatesUrl, location.href);
    if (root.querySelector(".conversation-list").getClientRects().length) url.searchParams.set("list_version", version);
    else url.searchParams.set("list", "0");
    const visibleChat = messages && thread.getClientRects().length > 0;
    const viewport = visibleChat && root.querySelector(mobile.matches ? ".dialog-scroll" : ".chat-history");
    const seen = viewport && viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight < 48
      && latestIncoming > acknowledged ? latestIncoming : 0;
    if (visibleChat) {
      url.searchParams.set("conversation", root.dataset.selected);
      url.searchParams.set("after", cursor);
      url.searchParams.set("count", count);
      const pending = Array.from(messages.querySelectorAll(".delivery-state.pending,.delivery-state.sending"),
        state => state.closest("[data-message-id]").dataset.messageId);
      if (pending.length) {
        pendingOffset %= pending.length;
        const batch = pending.slice(pendingOffset).concat(pending.slice(0, pendingOffset)).slice(0, 25);
        url.searchParams.set("pending", batch.join(","));
        pendingOffset += batch.length;
      }
    }
    let next = delay;
    try {
      const options = {credentials: "same-origin", cache: "no-store", signal: request.signal};
      if (seen) {
        options.method = "POST";
        options.headers = {"X-CSRFToken": root.dataset.csrfToken};
        options.body = new URLSearchParams({seen});
      }
      const response = await fetch(url, options);
      if (response.status === 403 || response.status === 404 || response.redirected) return;
      if (!response.ok) throw new Error("Dialog refresh unavailable");
      const data = await response.json();
      if (request.signal.aborted || document.hidden) return;
      if (visibleChat) {
        if (data.seen) acknowledged = Math.max(acknowledged, data.seen);
        updateMessages(data);
      }
      if (data.rows_html !== undefined) updateRows(data.rows_html);
      if (data.list_version) version = data.list_version;
      retry = delay;
      if (data.more) next = 0;
    } catch (error) {
      if (request.signal.aborted) return;
      retry = Math.min(retry * 2, 60000);
      next = retry;
    } finally {
      if (current === request) current = null;
    }
    if (!request.signal.aborted) schedule(next);
  }
  function pause() {
    clearTimeout(timer);
    current?.abort();
    current = null;
  }
  document.addEventListener("visibilitychange", () => { if (document.hidden) pause(); else schedule(0); });
  window.addEventListener("pagehide", pause);
  window.addEventListener("pageshow", event => { if (event.persisted) schedule(0); });
  schedule(delay);
})();
