from django.contrib import messages
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from .models import Profile
from .security import (
    MAX_PIN_ATTEMPTS,
    SESSION_LOCKED_KEY,
    clear_security_pin,
    has_security_pin,
    lock_session,
    mark_session_unlocked,
    set_security_pin,
    unlock_url,
    verify_security_pin,
)
from .security_forms import SecurityPinDisableForm, SecurityPinForm, SecurityUnlockForm


@login_required
def security_unlock(request):
    profile, _ = Profile.objects.get_or_create(user=request.user)
    next_url = request.GET.get("next") or request.POST.get("next") or request.session.get("security_next") or "/"
    if not profile.security_pin_hash:
        logout(request)
        messages.error(request, "PIN не настроен. Войдите заново и настройте PIN в профиле.")
        return redirect("login")
    if not request.session.get(SESSION_LOCKED_KEY):
        return redirect(next_url)

    form = SecurityUnlockForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        if verify_security_pin(profile, form.cleaned_data["pin"]):
            profile.security_pin_failed_attempts = 0
            profile.save(update_fields=["security_pin_failed_attempts"])
            mark_session_unlocked(request)
            request.session.pop("security_next", None)
            messages.success(request, "Доступ разблокирован.")
            return redirect(next_url)

        profile.security_pin_failed_attempts += 1
        attempts = profile.security_pin_failed_attempts
        profile.save(update_fields=["security_pin_failed_attempts"])
        if attempts >= MAX_PIN_ATTEMPTS:
            clear_security_pin(profile)
            logout(request)
            messages.error(request, "Слишком много неверных PIN. Быстрый вход отключён, войдите заново.")
            return redirect("login")
        form.add_error("pin", f"Неверный PIN. Осталось попыток: {MAX_PIN_ATTEMPTS - attempts}.")

    return render(
        request,
        "pool_service/security/unlock.html",
        {
            "form": form,
            "next_url": next_url,
            "hide_header": True,
            "hide_bottom_nav": True,
            "user_full_name": request.user.get_full_name() or request.user.username,
        },
    )


@require_POST
@login_required
def security_lock(request):
    if not has_security_pin(request.user):
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
