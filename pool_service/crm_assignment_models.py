"""One notification delivery receipt per immutable CRM assignment event."""
from django.conf import settings
from django.db import models


class CrmAssignmentDelivery(models.Model):
    event = models.OneToOneField('CrmEvent', on_delete=models.PROTECT, related_name='assignment_delivery')
    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    notification = models.ForeignKey('Notification', null=True, on_delete=models.SET_NULL)
    push_requested = models.BooleanField(default=False)
    push_attempted_at = models.DateTimeField(null=True)
