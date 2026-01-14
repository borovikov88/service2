from django.db import migrations, models
import uuid


def populate_phone_tokens(apps, schema_editor):
    Profile = apps.get_model("pool_service", "Profile")
    for profile in Profile.objects.all():
        profile.phone_verification_token = uuid.uuid4()
        profile.save(update_fields=["phone_verification_token"])


class Migration(migrations.Migration):

    dependencies = [
        ("pool_service", "0028_alter_organizationaccess_role_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="profile",
            name="phone_confirmed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="profile",
            name="phone_verification_required",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="profile",
            name="phone_verification_token",
            field=models.UUIDField(editable=False, null=True),
        ),
        migrations.AddField(
            model_name="profile",
            name="phone_verification_attempts",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="profile",
            name="phone_verification_started_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="profile",
            name="phone_verification_expires_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="profile",
            name="phone_verification_check_id",
            field=models.CharField(blank=True, max_length=50),
        ),
        migrations.AddField(
            model_name="profile",
            name="phone_verification_call_phone",
            field=models.CharField(blank=True, max_length=20),
        ),
        migrations.AddField(
            model_name="profile",
            name="phone_sms_code_hash",
            field=models.CharField(blank=True, max_length=128),
        ),
        migrations.AddField(
            model_name="profile",
            name="phone_sms_sent_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(populate_phone_tokens, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="profile",
            name="phone_verification_token",
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
    ]
