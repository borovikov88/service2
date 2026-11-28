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
    await context.route("**/*", route => {
      const url = new URL(route.request().url());
      if (url.origin === base) return route.continue();
      const assets = {
        "/npm/bootstrap@5.2.3/dist/css/bootstrap.min.css": "bootstrap/dist/css/bootstrap.min.css",
        "/npm/bootstrap@5.2.3/dist/js/bootstrap.bundle.min.js": "bootstrap/dist/js/bootstrap.bundle.min.js"
      };
      const file = url.origin === "https://cdn.jsdelivr.net" && assets[url.pathname];
      if (file) return route.fulfill({body: readFileSync(require.resolve(file)), contentType: file.endsWith(".css") ? "text/css" : "text/javascript"});
      return route.abort();
    });
    const page = await context.newPage();
    await page.goto(base + path);
    const row = page.locator(".mail-row");
    await row.locator(".mail-has-files").waitFor({state: "visible"});
    assert.match(await row.locator("time").innerText(), /^\d{2}\.\d{2} \d{2}:\d{2}$/);
    for (const width of [1100, 390, 320]) {
      await page.setViewportSize({width, height: 760});
      const positions = await row.evaluate(node => {
        const rects = [".mail-read-button", ".mail-avatar", ".mail-sender", ".mail-summary", ".mail-has-files", "time"]
          .map(selector => node.querySelector(selector).getBoundingClientRect());
        return rects.every((rect, i) => rect.width > 0 && rect.left >= -1 && rect.right <= innerWidth + 1
          && (!i || rect.left >= rects[i - 1].right - 1));
      });
      assert.ok(positions, "Inbox controls should fit in one row at width " + width);
    }
    const url = page.url();
    await row.getByRole("button", {name: /Отметить прочитанным/}).click();
    await row.locator(".mail-read-done").waitFor({state: "attached"});
    await page.waitForFunction(() => !document.querySelector(".mail-row").classList.contains("is-unread"));
    assert.equal(page.url(), url);
    await page.reload();
    await row.waitFor();
    assert.equal(await row.getByRole("button", {name: /Отметить прочитанным/}).count(), 0);
    assert.ok(await row.locator(".mail-has-files").isVisible());
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
