/* Shared post-init shell helpers. */

    function formatPhoneMask(value) {
      let digits = value.replace(/\D/g, "");
      if (digits.startsWith("8")) digits = digits.slice(1);
      if (digits.startsWith("7")) digits = digits.slice(1);
      digits = digits.slice(0, 10);
      let res = "+7";
      if (digits.length > 0) res += " " + digits.substring(0, Math.min(3, digits.length));
      if (digits.length > 3) res += " " + digits.substring(3, Math.min(6, digits.length));
      if (digits.length > 6) res += " " + digits.substring(6, Math.min(10, digits.length));
      return res.trimEnd();
    }

    function applyPhoneMasks(root) {
      const scope = root || document;
      const selector = ".phone-mask, input[type='tel'], input[name*='phone'], input[name*='Phone']";
      scope.querySelectorAll(selector).forEach((inp) => {
        if (inp.dataset.phoneMask === "1") return;
        inp.dataset.phoneMask = "1";
        inp.classList.add("phone-mask");
        inp.value = formatPhoneMask(inp.value || "+7");
        inp.addEventListener("input", () => {
          inp.value = formatPhoneMask(inp.value);
          inp.setSelectionRange(inp.value.length, inp.value.length);
        });
        inp.addEventListener("focus", () => {
          if (!inp.value.trim()) inp.value = "+7 ";
        });
      });
    }

    document.addEventListener("DOMContentLoaded", function () {
      applyPhoneMasks();
    });
  


    (function () {
      const loadingSelector = ".btn, button[type='submit'], input[type='submit'], input[type='button']";
      const skipSelector = [
        "[data-no-loading]",
        "[data-bs-toggle]",
        "[data-bs-dismiss]",
        "[data-finance-modal]",
        "[data-task-modal-url]",
        "[data-menu-button]",
        ".btn-close",
        ".js-confirm-action",
      ].join(",");

      const isSkippable = (element) => {
        if (!element || element.matches(skipSelector)) return true;
        if (element.hasAttribute("disabled") || element.getAttribute("aria-disabled") === "true") return true;
        if (element.tagName === "A") {
          const href = element.getAttribute("href") || "";
          if (!href || href === "#" || href.startsWith("#")) return true;
          if (element.target && element.target !== "_self") return true;
          if (element.hasAttribute("download")) return true;
        }
        const type = (element.getAttribute("type") || "").toLowerCase();
        if (element.tagName === "BUTTON" && type === "button" && !element.closest("form")) return true;
        return false;
      };

      const setLoading = (element) => {
        if (!element || element.dataset.buttonLoading === "1" || isSkippable(element)) return;
        element.dataset.buttonLoading = "1";
        element.setAttribute("aria-busy", "true");
        element.classList.add("btn-loading");

        if (element.tagName === "INPUT") {
          element.dataset.originalValue = element.value || "";
          element.value = "\u0417\u0430\u0433\u0440\u0443\u0437\u043a\u0430...";
          return;
        }

        const rect = element.getBoundingClientRect();
        if (rect.width) {
          element.style.minWidth = `${Math.ceil(rect.width)}px`;
        }
        const text = (element.textContent || "").trim();
        if (!text || text.length <= 2) {
          element.classList.add("btn-loading--icon-only");
        }
        const spinner = document.createElement("span");
        spinner.className = "spinner-border spinner-border-sm btn-loading__spinner";
        spinner.setAttribute("aria-hidden", "true");
        element.prepend(spinner);
      };

      const resetLoading = (element) => {
        if (!element || element.dataset.buttonLoading !== "1") return;
        element.dataset.buttonLoading = "0";
        element.removeAttribute("aria-busy");
        element.classList.remove("btn-loading", "btn-loading--icon-only");
        element.style.minWidth = "";
        element.querySelector(".btn-loading__spinner")?.remove();
        if (element.tagName === "INPUT" && Object.prototype.hasOwnProperty.call(element.dataset, "originalValue")) {
          element.value = element.dataset.originalValue;
          delete element.dataset.originalValue;
        }
      };

      window.setButtonLoading = setLoading;
      window.resetButtonLoading = resetLoading;

      document.addEventListener("submit", (event) => {
        const form = event.target;
        const submitter = event.submitter || form.querySelector("button[type='submit'], input[type='submit'], .btn[type='submit']");
        if (submitter) setLoading(submitter);
      });

      document.addEventListener("click", (event) => {
        const element = event.target.closest(loadingSelector);
        if (!element || event.defaultPrevented || isSkippable(element)) return;
        const type = (element.getAttribute("type") || "").toLowerCase();
        if (element.closest("form") && (type === "submit" || type === "" || element.tagName === "INPUT")) return;
        setLoading(element);
      });

      window.addEventListener("pageshow", () => {
        document.querySelectorAll("[data-button-loading='1']").forEach(resetLoading);
      });
    })();
  


    document.addEventListener("DOMContentLoaded", function () {
      const interactiveSelector = "a, button, input, select, textarea, label, summary, details, form";
      document.querySelectorAll("tr[data-row-href]").forEach((row) => {
        if (row.dataset.clickableRowReady === "1") return;
        row.dataset.clickableRowReady = "1";
        row.addEventListener("click", (event) => {
          if (event.target.closest(interactiveSelector)) return;
          window.location.assign(row.dataset.rowHref);
        });
      });

      document.querySelectorAll("tbody tr").forEach((row) => {
        if (row.dataset.clickableRowReady === "1" || row.dataset.rowHref) return;
        if (row.querySelector("button, input, select, textarea, form, details")) return;
        const link = row.querySelector("td a[href]");
        if (!link) return;
        row.classList.add("clickable-row");
        row.dataset.clickableRowReady = "1";
        row.addEventListener("click", (event) => {
          if (event.target.closest(interactiveSelector)) return;
          window.location.assign(link.href);
        });
      });
    });
  
