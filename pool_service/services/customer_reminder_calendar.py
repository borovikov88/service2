from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo


BARNAUL = ZoneInfo("Asia/Barnaul")
OPEN = time(9)
CLOSE = time(18)
SUMMARY = time(9, 35)


def _next_open(day):
    day += timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return datetime.combine(day, OPEN, BARNAUL)


def previous_workday_open(moment):
    day = moment.astimezone(BARNAUL).date() - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return datetime.combine(day, OPEN, BARNAUL)


def summary_ready(moment, last_digest_on):
    local = moment.astimezone(BARNAUL)
    return local.weekday() < 5 and SUMMARY <= local.time() < CLOSE and last_digest_on != local.date()


def working_start(moment):
    local = moment.astimezone(BARNAUL)
    if local.weekday() >= 5 or local.time() >= CLOSE:
        return _next_open(local.date())
    return max(local, datetime.combine(local.date(), OPEN, BARNAUL))


def working_deadline(moment, minutes):
    start = working_start(moment)
    closing = datetime.combine(start.date(), CLOSE, BARNAUL)
    duration = timedelta(minutes=minutes)
    remaining = closing - start
    if duration < remaining:
        return start + duration
    return _next_open(start.date()) + (duration - remaining)


def reminder_step(started_at, now):
    local = now.astimezone(BARNAUL)
    if local.weekday() >= 5 or not OPEN <= local.time() < CLOSE:
        return None
    # All pending work has the same digest boundary, after any overnight carryover.
    morning_day = _next_open(working_start(started_at).date()).date()
    morning = datetime.combine(morning_day, SUMMARY, BARNAUL)
    if local >= morning:
        return "morning"
    if local >= working_deadline(started_at, 30):
        return "30"
    if local >= working_deadline(started_at, 5):
        return "5"
    return None
