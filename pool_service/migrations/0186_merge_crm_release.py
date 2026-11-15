"""Join the deployed 1C review history and the prepared CRM release unchanged."""
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("pool_service", "0182_merge_onec_review_crm_contact"),
        ("pool_service", "0185_crm_failure_stage"),
    ]

    operations = []
