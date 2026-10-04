import json
import logging
import re
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone
from openai import OpenAI

from pool_service.communication_models import CallAnalysis, PhoneCall


logger = logging.getLogger(__name__)

DEFAULT_TRANSCRIPTION_MODEL = "gpt-4o-transcribe-diarize"
DEFAULT_ANALYSIS_MODEL = "gpt-5.6-luna"
DEFAULT_MAX_ATTEMPTS = 5
PROCESSING_STALE_MINUTES = 30


class CallAnalysisError(RuntimeError):
    pass


def _setting(name, default):
    return getattr(settings, name, default)


def _client():
    api_key = (_setting("OPENAI_API_KEY", "") or "").strip()
    if not api_key:
        raise CallAnalysisError("openai_api_key_missing")
    timeout = float(_setting("OPENAI_CALL_TIMEOUT_SECONDS", 120))
    return OpenAI(api_key=api_key, timeout=timeout, max_retries=1)


def _claim(call_id, *, force=False):
    now = timezone.now()
    stale_before = now - timedelta(minutes=PROCESSING_STALE_MINUTES)
    max_attempts = int(_setting("OPENAI_CALL_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS))

    with transaction.atomic():
        call = (
            PhoneCall.objects.select_for_update()
            .filter(pk=call_id, recording_file__isnull=False)
            .exclude(recording_file="")
            .first()
        )
        if call is None:
            return None

        analysis, _ = CallAnalysis.objects.select_for_update().get_or_create(call=call)
        if not force:
            if analysis.status == CallAnalysis.STATUS_READY:
                return None
            if analysis.attempts >= max_attempts:
                return None
            if (
                analysis.status == CallAnalysis.STATUS_PROCESSING
                and analysis.processing_started_at
                and analysis.processing_started_at >= stale_before
            ):
                return None

        analysis.status = CallAnalysis.STATUS_PROCESSING
        analysis.error = ""
        analysis.attempts = F("attempts") + 1
        analysis.processing_started_at = now
        analysis.processed_at = None
        analysis.save(
            update_fields=[
                "status",
                "error",
                "attempts",
                "processing_started_at",
                "processed_at",
                "updated_at",
            ]
        )
        analysis.refresh_from_db()
        return analysis.pk


def _speaker_transcript(result):
    segments = getattr(result, "segments", None) or []
    rendered = []
    for segment in segments:
        speaker = getattr(segment, "speaker", None)
        text = (getattr(segment, "text", "") or "").strip()
        if not text:
            continue
        rendered.append(f"{speaker or 'Спикер'}: {text}")
    if rendered:
        return "\n".join(rendered)
    return (getattr(result, "text", "") or "").strip()


def _transcribe(client, call):
    model = _setting("OPENAI_CALL_TRANSCRIPTION_MODEL", DEFAULT_TRANSCRIPTION_MODEL)
    try:
        call.recording_file.open("rb")
        kwargs = {
            "model": model,
            "file": call.recording_file.file,
        }
        if model == "gpt-4o-transcribe-diarize":
            kwargs.update(
                response_format="diarized_json",
                chunking_strategy="auto",
            )
        result = client.audio.transcriptions.create(**kwargs)
    finally:
        try:
            call.recording_file.close()
        except Exception:
            pass

    transcript = _speaker_transcript(result)
    if not transcript:
        raise CallAnalysisError("empty_transcript")
    return transcript, model


def _strip_json_fence(value):
    text = (value or "").strip()
    match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.S | re.I)
    return match.group(1).strip() if match else text


def _normalize_facts(value):
    facts = value if isinstance(value, dict) else {}
    keys = [
        "client_name",
        "company",
        "phone",
        "address",
        "object_type",
        "request",
        "dimensions",
        "technical_details",
        "timeline",
        "budget",
        "existing_equipment",
        "problems",
        "agreements",
        "send_to_client",
        "next_step",
        "next_contact_at",
        "responsible",
    ]
    return {key: facts.get(key) for key in keys}


def _analyze_transcript(client, call, transcript):
    model = _setting("OPENAI_CALL_ANALYSIS_MODEL", DEFAULT_ANALYSIS_MODEL)
    employee_name = ""
    if call.employee_profile_id:
        employee_name = call.employee_profile.display_name
    elif call.employee_id:
        employee_name = call.employee.get_full_name() or call.employee.username

    instructions = (
        "Ты анализируешь телефонный разговор компании Аквалайн. "
        "Верни только один JSON-объект без markdown. Ничего не выдумывай: "
        "если факт не прозвучал, используй null; для множественных фактов можно использовать массив строк. "
        "summary — краткий итог разговора на русском в 2-5 предложениях. "
        "facts должен содержать ровно ключи: client_name, company, phone, address, object_type, request, "
        "dimensions, technical_details, timeline, budget, existing_equipment, problems, agreements, "
        "send_to_client, next_step, next_contact_at, responsible. "
        "Телефон из метаданных можно использовать как phone, но не приписывай его словам клиента. "
        "Если в расшифровке спикеры обозначены буквами, не угадывай их личности без контекста."
    )
    input_text = (
        f"Метаданные звонка:\n"
        f"Направление: {call.get_direction_display()}\n"
        f"Телефон: {call.phone_number}\n"
        f"Сотрудник: {employee_name or 'не определён'}\n"
        f"Контакт из телефонии: {call.contact_name or 'не указан'}\n\n"
        f"Расшифровка:\n{transcript}"
    )
    response = client.responses.create(
        model=model,
        reasoning={"effort": "low"},
        max_output_tokens=2500,
        instructions=instructions,
        input=input_text,
    )
    raw = _strip_json_fence(getattr(response, "output_text", ""))
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise CallAnalysisError("analysis_invalid_json") from exc
    if not isinstance(payload, dict):
        raise CallAnalysisError("analysis_invalid_json")

    summary = str(payload.get("summary") or "").strip()
    facts = _normalize_facts(payload.get("facts"))
    if not summary:
        raise CallAnalysisError("analysis_empty_summary")
    return summary, facts, model


def process_call_analysis(call_id, *, force=False):
    analysis_id = _claim(call_id, force=force)
    if not analysis_id:
        return False

    analysis = (
        CallAnalysis.objects.select_related(
            "call",
            "call__employee",
            "call__employee_profile",
        )
        .get(pk=analysis_id)
    )
    call = analysis.call

    try:
        client = _client()
        transcript, transcription_model = _transcribe(client, call)
        summary, facts, analysis_model = _analyze_transcript(client, call, transcript)
        CallAnalysis.objects.filter(pk=analysis.pk).update(
            transcript=transcript,
            summary=summary,
            facts=facts,
            status=CallAnalysis.STATUS_READY,
            error="",
            transcription_model=transcription_model,
            analysis_model=analysis_model,
            processed_at=timezone.now(),
        )
        return True
    except Exception as exc:
        code = str(exc)
        if not isinstance(exc, CallAnalysisError):
            logger.exception("Call analysis failed for call_id=%s", call_id)
            code = "openai_processing_failed"
        CallAnalysis.objects.filter(pk=analysis.pk).update(
            status=CallAnalysis.STATUS_FAILED,
            error=code[:500],
            processed_at=timezone.now(),
        )
        return False


def reset_call_analysis(call_id):
    analysis, _ = CallAnalysis.objects.get_or_create(call_id=call_id)
    analysis.status = CallAnalysis.STATUS_PENDING
    analysis.error = ""
    analysis.processing_started_at = None
    analysis.processed_at = None
    analysis.save(
        update_fields=[
            "status",
            "error",
            "processing_started_at",
            "processed_at",
            "updated_at",
        ]
    )
    return analysis
