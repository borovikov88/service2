const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const {chromium} = require("playwright");

(async () => {
  let input = "";
  for await (const chunk of process.stdin) input += chunk;
  const f = JSON.parse(input);
  assert.match(f.base, /^http:\/\/(localhost|127\.0\.0\.1):[0-9]+$/);
  const browser = await chromium.launch({headless: true});
  let context, waitForList, releaseList;
  try {
    context = await browser.newContext({viewport:{width:1440,height:1000}, serviceWorkers:"block"});
    await context.addCookies([{name:f.sessionCookieName, value:f.sessionCookie, url:f.base}]);
    const bootstrap = readFileSync(require.resolve("bootstrap/dist/js/bootstrap.bundle.min.js"), "utf8");
    await context.route("**/*", async route => {
      if (route.request().url() === "https://cdn.jsdelivr.net/npm/bootstrap@5.2.3/dist/js/bootstrap.bundle.min.js") {
        return route.fulfill({contentType:"application/javascript", body:bootstrap});
      }
      const url = new URL(route.request().url());
      if (url.origin !== f.base) return route.abort();
      if (url.pathname === f.dealData && waitForList) {
        const pending = waitForList;
        waitForList = null;
        await pending;
      }
      return route.continue();
    });
    const page = await context.newPage(), errors = [], leadReads = [];
    page.setDefaultTimeout(10000);
    page.on("pageerror", error => errors.push(error.message));
    page.on("request", request => {
      const url = new URL(request.url());
      if (request.method() === "GET" && url.pathname === f.leadDetail) leadReads.push(url);
    });
    const dialog = page.locator("#lead-dialog");
    const card = page.locator(`.lead-row[data-lead-id="${f.leadId}"]`);
    const response = kind => page.waitForResponse(res => new URL(res.url()).pathname === f.command &&
      res.request().method() === "POST" && res.request().postDataJSON().command_type === kind);
    await page.goto(f.base + f.path);
    await card.locator(".lead-card-heading").click();
    await dialog.getByRole("button",{name:"Редактировать сумму",exact:true}).click();
    await dialog.getByLabel("Сумма / от, ₽",{exact:true}).fill("62 500,25");
    const edit = response("lead.edit");
    await dialog.getByRole("button",{name:"Сохранить сумму",exact:true}).click();
    const editResult = await edit;
    assert.equal(editResult.status(), 200);
    await dialog.locator(".crm-card-money-value").filter({hasText:/500,25/}).waitFor();
    assert.ok(await dialog.isVisible());
    await page.locator("#lead-close").click();
    await page.locator('#lead-list[aria-busy="false"]').waitFor();
    const readsBeforeMenu = leadReads.length;
    const transfer = await page.evaluateHandle(() => new DataTransfer());
    await card.dispatchEvent("dragstart", {dataTransfer:transfer});
    const drop = page.locator(".lead-conversion-drop").filter({hasText:"Завершить лид"});
    await drop.waitFor();
    await drop.dispatchEvent("dragover", {dataTransfer:transfer});
    await drop.dispatchEvent("drop", {dataTransfer:transfer});
    await dialog.getByRole("button",{name:"Создать сделку и контакт",exact:true}).waitFor();
    await dialog.getByRole("button",{name:"Создать сделку, контакт и компанию",exact:true}).waitFor();
    assert.equal(await dialog.locator('form').count(), 0);
    assert.equal(leadReads.length, readsBeforeMenu, "Opening the chooser must not read the full card");
    const bounds = await dialog.boundingBox();
    if (process.env.CRM_SYNTHETIC_SCREENSHOTS) {
      await dialog.screenshot({path: process.env.CRM_SYNTHETIC_SCREENSHOTS + "/crm-lead-completion-menu.png"});
    }
    assert.ok(bounds.width <= 420 && bounds.height < 480, "The chooser must be compact: " + JSON.stringify(bounds));
    await dialog.getByRole("button",{name:"Создать сделку",exact:true}).click();
    const amount = dialog.getByLabel("Коммерческая сумма, ₽",{exact:true});
    assert.match(await amount.inputValue(), /500,25/);
    assert.equal(leadReads.length, readsBeforeMenu + 1);
    assert.equal(leadReads.at(-1).searchParams.get("view"), "conversion");
    await dialog.getByRole("combobox",{name:"Ответственный",exact:true}).locator(`option[value="${f.userId}"]`).waitFor({state:"attached"});
    waitForList = new Promise(resolve => { releaseList = resolve; });
    const listReceived = page.waitForRequest(request => new URL(request.url()).pathname === f.dealData);
    const qualification = response("lead.qualify");
    await dialog.getByRole("button",{name:"Подтвердить создание сделки",exact:true}).click();
    const result = await qualification;
    assert.equal(result.status(), 200);
    assert.equal(new URL(result.url()).searchParams.get("view"), "conversion");
    await page.waitForURL(url => url.pathname === f.deals && url.searchParams.has("deal") && url.searchParams.get("org_id") === String(f.orgId));
    await listReceived;
    const deal = page.locator("#crm-live-dialog");
    await deal.locator(".crm-live-detail-meta").waitFor();
    releaseList();
    await page.locator('#crm-live-content[aria-busy="false"]').waitFor();
    await deal.getByRole("button",{name:/^Сменить воронку/}).click();
    const pipelineForm = deal.locator('[data-command-form="change_pipeline"]');
    await pipelineForm.getByLabel("Новая воронка").selectOption(String(f.pipelineId));
    await pipelineForm.getByLabel("Этап в новой воронке").selectOption(String(f.stageId));
    const move = page.waitForResponse(res => new URL(res.url()).pathname === f.dealCommand &&
      res.request().method() === "POST" && res.request().postDataJSON().command_type === "deal.change_pipeline");
    await pipelineForm.getByRole("button",{name:"Сменить воронку",exact:true}).click();
    const moved = await move;
    assert.equal(moved.status(), 200);
    await page.waitForURL(url => url.searchParams.get("pipeline") === String(f.pipelineId) && url.searchParams.has("deal"));
    await deal.getByRole("button",{name:/^Сменить воронку.*Розничная продажа/}).waitFor();
    assert.equal(await deal.getByText("Ссылка на сделку",{exact:true}).count(), 0);
    await page.locator("#crm-live-dialog [data-action=close]").first().click();
    await page.locator("#crm-live-create").click();
    const createForm = page.locator('#crm-live-dialog [data-command-form="create"]');
    await createForm.locator('input[name="amount"]').waitFor();
    await createForm.locator('select[name="responsible_id"]').waitFor();
    assert.deepEqual(errors, []);
    console.log("Compact lead chooser, scoped conversion reads, early deal opening and pipeline change passed");
  } finally {
    releaseList?.();
    await context?.unrouteAll({behavior:"ignoreErrors"});
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
