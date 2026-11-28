(() => {
  "use strict";
  const filterSelector = "[data-call-filters], [data-mail-filters]";
  const resultSelector = "[data-call-results], [data-mail-results]";
  const form = document.querySelector(filterSelector);
  if (!form) return;
  let timer, controller, version = 0;
  const load = async (url, restore = false) => {
    controller?.abort();
    controller = new AbortController();
    const ticket = ++version;
    const current = document.querySelector(resultSelector);
    current.setAttribute("aria-busy", "true");
    try {
      const response = await fetch(url, {signal: controller.signal, credentials: "same-origin", headers: {"X-Requested-With": "XMLHttpRequest"}});
      if (!response.ok) throw new Error("filter_failed");
      const page = new DOMParser().parseFromString(await response.text(), "text/html");
      const next = page.querySelector(resultSelector);
      if (ticket !== version) return;
      if (!next) throw new Error("filter_results_missing");
      current.replaceWith(next);
      const shortcuts = page.querySelector("[data-mail-shortcuts]");
      if (shortcuts) document.querySelector("[data-mail-shortcuts]")?.replaceWith(shortcuts);
      if (restore) {
        const saved = page.querySelector(filterSelector);
        form.querySelectorAll("input, select").forEach(input => {
          const source = input.name ? saved.elements.namedItem(input.name) : saved.querySelector("[data-lookup-input]");
          if (!source) return;
          if (input.type === "checkbox") input.checked = source.checked;
          else input.value = source.value;
        });
        form.querySelector("[data-lookup-input]")?.setCustomValidity("");
      } else history.replaceState(null, "", url);
      const selectAll = document.querySelector("[data-call-select-all]");
      if (selectAll) selectAll.checked = false;
      document.dispatchEvent(new Event("communications:updated"));
    } catch (error) {
      if (error.name !== "AbortError" && ticket === version) window.location.assign(url);
    } finally {
      if (ticket === version) document.querySelector(resultSelector)?.removeAttribute("aria-busy");
    }
  };
  const schedule = (delay = 300) => {
    window.clearTimeout(timer);
    controller?.abort();
    ++version;
    document.querySelector(resultSelector)?.removeAttribute("aria-busy");
    timer = window.setTimeout(() => {
      // The client picker emits change only after choosing or clearing a client.
      if (!form.checkValidity()) return;
      const url = new URL(form.action || window.location.href);
      url.search = new URLSearchParams(Array.from(new FormData(form)).filter(([, value]) => value !== "")).toString();
      load(url);
    }, delay);
  };
  form.addEventListener("input", event => {
    if (!event.target.matches("[data-lookup-input]")) schedule();
  });
  form.addEventListener("change", event => {
    if (!event.target.matches("[data-lookup-input]")) schedule(event.target.name === "folder" ? 0 : 300);
  });
  form.addEventListener("submit", event => {
    if (event.submitter?.hasAttribute("formaction")) return;
    event.preventDefault();
    schedule();
  });
  window.addEventListener("popstate", () => {
    window.clearTimeout(timer);
    load(new URL(window.location.href), true);
  });
})();
