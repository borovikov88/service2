const assert = require("node:assert/strict");

async function verifyBoardDragScroll(page, card, boardSelector) {
  const viewport = page.viewportSize();
  const board = page.locator(boardSelector);
  await page.setViewportSize({width: 500, height: viewport.height});
  await card.scrollIntoViewIfNeeded();
  await board.evaluate(node => { node.scrollLeft = 0; });
  const bounds = await board.evaluate(node => {
    const rect = node.getBoundingClientRect();
    return {
      left: Math.max(0, rect.left) + 3,
      right: Math.min(innerWidth, rect.right) - 3,
      y: Math.max(0, rect.top) + 30,
      overflow: node.scrollWidth - node.clientWidth,
    };
  });
  assert.ok(bounds.overflow > 40, "Narrow board should overflow horizontally");
  const transfer = await page.evaluateHandle(() => new DataTransfer());
  await card.dispatchEvent("dragstart", {dataTransfer: transfer});
  await board.dispatchEvent("dragover", {dataTransfer: transfer, clientX: bounds.right, clientY: bounds.y});
  await page.waitForFunction(selector => document.querySelector(selector).scrollLeft > 40, boardSelector);
  await board.dispatchEvent("dragover", {dataTransfer: transfer, clientX: bounds.left, clientY: bounds.y});
  await page.waitForFunction(selector => document.querySelector(selector).scrollLeft === 0, boardSelector);
  await card.dispatchEvent("dragend", {dataTransfer: transfer});
  assert.equal(await board.evaluate(node => node.classList.contains("is-drag-scrolling")), false);
  await transfer.dispose();
  await page.setViewportSize(viewport);
}

module.exports = {verifyBoardDragScroll};

