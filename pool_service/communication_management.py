import secrets

from django.db import transaction

from pool_service.communication_models import (
    AvitoCredential,
    ChannelConnection,
    CommunicationChannel,
)
from pool_service.communication_secrets import encrypt_secret


DEFAULT_CHANNEL_NAMES = {
    CommunicationChannel.KIND_WEBSITE: "Сайт",
    CommunicationChannel.KIND_AVITO: "Авито",
    CommunicationChannel.KIND_MEGAFON: "Мегафон",
}


def get_or_create_provider_channel(organization, kind):
    if kind not in DEFAULT_CHANNEL_NAMES:
        raise ValueError("Unsupported communication channel kind")
    channel = (
        CommunicationChannel.objects.filter(organization=organization, kind=kind)
        .order_by("pk")
        .first()
    )
    if channel is not None:
        return channel
    return CommunicationChannel.objects.create(
        organization=organization,
        kind=kind,
        name=DEFAULT_CHANNEL_NAMES[kind],
    )


def rotate_connection_token(connection):
    token = secrets.token_urlsafe(32)
    connection.set_api_token(token)
    connection.save(update_fields=["api_token_hash"])
    return token


@transaction.atomic
def create_connection(*, organization, kind, name, external_id):
    channel = get_or_create_provider_channel(organization, kind)
    return ChannelConnection.objects.create(
        channel=channel,
        name=name,
        external_id=external_id,
    )


@transaction.atomic
def configure_avito_credentials(*, connection, client_id, client_secret):
    if connection.channel.kind != CommunicationChannel.KIND_AVITO:
        raise ValueError("Avito credentials require an Avito connection")
    encrypted_client_id = encrypt_secret(client_id)
    encrypted_client_secret = encrypt_secret(client_secret)
    credential, _ = AvitoCredential.objects.get_or_create(
        connection=connection,
        defaults={
            "client_id_encrypted": encrypted_client_id,
            "client_secret_encrypted": encrypted_client_secret,
        },
    )
    credential.client_id_encrypted = encrypted_client_id
    credential.client_secret_encrypted = encrypted_client_secret
    credential.access_token_encrypted = ""
    credential.access_token_expires_at = None
    credential.save()
    return credential
