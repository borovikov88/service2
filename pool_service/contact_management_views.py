import json
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.decorators.http import require_POST


def _error(exc):
    if isinstance(exc, PermissionDenied):
        return JsonResponse({"error": "Недостаточно прав для изменения выбранных карточек."}, status=403)
    if isinstance(exc, ValidationError):
        message = " ".join(exc.messages)
    elif isinstance(exc, ValueError):
        message = str(exc)
    else:
        message = "Связи карточек конфликтуют. Ничего не изменено; проверьте выбранные записи."
    return JsonResponse({"error": message}, status=409)


@login_required
@require_POST
def client_avito_phone_confirm(request, client_id):
    from .avito_contact_phones import confirm_phone
    from .client_crm_ui import card_url
    from .client_queries import readable_clients

    client = get_object_or_404(readable_clients(request.user).select_related("organization"), pk=client_id)
    message_id = request.POST.get("message_id", "")
    if not message_id.isascii() or not message_id.isdecimal() or len(message_id) > 18:
        return HttpResponseBadRequest("Некорректный номер сообщения.")
    try:
        confirm_phone(request.user, client, int(message_id), request.POST.get("phone_key", ""),
                      request.POST.get("contacts_revision", ""))
    except ValidationError as error:
        messages.error(request, " ".join(error.messages))
    else:
        messages.success(request, "Номер из Авито сохранён как основной телефон клиента.")
    return redirect(card_url(client.pk, "contacts"))


@login_required
@require_POST
def client_phone_manage(request, client_id):
    from .services.contact_people import manage_phone
    try:
        manage_phone(request.user, client_id, action=request.POST.get("operation", ""),
            key=request.POST.get("phone_key", ""), entry_key=request.POST.get("entry_key", ""),
            value=request.POST.get("phone", "").strip(),
            phone_type=request.POST.get("phone_type", "other"), label=request.POST.get("label", ""),
            primary=request.POST.get("primary", "0"), revision=request.POST.get("contacts_revision", ""))
    except (PermissionDenied, ValidationError, IntegrityError) as exc:
        return _error(exc)
    return JsonResponse({"url": reverse("client_detail", args=[client_id]) + "?tab=contacts"})


@login_required
@require_POST
def client_bulk_merge(request):
    from .services.contact_management import preview_merge, execute_merge
    try:
        if len(request.body) > 16384:
            raise ValidationError("Слишком большой запрос.")
        try:
            data = json.loads(request.body)
        except (ValueError, UnicodeDecodeError):
            raise ValidationError("Обновите страницу и повторите действие.")
        if type(data) is not dict:
            raise ValidationError("Проверьте выбранные карточки.")
        if data.get("action") == "preview":
            result = preview_merge(request.user, data.get("ids"), data.get("target_id"))
        elif data.get("action") == "merge":
            target = execute_merge(request.user, data.get("token"))
            result = {"url": reverse("client_detail", args=[target.pk]) + "?tab=contacts"}
        else:
            raise ValidationError("Выберите действие.")
    except (PermissionDenied, ValidationError, ValueError, IntegrityError) as exc:
        return _error(exc)
    response = JsonResponse(result)
    response["Cache-Control"] = "private, no-store"
    return response
