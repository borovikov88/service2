(function () {
  const DATABASE_NAME = "rovik-finance";
  const DATABASE_VERSION = 1;
  const STORE_NAME = "expenseQueue";

  function openDatabase() {
    return new Promise(function (resolve, reject) {
      const request = indexedDB.open(DATABASE_NAME, DATABASE_VERSION);
      request.onupgradeneeded = function () {
        const database = request.result;
        if (!database.objectStoreNames.contains(STORE_NAME)) {
          database.createObjectStore(STORE_NAME, { keyPath: "id" });
        }
      };
      request.onsuccess = function () { resolve(request.result); };
      request.onerror = function () { reject(request.error); };
    });
  }

  async function withStore(mode, callback) {
    const database = await openDatabase();
    return new Promise(function (resolve, reject) {
      const transaction = database.transaction(STORE_NAME, mode);
      const store = transaction.objectStore(STORE_NAME);
      const result = callback(store);
      transaction.oncomplete = function () { database.close(); resolve(result); };
      transaction.onerror = function () { database.close(); reject(transaction.error); };
    });
  }

  function requestResult(request) {
    return new Promise(function (resolve, reject) {
      request.onsuccess = function () { resolve(request.result); };
      request.onerror = function () { reject(request.error); };
    });
  }

  async function queueItems() {
    const database = await openDatabase();
    try {
      const transaction = database.transaction(STORE_NAME, "readonly");
      return await requestResult(transaction.objectStore(STORE_NAME).getAll());
    } finally {
      database.close();
    }
  }

  async function saveItem(item) {
    return withStore("readwrite", function (store) { store.put(item); });
  }

  async function deleteItem(id) {
    return withStore("readwrite", function (store) { store.delete(id); });
  }

  function csrfToken() {
    const match = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : "";
  }

  function newRequestId() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") return window.crypto.randomUUID();
    return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, function (character) {
      const random = Math.random() * 16 | 0;
      const value = character === "x" ? random : (random & 3 | 8);
      return value.toString(16);
    });
  }

  function statusElements() {
    return [
      document.getElementById("finance-offline-status"),
      document.getElementById("finance-sync-status"),
    ].filter(Boolean);
  }

  function showStatus(message, type) {
    statusElements().forEach(function (element) {
      element.textContent = message;
      element.className = `alert alert-${type || "info"}`;
      element.classList.remove("d-none");
    });
  }

  async function updateStatus() {
    if (!("indexedDB" in window)) return;
    const currentUserId = document.body.dataset.currentUserId || "";
    const items = (await queueItems()).filter(function (item) { return item.userId === currentUserId; });
    if (!items.length) return;
    const suffix = items.length === 1 ? "расход" : "расхода";
    if (navigator.onLine) {
      showStatus(`Синхронизация: ожидает ${items.length} ${suffix}.`, "info");
    } else {
      showStatus(`Офлайн: сохранено ${items.length} ${suffix}. Они отправятся при появлении интернета.`, "warning");
    }
  }

  async function serializeForm(form) {
    const formData = new FormData(form);
    const fields = [];
    const files = [];
    formData.forEach(function (value, name) {
      if (value instanceof File) {
        if (!value.name || !value.size) return;
        files.push({
          field: name,
          name: value.name,
          type: value.type,
          lastModified: value.lastModified,
          blob: value,
        });
      } else {
        fields.push([name, value]);
      }
    });
    const requestInput = form.querySelector("[name='request_id']");
    return {
      id: requestInput.value,
      action: form.action,
      method: (form.method || "POST").toUpperCase(),
      fields: fields,
      files: files,
      userId: document.body.dataset.currentUserId || "",
      createdAt: new Date().toISOString(),
    };
  }

  function restoreFormData(item) {
    const formData = new FormData();
    item.fields.forEach(function (field) { formData.append(field[0], field[1]); });
    item.files.forEach(function (file) {
      formData.append(
        file.field,
        new File([file.blob], file.name, { type: file.type, lastModified: file.lastModified }),
      );
    });
    return formData;
  }

  async function processQueue() {
    if (!("indexedDB" in window) || !navigator.onLine) {
      await updateStatus();
      return;
    }
    const currentUserId = document.body.dataset.currentUserId || "";
    if (!currentUserId) return;
    const items = (await queueItems()).filter(function (item) {
      return item.userId === currentUserId;
    }).sort(function (left, right) {
      return left.createdAt.localeCompare(right.createdAt);
    });
    let sent = 0;
    for (const item of items) {
      try {
        const response = await fetch(item.action, {
          method: item.method,
          body: restoreFormData(item),
          credentials: "include",
          headers: {
            "Accept": "application/json",
            "X-CSRFToken": csrfToken(),
            "X-Finance-Offline": "1",
          },
        });
        const contentType = response.headers.get("Content-Type") || "";
        if (!response.ok || !contentType.includes("application/json")) break;
        const payload = await response.json();
        if (!payload.ok) break;
        await deleteItem(item.id);
        sent += 1;
      } catch (error) {
        break;
      }
    }
    if (sent) showStatus(`Отправлено расходов: ${sent}.`, "success");
    await updateStatus();
  }

  async function prepareForm(form) {
    const requestInput = form.querySelector("[name='request_id']");
    const currentUserId = document.body.dataset.currentUserId || "";
    const queued = (await queueItems()).filter(function (item) { return item.userId === currentUserId; });
    if (!requestInput.value || queued.some(function (item) { return item.id === requestInput.value; })) {
      requestInput.value = newRequestId();
    }
    form.addEventListener("submit", async function (event) {
      if (event.defaultPrevented) return;
      if (navigator.onLine) return;
      event.preventDefault();
      if (!form.reportValidity()) return;
      const receiptInput = form.querySelector("[data-finance-receipts]");
      const skipReceipt = form.querySelector("[name='receipt_missing_confirmed']");
      const receiptSkipped = skipReceipt && skipReceipt.value === "1";
      if (form.dataset.receiptRequired === "1" && !receiptSkipped && (!receiptInput || !receiptInput.files.length)) {
        showStatus("Добавьте фотографию/PDF чека или нажмите «Пропустить».", "danger");
        return;
      }
      if (!("indexedDB" in window)) {
        showStatus("Этот браузер не поддерживает офлайн-сохранение файлов.", "danger");
        return;
      }
      const submitButton = form.querySelector("[data-finance-submit]");
      if (submitButton) submitButton.disabled = true;
      try {
        await saveItem(await serializeForm(form));
        showStatus("Расход сохранён на телефоне и отправится при появлении интернета.", "success");
        requestInput.value = newRequestId();
      } catch (error) {
        showStatus("Не удалось сохранить расход офлайн. Освободите место на устройстве и попробуйте снова.", "danger");
      } finally {
        if (submitButton) submitButton.disabled = false;
      }
      await updateStatus();
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    const form = document.querySelector("[data-finance-offline-form='1']");
    if (form && "indexedDB" in window) prepareForm(form);
    updateStatus();
    processQueue();
  });
  window.addEventListener("online", processQueue);
  window.addEventListener("offline", updateStatus);
})();
