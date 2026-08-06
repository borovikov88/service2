# pool-service

## Локальная разработка

Безопасная настройка отдельной локальной MySQL описана в
[docs/local-development.md](docs/local-development.md). Не используйте для
локального запуска доступ к продакшен-базе.

Ручной импорт XLSX-отчёта 1С «Валовая прибыль по месяцам» описан в
[docs/1c-monthly-profit-import.md](docs/1c-monthly-profit-import.md).

## Архив и автоочистка

В проекте есть команда `cleanup_archive`, которая удаляет навсегда только архивные записи с причиной `deleted`, если они старше указанного числа дней.

По умолчанию команда:
- очищает архив задач `ServiceTask`
- очищает архив CRM-записей `CrmItem`
- не трогает записи с причиной `completed`
- использует порог `30` дней

Быстрая проверка без удаления:

```powershell
python manage.py cleanup_archive --dry-run
```

Обычный запуск:

```powershell
python manage.py cleanup_archive
```

Запуск с другим порогом:

```powershell
python manage.py cleanup_archive --days 45
```

Подробная инструкция и готовые примеры для расписания:

- [docs/archive_cleanup.md](docs/archive_cleanup.md)
