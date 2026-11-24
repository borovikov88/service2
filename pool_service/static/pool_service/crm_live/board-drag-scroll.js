(function (root, factory) {
  "use strict";
  const create = factory();
  if (typeof module === "object" && module.exports) module.exports = create;
  if (typeof window !== "undefined") root.createCrmBoardDragScroll = create;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  return function create(win, getBoard, isDragging) {
    let point = null;
    let frame = null;
    let board = null;
    let lastTime = null;
    let position = null;

    function stop() {
      if (frame !== null) win.cancelAnimationFrame(frame);
      frame = null;
      point = null;
      lastTime = null;
      position = null;
      board?.classList.remove("is-drag-scrolling");
      board = null;
    }

    function edgeMotion() {
      if (!point || !board || getBoard() !== board || !isDragging()) return 0;
      const bounds = board.getBoundingClientRect();
      const left = Math.max(0, bounds.left);
      const right = Math.min(win.innerWidth, bounds.right);
      const bottom = Math.min(win.innerHeight, bounds.bottom);
      if (point.x < left || point.x > right || point.y < Math.max(0, bounds.top) || point.y > bottom) return 0;

      const edge = Math.min(64, (right - left) / 3);
      const max = board.scrollWidth - board.clientWidth;
      if (max <= 0 || edge <= 0) return 0;
      if (point.x < left + edge && board.scrollLeft > 0) return -(left + edge - point.x) / edge;
      if (point.x > right - edge && board.scrollLeft < max - 0.5) return (point.x - (right - edge)) / edge;
      return 0;
    }

    function tick(time) {
      frame = null;
      const motion = edgeMotion();
      if (!motion) { stop(); return; }
      const dt = lastTime === null ? 16.7 : Math.min(34, time - lastTime);
      const max = board.scrollWidth - board.clientWidth;
      const delta = Math.sign(motion) * (110 + 1150 * motion * motion) * dt / 1000;
      // Chrome exposes integer scroll positions. Preserve small frame deltas.
      position = Math.max(0, Math.min(max, (position ?? board.scrollLeft) + delta));
      board.scrollLeft = position;
      lastTime = time;
      frame = win.requestAnimationFrame(tick);
    }

    function update(event) {
      const current = isDragging() && getBoard();
      if (!current || !current.contains(event.target)) { stop(); return; }
      if (board !== current) { stop(); board = current; }
      point = {x: event.clientX, y: event.clientY};
      if (!edgeMotion()) { stop(); return; }
      if (frame === null) {
        board.classList.add("is-drag-scrolling");
        frame = win.requestAnimationFrame(tick);
      }
    }

    return {update, stop};
  };
});

