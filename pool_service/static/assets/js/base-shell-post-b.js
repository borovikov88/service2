/* Shared UI initializers loaded after access-blocked setup. */

    document.addEventListener("DOMContentLoaded", function () {
      const modalEl = document.getElementById("confirmActionModal");
      if (!modalEl) return;
      const modal = new bootstrap.Modal(modalEl);
      const titleEl = modalEl.querySelector("#confirmActionTitle");
      const bodyEl = modalEl.querySelector("#confirmActionBody");
      const submitBtn = modalEl.querySelector(".js-confirm-submit");
      let pendingForm = null;

      const setVariant = (variant) => {
        const base = "btn js-confirm-submit";
        submitBtn.className = `${base} ${variant ? "btn-" + variant : "btn-primary"}`;
      };

      modalEl.addEventListener("hidden.bs.modal", () => {
        pendingForm = null;
      });

      submitBtn.addEventListener("click", () => {
        if (!pendingForm) return;
        window.setButtonLoading?.(submitBtn);
        pendingForm.submit();
      });

      document.querySelectorAll(".js-confirm-action").forEach((btn) => {
        btn.addEventListener("click", (event) => {
          if (btn.hasAttribute("data-requires-access")) return;
          event.preventDefault();
          pendingForm = btn.closest("form");
          if (!pendingForm) return;
          titleEl.textContent = btn.dataset.confirmTitle || "\u041f\u043e\u0434\u0442\u0432\u0435\u0440\u0434\u0438\u0442\u0435 \u0434\u0435\u0439\u0441\u0442\u0432\u0438\u0435";
          bodyEl.textContent = btn.dataset.confirmBody || "\u041f\u043e\u0434\u0442\u0432\u0435\u0440\u0434\u0438\u0442\u0435 \u0434\u0435\u0439\u0441\u0442\u0432\u0438\u0435.";
          submitBtn.textContent = btn.dataset.confirmConfirm || "\u041f\u043e\u0434\u0442\u0432\u0435\u0440\u0434\u0438\u0442\u044c";
          setVariant(btn.dataset.confirmVariant || "");
          modal.show();
        });
      });
    });
  


    (function () {
      const isFormControl = (el) => {
        if (!el) return false;
        const tag = el.tagName;
        if (tag === "TEXTAREA" || tag === "SELECT") return true;
        if (tag !== "INPUT") return false;
        const type = (el.getAttribute("type") || "text").toLowerCase();
        return !["checkbox", "radio", "range", "file", "color"].includes(type);
      };

      const updateKeyboardOffset = () => {
        if (!window.visualViewport) return;
        const vv = window.visualViewport;
        const keyboard = Math.max(0, window.innerHeight - vv.height - vv.offsetTop);
        document.documentElement.style.setProperty("--keyboard-offset", `${keyboard}px`);
      };

      if (window.visualViewport) {
        window.visualViewport.addEventListener("resize", updateKeyboardOffset);
        window.visualViewport.addEventListener("scroll", updateKeyboardOffset);
        updateKeyboardOffset();
      }

      document.addEventListener("focusin", (event) => {
        if (!isFormControl(event.target)) return;
        setTimeout(() => {
          try {
            event.target.scrollIntoView({ block: "center", behavior: "smooth" });
          } catch (err) {
            event.target.scrollIntoView(true);
          }
        }, 250);
      });
    })();
  


    (function () {
      const getOptionLabel = (checkbox) => {
        const label = checkbox.closest(".multi-select__option");
        if (!label) return checkbox.value;
        return label.textContent.replace(/\s+/g, " ").trim();
      };

      const updateLabel = (root) => {
        const valueEl = root.querySelector(".multi-select__value");
        if (!valueEl) return;
        const placeholder = root.dataset.placeholder || "\u0412\u044b\u0431\u0435\u0440\u0438\u0442\u0435";
        const checked = Array.from(root.querySelectorAll("input[type='checkbox']:checked"));
        if (!checked.length) {
          valueEl.textContent = placeholder;
          return;
        }
        const labels = checked.map(getOptionLabel);
        valueEl.textContent = labels.length <= 2 ? labels.join(", ") : `\u0412\u044b\u0431\u0440\u0430\u043d\u043e: ${labels.length}`;
      };

      const setDropdownPosition = (root) => {
        const dropdown = root.querySelector("[data-multi-select-list]");
        if (!dropdown) return;
        const rect = root.getBoundingClientRect();
        const modalBody = root.closest(".modal-body");
        const boundaryRect = modalBody
          ? modalBody.getBoundingClientRect()
          : { top: 0, bottom: window.innerHeight || document.documentElement.clientHeight };
        const spaceBelow = boundaryRect.bottom - rect.bottom - 12;
        const spaceAbove = rect.top - boundaryRect.top - 12;
        const computed = window.getComputedStyle(dropdown);
        const maxHeight = parseInt(computed.maxHeight, 10) || 260;
        const needed = Math.min(dropdown.scrollHeight || maxHeight, maxHeight);
        const useDropUp = spaceBelow < needed && spaceAbove > spaceBelow;
        root.classList.toggle("multi-select--drop-up", useDropUp);
        const available = (useDropUp ? spaceAbove : spaceBelow) - 12;
        if (available > 120) {
          dropdown.style.maxHeight = `${Math.min(maxHeight, available)}px`;
        } else {
          dropdown.style.maxHeight = `${maxHeight}px`;
        }
      };

      const initMultiSelect = (root) => {
        if (!root || root.dataset.multiSelectReady === "1") return;
        const trigger = root.querySelector("[data-multi-select-trigger]");
        const list = root.querySelector("[data-multi-select-list]");
        if (!trigger || !list) return;
        root.dataset.multiSelectReady = "1";

        trigger.addEventListener("click", (event) => {
          event.preventDefault();
          const willOpen = !root.classList.contains("multi-select--open");
          root.classList.toggle("multi-select--open", willOpen);
          if (willOpen) {
            setDropdownPosition(root);
          }
        });

        root.addEventListener("change", (event) => {
          if (!event.target.matches("input[type='checkbox']")) return;
          updateLabel(root);
        });

        document.addEventListener("click", (event) => {
          if (root.contains(event.target)) return;
          root.classList.remove("multi-select--open");
          root.classList.remove("multi-select--drop-up");
        });

        updateLabel(root);
      };

      const initAllMultiSelects = (scope) => {
        const root = scope || document;
        root.querySelectorAll("[data-multi-select]").forEach(initMultiSelect);
      };

      document.addEventListener("DOMContentLoaded", () => initAllMultiSelects());
      window.initMultiSelect = initMultiSelect;
      window.initAllMultiSelects = initAllMultiSelects;
    })();
  


    (function () {
      const isTouchTooltip = () => window.matchMedia("(hover: none), (pointer: coarse)").matches;

      const getStatusTooltip = (button) => {
        if (typeof bootstrap === "undefined") return;
        return bootstrap.Tooltip.getOrCreateInstance(button, { trigger: "manual", placement: "top" });
      };

      const showStatusTooltip = (button, autohide) => {
        const tooltip = getStatusTooltip(button);
        if (!tooltip) return;
        tooltip.show();
        if (autohide) {
          window.setTimeout(() => tooltip.hide(), 1800);
        }
      };

      const hideStatusTooltip = (button) => {
        const tooltip = getStatusTooltip(button);
        if (tooltip) tooltip.hide();
      };

      document.addEventListener("mouseover", (event) => {
        if (isTouchTooltip()) return;
        const button = event.target.closest(".finance-status-icon");
        if (!button || button.contains(event.relatedTarget)) return;
        showStatusTooltip(button, false);
      });

      document.addEventListener("mouseout", (event) => {
        if (isTouchTooltip()) return;
        const button = event.target.closest(".finance-status-icon");
        if (!button || button.contains(event.relatedTarget)) return;
        hideStatusTooltip(button);
      });

      document.addEventListener("focusin", (event) => {
        const button = event.target.closest(".finance-status-icon");
        if (!button || isTouchTooltip()) return;
        showStatusTooltip(button, false);
      });

      document.addEventListener("focusout", (event) => {
        const button = event.target.closest(".finance-status-icon");
        if (!button || isTouchTooltip()) return;
        hideStatusTooltip(button);
      });

      document.addEventListener("click", (event) => {
        const button = event.target.closest(".finance-status-icon");
        if (!button) return;
        event.preventDefault();
        event.stopPropagation();
        if (isTouchTooltip()) {
          showStatusTooltip(button, true);
        }
      });
    })();
  


    (function () {
      const initTaskForms = (scope) => {
        const root = scope || document;
        root.querySelectorAll("[data-task-form]").forEach((form) => {
          if (form.dataset.taskFormReady === "1") return;
          form.dataset.taskFormReady = "1";

          const timeToggle = form.querySelector("[data-task-time-toggle]");
          const timeFields = form.querySelector("[data-task-time-fields]");
          const timeInputs = timeFields ? timeFields.querySelectorAll("input[type='time']") : [];

          const syncTimeFields = (clearValues) => {
            if (!timeFields || !timeToggle) return;
            const show = timeToggle.checked;
            timeFields.classList.toggle("d-none", !show);
            timeInputs.forEach((input) => {
              input.disabled = !show;
              if (!show && clearValues) {
                input.value = "";
              }
            });
          };

          if (timeToggle) {
            syncTimeFields(false);
            timeToggle.addEventListener("change", () => syncTimeFields(true));
          }

          form.querySelectorAll("input[data-locked='1']").forEach((input) => {
            input.checked = true;
            input.addEventListener("click", (event) => {
              event.preventDefault();
            });
          });

          if (window.initAllMultiSelects) {
            window.initAllMultiSelects(form);
          }
        });
      };

      document.addEventListener("DOMContentLoaded", () => initTaskForms());
      window.initTaskForms = initTaskForms;
    })();
  
