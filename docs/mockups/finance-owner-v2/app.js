"use strict";

const DEMO_TODAY = new Date(2026, 7, 25);
const CURRENT_MONTH_KEY = "2026-08";
const REPORTS = {
  profit: { name: "Валовая прибыль", href: "/finance/1c-profit/" },
  payroll: { name: "Фонд оплаты труда", href: "/finance/payroll/" },
  cashflow: { name: "Движение денег", href: "/finance/1c-cashflow/" },
  cost: { name: "Контроль себестоимости", href: "/finance/1c-imports/cost-control/" },
};

const monthNames = ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"];
const monthNamesLong = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"];
const money = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 0 });
const oneDecimal = new Intl.NumberFormat("ru-RU", { minimumFractionDigits: 1, maximumFractionDigits: 1 });

const state = {
  preset: "current_month",
  start: "2026-08",
  end: "2026-08",
  seasonMetric: "grossProfit",
};

function key(year, monthIndex) {
  return `${year}-${String(monthIndex + 1).padStart(2, "0")}`;
}

function parseKey(value) {
  const [year, month] = value.split("-").map(Number);
  return { year, monthIndex: month - 1 };
}

function shiftMonth(value, delta) {
  const { year, monthIndex } = parseKey(value);
  const date = new Date(year, monthIndex + delta, 1);
  return key(date.getFullYear(), date.getMonth());
}

function monthRange(start, end) {
  const values = [];
  let current = start;
  while (current <= end && values.length < 60) {
    values.push(current);
    current = shiftMonth(current, 1);
  }
  return values;
}

function buildDemoData() {
  const seasonal = [0.72, 0.76, 0.89, 1.02, 1.13, 1.22, 1.15, 0.91, 1.04, 1.09, 0.84, 0.67];
  const margin = [0.682, 0.689, 0.704, 0.716, 0.729, 0.735, 0.721, 0.708, 0.714, 0.722, 0.701, 0.686];
  const rows = [];

  for (let year = 2025; year <= 2026; year += 1) {
    const lastMonth = year === 2026 ? 7 : 11;
    for (let monthIndex = 0; monthIndex <= lastMonth; monthIndex += 1) {
      const trend = 1 + (year - 2024) * 0.09 + monthIndex * 0.004;
      let revenue = 5_000_000 * seasonal[monthIndex] * trend;
      let grossProfit = revenue * (margin[monthIndex] + (year - 2024) * 0.004);
      let payroll = 1_510_000 * (1 + (year - 2024) * 0.07 + monthIndex * 0.0025);
      let receipts = revenue * (0.91 + ((monthIndex % 4) - 1) * 0.045);
      let payments = 2_420_000 * seasonal[(monthIndex + 1) % 12] * trend + payroll * 0.86;

      if (year === 2026 && monthIndex === 7) {
        revenue = 5_375_756;
        grossProfit = 3_920_374;
        payroll = 1_768_400;
        receipts = 4_426_900;
        payments = 4_781_300;
      }

      rows.push({
        key: key(year, monthIndex),
        year,
        monthIndex,
        revenue: Math.round(revenue),
        grossProfit: Math.round(grossProfit),
        payroll: Math.round(payroll),
        receipts: Math.round(receipts),
        payments: Math.round(payments),
        netCash: Math.round(receipts - payments),
        missingCostRows: year === 2026 && monthIndex === 7 ? 18 : (year === 2025 && monthIndex === 10 ? 4 : 0),
        dataStatus: year === 2026 && monthIndex === 7 ? "partial" : "closed",
      });
    }
  }
  return rows;
}

const demoData = buildDemoData();
const byKey = new Map(demoData.map((row) => [row.key, row]));

function rowsBetween(start, end) {
  return monthRange(start, end).map((value) => byKey.get(value)).filter(Boolean);
}

function total(rows, field) {
  return rows.reduce((sum, row) => sum + row[field], 0);
}

function aggregate(rows) {
  const revenue = total(rows, "revenue");
  const grossProfit = total(rows, "grossProfit");
  const payroll = total(rows, "payroll");
  const receipts = total(rows, "receipts");
  const payments = total(rows, "payments");
  return {
    revenue,
    grossProfit,
    payroll,
    receipts,
    payments,
    netCash: receipts - payments,
    grossMargin: revenue ? (grossProfit / revenue) * 100 : null,
    payrollToGrossProfit: grossProfit ? (payroll / grossProfit) * 100 : null,
    payrollToRevenue: revenue ? (payroll / revenue) * 100 : null,
    missingCostRows: total(rows, "missingCostRows"),
  };
}

function presetRange(name) {
  const currentYear = DEMO_TODAY.getFullYear();
  switch (name) {
    case "current_month": return { start: "2026-08", end: "2026-08" };
    case "previous_month": return { start: "2026-07", end: "2026-07" };
    case "current_year": return { start: `${currentYear}-01`, end: "2026-08" };
    case "previous_year": return { start: "2025-01", end: "2025-12" };
    case "last_12": return { start: "2025-09", end: "2026-08" };
    case "off_season": return { start: "2025-11", end: "2026-03" };
    default: return { start: state.start, end: state.end };
  }
}

function comparisonRange() {
  const length = monthRange(state.start, state.end).length;
  const end = shiftMonth(state.start, -1);
  const start = shiftMonth(end, -(length - 1));
  return { start, end };
}

function monthLabel(value) {
  const { year, monthIndex } = parseKey(value);
  return `${monthNamesLong[monthIndex]} ${year}`;
}

function rangeLabel(start, end) {
  if (start === end) return monthLabel(start);
  const first = parseKey(start);
  const last = parseKey(end);
  if (first.year === last.year) return `${monthNames[first.monthIndex]}–${monthNames[last.monthIndex]} ${first.year}`;
  return `${monthNames[first.monthIndex]} ${first.year} – ${monthNames[last.monthIndex]} ${last.year}`;
}

function compactMoney(value) {
  const absolute = Math.abs(value);
  if (absolute >= 1_000_000) return `${oneDecimal.format(value / 1_000_000)} млн ₽`;
  if (absolute >= 1_000) return `${oneDecimal.format(value / 1_000)} тыс. ₽`;
  return `${money.format(value)} ₽`;
}

function fullMoney(value) {
  return `${money.format(Math.round(value))} ₽`;
}

function percentage(value) {
  return value == null ? "Нет данных" : `${oneDecimal.format(value)}%`;
}

function deltaInfo(current, previous, format, polarity, preliminary) {
  if (current == null || previous == null || !Number.isFinite(current) || !Number.isFinite(previous)) {
    return { html: "Нет полного сопоставимого периода", className: "neutral" };
  }
  const difference = current - previous;
  const pct = previous === 0 ? null : (difference / Math.abs(previous)) * 100;
  const arrow = difference > 0 ? "↑" : difference < 0 ? "↓" : "→";
  let className = "neutral";

  if (!preliminary && difference !== 0) {
    if (polarity === "higher") className = difference > 0 ? "good" : "bad";
    if (polarity === "lower") className = difference > 0 ? "bad" : "neutral";
    if (polarity === "cash") className = current < 0 ? "bad" : (difference > 0 ? "good" : "neutral");
  }

  const pctText = pct == null ? "без процентного сравнения" : `${pct > 0 ? "+" : ""}${oneDecimal.format(pct)}%`;
  const prefix = difference > 0 ? "+" : "";
  return {
    html: `${arrow} ${prefix}${format(difference)}<small>${pctText}${preliminary ? " · без цветовой оценки" : ""}</small>`,
    className,
  };
}

function cardMarkup(card, current, previous, comparisonLabel, preliminary, hasComparablePeriod) {
  const value = card.getValue(current);
  const previousValue = card.getValue(previous);
  const delta = deltaInfo(value, previousValue, card.deltaFormat, card.polarity, preliminary);
  const specialClass = card.key === "netCash" ? (value < 0 ? "net-negative" : "net-positive") : "";
  const comparisonMarkup = hasComparablePeriod
    ? `<span class="kpi-context">
        <span class="comparison-label">Сравнимый период<strong>${comparisonLabel}</strong></span>
        <span class="delta ${delta.className}">${delta.html}</span>
      </span>`
    : '<span class="kpi-context unavailable"><span class="comparison-unavailable">Нет полного сопоставимого периода</span></span>';
  return `
    <a class="kpi-card report-link ${specialClass}" href="${card.report.href}" data-report="${card.report.name}">
      <span class="kpi-label">${card.label}</span>
      <strong class="kpi-value ${card.isPercent ? "percent" : ""}">${card.format(value)}</strong>
      ${preliminary ? '<span class="preliminary-badge">Предварительно</span>' : ""}
      ${comparisonMarkup}
    </a>`;
}

const economyDefinitions = [
  { key: "revenue", label: "Выручка", report: REPORTS.profit, getValue: (x) => x.revenue, format: fullMoney, deltaFormat: compactMoney, polarity: "higher" },
  { key: "grossProfit", label: "Валовая прибыль", report: REPORTS.profit, getValue: (x) => x.grossProfit, format: fullMoney, deltaFormat: compactMoney, polarity: "higher" },
  { key: "grossMargin", label: "Валовая рентабельность", report: REPORTS.profit, getValue: (x) => x.grossMargin, format: percentage, deltaFormat: (x) => `${oneDecimal.format(x)} п.п.`, polarity: "higher", isPercent: true },
  { key: "payroll", label: "Начисленный ФОТ", report: REPORTS.payroll, getValue: (x) => x.payroll, format: fullMoney, deltaFormat: compactMoney, polarity: "lower" },
  { key: "payrollToGrossProfit", label: "ФОТ / валовая прибыль", report: REPORTS.payroll, getValue: (x) => x.payrollToGrossProfit, format: percentage, deltaFormat: (x) => `${oneDecimal.format(x)} п.п.`, polarity: "lower", isPercent: true },
  { key: "payrollToRevenue", label: "ФОТ / выручка", report: REPORTS.payroll, getValue: (x) => x.payrollToRevenue, format: percentage, deltaFormat: (x) => `${oneDecimal.format(x)} п.п.`, polarity: "lower", isPercent: true },
];

const cashDefinitions = [
  { key: "receipts", label: "Поступления", report: REPORTS.cashflow, getValue: (x) => x.receipts, format: fullMoney, deltaFormat: compactMoney, polarity: "higher" },
  { key: "payments", label: "Платежи", report: REPORTS.cashflow, getValue: (x) => x.payments, format: fullMoney, deltaFormat: compactMoney, polarity: "lower" },
  { key: "netCash", label: "Чистый денежный поток", report: REPORTS.cashflow, getValue: (x) => x.netCash, format: fullMoney, deltaFormat: compactMoney, polarity: "cash" },
];

function renderCards(rows, comparisonRows, preliminary) {
  const current = aggregate(rows);
  const hasComparablePeriod = comparisonRows.length === rows.length;
  const previous = hasComparablePeriod ? aggregate(comparisonRows) : {
    revenue: null,
    grossProfit: null,
    grossMargin: null,
    payroll: null,
    payrollToGrossProfit: null,
    payrollToRevenue: null,
    receipts: null,
    payments: null,
    netCash: null,
  };
  const comparison = comparisonRange();
  const label = hasComparablePeriod ? rangeLabel(comparison.start, comparison.end) : "";
  document.querySelector("#economyCards").innerHTML = economyDefinitions.map((card) => cardMarkup(card, current, previous, label, preliminary, hasComparablePeriod)).join("");
  document.querySelector("#cashCards").innerHTML = cashDefinitions.map((card) => cardMarkup(card, current, previous, label, preliminary, hasComparablePeriod)).join("");
}

function linePath(values, width, height, padding, min, max) {
  const usableWidth = width - padding * 2;
  const usableHeight = height - padding * 2;
  return values.map((value, index) => {
    const x = padding + (values.length === 1 ? usableWidth / 2 : (index / (values.length - 1)) * usableWidth);
    const y = padding + ((max - value) / Math.max(max - min, 1)) * usableHeight;
    return { x, y, value };
  });
}

function lineChartSvg(rows, field, color) {
  const width = 620;
  const height = 88;
  const padding = 10;
  const values = rows.map((row) => row[field]);
  const min = Math.min(...values) * 0.94;
  const max = Math.max(...values) * 1.04;
  const points = linePath(values, width, height, padding, min, max);
  const line = points.map((point, index) => `${index ? "L" : "M"}${point.x.toFixed(1)},${point.y.toFixed(1)}`).join(" ");
  const area = `${line} L${points.at(-1).x},${height - padding} L${points[0].x},${height - padding} Z`;
  const labels = points.map((point, index) => {
    if (rows.length > 8 && index % 2 && index !== rows.length - 1) return "";
    return `<text class="axis-label" x="${point.x}" y="${height + 2}" text-anchor="middle">${monthNames[rows[index].monthIndex]}</text>`;
  }).join("");
  const circles = points.map((point, index) => `<circle class="point ${rows[index].key === CURRENT_MONTH_KEY ? "current-point" : ""}" cx="${point.x}" cy="${point.y}" r="${rows[index].key === CURRENT_MONTH_KEY ? 3.5 : 2.5}" fill="${color}"></circle>`).join("");
  return `<svg class="chart-svg" viewBox="0 0 ${width} ${height + 8}" role="img" aria-label="Динамика по месяцам">
    <line class="grid-line" x1="${padding}" y1="${padding + 15}" x2="${width - padding}" y2="${padding + 15}"></line>
    <line class="grid-line" x1="${padding}" y1="${height - padding}" x2="${width - padding}" y2="${height - padding}"></line>
    <path class="area-fill" d="${area}" fill="${color}"></path><path class="series-line" d="${line}" stroke="${color}"></path>${circles}${labels}
  </svg>`;
}

function renderEconomyCharts(rows) {
  const definitions = [
    { field: "revenue", label: "Выручка", color: "#1769aa" },
    { field: "grossProfit", label: "Валовая прибыль", color: "#118b88" },
    { field: "payroll", label: "Начисленный ФОТ", color: "#8a6cab" },
  ];
  document.querySelector("#economyCharts").innerHTML = definitions.map((definition) => `
    <div class="mini-chart">
      <div class="mini-chart-head"><span>${definition.label}</span><strong>${compactMoney(rows.at(-1)[definition.field])}</strong></div>
      ${lineChartSvg(rows, definition.field, definition.color)}
    </div>`).join("");
}

function cashflowSvg(rows) {
  const width = 500;
  const height = 310;
  const left = 38;
  const right = 12;
  const top = 16;
  const bottom = 35;
  const all = rows.flatMap((row) => [row.receipts, row.payments]);
  const max = Math.max(...all) * 1.12;
  const xStep = (width - left - right) / Math.max(rows.length, 1);
  const moneyY = (value) => top + (1 - value / max) * 172;
  const netMax = Math.max(...rows.map((row) => Math.abs(row.netCash)), 1) * 1.18;
  const baseline = 226;
  const netScale = 62 / netMax;
  const receiptPoints = rows.map((row, index) => ({ x: left + xStep * (index + 0.5), y: moneyY(row.receipts) }));
  const paymentPoints = rows.map((row, index) => ({ x: left + xStep * (index + 0.5), y: moneyY(row.payments) }));
  const path = (points) => points.map((point, index) => `${index ? "L" : "M"}${point.x.toFixed(1)},${point.y.toFixed(1)}`).join(" ");
  const bars = rows.map((row, index) => {
    const x = left + xStep * (index + 0.28);
    const barWidth = Math.max(xStep * 0.44, 4);
    const barHeight = Math.abs(row.netCash) * netScale;
    const y = row.netCash >= 0 ? baseline - barHeight : baseline;
    return `<rect x="${x}" y="${y}" width="${barWidth}" height="${barHeight}" rx="2" fill="${row.netCash >= 0 ? "#278257" : "#bd4b56"}"></rect>`;
  }).join("");
  const labels = rows.map((row, index) => {
    if (rows.length > 8 && index % 2 && index !== rows.length - 1) return "";
    const x = left + xStep * (index + 0.5);
    return `<text class="axis-label" x="${x}" y="${height - 8}" text-anchor="middle">${monthNames[row.monthIndex]}</text>`;
  }).join("");
  return `<svg class="chart-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="Поступления, платежи и чистый денежный поток">
    <line class="grid-line" x1="${left}" y1="${moneyY(max * 0.75)}" x2="${width - right}" y2="${moneyY(max * 0.75)}"></line>
    <line class="grid-line" x1="${left}" y1="${moneyY(max * 0.25)}" x2="${width - right}" y2="${moneyY(max * 0.25)}"></line>
    <path class="series-line" d="${path(receiptPoints)}" stroke="#1769aa"></path>
    <path class="series-line" d="${path(paymentPoints)}" stroke="#8a9aae"></path>
    ${receiptPoints.map((p) => `<circle class="point" cx="${p.x}" cy="${p.y}" r="2.5" fill="#1769aa"></circle>`).join("")}
    ${paymentPoints.map((p) => `<circle class="point" cx="${p.x}" cy="${p.y}" r="2.5" fill="#8a9aae"></circle>`).join("")}
    <line x1="${left}" y1="${baseline}" x2="${width - right}" y2="${baseline}" stroke="#9aa9b8" stroke-width="1"></line>
    ${bars}${labels}
  </svg>`;
}

function renderCashflowChart(rows) {
  document.querySelector("#cashflowChart").innerHTML = cashflowSvg(rows);
}

function seasonalitySvg(metric) {
  const current = demoData.filter((row) => row.year === 2026);
  const previous = demoData.filter((row) => row.year === 2025 && row.monthIndex <= 7);
  const hasFullComparablePeriod = current.length >= 2
    && previous.length === current.length
    && current.every((row, index) => previous[index]?.monthIndex === row.monthIndex);
  if (!hasFullComparablePeriod) {
    return '<div class="empty-state"><div><strong>Нет полного сопоставимого периода</strong>Истории недостаточно для корректного сравнения.</div></div>';
  }
  const width = 930;
  const height = 220;
  const padding = 28;
  const values = [...current.map((row) => row[metric]), ...previous.map((row) => row[metric])];
  const min = metric === "netCash" ? Math.min(0, ...values) : Math.min(...values) * 0.9;
  const max = Math.max(...values) * 1.08;
  const currentPoints = linePath(current.map((row) => row[metric]), width, height, padding, min, max);
  const previousPoints = linePath(previous.map((row) => row[metric]), width, height, padding, min, max);
  const path = (points) => points.map((point, index) => `${index ? "L" : "M"}${point.x.toFixed(1)},${point.y.toFixed(1)}`).join(" ");
  const labels = currentPoints.map((point, index) => `<text class="axis-label" x="${point.x}" y="${height - 4}" text-anchor="middle">${monthNames[index]}</text>`).join("");
  const zeroY = padding + ((max - 0) / Math.max(max - min, 1)) * (height - padding * 2);
  return `<div class="legend"><span><i class="legend-dot" style="background:#1769aa"></i>2026</span><span><i class="legend-dot" style="background:#a8b5c2"></i>2025</span></div>
    <svg class="chart-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="Сезонность за 2026 и 2025 годы">
      <line class="grid-line" x1="${padding}" y1="${padding}" x2="${width - padding}" y2="${padding}"></line>
      <line class="grid-line" x1="${padding}" y1="${height - padding}" x2="${width - padding}" y2="${height - padding}"></line>
      ${metric === "netCash" && min < 0 ? `<line x1="${padding}" y1="${zeroY}" x2="${width - padding}" y2="${zeroY}" stroke="#a9b6c2"></line>` : ""}
      <path class="series-line" d="${path(previousPoints)}" stroke="#a8b5c2"></path>
      <path class="series-line" d="${path(currentPoints)}" stroke="#1769aa"></path>
      ${previousPoints.map((p) => `<circle class="point" cx="${p.x}" cy="${p.y}" r="2.5" fill="#a8b5c2"></circle>`).join("")}
      ${currentPoints.map((p, index) => `<circle class="point ${index === 7 ? "current-point" : ""}" cx="${p.x}" cy="${p.y}" r="${index === 7 ? 3.8 : 2.8}" fill="#1769aa"></circle>`).join("")}
      ${labels}
    </svg>`;
}

function renderSeasonality() {
  document.querySelector("#seasonalityChart").innerHTML = seasonalitySvg(state.seasonMetric);
}

function signalMarkup(type, title, text, report, icon) {
  return `<a class="signal ${type} report-link" href="${report.href}" data-report="${report.name}">
    <span class="signal-icon">${icon}</span><span class="signal-copy"><strong>${title}</strong><span>${text}</span></span><span class="signal-link">Открыть отчёт ↗</span>
  </a>`;
}

function renderSignals(rows) {
  const current = aggregate(rows);
  const signals = [];
  if (current.netCash < 0) {
    signals.push(signalMarkup("critical", "Отрицательный чистый денежный поток", `${rangeLabel(state.start, state.end)}: ${fullMoney(current.netCash)}.`, REPORTS.cashflow, "−"));
  }
  if (current.missingCostRows > 0) {
    signals.push(signalMarkup("warning", "Продажи без определённой себестоимости", `${current.missingCostRows} строк требуют проверки источника себестоимости.`, REPORTS.cost, "!"));
  }
  if (!signals.length) {
    signals.push(signalMarkup("ok", "Объективных сигналов нет", "В выбранном периоде нет отрицательного потока, пропусков данных или неопределённой себестоимости.", REPORTS.profit, "✓"));
  }
  document.querySelector("#signalList").innerHTML = signals.join("");
}

function bindReportLinks() {
  document.querySelectorAll(".report-link").forEach((link) => {
    if (link.dataset.bound === "true") return;
    link.dataset.bound = "true";
    link.addEventListener("click", (event) => {
      event.preventDefault();
      showToast(`Макет перехода: ${link.dataset.report}`);
    });
  });
}

let toastTimer;
function showToast(message) {
  const toast = document.querySelector("#toast");
  toast.textContent = message;
  toast.classList.add("visible");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("visible"), 2200);
}

function render() {
  const rows = rowsBetween(state.start, state.end);
  const comparison = comparisonRange();
  const comparisonRows = rowsBetween(comparison.start, comparison.end);
  const preliminary = rows.some((row) => row.key === CURRENT_MONTH_KEY);
  document.querySelector("#periodSummary").innerHTML = `${rangeLabel(state.start, state.end)}<small>${rows.length} ${rows.length === 1 ? "месяц" : rows.length < 5 ? "месяца" : "месяцев"} · полные календарные месяцы</small>`;
  document.querySelector("#preliminaryNote").hidden = !preliminary;
  renderCards(rows, comparisonRows, preliminary);
  renderEconomyCharts(rows);
  renderCashflowChart(rows);
  renderSeasonality();
  renderSignals(rows);
  bindReportLinks();
}

document.querySelector("#presetStrip").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-preset]");
  if (!button) return;
  const preset = button.dataset.preset;
  document.querySelectorAll(".preset").forEach((item) => item.classList.toggle("active", item === button));
  document.querySelector("#customPeriod").hidden = preset !== "custom";
  if (window.matchMedia("(max-width: 760px)").matches) button.closest("details")?.removeAttribute("open");
  if (preset === "custom") return;
  state.preset = preset;
  Object.assign(state, presetRange(preset));
  render();
});

document.querySelector("#customPeriod").addEventListener("submit", (event) => {
  event.preventDefault();
  const start = document.querySelector("#customStart").value;
  const end = document.querySelector("#customEnd").value;
  if (!start || !end || start > end) {
    showToast("Укажите корректный период полными месяцами");
    return;
  }
  state.preset = "custom";
  state.start = start;
  state.end = end;
  render();
});

document.querySelector("#seasonTabs").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-metric]");
  if (!button) return;
  state.seasonMetric = button.dataset.metric;
  document.querySelectorAll(".metric-tab").forEach((item) => item.classList.toggle("active", item === button));
  renderSeasonality();
});

document.querySelector("#mobileMenuButton").addEventListener("click", () => {
  document.querySelector(".sidebar").classList.toggle("open");
});

document.querySelector(".sidebar").addEventListener("click", (event) => {
  if (event.target.closest("a") && window.innerWidth <= 760) document.querySelector(".sidebar").classList.remove("open");
});

const mobilePeriodsQuery = window.matchMedia("(max-width: 760px)");
function syncMorePeriodsControl(event) {
  document.querySelector(".more-periods").toggleAttribute("open", !event.matches);
}
syncMorePeriodsControl(mobilePeriodsQuery);
mobilePeriodsQuery.addEventListener("change", syncMorePeriodsControl);

render();
