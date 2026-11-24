import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("pool_service", "0203_max_media_and_contact")]

    operations = [
        migrations.CreateModel(
            name="CustomerReminderPolicy",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("last_tick_at", models.DateTimeField(blank=True, editable=False, null=True)),
                ("last_digest_on", models.DateField(blank=True, editable=False, null=True)),
                ("organization", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, to="pool_service.organization")),
                ("duty_manager", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="+", to=settings.AUTH_USER_MODEL)),
                ("supervisor", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="+", to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.CreateModel(
            name="MissedCallFollowup",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("next_attempt_at", models.DateTimeField(blank=True, null=True)),
                ("resolved_at", models.DateTimeField(blank=True, null=True)),
                ("note", models.CharField(blank=True, max_length=250)),
                ("actor", models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
                ("call", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, to="pool_service.phonecall")),
            ],
        ),
        migrations.CreateModel(
            name="CustomerReminderReceipt",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("work_key", models.CharField(max_length=80)),
                ("step", models.CharField(max_length=12)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="pool_service.organization")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "constraints": [models.UniqueConstraint(fields=("organization", "user", "work_key", "step"), name="customer_reminder_work_step")],
            },
        ),
    ]
