"""Read current Autoload configuration; never persist private URLs or email."""
import hashlib
import json
import time

from django.utils import timezone
from pool_service import avito_autoload_report, avito_workspace
from pool_service.communication_avito import AvitoError

def profile_data(raw):
    if not isinstance(raw, dict) or type(raw.get("autoload_enabled")) is not bool:
        raise AvitoError("audit_profile_source_invalid")
    entries = raw.get("feeds_data")
    if entries is None:
        entries = []
    if not isinstance(entries, list) or len(entries) > 50:
        raise AvitoError("audit_profile_source_invalid")
    feeds = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("feed_url"), str) or len(entry["feed_url"]) > 2000:
            raise AvitoError("audit_profile_source_invalid")
        feeds.append({"url": entry["feed_url"]})
    # Compare private source values in-process only. The fingerprint is not output.
    fingerprint = hashlib.sha256(json.dumps([raw["autoload_enabled"], feeds],
                                            ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return {"complete": True, "autoload_enabled": raw["autoload_enabled"],
            "shop_feed_source": avito_autoload_report.shop_feed_source(feeds),
            "shop_feed_diagnostics": avito_autoload_report.feed_source_diagnostics(feeds)}, fingerprint

def fetch_profile_source(token, account_id):
    deadline = time.monotonic() + 10
    while True:
        try:
            avito_workspace.enforce_rate(account_id, "autoload", seconds=3)
            break
        except avito_workspace.AvitoCooldownError as error:
            wait = max(0.05, (error.retry_at - timezone.now()).total_seconds())
            if time.monotonic() + wait >= deadline:
                raise AvitoError("audit_provider_cooldown") from None
            time.sleep(min(wait, 3))
    # Official provider root is verified by the existing operator before calls.
    return profile_data(avito_workspace._get(token, "/autoload/v2/profile"))
