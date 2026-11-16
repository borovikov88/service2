(function (win) {
  "use strict";
  const el = (tag, className, text) => { const node = document.createElement(tag); if (className) node.className = className; if (text != null) node.textContent = text; return node; };
  function parseMoney(value) {
    const text = String(value ?? "").trim();
    if (!text) return "";
    if (!/^(?:[0-9]+|[0-9]{1,3}(?:[ \u00a0\u202f][0-9]{3})+)(?:[.,][0-9]{1,2})?$/.test(text)) throw new Error("Введите неотрицательную сумму, например 12 500,50.");
    const [whole, fraction = ""] = text.replace(/[ \u00a0\u202f]/g, "").replace(",", ".").split(".");
    return whole.replace(/^0+(?=\d)/, "") + "." + fraction.padEnd(2, "0");
  }
  function formatMoney(value) {
    const money = parseMoney(value); if (!money) return "";
    const [whole, fraction] = money.split(".");
    return whole.replace(/\B(?=(\d{3})+(?!\d))/g, "\u00a0") + "," + fraction;
  }
  function icon(kind) {
    const node = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    node.setAttribute("viewBox", "0 0 24 24"); node.setAttribute("fill", "none"); node.setAttribute("stroke", "currentColor"); node.setAttribute("stroke-width", "1.7"); node.setAttribute("aria-hidden", "true");
    const path = document.createElementNS(node.namespaceURI, "path");
    path.setAttribute("d", ({comment:"M5 4h14v12H9l-4 4V4Z M8 8h8M8 12h5", message:"M3 5h18v14H3V5Z m0 1 9 7 9-7", call:"M7 3H3c0 10 8 18 18 18v-4l-5-2-2 2c-3-1-6-4-7-7l2-2-2-5Z", task:"M9 3h6v4H9V3Z M6 5H4v16h16V5h-2M8 14l3 3 5-6", change:"M3 12a9 9 0 1 0 3-7M3 3v5h5M12 7v6l4 2"})[kind] || "M12 4v16M4 12h16");
    node.append(path); return node;
  }
  function mount(host, config) {
    host.classList.add("crm-card-activity"); host.setAttribute("aria-label", "История работы");
    const heading = el("div", "crm-activity-heading"); heading.append(el("h3", "", "История работы"));
    const refresh = el("button", "crm-activity-refresh", "Обновить"); refresh.type = "button"; refresh.setAttribute("aria-label", "Обновить историю"); heading.append(refresh); host.append(heading);
    if (config.commentForm) host.append(config.commentForm());
    else host.append(el("p", "crm-activity-note", "Комментарии добавляет ответственный с правом редактирования карточки."));
    const filters = el("div", "crm-activity-filters"); filters.setAttribute("role", "group"); filters.setAttribute("aria-label", "Тип события");
    const status = el("p", "crm-activity-status"); status.setAttribute("role", "status");
    const list = el("div", "crm-activity-list");
    const more = el("button", "crm-live-button crm-activity-more", "Показать ещё события"); more.type = "button"; more.hidden = true;
    const note = el("p", "crm-activity-note");
    host.append(filters, status, list, more, note);
    let filter = "all", cursor = null, sequence = 0, controller = null;
    const seen = new Set();
    const date = value => new Date(value).toLocaleString("ru-RU", {timeZone:config.timezone || "Asia/Barnaul", day:"2-digit", month:"short", year:"numeric", hour:"2-digit", minute:"2-digit"});
    function render(item) {
      const row = el("article", "crm-activity-event is-" + item.kind); row.dataset.activityId = item.id;
      const marker = el("span", "crm-activity-marker"); marker.append(icon(item.kind));
      const card = el("div", "crm-activity-event-card"), meta = el("div", "crm-activity-event-meta");
      meta.append(el("strong", "", item.actor || ({call:"Телефония", message:"Сообщение", task:"Дело"})[item.kind] || "Система"), el("time", "", date(item.at))); meta.lastChild.dateTime = item.at;
      card.append(meta, el("h4", "", item.title));
      if (item.body) card.append(el("p", "crm-activity-body", item.body));
      if (item.status) card.append(el("p", "crm-activity-result", item.status));
      if (item.details?.length) { const changes = el("ul", "crm-activity-changes"); for (const detail of item.details) changes.append(el("li", "", detail)); card.append(changes); }
      if (item.truncated) card.append(el("small", "crm-activity-note", "Показано начало сообщения. Полный текст доступен в источнике."));
      const media = item.media;
      if (media?.recording_url) { const audio = el("audio"); audio.controls = true; audio.preload = "none"; audio.src = media.recording_url; audio.setAttribute("aria-label", "Запись разговора"); card.append(audio); }
      if (media?.summary || media?.transcript) { const text = el("details", "crm-activity-transcript"); text.append(el("summary", "", "Резюме и расшифровка")); if (media.summary) text.append(el("p", "crm-activity-body", media.summary)); if (media.transcript) text.append(el("p", "crm-activity-body", media.transcript)); card.append(text); }
      if (media?.facts?.request && config.suggestNeed) { const suggest = el("button", "crm-live-button crm-live-button-quiet", "Предложить текст потребности"); suggest.type = "button"; suggest.addEventListener("click", () => config.suggestNeed(media.facts.request)); card.append(suggest); }
      if (media?.analysis_url && config.analyze) { const analyze = el("button", "crm-live-button crm-live-button-quiet", media.analysis_status === "ready" ? "Повторить анализ" : "Расшифровать"); analyze.type = "button"; analyze.addEventListener("click", async () => { analyze.disabled = true; try { await config.analyze(media.analysis_url); analyze.textContent = "Запрос передан в очередь"; } catch (error) { analyze.disabled = false; card.append(el("p", "crm-live-form-error", error.message)); } }); card.append(analyze); }
      const footer = el("div", "crm-activity-event-footer");
      if (item.origin) footer.append(el("span", "", item.origin));
      if (item.url) { const target = new URL(item.url, win.location.origin); if (target.origin === win.location.origin && /^https?:$/.test(target.protocol)) { const link = el("a", "", item.kind === "task" ? "Открыть дело" : "Открыть источник"); link.href = target.href; footer.append(link); } }
      card.append(footer); row.append(marker, card); list.append(row);
    }
    async function load(append) {
      const generation = ++sequence; controller?.abort(); controller = new AbortController();
      refresh.disabled = more.disabled = true; status.textContent = "Загружаем историю…";
      if (!append) { cursor = null; seen.clear(); list.replaceChildren(); more.hidden = true; }
      const url = new URL(config.url, win.location.origin); url.searchParams.set("type", filter); if (append && cursor) url.searchParams.set("cursor", cursor);
      try {
        const response = await win.fetch(url.href, {credentials:"same-origin", headers:{Accept:"application/json"}, signal:controller.signal});
        if (!response.ok) throw new Error(response.status === 403 || response.status === 404 ? "История недоступна. Проверьте доступ или откройте карточку заново." : "Не удалось загрузить историю. Нажмите «Обновить».");
        const page = await response.json(); if (generation !== sequence || !host.isConnected) return;
        for (const item of page.items || []) if (!seen.has(item.id)) { seen.add(item.id); render(item); }
        cursor = page.cursor; more.hidden = !cursor; note.textContent = page.scope_note || "";
        status.textContent = seen.size ? "" : "Пока нет доступных событий этого типа.";
      } catch (error) { if (generation === sequence && host.isConnected && error.name !== "AbortError") status.textContent = error.message; }
      finally { if (generation === sequence) refresh.disabled = more.disabled = false; }
    }
    for (const [key, label] of [["all", "Все"], ["comment", "Комментарии"], ["call", "Звонки"], ["message", "Сообщения"], ["task", "Дела"], ["change", "Изменения"]]) {
      const button = el("button", "", label); button.type = "button"; button.setAttribute("aria-pressed", String(key === filter));
      button.addEventListener("click", () => { filter = key; for (const other of filters.children) other.setAttribute("aria-pressed", String(other === button)); load(false); }); filters.append(button);
    }
    refresh.addEventListener("click", () => load(false)); more.addEventListener("click", () => load(true)); load(false);
    return {refresh:() => load(false)};
  }
  win.CrmCardActivity = {mount, parseMoney, formatMoney};
})(window);
