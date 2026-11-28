"""Organization mailboxes and their IMAP archive; importing a message never creates a CRM card."""

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from .storage import private_media_storage


class CrmMailbox(models.Model):
    class Provider(models.TextChoices):
        MAIL = "mailru", "Mail.ru / VK WorkSpace"
        YANDEX = "yandex", "Яндекс Почта"
        GOOGLE = "gmail", "Gmail / Google Workspace"
        CUSTOM = "custom", "Другой провайдер"

    organization = models.ForeignKey("pool_service.Organization", on_delete=models.PROTECT, related_name="mailboxes")
    address = models.EmailField(unique=True)
    provider = models.CharField(max_length=12, choices=Provider.choices, default=Provider.MAIL)
    login = models.CharField(max_length=254, blank=True)
    imap_host = models.CharField(max_length=253, default="imap.mail.ru")
    imap_port = models.PositiveSmallIntegerField(default=993)
    smtp_host = models.CharField(max_length=253, default="smtp.mail.ru")
    smtp_port = models.PositiveSmallIntegerField(default=465)
    config_version = models.PositiveIntegerField(default=0)
    last_attempt_at = models.DateTimeField(null=True)
    credential = models.TextField(editable=False)
    enabled = models.BooleanField(default=True)
    connected_at = models.DateTimeField(default=timezone.now)
    last_sync_at = models.DateTimeField(null=True)
    sync_error = models.CharField(max_length=100, blank=True)
    sync_token = models.UUIDField(null=True, editable=False)
    lease_until = models.DateTimeField(null=True, editable=False)

    @property
    def incoming_account(self):
        return self.imap_host, self.imap_port, self.login or self.address


class MailFolder(models.Model):
    mailbox = models.ForeignKey(CrmMailbox, on_delete=models.CASCADE, related_name="folders")
    key = models.CharField(max_length=64)
    wire_name = models.CharField(max_length=1024)
    name = models.CharField(max_length=500)
    role = models.CharField(max_length=12, blank=True)
    available = models.BooleanField(default=True)
    generation = models.PositiveBigIntegerField(default=0)
    latest_uid = models.PositiveBigIntegerField(default=0)
    backfill_uid = models.PositiveBigIntegerField(default=0)
    audit_uid = models.PositiveBigIntegerField(default=0)
    scanned_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["mailbox", "key"], name="mail_folder_key")]


class MailLetter(models.Model):
    mailbox = models.ForeignKey(CrmMailbox, on_delete=models.CASCADE, related_name="letters")
    fingerprint = models.CharField(max_length=64)
    message_id = models.CharField(max_length=998, blank=True)
    message_key = models.CharField(max_length=64, blank=True, db_index=True)
    reference_ids = models.JSONField(default=list)
    sender = models.CharField(max_length=254, blank=True)
    sender_name = models.CharField(max_length=255, blank=True)
    recipients = models.JSONField(default=list)
    copies = models.JSONField(default=list)
    reply_to = models.EmailField(blank=True)
    subject = models.CharField(max_length=998, blank=True)
    sent_at = models.DateTimeField(db_index=True)
    outgoing = models.BooleanField(default=False)
    historical = models.BooleanField(default=False)
    commercial = models.BooleanField(default=False)
    needs_attention = models.BooleanField(default=False)
    processed_at = models.DateTimeField(null=True)
    body_loaded = models.BooleanField(default=False)
    body = models.TextField(blank=True)
    attachments = models.JSONField(default=list)
    lead = models.ForeignKey(
        "pool_service.CrmLead", null=True, on_delete=models.PROTECT, related_name="mail_letters"
    )
    deal = models.ForeignKey(
        "pool_service.CrmDeal", null=True, on_delete=models.PROTECT, related_name="mail_letters"
    )
    link_kind = models.CharField(max_length=12, blank=True)
    linked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    revision = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["mailbox", "fingerprint"], name="mail_letter_identity"),
            models.CheckConstraint(
                condition=Q(lead__isnull=True) | Q(deal__isnull=True), name="mail_one_card"
            ),
        ]
        indexes = [models.Index(fields=["mailbox", "sent_at"], name="mail_chronological")]


class MailPeer(models.Model):
    letter = models.ForeignKey(MailLetter, on_delete=models.CASCADE, related_name="peers")
    address = models.CharField(max_length=254, db_index=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["letter", "address"], name="mail_peer_once")]


class MailLocation(models.Model):
    folder = models.ForeignKey(MailFolder, on_delete=models.CASCADE)
    letter = models.ForeignKey(MailLetter, on_delete=models.CASCADE, related_name="locations")
    generation = models.PositiveBigIntegerField()
    uid = models.PositiveBigIntegerField()
    size = models.PositiveBigIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["folder", "generation", "uid"], name="mail_imap_uid")]


class MailSubmission(models.Model):
    """Once SMTP has been attempted a browser retry must never transmit the message again."""

    token = models.UUIDField(primary_key=True, editable=False)
    mailbox = models.ForeignKey(CrmMailbox, on_delete=models.PROTECT)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=16, default="attempting")
    letter = models.ForeignKey(MailLetter, null=True, on_delete=models.PROTECT)
    attachment_source = models.FileField(
        storage=private_media_storage, upload_to="crm_mail/outbound", blank=True
    )
