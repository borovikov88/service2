from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
import re

from openpyxl import load_workbook

from .employee_matching import normalize_onec_name
from .monthly_profit_parser import parse_month
from .validators import validate_xlsx_archive


PARSER_VERSION = "1"
MAX_ROWS = 100_000
CONTROL_TOLERANCE = Decimal("0.01")


class PayrollParseError(ValueError):
    pass


@dataclass
class PayrollParseResult:
    records: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    critical_errors: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


def _text(value):
    return re.sub(r"\s+", " ", str(value or "").replace("\u00a0", " ")).strip()


def _decimal(value):
    if value in (None, "", "-", "—"):
        return Decimal("0.00")
    if isinstance(value, bool):
        raise PayrollParseError("Логическое значение не является суммой.")
    try:
        return Decimal(str(value).replace(" ", "").replace(",", ".")).quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise PayrollParseError(f"Не удалось распознать сумму: {value!r}") from exc


def _totals(values):
    return tuple(_decimal(value) for value in values)


def _different(left, right):
    return any(abs(a - b) > CONTROL_TOLERANCE for a, b in zip(left, right))


def _normalized_header(value):
    return _text(value).casefold().replace("ё", "е")


def _validate_header(sheet):
    expected = {
        "A1": "подразделение",
        "A2": "сотрудник",
        "B2": "период регистрации",
        "C1": "нач. остаток",
        "D1": "начислено",
        "E1": "выплачено",
        "F1": "кон. остаток",
    }
    for coordinate, marker in expected.items():
        if _normalized_header(sheet[coordinate].value) != marker:
            raise PayrollParseError(
                f"Некорректный заголовок payroll: ожидалось {coordinate}={marker!r}."
            )
    merged = {str(cell_range) for cell_range in sheet.merged_cells.ranges}
    required_merged = {"A1:B1", "C1:C2", "D1:D2", "E1:E2", "F1:F2"}
    if not required_merged.issubset(merged):
        raise PayrollParseError("Некорректная merged-структура заголовка payroll.")
    return 3


def parse_payroll(file_obj, *, filename=None, size=None):
    validate_xlsx_archive(file_obj, filename=filename, size=size)
    workbook = load_workbook(file_obj, read_only=False, data_only=True)
    if len(workbook.worksheets) != 1:
        raise PayrollParseError("Отчёт по расчётам с персоналом должен содержать один лист.")
    sheet = workbook.worksheets[0]
    if sheet.max_row > MAX_ROWS or sheet.max_column != 6:
        raise PayrollParseError("Неожиданная размерность отчёта по расчётам с персоналом.")
    data_start = _validate_header(sheet)

    result = PayrollParseResult()
    department = ""
    department_control = None
    department_records = []

    def validate_department():
        if department and department_control is not None:
            actual = tuple(sum((row[key] for row in department_records), Decimal("0")) for key in (
                "opening_balance", "accrued", "paid", "closing_balance"
            ))
            if _different(department_control, actual):
                result.critical_errors.append(f"Контрольные итоги подразделения не совпадают: {department}.")

    for row_number, row in enumerate(
        sheet.iter_rows(min_row=data_start, max_col=6), data_start
    ):
        label = _text(row[0].value)
        if not label:
            continue
        indent = int(row[0].alignment.indent or 0)
        values = [cell.value for cell in row[2:6]]
        if indent == 0:
            if _text(row[1].value):
                raise PayrollParseError(f"Некорректная строка подразделения, строка {row_number}.")
            validate_department()
            department, department_control = label, _totals(values)
            department_records = []
        elif indent == 2:
            employee = label
            period = parse_month(row[1].value, workbook_epoch=workbook.epoch)
            if not department or not employee or not isinstance(period, date):
                raise PayrollParseError(f"Некорректная Employee/Period строка {row_number}.")
            amounts = _totals(values)
            record = {
                "period_month": period,
                "source_row_number": row_number,
                "department_name": department,
                "employee_raw_name": employee,
                "employee_normalized_name": normalize_onec_name(employee),
                "personnel_number": "",
                "opening_balance": amounts[0],
                "accrued": amounts[1],
                "paid": amounts[2],
                "closing_balance": amounts[3],
                "source_data": {"hierarchy_level": "period"},
            }
            result.records.append(record)
            department_records.append(record)
        else:
            raise PayrollParseError(f"Неизвестный уровень иерархии, строка {row_number}.")
    validate_department()
    if not result.records:
        raise PayrollParseError("В отчёте не найдены строки Employee/Period.")
    periods = sorted({row["period_month"] for row in result.records})
    result.metadata = {
        "format": "department_employee_period_v1",
        "period_first": periods[0].isoformat(),
        "period_last": periods[-1].isoformat(),
        "row_count": len(result.records),
        "control_totals": {
            key: str(sum((row[key] for row in result.records), Decimal("0")))
            for key in ("opening_balance", "accrued", "paid", "closing_balance")
        },
    }
    return result
