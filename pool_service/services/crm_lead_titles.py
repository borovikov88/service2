"""Automatic call titles; explicitly edited lead names are preserved."""

LEGACY_CALL_TITLE = "Входящий звонок — требуется уточнение"


def call_title(phone, missed=False):
    label = "Пропущенный звонок" if missed else "Входящий звонок"
    return (label + (" · " + phone.strip() if phone else " · номер не определён"))[:255]


def automatic_call_title(title, phone, missed=False):
    if title in {LEGACY_CALL_TITLE, call_title(phone), call_title(phone, True)}:
        return call_title(phone, missed)
    return title
