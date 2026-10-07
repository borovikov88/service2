(function () {
  "use strict";
  const searchable = (value) =>
    Array.from(String(value || "").normalize("NFKC").replace(/[^\p{L}\p{N}]/gu, "")).length >= 3;

  function initPicker(root) {
    if (root.dataset.lookupInitialized === "true") return;
    const input = root.querySelector("[data-lookup-input]");
    const selected = root.querySelector("[data-lookup-value]");
    const results = root.querySelector("[data-lookup-results]");
    const status = root.querySelector("[data-lookup-status]");
    const clearButton = root.querySelector("[data-lookup-clear]");
    if (!input || !selected || !results || !status) return;
    root.dataset.lookupInitialized = "true";
    const required = root.dataset.lookupRequired !== "false";
    let timer = null, controller = null, version = 0, items = [], active = -1;
    const chooseMessage = "Выберите значение из результатов поиска.";
    function validity() {
      const invalid = !selected.value && (required || Boolean(input.value.trim()));
      input.setCustomValidity(invalid ? chooseMessage : "");
      return !invalid;
    }
    function emitChange() {
      if (typeof selected.dispatchEvent === "function" && typeof Event === "function") {
        selected.dispatchEvent(new Event("change", {bubbles: true}));
      }
    }
    function cancel() {
      version += 1;
      window.clearTimeout(timer);
      if (controller) controller.abort();
    }
    function close() {
      results.replaceChildren();
      results.hidden = true;
      input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-activedescendant");
      items = [];
      active = -1;
    }
    function choose(item) {
      cancel();
      selected.value = String(item.id);
      input.value = item.name;
      validity();
      status.textContent = "Выбрано: " + item.name;
      close();
      emitChange();
      input.focus();
    }
    function highlight(index) {
      active = index;
      Array.from(results.children).forEach((button, i) => {
        button.classList.toggle("active", i === active);
        button.setAttribute("aria-selected", i === active ? "true" : "false");
      });
      const button = results.children[active];
      if (button) {
        input.setAttribute("aria-activedescendant", button.id);
        button.scrollIntoView({block: "nearest"});
      }
    }
    function changed() {
      cancel();
      const ticket = version;
      selected.value = "";
      validity();
      close();
      const query = input.value.trim();
      if (!searchable(query)) {
        status.textContent = "Введите минимум 3 символа.";
        return;
      }
      status.textContent = "Поиск…";
      timer = window.setTimeout(async () => {
        controller = new AbortController();
        try {
          const url = new URL(root.dataset.lookupUrl, window.location.href);
          if (url.origin !== new URL(window.location.href).origin) throw new Error("Invalid lookup origin");
          url.searchParams.set("q", query);
          const response = await fetch(url, {
            credentials: "same-origin", cache: "no-store",
            headers: {"Accept": "application/json"},
            signal: controller.signal,
          });
          if (!response.ok) throw new Error("Lookup request failed");
          const data = await response.json();
          if (ticket !== version) return;
          items = (Array.isArray(data.results) ? data.results : []).filter(
            (item) => Number.isInteger(item.id) && item.id > 0 && typeof item.name === "string"
          ).slice(0, 20);
          results.replaceChildren();
          items.forEach((item, index) => {
            const button = document.createElement("button");
            button.type = "button";
            button.className = "list-group-item list-group-item-action text-start";
            button.id = input.id + "-option-" + index;
            button.tabIndex = -1;
            button.setAttribute("role", "option");
            button.setAttribute("aria-selected", "false");
            const title = document.createElement("span");
            title.className = "d-block fw-semibold";
            title.textContent = item.name;
            button.append(title);
            const description = [item.inn ? "ИНН " + item.inn : "", item.phone || ""].filter(Boolean).join(" · ");
            if (description) {
              const detail = document.createElement("span");
              detail.className = "d-block small text-muted";
              detail.textContent = description;
              button.append(detail);
            }
            button.addEventListener("click", () => choose(item));
            results.append(button);
          });
          results.hidden = !items.length;
          input.setAttribute("aria-expanded", items.length ? "true" : "false");
          status.textContent = !items.length ? "Ничего не найдено." :
            data.has_more ? "Первые 20 совпадений. Уточните запрос." : "Найдено: " + items.length;
        } catch (error) {
          if (ticket !== version || error.name === "AbortError") return;
          close();
          status.textContent = "Не удалось выполнить поиск. Измените запрос и повторите.";
        }
      }, 300);
    }
    input.addEventListener("input", changed);
    input.addEventListener("focus", () => {
      if (!selected.value && results.hidden && searchable(input.value)) changed();
    });
    input.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        cancel();
        close();
      } else if (!results.hidden && items.length && ["ArrowDown", "ArrowUp", "Enter"].includes(event.key)) {
        event.preventDefault();
        if (event.key === "Enter") choose(items[active < 0 ? 0 : active]);
        else if (active < 0) highlight(event.key === "ArrowDown" ? 0 : items.length - 1);
        else highlight((active + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length);
      }
    });
    if (clearButton) clearButton.addEventListener("click", () => {
      cancel();
      input.value = "";
      selected.value = "";
      close();
      validity();
      status.textContent = "Введите минимум 3 символа.";
      emitChange();
      input.focus();
    });
    if (input.form) input.form.addEventListener("submit", (event) => {
      if (!validity()) {
        event.preventDefault();
        input.reportValidity();
      }
    });
    document.addEventListener("click", (event) => {
      if (!root.contains(event.target)) {
        cancel();
        close();
      }
    });
    validity();
    status.textContent = selected.value ? "" : "Введите минимум 3 символа.";
  }

  if (typeof module !== "undefined" && module.exports) module.exports = {searchable, initPicker};
  if (typeof document !== "undefined") {
    const start = () => document.querySelectorAll("[data-crm-lookup]").forEach(initPicker);
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
    else start();
  }
})();
