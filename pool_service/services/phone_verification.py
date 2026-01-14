import json
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.conf import settings


def _smsru_request(path, params):
    api_id = getattr(settings, "SMS_RU_API_ID", "")
    if not api_id:
        return {"ok": False, "error": "SMS_RU_API_ID not configured"}

    payload = dict(params or {})
    payload["api_id"] = api_id
    payload["json"] = 1
    url = f"https://sms.ru/{path}?{urlencode(payload)}"
    req = Request(url, headers={"User-Agent": "RovikPool/1.0"})
    try:
        with urlopen(req, timeout=getattr(settings, "SMS_RU_TIMEOUT", 8)) as response:
            raw = response.read().decode("utf-8", errors="ignore")
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"ok": False, "error": "Invalid response", "raw": raw}
    return data


def smsru_callcheck_add(phone):
    data = _smsru_request("callcheck/add", {"phone": phone})
    if data.get("status") != "OK":
        return {
            "ok": False,
            "error": data.get("status_text") or "SMS.RU error",
            "data": data,
        }
    return {
        "ok": True,
        "check_id": data.get("check_id"),
        "call_phone": data.get("call_phone"),
        "call_phone_pretty": data.get("call_phone_pretty"),
    }


def smsru_callcheck_status(check_id):
    data = _smsru_request("callcheck/status", {"check_id": check_id})
    if data.get("status") != "OK":
        return {
            "ok": False,
            "error": data.get("status_text") or "SMS.RU error",
            "data": data,
        }
    return {
        "ok": True,
        "check_status": data.get("check_status"),
        "check_status_text": data.get("check_status_text"),
    }


def smsru_send_sms(phone, text):
    data = _smsru_request("sms/send", {"to": phone, "msg": text})
    if data.get("status") != "OK":
        return {
            "ok": False,
            "error": data.get("status_text") or "SMS.RU error",
            "data": data,
        }
    return {"ok": True, "data": data}
