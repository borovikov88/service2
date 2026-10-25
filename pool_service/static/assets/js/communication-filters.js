(() => {
  "use strict";
  const form = document.querySelector("[data-call-filters]");
  if (!form) return;
  let timer, controller, version = 0;
  const load = async (url, restore = false) => {
    controller?.abort();
    controller = new AbortController();
    const ticket = ++version;
    const current = document.querySelector("[data-call-results]");
    current.setAttribute("aria-busy", "true");
    try {
      const response = await fetch(url, {signal: controller.signal, credentials: "same-origin", headers: {"X-Requested-With": "XMLHttpRequest"}});
      if (!response.ok) throw new Error("filter_failed");
      const page = new DOMParser().parseFromString(await response.text(), "text/html");
      const next = page.querySelector("[data-call-results]");
      if (ticket !== version) return;
      if (!next) throw new Error("filter_results_missing");
      current.replaceWith(next);
      if (restore) {
        const saved = page.querySelector("[data-call-filters]");
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
      if (ticket === version) document.querySelector("[data-call-results]")?.removeAttribute("aria-busy");
    }
  };
  const schedule = () => {
    window.clearTimeout(timer);
    controller?.abort();
    ++version;
    document.querySelector("[data-call-results]")?.removeAttribute("aria-busy");
    timer = window.setTimeout(() => {
      // The client picker emits change only after choosing or clearing a client.
      if (!form.checkValidity()) return;
      const url = new URL(form.action || window.location.href);
      url.search = new URLSearchParams(Array.from(new FormData(form)).filter(([, value]) => value !== "")).toString();
      load(url);
    }, 300);
  };
  form.addEventListener("input", event => {
    if (!event.target.matches("[data-lookup-input]")) schedule();
  });
  form.addEventListener("change", event => {
    if (!event.target.matches("[data-lookup-input]")) schedule();
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
