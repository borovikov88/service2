const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const {chromium} = require("playwright");

(async () => {
  let input = "";
  for await (const chunk of process.stdin) input += chunk;
  const {base, path, name, session} = JSON.parse(input);
  assert.match(base, /^http:\/\/(localhost|127\.0\.0\.1):\d+$/);
  const browser = await chromium.launch({headless: true});
  try {
    const context = await browser.newContext({serviceWorkers: "block"});
    await context.addCookies([{name, value: session, url: base}]);
    let imageRequests = 0;
    await context.route("**/*", route => {
      const url = new URL(route.request().url());
      if (url.origin === base) return route.continue();
      if (url.host === "images.example.org") {
        imageRequests++;
        return route.fulfill({contentType: "image/png", body: readFileSync(require.resolve("../static/assets/images/mail/taxcom.png"))});
      }
      const assets = {
        "/npm/bootstrap@5.2.3/dist/css/bootstrap.min.css": "bootstrap/dist/css/bootstrap.min.css",
        "/npm/bootstrap@5.2.3/dist/js/bootstrap.bundle.min.js": "bootstrap/dist/js/bootstrap.bundle.min.js"
      };
      const file = url.origin === "https://cdn.jsdelivr.net" && assets[url.pathname];
      if (file) return route.fulfill({body: readFileSync(require.resolve(file)), contentType: file.endsWith("css") ? "text/css" : "text/javascript"});
      return route.abort();
    });
    const page = await context.newPage();
    page.on("pageerror", error => console.error(error.message));
    await page.goto(base + path);
    const reader = page.frameLocator(".mail-html-reader");
    assert.equal(await reader.locator("td").count(), 2);
    assert.equal(await reader.getByRole("link", {name: "Открыть чек"}).getAttribute("href"), "https://example.org/receipt");
    assert.equal(await reader.locator("img").getAttribute("src"), null);
    assert.equal(imageRequests, 0);
    assert.ok(await page.locator(".mail-header-sender img").evaluate(img => img.complete && img.naturalWidth > 0));
    await page.getByText("Показать внешние изображения").click();
    await reader.locator('img[src^="https://"]').waitFor({state: "attached"});
    await page.waitForLoadState("load");
    assert.equal(imageRequests, 1);
    assert.ok(await reader.locator("img").evaluate(img => img.complete && img.naturalWidth > 0));
    await page.getByRole("link", {name: "Ответить", exact: true}).click();
    const compose = page.locator("[data-compose-dialog]");
    const body = compose.locator(".ck-editor__editable");
    await body.waitFor();
    await body.press("Control+Home");
    await page.keyboard.insertText("Добрый день");
    await compose.getByRole("link", {name: "Моя подпись"}).click();
    const signature = page.locator("[data-signature-dialog]");
    const editor = signature.locator(".ck-editor__editable");
    await editor.fill("Команда проекта");
    await editor.press("Control+a");
    await editor.press("Control+b");
    await signature.locator(".ck-font-family-dropdown > button").click();
    await signature.getByRole("menuitemradio", {name: "Courier New"}).click();
    await signature.getByRole("button", {name: "Сохранить", exact: true}).click();
    await signature.waitFor({state: "hidden"});
    assert.ok((await body.innerText()).includes("Добрый день"));
    assert.ok((await body.innerHTML()).includes("Courier New"));
    await page.setViewportSize({width: 390, height: 800});
    const fits = await body.evaluate(e => e.getBoundingClientRect().right <= innerWidth + 1);
    assert.ok(fits, "Mobile editor must fit the message window");
    await compose.getByRole("button", {name: "Отправить", exact: true}).click();
    await page.waitForURL(url => url.pathname !== new URL(path, base).pathname);
    await page.frameLocator(".mail-html-reader").getByText("Команда проекта").waitFor();
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
