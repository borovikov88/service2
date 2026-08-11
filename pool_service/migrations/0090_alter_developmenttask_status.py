from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("pool_service", "0089_alter_notification_kind")]

    operations = [
        migrations.AlterField(
            model_name="developmenttask",
            name="status",
            field=models.CharField(
                choices=[
                    ("new", "Новая"), ("analysis", "Анализ"),
                    ("ready_for_codex", "Готова к передаче Codex"),
                    ("codex_working", "Codex работает"), ("testing", "Тестирование"),
                    ("review", "Проверка"), ("revision", "Требуется доработка"),
                    ("ready_for_deploy", "Готова к деплою"),
                    ("blocked", "Заблокирована"), ("done", "Выполнена"),
                    ("failed", "Не выполнена"), ("cancelled", "Отменена"),
                ],
                default="new", max_length=24, verbose_name="Статус",
            ),
        ),
    ]
