(() => {
  "use strict";
  const photoDialog = document.getElementById("conversation-photo-dialog");
  if (photoDialog) {
    const photo = photoDialog.querySelector("img");
    const title = photoDialog.querySelector("#conversation-photo-title");
    const error = photoDialog.querySelector("[data-photo-error]");
    photo.addEventListener("error", () => { photo.hidden = true; error.hidden = false; });
    document.addEventListener("click", (event) => {
      const trigger = event.target.closest("[data-conversation-photo]");
      if (!trigger || photoDialog.open) return;
      const thumbnail = trigger.querySelector("img");
      title.textContent = thumbnail.alt;
      photo.alt = thumbnail.alt;
      photo.hidden = false;
      error.hidden = true;
      photo.src = thumbnail.currentSrc || thumbnail.src;
      photoDialog.showModal();
    });
    photoDialog.addEventListener("close", () => photo.removeAttribute("src"));
  }

  const root = document.querySelector("[data-conversation-identity]");
  const listing = document.querySelector("[data-conversation-listing]");
  if (!root || !listing) return;
  let remaining = 30;
  let running = false, timer;
  const schedule = () => { window.clearTimeout(timer); timer = window.setTimeout(update, 2000); };
  const update = async () => {
    if (document.visibilityState !== "visible" || running || remaining-- <= 0) return;
    running = true;
    try {
      const response = await fetch(root.dataset.identityUrl, {credentials: "same-origin", cache: "no-store"});
      if (!response.ok) return;
      const data = await response.json();
      // HTML comes from our escaped Django partials, not from provider payloads.
      root.innerHTML = data.identity_html;
      listing.innerHTML = data.listing_html;
      const name = document.querySelector("[data-selected-conversation-name]");
      if (name) {
        name.textContent = data.name;
        name.title = data.name;
      }
      const client = document.querySelector("[data-selected-conversation-client]");
      if (client && data.client_url) {
        client.href = data.client_url;
        client.setAttribute("aria-label", "Открыть карточку " + data.name);
        client.hidden = false;
      } else if (client) {
        client.removeAttribute("href");
        client.hidden = true;
      }
      document.querySelectorAll("[data-incoming-sender]").forEach(sender => { sender.textContent = data.name; sender.title = data.name; });
      if (!data.pending) { remaining = 0; return; }
    } catch (_) {
      // A temporary metadata failure must not affect typing or sending messages.
    } finally { running = false; }
    schedule();
  };
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") { window.clearTimeout(timer); update(); } });
  schedule();
})();
