from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    dependencies = [
        ("pool_service", "0032_client_access_invite"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="organizationaccess",
            constraint=models.UniqueConstraint(
                fields=("organization",),
                condition=Q(role="owner"),
                name="unique_owner_per_org",
            ),
        ),
    ]
