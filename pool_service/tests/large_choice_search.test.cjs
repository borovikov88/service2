"use strict";
// Standard-library CDP driver: exercise the real shared script in Chromium.
// No application login, production data, browser-policy changes or network pages.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const {spawn} = require("node:child_process");
const chrome = [process.env.CHROME_BIN, "/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome", "/opt/google/chrome/chrome"]
  .find(file => file && fs.existsSync(file));
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));

test("search-first native and large multiselect browser regressions", {
  skip: !chrome || typeof WebSocket === "undefined" ? "Chromium and Node with WebSocket are required" : false,
  timeout: 45000,
}, async t => {
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "service2-choice-browser-"));
  // Some CI runners export a malformed session bus address. The isolated
  // headless browser does not need the caller's desktop session bus.
  const browserEnv = {...process.env};
  delete browserEnv.DBUS_SESSION_BUS_ADDRESS;
  const child = spawn(chrome, ["--headless", "--no-sandbox", "--disable-dev-shm-usage", "--disable-background-networking",
    "--no-first-run", "--remote-debugging-port=0", "--user-data-dir=" + profile, "about:blank"], {env: browserEnv, stdio: ["ignore", "ignore", "pipe"]});
  let stderr = "", socket;
  child.stderr.on("data", chunk => {stderr = (stderr + chunk).slice(-20000);});
  const pending = new Map();
  try {
    const deadline = Date.now() + 10000;
    let endpoint;
    while (Date.now() < deadline && !(endpoint = stderr.match(/DevTools listening on (ws:\/\/\S+)/)?.[1])) {
      if (child.exitCode !== null) throw Error("Chromium exited before CDP: " + stderr.slice(-1500));
      await pause(50);
    }
    assert.ok(endpoint, "Chromium CDP did not start: " + stderr.slice(-1500));
    socket = new WebSocket(endpoint);
    await new Promise((resolve, reject) => {
      socket.addEventListener("open", resolve, {once: true});
      socket.addEventListener("error", reject, {once: true});
    });
    let sequence = 0;
    socket.addEventListener("message", event => {
      const result = JSON.parse(String(event.data));
      const call = pending.get(result.id);
      if (!call) return;
      pending.delete(result.id); clearTimeout(call.timer);
      if (result.error) call.reject(Error(JSON.stringify(result.error)));
      else call.resolve(result.result);
    });
    function send(method, params = {}, sessionId) {
      const id = ++sequence;
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => {pending.delete(id); reject(Error("CDP timeout: " + method));}, 10000);
        pending.set(id, {resolve, reject, timer});
        socket.send(JSON.stringify({id, method, params, ...(sessionId ? {sessionId} : {})}));
      });
    }
    const {targetInfos} = await send("Target.getTargets");
    const target = targetInfos.find(item => item.type === "page");
    assert.ok(target, "No blank browser page");
    const {sessionId} = await send("Target.attachToTarget", {targetId: target.targetId, flatten: true});
    const {frameTree} = await send("Page.getFrameTree", {}, sessionId);
    const source = fs.readFileSync(path.join(__dirname, "../static/assets/js/base-shell-post-b.js"), "utf8");
    const cases = fs.readFileSync(path.join(__dirname, "large_choice_search.browser.js"), "utf8");
    const html = '<!doctype html><meta charset="utf-8"><style>[hidden],.d-none{display:none!important}.multi-select__option{display:flex}</style><body><script>' +
      source + '</script><script>window.addEventListener("DOMContentLoaded",()=>{' + cases + '});</script>';
    // Populate a blank document, without navigating to even a local file URL.
    await send("Page.setDocumentContent", {frameId: frameTree.frame.id, html}, sessionId);
    const end = Date.now() + 20000;
    let encoded;
    while (Date.now() < end) {
      const data = await send("Runtime.evaluate", {expression: "document.documentElement.dataset.testReport", returnByValue: true}, sessionId);
      encoded = data.result?.value;
      if (encoded) break;
      await pause(100);
    }
    assert.ok(encoded, "Browser did not finish the regression cases");
    const report = JSON.parse(Buffer.from(encoded, "base64").toString("utf8"));
    assert.equal(report.length, 30, "Every browser regression must run");
    for (const result of report) {
      await t.test(result.name, () => assert.equal(result.ok, true, result.error || result.name));
    }
  } finally {
    for (const call of pending.values()) clearTimeout(call.timer);
    socket?.close();
    child.kill("SIGTERM");
    for (let i = 0; child.exitCode === null && i < 20; i++) await pause(50);
    if (child.exitCode === null) child.kill("SIGKILL");
    fs.rmSync(profile, {recursive: true, force: true, maxRetries: 5, retryDelay: 100});
  }
});
