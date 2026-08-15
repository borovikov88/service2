from io import BytesIO

from openpyxl import Workbook


def _xlsx(rows):
    workbook = Workbook()
    sheet = workbook.active
    for values, indent in rows:
        sheet.append(values)
        sheet.cell(sheet.max_row, 1).alignment = sheet.cell(sheet.max_row, 1).alignment.copy(
            indent=indent
        )
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def payroll_xlsx(*, accrued_delta=0):
    delta = accrued_delta
    rows = [
        (("Основное подразделение", "", 30, 300 + delta, 240, 90 + delta), 0),
        (("Иванов Иван Иванович", "001", 10, 100 + delta, 80, 30 + delta), 1),
        (("12.2024", "", 5, 40, 30, 15), 2),
        (("01.2025", "", 5, 60 + delta, 50, 15 + delta), 2),
        (("Петров Пётр Петрович", "002", 20, 200, 160, 60), 1),
        (("12.2024", "", 10, 80, 60, 30), 2),
        (("01.2025", "", 10, 120, 100, 30), 2),
    ]
    return _xlsx(rows)


def cashflow_xlsx(*, payment_delta=0):
    delta = payment_delta
    rows = [
        (("Заработная плата", 10, 100 + delta, -90 - delta), 0),
        (("01.2025", 10, 100 + delta, -90 - delta), 1),
        (("Платёжное поручение 1", 0, 60 + delta, -60 - delta), 2),
        (("Возврат сотрудника", 10, 0, 10), 2),
        (("Платёжное поручение 2", 0, 40, -40), 2),
        (("Внутреннее перемещение", 50, 50, 0), 0),
        (("02.2025", 50, 50, 0), 1),
        (("Перевод между счетами", 50, 50, 0), 2),
    ]
    return _xlsx(rows)
