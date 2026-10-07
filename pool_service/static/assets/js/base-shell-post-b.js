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

      const initParticipantSearch = (root, list, trigger) => {
        // Task participants and other large lists; small status/role lists stay native.
        const taskParticipants = root.closest("[data-task-form]") && root.closest("[data-responsibles-block]");
        if (!taskParticipants && list.querySelectorAll(".multi-select__option").length <= 20) return null;
        // Reuse the server-authorized options and original submitted checkboxes.
        const entries = Array.from(list.querySelectorAll(".multi-select__option"))
          .map(option => ({option, checkbox: option.querySelector("input[type='checkbox']")}))
          .filter(({option, checkbox}) => checkbox && !option.hidden && !option.classList.contains("d-none"));
        if (!entries.length) return null;
        const normalize = value => String(value || "").normalize("NFKC").toLowerCase()
          .replace(/\u0451/g, "\u0435").match(/[\p{L}\p{N}]+/gu) || [];
        const indexed = entries.map(entry => ({...entry, words: normalize(entry.option.textContent).join(" ")}));
        const input = document.createElement("input");
        input.type = "search";
        input.className = "form-control form-control-sm mb-2";
        input.maxLength = 160;
        input.autocomplete = "off";
        input.placeholder = "\u041f\u043e\u0438\u0441\u043a \u0443\u0447\u0430\u0441\u0442\u043d\u0438\u043a\u043e\u0432";
        if (!taskParticipants) input.placeholder = "\u041f\u043e\u0438\u0441\u043a";
        input.setAttribute("aria-label", input.placeholder);
        input.setAttribute("data-participant-search", "");
        const status = document.createElement("div");
        status.className = "small text-muted mb-2";
        status.setAttribute("role", "status");
        status.setAttribute("aria-live", "polite");
        list.prepend(input, status);
        trigger.setAttribute("aria-expanded", "false");
        const refresh = () => {
          const tokens = normalize(input.value);
          const eligible = Array.from(tokens.join("")).length >= 3;
          let matches = 0;
          indexed.forEach(({option, checkbox, words}) => {
            const match = eligible && tokens.every(token => words.includes(token));
            // Selected (including locked) rows never disappear behind the filter.
            if (match && !checkbox.checked) matches += 1;
            const visible = checkbox.checked || (match && matches <= 20);
            option.hidden = !visible;
            option.classList.toggle("d-none", !visible);
          });
          status.textContent = !eligible
            ? "\u0412\u0432\u0435\u0434\u0438\u0442\u0435 \u043c\u0438\u043d\u0438\u043c\u0443\u043c 3 \u0441\u0438\u043c\u0432\u043e\u043b\u0430. \u0412\u044b\u0431\u0440\u0430\u043d\u043d\u044b\u0435 \u0443\u0447\u0430\u0441\u0442\u043d\u0438\u043a\u0438 \u043e\u0441\u0442\u0430\u044e\u0442\u0441\u044f \u0432\u0438\u0434\u0438\u043c\u044b\u043c\u0438."
            : matches > 20
              ? "\u041f\u0435\u0440\u0432\u044b\u0435 20 \u0441\u043e\u0432\u043f\u0430\u0434\u0435\u043d\u0438\u0439. \u0423\u0442\u043e\u0447\u043d\u0438\u0442\u0435 \u0437\u0430\u043f\u0440\u043e\u0441."
              : matches
                ? "\u041d\u0430\u0439\u0434\u0435\u043d\u043e \u0434\u043b\u044f \u0434\u043e\u0431\u0430\u0432\u043b\u0435\u043d\u0438\u044f: " + matches
                : "\u041d\u043e\u0432\u044b\u0445 \u0441\u043e\u0432\u043f\u0430\u0434\u0435\u043d\u0438\u0439 \u043d\u0435\u0442. \u0412\u044b\u0431\u0440\u0430\u043d\u043d\u044b\u0435 \u0443\u0447\u0430\u0441\u0442\u043d\u0438\u043a\u0438 \u043d\u0435 \u0438\u0437\u043c\u0435\u043d\u0435\u043d\u044b.";
          if (!taskParticipants) status.textContent = !eligible ? "\u0412\u0432\u0435\u0434\u0438\u0442\u0435 \u043c\u0438\u043d\u0438\u043c\u0443\u043c 3 \u0441\u0438\u043c\u0432\u043e\u043b\u0430."
            : matches > 20 ? "\u041f\u0435\u0440\u0432\u044b\u0435 20 \u0441\u043e\u0432\u043f\u0430\u0434\u0435\u043d\u0438\u0439. \u0423\u0442\u043e\u0447\u043d\u0438\u0442\u0435 \u0437\u0430\u043f\u0440\u043e\u0441."
            : "\u041d\u0430\u0439\u0434\u0435\u043d\u043e: " + matches;
          if (root.classList.contains("multi-select--open")) setDropdownPosition(root);
        };
        input.addEventListener("input", refresh);
        input.addEventListener("keydown", event => {
          if (event.key === "Enter") event.preventDefault();
          if (event.key === "ArrowDown") {
            const first = entries.find(({option, checkbox}) => !option.hidden && !checkbox.disabled);
            if (first) {event.preventDefault(); first.checkbox.focus();}
          }
        });
        root.addEventListener("keydown", event => {
          if (event.key !== "Escape" || !root.classList.contains("multi-select--open")) return;
          event.preventDefault();
          event.stopPropagation();
          root.classList.remove("multi-select--open", "multi-select--drop-up");
          trigger.setAttribute("aria-expanded", "false");
          trigger.focus();
        });
        refresh();
        return {refresh, focus: () => input.focus()};
      };

      const initMultiSelect = (root) => {
        if (!root || root.dataset.multiSelectReady === "1") return;
        const trigger = root.querySelector("[data-multi-select-trigger]");
        const list = root.querySelector("[data-multi-select-list]");
        if (!trigger || !list) return;
        root.dataset.multiSelectReady = "1";
        const participantSearch = initParticipantSearch(root, list, trigger);

        trigger.addEventListener("click", (event) => {
          event.preventDefault();
          const willOpen = !root.classList.contains("multi-select--open");
          root.classList.toggle("multi-select--open", willOpen);
          if (participantSearch) trigger.setAttribute("aria-expanded", String(willOpen));
          if (willOpen) {
            setDropdownPosition(root);
            participantSearch?.focus();
          }
        });

        root.addEventListener("change", (event) => {
          if (!event.target.matches("input[type='checkbox']")) return;
          updateLabel(root);
          participantSearch?.refresh();
        });

        document.addEventListener("click", (event) => {
          if (root.contains(event.target)) return;
          root.classList.remove("multi-select--open");
          root.classList.remove("multi-select--drop-up");
          if (participantSearch) trigger.setAttribute("aria-expanded", "false");
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
  

/* Search-first presentation of existing, server-scoped native choices. */
(function () {
  "use strict";
  if (typeof MutationObserver === "undefined") return;
  const LIMIT = 20;
  const instances = new WeakMap();
  let sequence = 0, active = null;
  const tokens = value => String(value || "").normalize("NFKC").toLowerCase()
    .replace(/\u0451/g, "\u0435").match(/[\p{L}\p{N}]+/gu) || [];
  const enough = value => Array.from(tokens(value).join("")).length >= 3;
  const hint = "\u0412\u0432\u0435\u0434\u0438\u0442\u0435 \u043c\u0438\u043d\u0438\u043c\u0443\u043c 3 \u0441\u0438\u043c\u0432\u043e\u043b\u0430.";
  const chooseHint = "\u0412\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u0437\u043d\u0430\u0447\u0435\u043d\u0438\u0435 \u0438\u0437 \u0440\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442\u043e\u0432 \u043f\u043e\u0438\u0441\u043a\u0430.";
  const unavailable = option => option.disabled || option.hidden || option.classList.contains("d-none") ||
    option.style.display === "none" || Boolean(option.closest("optgroup[disabled], optgroup[hidden], optgroup.d-none")) ||
    (option.parentElement.tagName === "OPTGROUP" && option.parentElement.style.display === "none");

  function init(source) {
    if (instances.has(source) || (!source.multiple && source.size > 1) || !source.parentNode ||
        source.hidden || source.classList.contains("d-none") || source.style.display === "none" ||
        source.closest("[data-crm-lookup], [data-native-select], .select2-container") ||
        source.classList.contains("select2-hidden-accessible") || source.classList.contains("tomselected")) return;
    const named = /(?:^|[-_])(?:client|pool)s?(?:_ids?)?$/.test(source.name);
    if (!named && Array.from(source.options).filter(option => option.value && !unavailable(option)).length <= LIMIT) return;

    const root = document.createElement("div");
    root.className = "large-choice-search";
    root.setAttribute("data-large-choice-search", "");
    const chosen = document.createElement("div");
    chosen.className = "d-flex flex-wrap gap-1 mb-1";
    chosen.setAttribute("data-chosen-options", "");
    chosen.hidden = !source.multiple;
    const controls = document.createElement("div");
    controls.className = "input-group";
    const input = document.createElement("input");
    input.type = "search";
    input.className = "form-control" + (source.classList.contains("form-select-sm") ? " form-control-sm" : "");
    input.id = "large-choice-" + (++sequence);
    input.autocomplete = "off";
    input.maxLength = 160;
    input.placeholder = hint;
    const label = source.getAttribute("aria-label") || Array.from(source.labels || [])
      .map(item => item.textContent.trim()).join(" ") || source.name || "\u041f\u043e\u0438\u0441\u043a";
    input.setAttribute("aria-label", label);
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("aria-expanded", "false");
    // Keep associated out-of-form controls associated for validation, without a name.
    if (source.hasAttribute("form")) input.setAttribute("form", source.getAttribute("form"));
    const clear = document.createElement("button");
    clear.type = "button";
    clear.className = "btn btn-outline-secondary";
    clear.textContent = "\u00d7";
    clear.setAttribute("aria-label", "\u041e\u0447\u0438\u0441\u0442\u0438\u0442\u044c: " + label);
    const list = document.createElement("div");
    list.className = "list-group mt-1";
    list.id = input.id + "-results";
    list.setAttribute("role", "listbox");
    if (source.multiple) list.setAttribute("aria-multiselectable", "true");
    list.hidden = true;
    list.style.maxHeight = "16rem";
    list.style.overflowY = "auto";
    const status = document.createElement("div");
    status.id = input.id + "-status";
    status.className = "form-text";
    status.setAttribute("role", "status");
    status.setAttribute("aria-live", "polite");
    input.setAttribute("aria-controls", list.id);
    input.setAttribute("aria-describedby", [source.getAttribute("aria-describedby"), status.id].filter(Boolean).join(" "));
    controls.append(input, clear);
    root.append(chosen, controls, list, status);
    source.after(root);
    source.classList.add("large-choice-source");
    let indexed = [], results = [], current = -1, dirty = false, timer = null;
    const api = {root, source, input, close};
    instances.set(source, api);

    function close() {
      window.clearTimeout(timer);
      list.replaceChildren();
      list.hidden = true;
      input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-activedescendant");
      results = []; current = -1;
      if (active === api) active = null;
    }
    function valid() {
      const hasChoice = source.multiple
        ? Array.from(source.selectedOptions).some(option => Boolean(option.value)) : Boolean(source.value);
      const invalid = !input.disabled && !root.hidden && !hasChoice &&
        (source.required || (!source.multiple && Boolean(input.value.trim())));
      input.setCustomValidity(invalid ? chooseHint : "");
      return !invalid;
    }
    function reindex() {
      indexed = Array.from(source.options).filter(option => option.value && !unavailable(option))
        .map(option => ({option, words: tokens(option.label).join(" ")}));
    }
    function sync() {
      const selected = source.options[source.selectedIndex];
      const blocked = source.matches(":disabled");
      root.hidden = source.hidden || source.classList.contains("d-none") || source.style.display === "none";
      input.disabled = blocked || root.hidden;
      clear.disabled = input.disabled;
      input.setAttribute("aria-required", String(source.required));
      if (source.multiple) {
        chosen.replaceChildren();
        Array.from(source.selectedOptions).forEach(option => {
          const button = document.createElement("button");
          button.type = "button";
          button.className = "btn btn-outline-secondary btn-sm";
          button.textContent = option.label + " \u00d7";
          button.disabled = input.disabled || unavailable(option);
          button.setAttribute("aria-label", "\u0423\u0431\u0440\u0430\u0442\u044c: " + option.label);
          button.addEventListener("click", () => {
            if (source.matches(":disabled") || unavailable(option) || !source.contains(option)) return;
            option.selected = false; dirty = false; sync(); emit();
          });
          chosen.append(button);
        });
        chosen.hidden = !chosen.children.length;
      }
      if (!dirty || (!source.multiple && source.value) || input.disabled) {
        input.value = !source.multiple && source.value && selected ? selected.label : "";
        dirty = false;
        close();
      }
      valid();
      if (list.hidden) status.textContent = source.value ? "" : hint;
    }
    function emit() {
      source.dispatchEvent(new Event("change", {bubbles: true}));
    }
    function choose(option) {
      // Recheck the actual node: dependent form logic may have replaced the list.
      if (source.matches(":disabled") || !source.contains(option) || unavailable(option)) {
        reindex(); close(); return;
      }
      if (source.multiple) option.selected = !option.selected;
      else source.value = option.value;
      dirty = false;
      sync();
      emit();
      input.focus();
    }
    function render() {
      const query = input.value;
      close();
      if (input.disabled || root.hidden || !enough(query)) {status.textContent = hint; return;}
      if (active && active !== api) active.close();
      const terms = tokens(query);
      const matches = indexed.filter(item => !unavailable(item.option) && terms.every(term => item.words.includes(term)));
      results = matches.slice(0, LIMIT);
      results.forEach(({option}, index) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "list-group-item list-group-item-action text-start";
        button.textContent = (source.multiple && option.selected ? "\u2713 " : "") + option.label;
        button.id = input.id + "-option-" + index;
        button.tabIndex = -1;
        button.setAttribute("role", "option");
        button.setAttribute("aria-selected", String(source.multiple && option.selected));
        button.addEventListener("click", () => choose(option));
        list.append(button);
      });
      list.hidden = !results.length;
      input.setAttribute("aria-expanded", String(Boolean(results.length)));
      status.textContent = !matches.length ? "\u041d\u0438\u0447\u0435\u0433\u043e \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d\u043e." : matches.length > LIMIT
        ? "\u041f\u0435\u0440\u0432\u044b\u0435 20 \u0441\u043e\u0432\u043f\u0430\u0434\u0435\u043d\u0438\u0439. \u0423\u0442\u043e\u0447\u043d\u0438\u0442\u0435 \u0437\u0430\u043f\u0440\u043e\u0441."
        : "\u041d\u0430\u0439\u0434\u0435\u043d\u043e: " + matches.length;
      active = api;
    }
    input.addEventListener("input", () => {
      dirty = true;
      // Do not emit change while typing: some filters submit on change.
      if (!source.multiple) source.selectedIndex = -1;
      valid(); close();
      status.textContent = hint;
      if (enough(input.value)) {
        if (active && active !== api) active.close();
        active = api;
        timer = window.setTimeout(render, 250);
      }
    });
    input.addEventListener("focus", () => {
      if (!dirty) sync();
      reindex();
      if (dirty && enough(input.value)) render();
    });
    input.addEventListener("keydown", event => {
      if (event.key === "Escape" && active === api) {
        event.preventDefault(); event.stopPropagation(); close(); return;
      }
      if (event.key === "Enter" && dirty) {
        event.preventDefault();
        if (!list.hidden && results.length) choose(results[current < 0 ? 0 : current].option);
        else {valid(); input.reportValidity();}
      }
      if (!["ArrowDown", "ArrowUp"].includes(event.key) || list.hidden || !results.length) return;
      event.preventDefault();
      current = current < 0 ? (event.key === "ArrowDown" ? 0 : results.length - 1)
        : (current + (event.key === "ArrowDown" ? 1 : -1) + results.length) % results.length;
      Array.from(list.children).forEach((button, index) => {
        button.classList.toggle("active", index === current);
        button.setAttribute("aria-selected", String(source.multiple ? results[index].option.selected : index === current));
      });
      input.setAttribute("aria-activedescendant", list.children[current].id);
      list.children[current].scrollIntoView({block: "nearest"});
    });
    clear.addEventListener("click", () => {
      const empty = Array.from(source.options).find(option => option.value === "");
      if (source.multiple) {
        Array.from(source.options).forEach(option => {if (!unavailable(option)) option.selected = false;});
      } else source.selectedIndex = empty ? empty.index : -1;
      dirty = false;
      sync(); emit(); input.focus();
    });
    source.addEventListener("change", () => {dirty = false; reindex(); sync();});
    source.addEventListener("invalid", event => {
      event.preventDefault(); valid(); input.focus(); input.reportValidity();
    });
    if (source.form) {
      source.form.addEventListener("submit", event => {
        sync();
        if (!valid()) {event.preventDefault(); event.stopImmediatePropagation(); input.focus(); input.reportValidity();}
      }, true);
      source.form.addEventListener("reset", () => window.setTimeout(() => {dirty = false; reindex(); sync();}, 0));
    }
    new MutationObserver(() => {
      reindex(); sync();
      if (active === api) render();
    }).observe(source, {subtree: true, childList: true, characterData: true, attributes: true,
      attributeFilter: ["disabled", "hidden", "required", "label", "value", "selected", "class", "style"]});
    reindex(); sync();
  }

  function start(scope) {
    const root = scope || document;
    if (root.matches && root.matches("select")) init(root);
    if (root.querySelectorAll) root.querySelectorAll("select").forEach(init);
    if (root.matches && root.matches("[data-multi-select]")) window.initMultiSelect?.(root);
    window.initAllMultiSelects?.(root);
  }
  window.initLargeChoiceSearch = start;
  document.addEventListener("click", event => {
    const label = event.target.closest && event.target.closest("label");
    const picker = label && label.control && instances.get(label.control);
    if (picker) {event.preventDefault(); picker.input.focus();}
    if (active && !active.root.contains(event.target)) active.close();
  });
  document.addEventListener("focusin", event => {
    if (active && !active.root.contains(event.target)) active.close();
  });
  const boot = () => {
    const style = document.createElement("style");
    style.textContent = ".large-choice-source{display:none!important}.large-choice-search [hidden]{display:none!important}.large-choice-search{min-width:0}";
    document.head.append(style);
    start();
    new MutationObserver(records => {
      records.forEach(record => {
        if (record.type === "attributes" && record.target.tagName === "SELECT") init(record.target);
        record.addedNodes.forEach(node => {if (node.nodeType === 1) start(node);});
      });
      if (active && !active.source.isConnected) active.close();
    }).observe(document.body, {childList: true, subtree: true, attributes: true,
      attributeFilter: ["class", "hidden", "style"]});
  };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
