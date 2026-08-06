from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from openpyxl import load_workbook
from openpyxl.utils.datetime import from_excel

from .validators import validate_xlsx_archive

PARSER_VERSION = "1"
MAX_SHEETS = 10
MAX_ROWS = 100_000
MAX_COLUMNS = 500
HEADER_SCAN_ROWS = 120
MAX_TOTAL_CELLS = 5_000_000
PARSER_WARNING_BUFFER = 200
MAX_SOURCE_CELLS = 50
MAX_SOURCE_VALUE_LENGTH = 500
MAX_FORMULA_CELLS = 10_000

MONTHS = {
    "январь": 1, "янв": 1, "февраль": 2, "фев": 2, "март": 3, "мар": 3,
    "апрель": 4, "апр": 4, "май": 5, "июнь": 6, "июн": 6, "июль": 7,
    "июл": 7, "август": 8, "авг": 8, "сентябрь": 9, "сент": 9, "сен": 9,
    "октябрь": 10, "окт": 10, "ноябрь": 11, "ноя": 11, "декабрь": 12, "дек": 12,
}
METRICS = {
    "profitability_percent": ("% прибыли", "процент прибыли", "рентабельность", "рентаб"),
    "quantity": ("количество", "кол-во", "кол во"),
    "revenue": ("выручка", "продажа", "сумма продаж"),
    "cost": ("себестоимость", "себест"),
    "gross_profit": ("сумма валовой прибыли", "валовая прибыль"),
}
NAME_MARKERS = ("номенклатура", "наименование", "товар", "продукция")
ARTICLE_MARKERS = ("артикул", "код")
TOTAL_MARKERS = ("итого", "всего", "общий итог")


class MonthlyProfitParseError(ValueError):
    pass


@dataclass
class ParseResult:
    records: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    warnings_total: int = 0
    critical_errors: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    rows_read: int = 0
    rows_skipped: int = 0

    def add_warning(self, message):
        self.warnings_total += 1
        if len(self.warnings) < PARSER_WARNING_BUFFER:
            self.warnings.append(str(message))


def _text(value):
    return "" if value is None else re.sub(r"\s+", " ", str(value).replace("\u00a0", " ")).strip()


def parse_decimal(value, *, percent=False):
    del percent  # Percent values are stored as percentage points, not fractions.
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("Логическое значение не является числом.")
    if isinstance(value, (int, Decimal)):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    raw = _text(value)
    if not raw or raw in {"-", "—", "–"}:
        return None
    negative = raw.startswith("(") and raw.endswith(")")
    if negative:
        raw = raw[1:-1].strip()
    raw = raw.replace("%", "").replace(" ", "").replace("'", "")
    if "," in raw and "." in raw:
        raw = raw.replace(".", "").replace(",", ".") if raw.rfind(",") > raw.rfind(".") else raw.replace(",", "")
    elif "," in raw:
        raw = raw.replace(",", ".") if raw.count(",") == 1 else raw.replace(",", "")
    try:
        result = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"Не удалось распознать число: {value!r}") from exc
    return -result if negative else result


DECIMAL_RULES = {
    "quantity": (Decimal("0.000001"), Decimal("1e14")),
    "revenue": (Decimal("0.01"), Decimal("1e18")),
    "cost": (Decimal("0.01"), Decimal("1e18")),
    "gross_profit": (Decimal("0.01"), Decimal("1e18")),
    "profitability_percent": (Decimal("0.0001"), Decimal("1e8")),
}


def normalize_metric_decimal(value, metric):
    if value is None:
        return None
    quantum, limit = DECIMAL_RULES[metric]
    if abs(value) >= limit:
        raise ValueError("Число превышает допустимую точность.")
    try:
        return value.quantize(quantum, rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise ValueError("Число превышает допустимую точность.") from exc


def parse_month(value, *, fallback_year=None, workbook_epoch=None):
    if isinstance(value, (date, datetime)):
        return date(value.year, value.month, 1)
    if isinstance(value, (int, float)) and workbook_epoch is not None:
        try:
            converted = from_excel(value, workbook_epoch)
            if isinstance(converted, (date, datetime)) and 2000 <= converted.year <= 2100:
                return date(converted.year, converted.month, 1)
        except (TypeError, ValueError, OverflowError):
            pass
    normalized = _text(value).lower().replace("ё", "е")
    match = re.search(r"\b(20\d{2})[-/.](0?[1-9]|1[0-2])\b", normalized)
    if match:
        return date(int(match.group(1)), int(match.group(2)), 1)
    match = re.search(r"\b(0?[1-9]|1[0-2])[./-](20\d{2})\b", normalized)
    if match:
        return date(int(match.group(2)), int(match.group(1)), 1)
    month_text = re.sub(r"[.]", "", normalized)
    for name, month in sorted(MONTHS.items(), key=lambda item: -len(item[0])):
        if re.search(rf"\b{re.escape(name)}\b", month_text):
            year_match = re.search(r"\b(20\d{2})\b", month_text)
            year = int(year_match.group(0)) if year_match else fallback_year
            return date(year, month, 1) if year else (None, month)
    return None


def _metric(text):
    normalized = _text(text).lower().replace("ё", "е")
    for key, markers in METRICS.items():
        if any(marker in normalized for marker in markers):
            return key
    return None


def _json_value(value):
    if value is None or isinstance(value, (int, bool)):
        return value
    if isinstance(value, str):
        return value[:MAX_SOURCE_VALUE_LENGTH]
    if isinstance(value, (float, Decimal)):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)[:MAX_SOURCE_VALUE_LENGTH]


def _detect_report_year(rows):
    years = []
    for row in rows[:60]:
        years.extend(int(item) for item in re.findall(r"\b20\d{2}\b", " ".join(_text(v) for v in row)))
    return years[0] if years and len(set(years)) == 1 else None


def _find_header(rows, year, epoch):
    best = None
    for start in range(min(len(rows), HEADER_SCAN_ROWS)):
        for depth in (1, 2, 3):
            block = rows[start:start + depth]
            width = max((len(row) for row in block), default=0)
            columns, name_col, article_col = [], None, None
            for col in range(width):
                pieces = [_text(row[col]) if col < len(row) else "" for row in block]
                combined = " ".join(filter(None, pieces))
                lowered = combined.lower().replace("ё", "е")
                if name_col is None and any(marker in lowered for marker in NAME_MARKERS): name_col = col
                if article_col is None and any(marker in lowered for marker in ARTICLE_MARKERS): article_col = col
                month = next((parse_month(piece, fallback_year=year, workbook_epoch=epoch) for piece in pieces if parse_month(piece, fallback_year=year, workbook_epoch=epoch)), None)
                columns.append({"index": col, "month": month, "metric": _metric(combined)})
            last_month = None
            for column in columns:
                if column["month"]: last_month = column["month"]
                elif last_month and column["metric"]: column["month"] = last_month
            metric_columns = [column for column in columns if column["metric"] and column["month"]]
            score = len(metric_columns) * 10 + (5 if name_col is not None else 0)
            if metric_columns and (best is None or score > best[0]):
                best = (score, start, depth, columns, name_col, article_col)
    if not best:
        raise MonthlyProfitParseError("Не удалось найти заголовки месяцев и показателей.")
    _, start, depth, columns, name_col, article_col = best
    if name_col is None:
        used = {column["index"] for column in columns if column["metric"]}
        name_col = next((index for index in range(max(used or {0}) + 1) if index not in used), 0)
    return start, depth, columns, name_col, article_col


def _is_total_label(value):
    normalized = _text(value).lower().replace("ё", "е")
    return bool(re.match(r"^(?:общий\s+итог|итого|всего)(?:\s*$|\s+(?:по|за)\b|\s*:)", normalized))


def _parse_sheet(sheet, epoch, formula_cells=None, formulas_truncated=False):
    if sheet.max_column and sheet.max_column > MAX_COLUMNS:
        raise MonthlyProfitParseError(f"Лист «{sheet.title}» содержит более {MAX_COLUMNS} колонок.")
    rows = []
    for number, row in enumerate(sheet.iter_rows(values_only=True), 1):
        if number > MAX_ROWS: raise MonthlyProfitParseError("Превышен лимит строк.")
        rows.append(tuple(row[:MAX_COLUMNS]))
    year = _detect_report_year(rows)
    start, depth, columns, name_col, article_col = _find_header(rows, year, epoch)
    result = ParseResult()
    unresolved = [c for c in columns if c["metric"] and isinstance(c["month"], tuple)]
    if unresolved: result.critical_errors.append("Найден месяц без года, а год отчёта определить не удалось.")
    metric_columns = [c for c in columns if c["metric"] and isinstance(c["month"], date)]
    months = sorted({c["month"] for c in metric_columns})
    for excel_row, row in enumerate(rows[start + depth:], start + depth + 1):
        result.rows_read += 1
        name = _text(row[name_col] if name_col < len(row) else "")
        if not name or _is_total_label(name):
            result.rows_skipped += 1; continue
        article = _text(row[article_col] if article_col is not None and article_col < len(row) else "")
        by_month = {}
        for column in metric_columns:
            raw = row[column["index"]] if column["index"] < len(row) else None
            try:
                value = normalize_metric_decimal(
                    parse_decimal(raw, percent=column["metric"] == "profitability_percent"),
                    column["metric"],
                )
            except ValueError:
                value = None
                result.add_warning(f"Строка {excel_row}: неверное число в показателе {column['metric']}.")
            by_month.setdefault(column["month"], {})[column["metric"]] = value
        if not any(any(v is not None for v in values.values()) for values in by_month.values()):
            result.rows_skipped += 1; continue
        source = {
            str(i + 1): _json_value(v)
            for i, v in enumerate(row[:MAX_SOURCE_CELLS])
            if v is not None
        }
        for month, values in sorted(by_month.items()):
            if not any(v is not None for v in values.values()): continue
            result.records.append({
                "period_month": month, "source_row_number": excel_row,
                "nomenclature": name[:500], "article": article[:120],
                "quantity": values.get("quantity"), "revenue": values.get("revenue"),
                "cost": values.get("cost"), "gross_profit": values.get("gross_profit"),
                "profitability_percent": values.get("profitability_percent"),
                "source_data": {"excel_row_number": excel_row, "cells": source},
            })
    for row_number, column_number in formula_cells or ():
        cached_value = (
            rows[row_number - 1][column_number - 1]
            if row_number <= len(rows) and column_number <= len(rows[row_number - 1])
            else None
        )
        if cached_value is None:
            result.add_warning(
                f"Ячейка R{row_number}C{column_number} содержит формулу без сохранённого значения."
            )
    if formulas_truncated:
        result.add_warning("Часть формул не перечислена из-за лимита диагностических ячеек.")
    if not result.records:
        result.critical_errors.append("В отчёте не обнаружено строк с числовыми показателями.")
    result.metadata = {
        "sheet": sheet.title, "header_row": start + 1, "header_depth": depth,
        "report_year": year, "months": [m.isoformat() for m in months], "month_count": len(months),
        "rows_read": result.rows_read, "rows_skipped": result.rows_skipped,
    }
    return result


def parse_monthly_profit(file_obj, *, filename="report.xlsx", size=None):
    validate_xlsx_archive(file_obj, size=size, filename=filename)
    file_obj.seek(0)
    try:
        formula_workbook = load_workbook(file_obj, read_only=True, data_only=False, keep_links=False)
    except Exception as exc:
        raise MonthlyProfitParseError("Не удалось безопасно открыть XLSX.") from exc
    try:
        if len(formula_workbook.sheetnames) > MAX_SHEETS:
            raise MonthlyProfitParseError(f"Количество листов превышает лимит {MAX_SHEETS}.")
        declared_rows = sum(min(sheet.max_row or 0, MAX_ROWS + 1) for sheet in formula_workbook.worksheets)
        if declared_rows > MAX_ROWS:
            raise MonthlyProfitParseError(f"Суммарное количество строк превышает лимит {MAX_ROWS}.")
        declared_cells = sum((sheet.max_row or 0) * (sheet.max_column or 0) for sheet in formula_workbook.worksheets)
        if declared_cells > MAX_TOTAL_CELLS:
            raise MonthlyProfitParseError("Количество ячеек превышает безопасный лимит.")
        formula_cells, formula_truncated = {}, {}
        for sheet in formula_workbook.worksheets:
            cells, truncated = set(), False
            for row in sheet.iter_rows():
                for cell in row:
                    if cell.data_type != "f":
                        continue
                    if len(cells) < MAX_FORMULA_CELLS:
                        cells.add((cell.row, cell.column))
                    else:
                        truncated = True
            formula_cells[sheet.title] = cells
            formula_truncated[sheet.title] = truncated
    finally:
        formula_workbook.close()
        file_obj.seek(0)
    try:
        workbook = load_workbook(file_obj, read_only=True, data_only=True, keep_links=False)
    except Exception as exc:
        raise MonthlyProfitParseError("Не удалось безопасно открыть XLSX.") from exc
    try:
        candidates, errors = [], []
        for sheet in workbook.worksheets:
            try:
                candidates.append(_parse_sheet(
                    sheet,
                    workbook.epoch,
                    formula_cells.get(sheet.title),
                    formula_truncated.get(sheet.title, False),
                ))
            except MonthlyProfitParseError as exc: errors.append(f"{sheet.title}: {exc}")
        if not candidates:
            raise MonthlyProfitParseError("Подходящий лист не найден. " + "; ".join(errors[:3]))
        result = max(candidates, key=lambda item: len(item.records))
        result.metadata["detected_sheets"] = list(workbook.sheetnames)
        return result
    finally:
        workbook.close()
        file_obj.seek(0)
