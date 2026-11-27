"""Small identity-only page and ChatGPT component; no pending business action runs here."""
from urllib.parse import urlsplit

from django.contrib.auth.views import redirect_to_login
from django.core import signing
from django.http import Http404
from django.shortcuts import render, redirect
from django.urls import reverse
from django.utils.crypto import salted_hmac
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods

from pool_service.services import onec_chat_identity as identity
from pool_service.services.onec_draft_requests import DraftUnavailable, is_staff_member
from pool_service.services.onec_write_connection import WriteError

WIDGET_URI = "ui://rovik/onec-chat-identity-v1.html"


def return_url(value):
    if not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 32 for c in value):
        return ""
    try:
        url = urlsplit(value)
        if url.scheme == "https" and url.hostname == "chatgpt.com" and url.port in (None, 443) and not url.username and not url.password:
            return value
    except ValueError:
        pass
    return ""


@never_cache
@csrf_protect
@require_http_methods(["GET", "POST"])
def confirm_identity(request, token):
    if not identity.enabled():
        raise Http404
    try:
        row = identity.from_token(token)
        if row.revoked_at or not identity._valid_grant(row):
            raise DraftUnavailable
    except DraftUnavailable:
        raise Http404 from None
    key = "onec-chat-return:" + salted_hmac("onec-return", token).hexdigest()
    if request.method == "GET":
        destination = return_url(request.GET.get("redirectUrl"))
        if destination:
            request.session[key] = destination
    if not request.user.is_authenticated:
        return redirect_to_login(reverse("onec_chat_confirm", kwargs={"token": token}), login_url=reverse("login"))
    if not is_staff_member(request.user.pk, row.organization_id):
        raise Http404
    message, done = "", bool(row.confirmed_at and row.user_id == request.user.pk)
    if row.confirmed_at and not done:
        raise Http404
    display_name = request.user.get_full_name() or request.user.get_username()
    try:
        _author, author_name = identity.mapped_author(request.user.pk, row.organization_id)
        display_name = author_name or display_name
        if request.method == "POST":
            expected = signing.loads(request.POST.get("account_binding", ""), salt=identity.SALT + "-account", max_age=600)
            if expected != [str(row.pk), str(row.challenge_nonce), request.user.pk]:
                raise DraftUnavailable
            identity.confirm(token, request.user)
            done = True
            destination = return_url(request.session.pop(key, ""))
            if destination:
                response = redirect(destination)
                response["Referrer-Policy"] = "no-referrer"
                return response
    except (WriteError, signing.BadSignature, DraftUnavailable) as exc:
        message = str(exc) if isinstance(exc, WriteError) else "Подтверждение устарело. Откройте ссылку заново из чата."
    response = render(request, "onec_drafts/confirm_identity.html", {
        "display_name": display_name, "done": done, "message": message,
        "account_binding": signing.dumps([str(row.pk), str(row.challenge_nonce), request.user.pk], salt=identity.SALT + "-account"),
    })
    response["Referrer-Policy"] = "no-referrer"
    response["X-Frame-Options"] = "DENY"
    return response
