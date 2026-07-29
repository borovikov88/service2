import json
import hashlib
from urllib.parse import urlparse

from django.conf import settings
from django.utils import timezone
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url, options_to_json
from webauthn.helpers.structs import AuthenticatorTransport, PublicKeyCredentialDescriptor

from .models import WebAuthnCredential


SESSION_WEBAUTHN_REGISTRATION_CHALLENGE = "webauthn_registration_challenge"
SESSION_WEBAUTHN_AUTHENTICATION_CHALLENGE = "webauthn_authentication_challenge"


def rp_id_for_request(request):
    return request.get_host().split(":", 1)[0]


def origin_for_request(request):
    host = request.get_host()
    forwarded_proto = (request.headers.get("x-forwarded-proto") or "").split(",", 1)[0].strip()
    if forwarded_proto:
        return f"{forwarded_proto}://{host}"

    site_url = getattr(settings, "SITE_URL", "")
    if site_url:
        parsed = urlparse(site_url)
        if parsed.scheme and parsed.netloc and parsed.netloc == host:
            return f"{parsed.scheme}://{host}"

    local_hosts = {"localhost", "127.0.0.1", "0.0.0.0"}
    host_without_port = host.split(":", 1)[0]
    if getattr(settings, "DEBUG", False) and host_without_port in local_hosts:
        return f"{request.scheme}://{host}"

    return f"https://{host}"


def options_response(options):
    return json.loads(options_to_json(options))


def challenge_to_session(request, key, challenge):
    request.session[key] = bytes_to_base64url(challenge)
    request.session.modified = True


def challenge_from_session(request, key):
    challenge = request.session.get(key)
    if not challenge:
        return None
    return base64url_to_bytes(challenge)


def credential_id_to_text(credential_id):
    return bytes_to_base64url(credential_id)


def credential_id_to_bytes(credential_id):
    return base64url_to_bytes(credential_id)


def credential_id_hash(credential_id):
    return hashlib.sha256(credential_id.encode("utf-8")).hexdigest()


def credential_descriptor(credential):
    transports = None
    if credential.transports:
        transports = []
        for transport in credential.transports:
            try:
                transports.append(AuthenticatorTransport(transport))
            except ValueError:
                continue
    return PublicKeyCredentialDescriptor(id=credential_id_to_bytes(credential.credential_id), transports=transports)


def user_credential_descriptors(user):
    return [credential_descriptor(credential) for credential in WebAuthnCredential.objects.filter(user=user)]


def mark_credential_used(credential, sign_count):
    credential.sign_count = sign_count
    credential.last_used_at = timezone.now()
    credential.save(update_fields=["sign_count", "last_used_at"])
