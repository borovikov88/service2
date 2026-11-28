(() => {
  "use strict";
  const workspace = document.querySelector(".mail-workspace");
  if (!workspace) return;
  const editors = new WeakMap();
  let assets;
  const plugins = new Set(["Essentials", "Paragraph", "Bold", "Italic", "Underline", "Strikethrough",
    "Alignment", "Font", "Link", "List", "RemoveFormat", "PasteFromOffice"]);

  function loadScript(url) {
    return new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = url;
      script.onload = resolve;
      script.onerror = reject;
      document.head.append(script);
    });
  }

  function loadAssets() {
    if (!assets) {
      const style = document.createElement("link");
      style.rel = "stylesheet";
      style.href = workspace.dataset.editorStyle;
      document.head.append(style);
      assets = (window.ClassicEditor ? Promise.resolve() : loadScript(workspace.dataset.editorScript))
        .then(() => loadScript(workspace.dataset.editorLocale)).catch(error => {
          style.remove();
          assets = null;
          throw error;
        });
    }
    return assets;
  }

  function plainMarkup(text) {
    const p = document.createElement("p");
    const lines = text.split("\n");
    lines.forEach((line, index) => {
      if (index) p.append(document.createElement("br"));
      p.append(line);
    });
    return text ? p.outerHTML : "";
  }

  function canonical(editor, html) {
    return html ? editor.data.stringify(editor.data.parse(html)) : "";
  }

  function positionPanels(scope) {
    const wrapper = document.querySelector(".ck-body-wrapper");
    const parent = scope.closest("dialog[open]") || document.body;
    if (wrapper && wrapper.parentElement !== parent) parent.append(wrapper);
  }

  async function mount(scope) {
    const host = scope.querySelector("[data-mail-editor]");
    if (!host || host.dataset.loading) return;
    host.dataset.loading = "true";
    const form = host.closest("form");
    const plain = form.elements.body || form.elements.text;
    const field = form.querySelector("[data-editor-html]");
    const initial = plain.value;
    try {
      await loadAssets();
      if (!host.isConnected) return;
      const Editor = window.ClassicEditor;
      const editor = await Editor.create(host, {
        licenseKey: "GPL", language: "ru",
        plugins: Editor.builtinPlugins.filter(plugin => plugins.has(plugin.pluginName)),
        toolbar: {items: ["fontFamily", "fontSize", "bold", "italic", "underline", "strikethrough", "fontColor",
          "fontBackgroundColor", "alignment", "bulletedList", "numberedList", "link", "removeFormat", "undo", "redo"],
          shouldNotGroupWhenFull: true},
        fontFamily: {options: ["default", "Arial, Helvetica, sans-serif", "Georgia, serif",
          "Times New Roman, Times, serif", "Courier New, Courier, monospace", "Verdana, Geneva, sans-serif"],
          supportAllValues: true},
        fontSize: {options: [10, 12, 14, 15, 16, 18, 20, 24, 28, 32], supportAllValues: true},
        initialData: field.value,
      });
      if (!host.isConnected) { await editor.destroy(); return; }
      if (plain.value !== initial) editor.setData(plainMarkup(plain.value));
      const root = editor.editing.view.getDomRoot();
      root.setAttribute("aria-label", host.getAttribute("aria-label"));
      editors.set(form, editor);
      const hadFocus = plain === document.activeElement;
      plain.required = false;
      plain.hidden = true;
      field.disabled = false;
      if (hadFocus) editor.editing.view.focus();
      if (!form.closest("dialog") || form.closest("dialog").open) positionPanels(form);
    } catch {
      const status = form.querySelector("[data-mail-feedback]");
      if (status && form.isConnected) status.textContent = "Редактор не загрузился. Доступен обычный текст.";
    }
  }

  function updateSignature(form, text, html) {
    const field = form.querySelector("[data-editor-html]");
    const previous = form.querySelector("[data-previous-html]");
    const editor = editors.get(form);
    const normalize = value => editor ? canonical(editor, value) : value;
    const old = normalize(previous.value);
    const next = normalize(html || plainMarkup(text));
    const current = editor ? editor.getData() : field.value;
    if (!old || current.endsWith(old)) {
      const prefix = old ? current.slice(0, -old.length) : current;
      const updated = prefix + (!old && next && prefix ? "<p><br></p>" : "") + next;
      field.value = updated;
      if (editor) editor.setData(updated);
    }
    previous.value = html || plainMarkup(text);
  }

  function unmount(scope) {
    const form = scope.querySelector("form");
    const editor = form && editors.get(form);
    if (editor) {
      editors.delete(form);
      void editor.destroy();
    }
  }

  workspace.addEventListener("focusin", event => {
    if (event.target.closest(".ck-editor")) positionPanels(event.target);
  });
  workspace.addEventListener("submit", event => {
    const form = event.target;
    const editor = editors.get(form);
    if (!editor) return;
    const html = editor.getData();
    form.querySelector("[data-editor-html]").value = html;
    const plain = form.elements.body || form.elements.text;
    plain.value = html ? editor.editing.view.getDomRoot().innerText.replace(/[\u200b\ufeff]/g, "").trim() : "";
  }, true);
  window.crmMailEditor = {mount, unmount, updateSignature, positionPanels};
  mount(workspace);

  const frame = workspace.querySelector(".mail-html-reader");
  if (frame) {
    function fit() {
      const body = frame.contentDocument?.body;
      if (!body) return;
      const resize = () => {
        const height = Math.ceil(Math.max(body.scrollHeight, body.getBoundingClientRect().height));
        if (height) frame.style.height = Math.min(height + 4, 45000) + "px";
      };
      resize();
      if (window.ResizeObserver) new ResizeObserver(resize).observe(body);
    }
    frame.addEventListener("load", fit);
    if (frame.contentDocument?.readyState === "complete") fit();
  }
})();
