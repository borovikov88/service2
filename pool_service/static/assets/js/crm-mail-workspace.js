(() => {
  "use strict";

  const workspace = document.querySelector(".mail-workspace");
  if (!workspace) return;

  window.addEventListener("pageshow", event => {
    if (event.persisted && workspace.querySelector(".mail-list")) window.location.reload();
  });

  async function loadPreviews() {
    const container = workspace.querySelector("[data-mail-previews]");
    if (!container) return;
    const batches = new Map();
    for (const slot of container.querySelectorAll("[data-preview-url]")) {
      const url = slot.dataset.previewUrl;
      if (!batches.has(url)) batches.set(url, new Map());
      batches.get(url).set(slot.dataset.letter, slot);
    }
    const csrf = container.querySelector("[name=csrfmiddlewaretoken]").value;
    for (const [url, slots] of batches) {
      if (!container.isConnected) return;
      const data = new URLSearchParams();
      for (const id of slots.keys()) data.append("letter", id);
      try {
        const response = await fetch(url, {method: "POST", body: data, headers: {"X-CSRFToken": csrf}});
        if (!response.ok || !response.headers.get("Content-Type")?.includes("application/json")) continue;
        const {previews} = await response.json();
        for (const [id, snippet] of Object.entries(previews)) {
          if (slots.has(id)) slots.get(id).textContent = snippet;
        }
      } catch {
        // Dates, subjects and navigation remain available while the provider is offline.
      }
    }
  }
  loadPreviews();
  document.addEventListener("communications:updated", loadPreviews);

  if (typeof HTMLDialogElement === "undefined" || !HTMLDialogElement.prototype.showModal) return;
  const compose = workspace.querySelector("[data-compose-dialog]");
  const signature = workspace.querySelector("[data-signature-dialog]");
  const dialogHeaders = {"X-Mail-Dialog": "1"};
  const loading = new WeakMap();

  function display(dialog, markup) {
    const fragment = new DOMParser().parseFromString(markup, "text/html").body;
    dialog.querySelector("[data-dialog-content]").replaceChildren(...Array.from(fragment.childNodes));
    if (!dialog.open) dialog.showModal();
    const first = dialog.querySelector('[name="to"], [name="text"], .mail-account-choices a');
    first?.focus();
  }

  async function openDialog(link) {
    const dialog = link.dataset.mailDialog === "signature" ? signature : compose;
    if (dialog === compose && !dialog.open && dialog.dataset.entry === link.href && dialog.querySelector(".mail-compose")) {
      dialog.showModal();
      return;
    }
    loading.get(dialog)?.abort();
    const controller = new AbortController();
    loading.set(dialog, controller);
    try {
      const response = await fetch(link.href, {headers: dialogHeaders, signal: controller.signal});
      if (!response.ok || response.headers.get("X-Mail-Dialog") !== "1") {
        window.location.assign(link.href);
        return;
      }
      const markup = await response.text();
      if (controller.signal.aborted) return;
      if (!dialog.contains(link)) dialog.dataset.entry = link.href;
      display(dialog, markup);
    } catch (error) {
      if (!controller.signal.aborted) window.location.assign(link.href);
    }
  }

  workspace.addEventListener("click", event => {
    const link = event.target.closest("a[data-mail-dialog]");
    if (link && !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey && event.button === 0) {
      event.preventDefault();
      openDialog(link);
      return;
    }
    const dialog = event.target.closest("dialog");
    if (!dialog || dialog.querySelector("[aria-busy=true]")) return;
    if (event.target.closest("[data-dialog-close]")) dialog.close();
    const maximize = event.target.closest("[data-maximize]");
    if (maximize) {
      const expanded = dialog.classList.toggle("is-wide");
      maximize.setAttribute("aria-pressed", String(expanded));
      maximize.setAttribute("aria-label", expanded ? "Уменьшить окно" : "Развернуть окно");
    }
  });

  for (const dialog of [compose, signature]) {
    dialog.addEventListener("cancel", event => {
      if (dialog.querySelector("[aria-busy=true]")) event.preventDefault();
    });
  }

  function showFiles(form) {
    const picker = form.querySelector('input[type="file"]');
    if (picker) form.querySelector(".mail-uploads").textContent = Array.from(picker.files, f => f.name).join(" · ");
  }
  workspace.addEventListener("change", event => {
    if (event.target.matches('.mail-compose input[type="file"]')) showFiles(event.target.form);
  });

  function applySignature(text) {
    const editor = workspace.querySelector(".mail-compose");
    if (!editor) return;
    const previous = editor.querySelector("[data-previous-signature]");
    const body = editor.elements.body;
    const suffix = "\n\n" + previous.value;
    if (!previous.value) {
      if (text) body.value += "\n\n" + text;
    } else if (body.value.endsWith(suffix)) {
      body.value = body.value.slice(0, -suffix.length) + (text ? "\n\n" + text : "");
    } else if (body.value === previous.value) {
      body.value = text;
    }
    previous.value = text;
  }

  workspace.addEventListener("submit", async event => {
    const form = event.target;
    const dialog = form.closest("dialog");
    if (!dialog) return;
    event.preventDefault();
    const submit = event.submitter || form.querySelector('button[type="submit"], .mail-compose-footer button');
    if (submit.disabled) return;
    submit.disabled = true;
    form.setAttribute("aria-busy", "true");
    const feedback = form.querySelector("[data-mail-feedback]");
    feedback.textContent = dialog === signature ? "Сохранение…" : "Отправка…";
    try {
      const response = await fetch(form.action, {method: "POST", body: new FormData(form), headers: dialogHeaders});
      if (!response.ok) throw new Error();
      if (response.headers.get("Content-Type")?.includes("application/json")) {
        const result = await response.json();
        if (dialog === signature && typeof result.signature === "string") {
          applySignature(result.signature);
          dialog.close();
        } else if (dialog === compose && result.sent) {
          window.location.assign(result.sent);
        } else {
          throw new Error();
        }
      } else if (response.headers.get("X-Mail-Dialog") === "1") {
        const picker = form.querySelector('input[type="file"]');
        display(dialog, await response.text());
        if (picker) {
          const replacement = dialog.querySelector('input[type="file"]');
          replacement.replaceWith(picker);
          showFiles(picker.form);
        }
        dialog.querySelector(".errorlist")?.scrollIntoView({block: "nearest"});
      } else {
        throw new Error();
      }
    } catch {
      feedback.textContent = dialog === signature ? "Не удалось сохранить подпись." :
        "Не удалось подтвердить отправку. Проверьте папку «Отправленные».";
    } finally {
      submit.disabled = false;
      form.removeAttribute("aria-busy");
    }
  });
})();
