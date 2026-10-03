import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from pool_service.communication_secrets import (
    CommunicationSecretError,
    decrypt_secret,
)


MAX_RESPONSE_BYTES = 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 10


class MegafonApiError(Exception):
    pass


def validate_megafon_api_url(value):
    raw = str(value or "").strip().rstrip("/")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise MegafonApiError("provider_invalid_url") from exc
    hostname = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or not hostname
        or (hostname != "megapbx.ru" and not hostname.endswith(".megapbx.ru"))
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or port not in (None, 443)
    ):
        raise MegafonApiError("provider_invalid_url")
    return raw


def _credentials(connection):
    settings_data = connection.settings or {}
    api_url = settings_data.get("megafon_api_base_url", "")
    encrypted_key = settings_data.get("megafon_api_key_encrypted", "")
    if not api_url or not encrypted_key:
        raise MegafonApiError("provider_credentials_missing")
    api_url = validate_megafon_api_url(api_url)
    try:
        api_key = decrypt_secret(encrypted_key)
    except CommunicationSecretError as exc:
        raise MegafonApiError("provider_credentials_unreadable") from exc
    if not api_key:
        raise MegafonApiError("provider_credentials_missing")
    return api_url, api_key


def _request(connection, command, *, payload=None, expect_json=True):
    api_url, api_key = _credentials(connection)
    body = {
        "cmd": command,
        "token": api_key,
    }
    if payload:
        body.update(payload)
    request = Request(
        api_url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
            status = getattr(response, "status", 200)
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        raise MegafonApiError(f"provider_http_{exc.code}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise MegafonApiError("provider_unavailable") from exc

    if status != 200:
        raise MegafonApiError(f"provider_http_{status}")
    if len(raw) > MAX_RESPONSE_BYTES:
        raise MegafonApiError("provider_response_too_large")
    if not expect_json:
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MegafonApiError("provider_invalid_json") from exc
    return result


def accounts(connection):
    result = _request(connection, "accounts")
    if not isinstance(result, list):
        raise MegafonApiError("provider_invalid_accounts")
    accounts_list = []
    for item in result:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "") or "").strip()
        extension = str(item.get("ext", "") or "").strip()
        if not extension:
            continue
        accounts_list.append({
            "name": name[:255],
            "ext": extension[:255],
        })
        if len(accounts_list) >= 500:
            break
    return accounts_list


def make_call(connection, *, user, phone):
    extension = str(user or "").strip()
    destination = str(phone or "").strip()
    if not extension or len(extension) > 255:
        raise MegafonApiError("invalid_user")
    if not destination or len(destination) > 40:
        raise MegafonApiError("invalid_phone")
    return _request(
        connection,
        "makeCall",
        payload={"user": extension, "phone": destination},
        expect_json=False,
    )
