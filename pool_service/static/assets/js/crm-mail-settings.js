(() => {
  const form = document.querySelector("[data-mail-setup]");
  if (!form) return;
  const provider = form.elements.provider;
  const update = () => {
    const custom = provider.value === "custom";
    for (const row of form.querySelectorAll("[data-mail-server]")) {
      row.hidden = !custom;
      const input = row.querySelector("input, select");
      input.disabled = !custom;
      input.required = custom;
    }
  };
  provider.addEventListener("change", update);
  update();
})();
