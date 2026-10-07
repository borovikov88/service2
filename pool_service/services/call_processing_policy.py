"""Pure, metadata-only call policy evaluation. Never reads audio or calls AI.

The settings UI is a preparation/preview surface. Nothing in this module starts
processing, changes call visibility, or enables the existing analysis worker.
All known employee participants must permit a call; any personal exclusion wins.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from typing import Mapping

from pool_service.phone_utils import normalize_phone

MANUAL = "manual"
ALL_EXCEPT = "all_except"
ALLOWLIST = "allowlist"
MODES = frozenset({MANUAL, ALL_EXCEPT, ALLOWLIST})
MAX_NUMBERS = 500
MAX_NUMBER_INPUT = 20000
_PHONE_INPUT = re.compile(r"^\+?[0-9 ()\t.\-]+$")


def phone_key(value: str | None) -> str:
    """Use CRM keys, but reject extensions, names and malformed pasted input."""
    value = str(value or "").strip()
    if len(value) > 40 or not _PHONE_INPUT.fullmatch(value):
        return ""
    return normalize_phone(value)


def parse_numbers(text: str) -> tuple[str, ...]:
    if not isinstance(text, str) or len(text) > MAX_NUMBER_INPUT:
        raise ValueError("number_list_size")
    result = set()
    for line in re.split(r"[\r\n;,]+", text):
        if not line.strip():
            continue
        key = phone_key(line)
        if not key:
            raise ValueError("invalid_phone")
        result.add(key)
        if len(result) > MAX_NUMBERS:
            raise ValueError("number_list_size")
    return tuple(sorted(result))


@dataclass(frozen=True)
class Rule:
    mode: str = MANUAL
    include_staff: bool = True
    work_numbers: frozenset[str] = frozenset()
    effective_from: datetime | None = None


@dataclass(frozen=True)
class CallFacts:
    participants: tuple[int, ...]
    counterpart_numbers: frozenset[str]
    started_at: datetime
    verified: bool = False
    source: str = "telephony"
    direction: str = "in"
    answered: bool = True
    duration_seconds: int = 0
    audio_ready: bool = False
    analysis_status: str = ""
    saved_transcript: bool = False


@dataclass(frozen=True)
class Decision:
    action: str
    reason: str

    @property
    def selected(self) -> bool:
        return self.action in {"transcribe", "resume_analysis", "wait_audio"}


def decide_call(
    call: CallFacts,
    rules: Mapping[int, Rule],
    personal_numbers: Mapping[int, frozenset[str]],
    *,
    simulation: bool = False,
) -> Decision:
    """Simulation ignores effective_from only, never privacy or identity checks.

Returned permission is a policy proposal, not authorization to invoke a paid
provider. A production worker also needs revalidation, budgets and dispatch.
"""
    participants = tuple(dict.fromkeys(call.participants))
    # Run before every other outcome, including "already ready", to avoid
    # leaking a private call's analysis state through the preview.
    if any(personal_numbers.get(uid, frozenset()) & call.counterpart_numbers for uid in participants):
        return Decision("exclude", "personal")
    if call.source != "telephony":
        return Decision("exclude", "manual_upload")
    if call.direction not in {"in", "out", "internal"}:
        return Decision("exclude", "direction_unknown")
    if not participants or not call.verified:
        return Decision("exclude", "mapping_required")
    if call.direction == "internal" and len(participants) != 2:
        return Decision("exclude", "mapping_required")
    if not call.answered or call.duration_seconds <= 0:
        return Decision("exclude", "no_conversation")
    if call.direction != "internal" and not call.counterpart_numbers:
        return Decision("exclude", "number_unknown")
    # A hidden/extension-only counterpart cannot bypass a personal deny-list.
    if not call.counterpart_numbers and any(personal_numbers.get(uid) for uid in participants):
        return Decision("exclude", "privacy_mapping_required")
    for uid in participants:
        rule = rules.get(uid)
        if rule is None or rule.mode == MANUAL:
            return Decision("exclude", "manual")
        if rule.mode not in MODES:
            return Decision("exclude", "invalid_rule")
        if not simulation:
            effective = rule.effective_from
            if (
                effective is None or effective.utcoffset() is None
                or call.started_at.utcoffset() is None
            ):
                return Decision("exclude", "activation_required")
            if call.started_at < effective:
                return Decision("exclude", "historical")
        if rule.mode == ALLOWLIST:
            staff_match = rule.include_staff and len(participants) == 2
            number_match = bool(rule.work_numbers & call.counterpart_numbers)
            if not (staff_match or number_match):
                return Decision("exclude", "not_allowed")
    if call.analysis_status == "ready":
        return Decision("exclude", "already_ready")
    if call.analysis_status == "processing":
        return Decision("exclude", "already_processing")
    if call.saved_transcript:
        return Decision("resume_analysis", "saved_transcript")
    if not call.audio_ready:
        return Decision("wait_audio", "audio_pending")
    return Decision("transcribe", "rules_match")
