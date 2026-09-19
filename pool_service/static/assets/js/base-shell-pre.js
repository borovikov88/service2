/* Shared shell utilities that must initialize before the main inline app script. */

    (function () {
      const pickerState = new WeakMap();
      let activeInput = null;
      let sheet = null;

      function acceptsImages(input) {
        const accept = (input.getAttribute("accept") || "").toLowerCase();
        return !accept || accept.includes("image") || /\.(jpe?g|png|webp)/i.test(accept);
      }

      function acceptsFiles(input) {
        const accept = (input.getAttribute("accept") || "").toLowerCase();
        return accept.includes("pdf") || accept.split(",").some((item) => item.trim() && !item.includes("image"));
      }

      function imageAccept(input) {
        const imageTypes = (input.getAttribute("accept") || "")
          .split(",")
          .map((item) => item.trim())
          .filter((item) => item && (item.includes("image") || /\.(jpe?g|png|webp)$/i.test(item)));
        return imageTypes.length ? imageTypes.join(",") : "image/*";
      }

      function createSheet() {
        const element = document.createElement("div");
        element.className = "photo-picker-sheet";
        Object.assign(element.style, {
          position: "fixed",
          inset: "0",
          zIndex: "5000",
          display: "none",
          alignItems: "flex-end",
          justifyContent: "center",
          background: "rgba(15, 23, 42, 0.38)",
          backdropFilter: "blur(2px)",
        });
        element.innerHTML = `
          <div class="photo-picker-sheet__panel" role="dialog" aria-modal="true" aria-label="Добавить фото">
            <div class="photo-picker-sheet__handle"></div>
            <div class="photo-picker-sheet__actions" data-photo-picker-actions></div>
            <button type="button" class="photo-picker-sheet__close" data-photo-picker-close>Отмена</button>
          </div>
        `;
        element.addEventListener("click", (event) => {
          if (event.target === element || event.target.closest("[data-photo-picker-close]")) {
            closeSheet();
          }
        });
        document.addEventListener("keydown", (event) => {
          if (event.key === "Escape") closeSheet();
        });
        document.body.appendChild(element);
        const panel = element.querySelector(".photo-picker-sheet__panel");
        const handle = element.querySelector(".photo-picker-sheet__handle");
        const actions = element.querySelector(".photo-picker-sheet__actions");
        const close = element.querySelector(".photo-picker-sheet__close");
        if (panel) {
          Object.assign(panel.style, {
            width: "min(100% - 1rem, 520px)",
            margin: "0.5rem",
            padding: "0.65rem",
            borderRadius: "28px",
            background: "rgba(255, 255, 255, 0.98)",
            boxShadow: "0 24px 70px rgba(15, 23, 42, 0.25)",
          });
        }
        if (handle) {
          Object.assign(handle.style, {
            width: "44px",
            height: "5px",
            margin: "0.25rem auto 0.8rem",
            borderRadius: "999px",
            background: "#dbe3ef",
          });
        }
        if (actions) {
          Object.assign(actions.style, {
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(92px, 1fr))",
            gap: "0.5rem",
          });
        }
        if (close) {
          Object.assign(close.style, {
            width: "100%",
            marginTop: "0.5rem",
            border: "0",
            borderRadius: "20px",
            background: "transparent",
            color: "#64748b",
            fontWeight: "700",
            padding: "0.75rem",
          });
        }
        return element;
      }

      function closeSheet() {
        if (sheet) sheet.classList.remove("is-open");
        if (sheet) sheet.style.display = "none";
        document.body.classList.remove("photo-picker-open");
      }

      function openNativePicker(input, mode) {
        const originalAccept = input.getAttribute("accept");
        const originalCapture = input.getAttribute("capture");
        if (mode === "camera" || mode === "gallery") {
          input.setAttribute("accept", imageAccept(input));
        } else if (originalAccept !== null) {
          input.setAttribute("accept", originalAccept);
        }
        if (mode === "camera") {
          input.setAttribute("capture", "environment");
        } else {
          input.removeAttribute("capture");
        }
        input.click();
        closeSheet();
        setTimeout(() => {
          if (originalAccept === null) input.removeAttribute("accept");
          else input.setAttribute("accept", originalAccept);
          if (originalCapture === null) input.removeAttribute("capture");
          else input.setAttribute("capture", originalCapture);
        }, 1000);
      }

      function isMobileDevice() {
        return (
          /Android|iPhone|iPad|iPod/i.test(navigator.userAgent || "") ||
          (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1)
        );
      }

      function isAndroidDevice() {
        return /Android/i.test(navigator.userAgent || "");
      }

      function openSheet(input) {
        activeInput = input;
        if (!sheet) sheet = createSheet();
        const actions = sheet.querySelector("[data-photo-picker-actions]");
        actions.innerHTML = "";

        if (acceptsImages(input)) {
          actions.insertAdjacentHTML(
            "beforeend",
            `<button type="button" class="photo-picker-sheet__action photo-picker-sheet__action--primary" data-photo-action="gallery"><i class="bi bi-images"></i><span>Галерея</span></button>
             <button type="button" class="photo-picker-sheet__action" data-photo-action="camera"><i class="bi bi-camera"></i><span>Камера</span></button>`
          );
        }
        if (acceptsFiles(input)) {
          actions.insertAdjacentHTML(
            "beforeend",
            `<button type="button" class="photo-picker-sheet__action" data-photo-action="file"><i class="bi bi-file-earmark"></i><span>Файл</span></button>`
          );
        }
        actions.querySelectorAll("[data-photo-action]").forEach((button) => {
          Object.assign(button.style, {
            display: "grid",
            placeItems: "center",
            gap: "0.35rem",
            minHeight: "84px",
            padding: "0.75rem 0.5rem",
            border: "0",
            borderRadius: "22px",
            background: button.dataset.photoAction === "gallery" ? "#e8f2ff" : "#f1f5f9",
            color: button.dataset.photoAction === "gallery" ? "#0d6efd" : "#0f172a",
            fontWeight: "700",
          });
          button.addEventListener("click", () => openNativePicker(activeInput, button.dataset.photoAction));
        });
        sheet.classList.add("is-open");
        sheet.style.display = "flex";
        sheet.style.alignItems = isMobileDevice() ? "flex-end" : "center";
        document.body.classList.add("photo-picker-open");
      }

      function removeFile(input, index) {
        if (typeof DataTransfer === "undefined") return;
        const transfer = new DataTransfer();
        Array.from(input.files || []).forEach((file, fileIndex) => {
          if (fileIndex !== index) transfer.items.add(file);
        });
        input.files = transfer.files;
        input.dispatchEvent(new Event("change", { bubbles: true }));
      }

      function renderPreview(input) {
        const state = pickerState.get(input);
        if (!state) return;
        state.urls.forEach((url) => URL.revokeObjectURL(url));
        state.urls = [];
        state.preview.innerHTML = "";

        Array.from(input.files || []).forEach((file, index) => {
          const thumb = document.createElement("div");
          thumb.className = "photo-picker__thumb";
          thumb.title = file.name || "Файл";
          if ((file.type || "").startsWith("image/")) {
            const url = URL.createObjectURL(file);
            state.urls.push(url);
            const img = document.createElement("img");
            img.src = url;
            img.alt = "Фото";
            thumb.appendChild(img);
          } else {
            const icon = document.createElement("div");
            icon.className = "photo-picker__file";
            icon.innerHTML = '<i class="bi bi-file-earmark-pdf"></i>';
            thumb.appendChild(icon);
          }
          const remove = document.createElement("button");
          remove.type = "button";
          remove.className = "photo-picker__remove";
          remove.setAttribute("aria-label", "Удалить файл");
          remove.innerHTML = "&times;";
          remove.addEventListener("click", () => removeFile(input, index));
          thumb.appendChild(remove);
          state.preview.appendChild(thumb);
        });
      }

      function enhanceInput(input) {
        if (!input || input.dataset.photoPickerReady === "1") return;
        const accept = (input.getAttribute("accept") || "").toLowerCase();
        if (input.dataset.photoPicker !== "1" && !accept.includes("image")) return;
        input.dataset.photoPickerReady = "1";
        input.classList.add("photo-picker-native");
        Object.assign(input.style, {
          position: "absolute",
          width: "1px",
          height: "1px",
          padding: "0",
          margin: "-1px",
          overflow: "hidden",
          clip: "rect(0, 0, 0, 0)",
          whiteSpace: "nowrap",
          border: "0",
        });

        const wrapper = document.createElement("div");
        wrapper.className = "photo-picker";
        const trigger = document.createElement("button");
        trigger.type = "button";
        trigger.className = "photo-picker__button";
        trigger.innerHTML = '<i class="bi bi-camera"></i><span>Добавить фото</span>';
        Object.assign(trigger.style, {
          display: "inline-flex",
          alignItems: "center",
          justifyContent: "center",
          gap: "0.5rem",
          width: "fit-content",
          minHeight: "44px",
          padding: "0.65rem 1rem",
          border: "1px dashed #86b7fe",
          borderRadius: "16px",
          background: "linear-gradient(135deg, rgba(13, 110, 253, 0.08), rgba(13, 110, 253, 0.02))",
          color: "#0d6efd",
          fontWeight: "700",
        });
        const preview = document.createElement("div");
        preview.className = "photo-picker__preview";
        wrapper.appendChild(trigger);
        wrapper.appendChild(preview);
        input.insertAdjacentElement("afterend", wrapper);

        pickerState.set(input, { wrapper, preview, urls: [] });
        trigger.addEventListener("click", () => {
          if (isAndroidDevice()) {
            openSheet(input);
          } else {
            openNativePicker(input, "file");
          }
        });
        input.addEventListener("change", () => renderPreview(input));
        renderPreview(input);
      }

      function init(root) {
        const scope = root || document;
        scope.querySelectorAll("input[type='file']").forEach(enhanceInput);
      }

      document.addEventListener("rovik:open-photo-picker", (event) => {
        const input = event.detail && event.detail.input;
        if (!input) return;
        enhanceInput(input);
        if (isAndroidDevice() && !(event.detail && event.detail.mode)) {
          openSheet(input);
        } else {
          openNativePicker(input, (event.detail && event.detail.mode) || "file");
        }
      });

      window.RovikPhotoPicker = { init, open: openSheet, openNative: openNativePicker };
      document.addEventListener("DOMContentLoaded", () => init(document));
    })();
  


    (function () {
      const modalElement = document.getElementById("globalPhotoViewerModal");
      if (!modalElement) return;
      const stage = modalElement.querySelector("[data-global-photo-stage]");
      const image = modalElement.querySelector("[data-global-photo-image]");
      const prevButton = modalElement.querySelector("[data-global-photo-prev]");
      const nextButton = modalElement.querySelector("[data-global-photo-next]");
      const counter = modalElement.querySelector("[data-global-photo-counter]");
      if (!stage || !image || !prevButton || !nextButton || !counter) return;

      const modal = typeof bootstrap !== "undefined" ? bootstrap.Modal.getOrCreateInstance(modalElement) : null;
      const viewportMeta = document.querySelector("meta[name='viewport']");
      const originalViewport = viewportMeta ? viewportMeta.getAttribute("content") : "";
      let urls = [];
      let index = 0;
      let zoom = 1;
      let panX = 0;
      let panY = 0;
      let lastTouchDistance = 0;
      let lastTouchCenter = null;
      let lastPointer = null;
      let pointerId = null;
      let suppressClick = false;

      function lockViewportZoom() {
        if (viewportMeta) {
          viewportMeta.setAttribute("content", "width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no");
        }
      }

      function unlockViewportZoom() {
        if (viewportMeta) {
          viewportMeta.setAttribute("content", originalViewport || "width=device-width, initial-scale=1");
        }
      }

      function resetZoom() {
        zoom = 1;
        panX = 0;
        panY = 0;
        applyTransform();
      }

      function applyTransform() {
        if (zoom <= 1) {
          zoom = 1;
          panX = 0;
          panY = 0;
        }
        image.style.transform = `translate(${Math.round(panX)}px, ${Math.round(panY)}px) scale(${zoom})`;
        image.style.cursor = zoom > 1 ? "grab" : "zoom-in";
      }

      function setZoom(value) {
        zoom = Math.min(5, Math.max(1, value));
        applyTransform();
      }

      function distanceBetween(touches) {
        const first = touches[0];
        const second = touches[1];
        return Math.hypot(second.clientX - first.clientX, second.clientY - first.clientY);
      }

      function centerBetween(touches) {
        const first = touches[0];
        const second = touches[1];
        return {
          x: (first.clientX + second.clientX) / 2,
          y: (first.clientY + second.clientY) / 2,
        };
      }

      function render() {
        if (!urls.length) return;
        image.src = urls[index];
        resetZoom();
        const hasMany = urls.length > 1;
        prevButton.classList.toggle("d-none", !hasMany);
        nextButton.classList.toggle("d-none", !hasMany);
        counter.classList.toggle("d-none", !hasMany);
        counter.textContent = `${index + 1} / ${urls.length}`;
      }

      function openGallery(nextUrls, startIndex) {
        if (!modal || !nextUrls || !nextUrls.length) return;
        urls = nextUrls.filter(Boolean);
        if (!urls.length) return;
        index = Math.min(Math.max(Number(startIndex) || 0, 0), urls.length - 1);
        render();
        modal.show();
      }

      function changeImage(delta) {
        if (urls.length <= 1) return;
        index = (index + delta + urls.length) % urls.length;
        render();
      }

      function urlsFromContainer(container) {
        if (!container) return [];
        try {
          return JSON.parse(container.dataset.photos || "[]");
        } catch (error) {
          return [];
        }
      }

      function imageReceiptLinks() {
        return Array.from(document.querySelectorAll("[data-receipt-modal][data-receipt-kind='image']"));
      }

      document.addEventListener("click", (event) => {
        const issueButton = event.target.closest(".issue-photo-thumb");
        if (issueButton) {
          const container = issueButton.closest(".issue-photo-thumbs");
          const galleryUrls = urlsFromContainer(container);
          if (galleryUrls.length) {
            event.preventDefault();
            event.stopPropagation();
            openGallery(galleryUrls, Number(issueButton.dataset.index || 0));
          }
          return;
        }

        const receiptLink = event.target.closest("[data-receipt-modal][data-receipt-kind='image']");
        if (receiptLink) {
          const links = imageReceiptLinks();
          event.preventDefault();
          event.stopPropagation();
          openGallery(links.map((link) => link.href), links.indexOf(receiptLink));
          return;
        }

        const photoLink = event.target.closest("[data-photo-viewer]");
        if (photoLink) {
          const container = photoLink.closest("[data-photo-gallery]");
          const galleryUrls = urlsFromContainer(container);
          event.preventDefault();
          event.stopPropagation();
          openGallery(galleryUrls.length ? galleryUrls : [photoLink.href], Number(photoLink.dataset.index || 0));
        }
      }, true);

      prevButton.addEventListener("click", () => changeImage(-1));
      nextButton.addEventListener("click", () => changeImage(1));
      image.addEventListener("click", () => {
        if (suppressClick) {
          suppressClick = false;
          return;
        }
        setZoom(zoom > 1 ? 1 : 2);
      });
      stage.addEventListener("wheel", (event) => {
        if (!event.ctrlKey && Math.abs(event.deltaY) < 40) return;
        event.preventDefault();
        setZoom(zoom + (event.deltaY < 0 ? 0.25 : -0.25));
      }, { passive: false });
      ["gesturestart", "gesturechange", "gestureend"].forEach((eventName) => {
        stage.addEventListener(eventName, (event) => event.preventDefault(), { passive: false });
        modalElement.addEventListener(eventName, (event) => event.preventDefault(), { passive: false });
      });
      stage.addEventListener("touchstart", (event) => {
        if (event.touches.length >= 2) {
          const touches = Array.from(event.touches).slice(0, 2);
          lastTouchDistance = distanceBetween(touches);
          lastTouchCenter = centerBetween(touches);
        } else if (event.touches.length === 1) {
          const touch = event.touches[0];
          lastTouchDistance = 0;
          lastTouchCenter = { x: touch.clientX, y: touch.clientY };
        }
      }, { passive: false });
      stage.addEventListener("touchmove", (event) => {
        if (!event.touches.length) return;
        event.preventDefault();
        suppressClick = true;
        if (event.touches.length >= 2) {
          const touches = Array.from(event.touches).slice(0, 2);
          const distance = distanceBetween(touches);
          const center = centerBetween(touches);
          if (lastTouchDistance) {
            zoom = Math.min(5, Math.max(1, zoom * (distance / lastTouchDistance)));
            if (lastTouchCenter && zoom > 1) {
              panX += center.x - lastTouchCenter.x;
              panY += center.y - lastTouchCenter.y;
            }
            applyTransform();
          }
          lastTouchDistance = distance;
          lastTouchCenter = center;
          return;
        }
        if (zoom <= 1) return;
        const touch = event.touches[0];
        if (lastTouchCenter) {
          panX += touch.clientX - lastTouchCenter.x;
          panY += touch.clientY - lastTouchCenter.y;
          applyTransform();
        }
        lastTouchCenter = { x: touch.clientX, y: touch.clientY };
      }, { passive: false });
      stage.addEventListener("touchend", () => {
        lastTouchDistance = 0;
        lastTouchCenter = null;
      }, { passive: false });
      stage.addEventListener("pointerdown", (event) => {
        if (event.pointerType === "touch" || zoom <= 1) return;
        pointerId = event.pointerId;
        lastPointer = { x: event.clientX, y: event.clientY };
        stage.setPointerCapture?.(event.pointerId);
        image.style.cursor = "grabbing";
        event.preventDefault();
      });
      stage.addEventListener("pointermove", (event) => {
        if (event.pointerType === "touch" || zoom <= 1 || pointerId !== event.pointerId || !lastPointer) return;
        suppressClick = true;
        panX += event.clientX - lastPointer.x;
        panY += event.clientY - lastPointer.y;
        lastPointer = { x: event.clientX, y: event.clientY };
        applyTransform();
        event.preventDefault();
      });
      ["pointerup", "pointercancel", "pointerleave"].forEach((eventName) => {
        stage.addEventListener(eventName, () => {
          pointerId = null;
          lastPointer = null;
          image.style.cursor = zoom > 1 ? "grab" : "zoom-in";
        });
      });
      document.addEventListener("keydown", (event) => {
        if (!modalElement.classList.contains("show")) return;
        if (event.key === "ArrowLeft") changeImage(-1);
        if (event.key === "ArrowRight") changeImage(1);
      });
      modalElement.addEventListener("shown.bs.modal", lockViewportZoom);
      modalElement.addEventListener("hidden.bs.modal", () => {
        image.src = "";
        urls = [];
        index = 0;
        resetZoom();
        unlockViewportZoom();
      });
    })();
  
