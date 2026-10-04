from urllib.parse import urlsplit, urlunsplit


MEGAFON_LEGACY_API_PATH = "/crmapi/v1"
MEGAFON_CANONICAL_API_PATH = "/sys/crm_api.wcgp"


def normalize_megafon_api_endpoint(value):
    """Repair the legacy MegaFon endpoint previously suggested by Service2."""
    raw = str(value or "").strip().rstrip("/")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        return raw

    hostname = (parsed.hostname or "").lower()
    is_megafon_host = hostname == "megapbx.ru" or hostname.endswith(".megapbx.ru")
    if (
        parsed.scheme == "https"
        and is_megafon_host
        and port in (None, 443)
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
        and parsed.path == MEGAFON_LEGACY_API_PATH
    ):
        return urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                MEGAFON_CANONICAL_API_PATH,
                "",
                "",
            )
        )
    return raw
