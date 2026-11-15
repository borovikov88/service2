(function(win) {
  "use strict";
  const doc=win.document, form=doc.getElementById("call-contact-create"), data=doc.getElementById("call-customer-bootstrap");
  if (!form || !data) return;
  const config=JSON.parse(data.textContent), values={...config.values,phone:config.phone};
  const editor=win.Service2CustomerEditor.mount(doc.getElementById("call-customer-editor"), {onlyKind:config.kind,lockedPhone:config.phone,[config.kind]:values});
  form.addEventListener("submit", event=>{
    try {
      const value=editor.value()[config.kind];
      for(const key of ["name","phone","email","inn"])form.elements.namedItem(key).value=value[key] || "";
    } catch(error) { event.preventDefault(); const warning=doc.getElementById("call-customer-error"); warning.textContent=error.message; warning.hidden=false; }
  });
})(window);
