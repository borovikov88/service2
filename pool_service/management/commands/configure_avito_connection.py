import os
import secrets

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from pool_service.communication_models import AvitoCredential, ChannelConnection
from pool_service.communication_secrets import encrypt_secret


class Command(BaseCommand):
    help = "Configure encrypted Avito API credentials and rotate the webhook bearer token."

    def add_arguments(self, parser):
        parser.add_argument("connection_id", type=int)
        parser.add_argument("--client-id-env", default="AVITO_CLIENT_ID")
        parser.add_argument("--client-secret-env", default="AVITO_CLIENT_SECRET")

    @transaction.atomic
    def handle(self, *args, **options):
        client_id = os.getenv(options["client_id_env"], "")
        client_secret = os.getenv(options["client_secret_env"], "")
        if not client_id or not client_secret:
            raise CommandError("Переменные окружения с client_id и client_secret не заданы.")
        try:
            connection = ChannelConnection.objects.select_related("channel").get(
                pk=options["connection_id"], channel__kind="avito",
            )
        except ChannelConnection.DoesNotExist as exc:
            raise CommandError("Подключение Авито с таким ID не найдено.") from exc
        credential, _ = AvitoCredential.objects.get_or_create(
            connection=connection,
            defaults={
                "client_id_encrypted": encrypt_secret(client_id),
                "client_secret_encrypted": encrypt_secret(client_secret),
            },
        )
        credential.client_id_encrypted = encrypt_secret(client_id)
        credential.client_secret_encrypted = encrypt_secret(client_secret)
        credential.access_token_encrypted = ""
        credential.access_token_expires_at = None
        credential.save()
        webhook_token = secrets.token_urlsafe(32)
        connection.set_api_token(webhook_token)
        connection.save(update_fields=["api_token_hash"])
        self.stdout.write(f"public_id={connection.public_id}")
        self.stdout.write(f"webhook_token={webhook_token}")
        self.stderr.write("Сохраните webhook_token сейчас: повторно он показан не будет.")
