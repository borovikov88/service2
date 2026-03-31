# Cleanup Archive

Команда `cleanup_archive` предназначена для фоновой очистки архива.

Она удаляет навсегда только те записи, которые:
- уже находятся в архиве
- имеют причину архивации `deleted`
- старше заданного порога в днях

Команда не удаляет:
- записи с причиной `completed`
- активные записи
- записи, архивированные позже порога

## Ручной запуск

Проверка без удаления:

```powershell
python manage.py cleanup_archive --dry-run
```

Обычный запуск:

```powershell
python manage.py cleanup_archive
```

Другой порог хранения:

```powershell
python manage.py cleanup_archive --days 45
```

## Что выводит команда

`--dry-run`:

```text
Dry run: tasks=12, crm=4, threshold=01.03.2026 03:00
```

Обычный запуск:

```text
Удалено из архива: tasks=12, crm=4, threshold=01.03.2026 03:00
```

## Рекомендация по расписанию

Лучше запускать 1 раз в сутки ночью.

Рекомендуемое время:
- `03:00` по серверному времени

## Linux cron

Пример для Linux-сервера:

```bash
0 3 * * * /home/b/borovidz/service2.aqualine22.ru/venv/bin/python /home/b/borovidz/service2.aqualine22.ru/service_site/manage.py cleanup_archive >> /home/b/borovidz/service2.aqualine22.ru/service_site/logs/cleanup_archive.log 2>&1
```

Если хотите сначала безопасно проверить:

```bash
0 3 * * * /home/b/borovidz/service2.aqualine22.ru/venv/bin/python /home/b/borovidz/service2.aqualine22.ru/service_site/manage.py cleanup_archive --dry-run >> /home/b/borovidz/service2.aqualine22.ru/service_site/logs/cleanup_archive.log 2>&1
```

## Windows Task Scheduler

Если запускать на Windows:

Программа:

```text
python
```

Аргументы:

```text
manage.py cleanup_archive
```

Рабочая папка:

```text
d:\Service\RovikPool\service_site
```

Для безопасной проверки:

```text
manage.py cleanup_archive --dry-run
```

## Перед постановкой в расписание

Рекомендуемый порядок:

1. Один раз выполнить `--dry-run`
2. Проверить, что в выводе удаляются только `deleted`
3. Выполнить команду без `--dry-run`
4. Только после этого ставить в cron / scheduler

## Что учитывать на проде

- Команда не требует остановки приложения
- Команда безопасна для активных записей
- Если запись должна оставаться восстановимой, её нельзя переводить в `deleted`
- Для физического удаления через 30 дней запись должна быть именно архивной и именно с причиной `deleted`
