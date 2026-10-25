(() => {
  const form = document.querySelector("[data-crm-attribution]");
  if (!form) return;
  const search = form.querySelector("[data-request-search]");
  const select = form.querySelector('[name="item_id"]');
  const task = form.querySelector('[name="next_task_id"]');
  const status = form.querySelector("[data-request-search-status]");
  let timer, controller;
  select.addEventListener("change", () => { if (task) task.value = ""; });
  search.addEventListener("input", () => {
    clearTimeout(timer);
    if (controller) controller.abort();
    const address = new URL(location.href);
    if (search.value) address.searchParams.set("request_q", search.value);
    else address.searchParams.delete("request_q");
    history.replaceState(null, "", address);
    timer = setTimeout(async () => {
      controller = new AbortController();
      const url = new URL(form.dataset.lookupUrl, location.origin);
      url.searchParams.set("q", search.value);
      try {
        const response = await fetch(url, { credentials: "same-origin", signal: controller.signal,
          headers: { "X-Requested-With": "XMLHttpRequest" } });
        if (!response.ok) throw new Error("lookup failed");
        const result = await response.json();
        const selected = select.selectedOptions[0];
        const selectedId = select.value;
        select.replaceChildren(new Option("Не связано / снять связь", ""));
        if (selectedId && selected) select.add(new Option(selected.textContent, selectedId, true, true));
        for (const row of result.results) {
          if (String(row.id) !== selectedId) select.add(new Option(row.text, String(row.id)));
        }
        status.textContent = result.limited ? "Показаны первые 20 вариантов; уточните название." : "";
      } catch (error) {
        if (error.name !== "AbortError") status.textContent = "Не удалось обновить список. Текущий выбор сохранён.";
      }
    }, 300);
  });
})();
