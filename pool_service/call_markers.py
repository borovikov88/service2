from collections import defaultdict
from datetime import timedelta

from django.db.models import Value
from django.db.models.functions import Replace

from .communication_models import PhoneCall
from .phone_utils import normalize_phone


def annotate_missed_call_callbacks(calls, organization_id):
    """Add display-only missed/callback markers to a bounded set of calls."""
    missed_by_phone = defaultdict(list)
    for call in calls:
        call.is_missed = call.result == PhoneCall.RESULT_MISSED
        call.callback_at = None
        call.missed_unreturned = call.is_missed
        if call.is_missed:
            phone_key = normalize_phone(call.phone_number)
            digits = "".join(char for char in phone_key if char.isdigit())
            if len(digits) >= 7:
                missed_by_phone[phone_key].append(call)

    for phone_key, missed_calls in missed_by_phone.items():
        digits = "".join(char for char in phone_key if char.isdigit())
        phone_digits = "phone_number"
        for separator in ("+", " ", "-", "(", ")", "."):
            phone_digits = Replace(phone_digits, Value(separator), Value(""))
        earliest = min(call.started_at for call in missed_calls)
        latest = max(call.started_at + timedelta(hours=1) for call in missed_calls)
        candidates = PhoneCall.objects.filter(
            organization_id=organization_id,
            direction=PhoneCall.DIRECTION_OUT,
            result=PhoneCall.RESULT_ANSWERED,
            started_at__gt=earliest,
            started_at__lte=latest,
        ).annotate(_phone_digits=phone_digits).filter(
            _phone_digits__contains=digits[-7:],
        ).order_by("started_at", "pk").values_list("phone_number", "started_at")

        for callback_phone, callback_at in candidates.iterator(chunk_size=200):
            if normalize_phone(callback_phone) != phone_key:
                continue
            for missed_call in missed_calls:
                if (
                    missed_call.callback_at is None
                    and missed_call.started_at < callback_at <= missed_call.started_at + timedelta(hours=1)
                ):
                    missed_call.callback_at = callback_at
                    missed_call.missed_unreturned = False

