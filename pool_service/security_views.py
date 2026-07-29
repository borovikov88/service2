import json

from django.contrib import messages
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from .models import Profile, WebAuthnCredential
from .security import (
    MAX_PIN_ATTEMPTS,
    SESSION_LOCKED_KEY,
    clear_security_pin,
    dismiss_passkey_prompt,
    has_fresh_password_login,
    has_security_pin,
    lock_session,
    mark_session_unlocked,
    set_security_pin,
    unlock_url,
    verify_security_pin,
)
from .security_forms import SecurityPinDisableForm, SecurityPinForm, SecurityUnlockForm
from .webauthn_utils import (
    SESSION_WEBAUTHN_AUTHENTICATION_CHALLENGE,
    SESSION_WEBAUTHN_REGISTRATION_CHALLENGE,
    challenge_from_session,
    challenge_to_session,
    credential_id_hash,
    credential_id_to_text,
    mark_credential_used,
    options_response,
    origin_for_request,
    rp_id_for_request,
    user_credential_descriptors,
)


def _json_request(request):
    if not request.body:
        return {}
    try:
        return json.loads(request.body.decode("utf-8"))
    except (TypeError, ValueError, UnicodeDecodeError):
        return {}


def _json_error(message, status=400):
    return JsonResponse({"ok": False, "error": message}, status=status)


def _is_ajax(request):
    return request.headers.get("x-requested-with") == "XMLHttpRequest"


@login_required
def security_unlock(request):
    profile, _ = Profile.objects.get_or_create(user=request.user)
    next_url = request.GET.get("next") or request.POST.get("next") or request.session.get("security_next") or "/"
    is_ajax = _is_ajax(request)
    has_passkey = WebAuthnCredential.objects.filter(user=request.user).exists()
    if not profile.security_pin_hash and not has_passkey:
        if is_ajax:
            return JsonResponse({"ok": False, "redirect_url": "/accounts/login/"}, status=403)
        logout(request)
        messages.error(request, "Быстрый вход не настроен. Войдите заново и настройте PIN или биометрию в профиле.")
        return redirect("login")
    if not request.session.get(SESSION_LOCKED_KEY):
        if is_ajax:
            return JsonResponse({"ok": True, "redirect_url": next_url})
        return redirect(next_url)

    form = SecurityUnlockForm(request.POST or None)
    if request.method == "POST":
        if not form.is_valid():
            error_message = "PIN должен содержать 4 цифры."
            form.add_error("pin", error_message)
            if is_ajax:
                return JsonResponse({"ok": False, "error": error_message}, status=400)
        elif verify_security_pin(profile, form.cleaned_data["pin"]):
            profile.security_pin_failed_attempts = 0
            profile.save(update_fields=["security_pin_failed_attempts"])
            mark_session_unlocked(request)
            request.session.pop("security_next", None)
            if is_ajax:
                return JsonResponse({"ok": True, "redirect_url": next_url})
            messages.success(request, "Доступ разблокирован.")
            return redirect(next_url)
        else:
            profile.security_pin_failed_attempts += 1
            attempts = profile.security_pin_failed_attempts
            profile.save(update_fields=["security_pin_failed_attempts"])
            if attempts >= MAX_PIN_ATTEMPTS:
                clear_security_pin(profile)
                logout(request)
                if is_ajax:
                    return JsonResponse(
                        {
                            "ok": False,
                            "error": "Слишком много неверных PIN. Быстрый вход отключён, войдите заново.",
                            "redirect_url": "/accounts/login/",
                        },
                        status=403,
                    )
                messages.error(request, "Слишком много неверных PIN. Быстрый вход отключён, войдите заново.")
                return redirect("login")
            error_message = f"Неверный PIN. Осталось попыток: {MAX_PIN_ATTEMPTS - attempts}."
            form.add_error("pin", error_message)
            if is_ajax:
                return JsonResponse({"ok": False, "error": error_message}, status=400)

    return render(
        request,
        "pool_service/security/unlock.html",
        {
            "form": form,
            "next_url": next_url,
            "hide_header": True,
            "hide_bottom_nav": True,
            "user_full_name": request.user.first_name or request.user.username,
            "security_pin_enabled": profile.has_security_pin,
            "security_passkey_enabled": has_passkey,
        },
    )


@require_POST
@login_required
def security_lock(request):
    if not has_security_pin(request.user) and not WebAuthnCredential.objects.filter(user=request.user).exists():
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse({"locked": False})
        return redirect("profile")
    next_url = request.POST.get("next") or request.META.get("HTTP_REFERER") or "/"
    lock_session(request, next_url=next_url)
    target_url = unlock_url(next_url)
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse({"locked": True, "unlock_url": target_url})
    return redirect(target_url)


@require_POST
@login_required
def security_pin_set(request):
    profile, _ = Profile.objects.get_or_create(user=request.user)
    form = SecurityPinForm(request.POST, user=request.user)
    if form.is_valid():
        set_security_pin(profile, form.cleaned_data["pin"])
        mark_session_unlocked(request)
        messages.success(request, "PIN-код включён.")
    else:
        for errors in form.errors.values():
            for error in errors:
                messages.error(request, error)
    return redirect("profile")


@require_POST
@login_required
def security_pin_quick_setup(request):
    if not has_fresh_password_login(request):
        return _json_error("Для настройки PIN войдите по паролю заново.", status=403)

    profile, _ = Profile.objects.get_or_create(user=request.user)
    pin = (request.POST.get("pin") or "").strip()
    pin_confirm = (request.POST.get("pin_confirm") or "").strip()
    if not pin.isdigit() or len(pin) != 4:
        return _json_error("PIN должен содержать 4 цифры.")
    if pin != pin_confirm:
        return _json_error("PIN-коды не совпадают.")

    set_security_pin(profile, pin)
    mark_session_unlocked(request)
    return JsonResponse({"ok": True})


@require_POST
@login_required
def security_pin_disable(request):
    profile, _ = Profile.objects.get_or_create(user=request.user)
    form = SecurityPinDisableForm(request.POST, user=request.user)
    if form.is_valid():
        clear_security_pin(profile)
        mark_session_unlocked(request)
        messages.success(request, "PIN-код отключён.")
    else:
        for errors in form.errors.values():
            for error in errors:
                messages.error(request, error)
    return redirect("profile")


@require_POST
@login_required
def webauthn_registration_options(request):
    data = _json_request(request)
    current_password = data.get("current_password") or ""
    if current_password:
        password_ok = request.user.check_password(current_password)
    else:
        password_ok = has_fresh_password_login(request)
    if not password_ok:
        return _json_error("Текущий пароль указан неверно.", status=403)

    display_name = request.user.get_full_name() or request.user.username
    options = generate_registration_options(
        rp_id=rp_id_for_request(request),
        rp_name="RovikPool",
        user_id=f"user-{request.user.pk}".encode("utf-8"),
        user_name=request.user.username,
        user_display_name=display_name,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=user_credential_descriptors(request.user),
    )
    challenge_to_session(request, SESSION_WEBAUTHN_REGISTRATION_CHALLENGE, options.challenge)
    request.session["webauthn_pending_name"] = (data.get("name") or "").strip()[:120]
    request.session.modified = True
    return JsonResponse({"ok": True, "publicKey": options_response(options)})


@require_POST
@login_required
def webauthn_registration_verify(request):
    challenge = challenge_from_session(request, SESSION_WEBAUTHN_REGISTRATION_CHALLENGE)
    if not challenge:
        return _json_error("Сессия регистрации истекла. Попробуйте ещё раз.")

    data = _json_request(request)
    try:
        verified = verify_registration_response(
            credential=data,
            expected_challenge=challenge,
            expected_rp_id=rp_id_for_request(request),
            expected_origin=origin_for_request(request),
            require_user_verification=True,
        )
    except WebAuthnException as exc:
        return _json_error(str(exc), status=400)

    credential_id = credential_id_to_text(verified.credential_id)
    transports = data.get("response", {}).get("transports") or []
    name = request.session.pop("webauthn_pending_name", "") or "Это устройство"
    request.session.pop(SESSION_WEBAUTHN_REGISTRATION_CHALLENGE, None)
    WebAuthnCredential.objects.update_or_create(
        credential_id_hash=credential_id_hash(credential_id),
        defaults={
            "user": request.user,
            "credential_id": credential_id,
            "public_key": verified.credential_public_key,
            "sign_count": verified.sign_count,
            "name": name,
            "transports": transports,
            "device_type": getattr(verified.credential_device_type, "value", str(verified.credential_device_type)),
            "backed_up": verified.credential_backed_up,
        },
    )
    return JsonResponse({"ok": True})


@require_POST
@login_required
def webauthn_authentication_options(request):
    credentials = user_credential_descriptors(request.user)
    if not credentials:
        return _json_error("Для этой учётной записи нет привязанных устройств.")

    options = generate_authentication_options(
        rp_id=rp_id_for_request(request),
        allow_credentials=credentials,
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    challenge_to_session(request, SESSION_WEBAUTHN_AUTHENTICATION_CHALLENGE, options.challenge)
    return JsonResponse({"ok": True, "publicKey": options_response(options)})


@require_POST
@login_required
def webauthn_authentication_verify(request):
    challenge = challenge_from_session(request, SESSION_WEBAUTHN_AUTHENTICATION_CHALLENGE)
    if not challenge:
        return _json_error("Сессия проверки истекла. Попробуйте ещё раз.")

    data = _json_request(request)
    raw_credential_id = data.get("id") or ""
    credential = WebAuthnCredential.objects.filter(
        user=request.user,
        credential_id_hash=credential_id_hash(raw_credential_id),
    ).first()
    if not credential:
        return _json_error("Устройство не привязано к этой учётной записи.", status=403)

    try:
        verified = verify_authentication_response(
            credential=data,
            expected_challenge=challenge,
            expected_rp_id=rp_id_for_request(request),
            expected_origin=origin_for_request(request),
            credential_public_key=credential.public_key,
            credential_current_sign_count=credential.sign_count,
            require_user_verification=True,
        )
    except WebAuthnException as exc:
        return _json_error(str(exc), status=400)

    request.session.pop(SESSION_WEBAUTHN_AUTHENTICATION_CHALLENGE, None)
    mark_credential_used(credential, verified.new_sign_count)
    mark_session_unlocked(request)
    request.session.pop("security_next", None)
    return JsonResponse({"ok": True, "redirect_url": data.get("next") or "/"})


@require_POST
@login_required
def webauthn_credential_delete(request, credential_id):
    credential = get_object_or_404(WebAuthnCredential, pk=credential_id, user=request.user)
    credential.delete()
    messages.success(request, "Устройство удалено.")
    return redirect("profile")


@require_POST
@login_required
def webauthn_prompt_dismiss(request):
    dismiss_passkey_prompt(request)
    return JsonResponse({"ok": True})
