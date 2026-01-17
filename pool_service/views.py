from django.contrib import messages
from django.contrib.auth import login, authenticate, update_session_auth_hash
from django.contrib.auth.models import User
from django.contrib.auth.tokens import default_token_generator
from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.password_validation import validate_password
from django.core.mail import send_mail
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.views import LoginView
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.cache import never_cache
from django.urls import reverse, reverse_lazy
from django.db import connection
from django.db.models import Count, Q, Max
from django.http import HttpResponse, HttpResponseForbidden, HttpResponseNotFound, JsonResponse
from django.utils import timezone
from django.utils.crypto import get_random_string
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode
from django.template.loader import render_to_string
from django.views.decorators.csrf import csrf_exempt
import html
from django.templatetags.static import static
from django.conf import settings
from django.contrib.sitemaps.views import sitemap as sitemap_view
import uuid
from urllib.parse import urlencode
from urllib.request import urlopen, Request
import json
from datetime import timedelta

from .forms import (
    WaterReadingForm,
    RegistrationForm,
    ClientCreateForm,
    PoolForm,
    EmailOrUsernameAuthenticationForm,
    PersonalSignupForm,
    CompanySignupForm,
    OrganizationInviteForm,
    InviteAcceptForm,
    normalize_phone,
)
from .sitemaps import HomeSitemap
from .seo import is_indexable_host
from .services.phone_verification import (
    smsru_callcheck_add,
    smsru_callcheck_status,
    smsru_send_sms,
)


def _request_host(request):
    return request.get_host().split(":", 1)[0].lower()

PHONE_VERIFY_TTL_MINUTES = getattr(settings, "PHONE_VERIFY_TTL_MINUTES", 5)
PHONE_VERIFY_MAX_ATTEMPTS = getattr(settings, "PHONE_VERIFY_MAX_ATTEMPTS", 3)


def _user_phone_digits(user):
    if user.username and user.username.isdigit() and len(user.username) == 10:
        return user.username
    client = Client.objects.filter(user=user).first()
    if client and client.phone:
        return normalize_phone(client.phone)
    return None


def _smsru_phone(digits):
    return f"7{digits}" if digits else None


def _remaining_phone_attempts(profile):
    used = profile.phone_verification_attempts or 0
    return max(0, PHONE_VERIFY_MAX_ATTEMPTS - used)


def robots_txt(request):
    host = _request_host(request)
    if not is_indexable_host(host):
        content = "\n".join(
            [
                "User-agent: *",
                "Disallow: /",
            ]
        )
        return HttpResponse(content, content_type="text/plain")

    sitemap_url = request.build_absolute_uri("/sitemap.xml")
    content = "\n".join(
        [
            "User-agent: *",
            "Allow: /",
            f"Sitemap: {sitemap_url}",
            f"Host: {host}",
        ]
    )
    return HttpResponse(content, content_type="text/plain")


def sitemap_xml(request):
    host = _request_host(request)
    if not is_indexable_host(host):
        return HttpResponseNotFound("")
    return sitemap_view(request, {"home": HomeSitemap()})
from .models import OrganizationAccess, Pool, PoolAccess, WaterReading, Client, Organization, OrganizationInvite, Profile
from .services.permissions import is_personal_free, is_personal_user, is_org_access_blocked
from django import forms

PER_PAGE_CHOICES = {20, 50, 100}
INVITE_EXPIRY_HOURS = 24


def _parse_per_page(value, default):
    try:
        per_page = int(value)
    except (TypeError, ValueError):
        return default
    return per_page if per_page in PER_PAGE_CHOICES else default


def _personal_pool_redirect(user):
    if not is_personal_user(user):
        return None
    client = Client.objects.filter(user=user, organization__isnull=True).first()
    if not client:
        return None
    pool = Pool.objects.filter(client=client).first()
    if not pool:
        return reverse("pool_create")
    return reverse("pool_detail", kwargs={"pool_uuid": pool.uuid})


def _mark_phone_confirmed(profile):
    profile.phone_confirmed_at = timezone.now()
    profile.phone_sms_code_hash = ""
    profile.phone_verification_check_id = ""
    profile.phone_verification_call_phone = ""
    profile.phone_verification_expires_at = None
    profile.save(
        update_fields=[
            "phone_confirmed_at",
            "phone_sms_code_hash",
            "phone_verification_check_id",
            "phone_verification_call_phone",
            "phone_verification_expires_at",
        ]
    )


def _start_phone_call(profile, phone_digits):
    if _remaining_phone_attempts(profile) <= 0:
        return False, "Попытки подтверждения закончились."
    api_phone = _smsru_phone(phone_digits)
    if not api_phone:
        return False, "Не удалось определить номер телефона."
    response = smsru_callcheck_add(api_phone)
    if not response.get("ok"):
        return False, response.get("error") or "Не удалось запросить звонок."
    now = timezone.now()
    profile.phone_verification_required = True
    profile.phone_verification_attempts += 1
    profile.phone_verification_started_at = now
    profile.phone_verification_expires_at = now + timedelta(minutes=PHONE_VERIFY_TTL_MINUTES)
    profile.phone_verification_check_id = response.get("check_id") or ""
    profile.phone_verification_call_phone = response.get("call_phone") or ""
    profile.phone_sms_code_hash = ""
    profile.phone_sms_sent_at = None
    profile.save(
        update_fields=[
            "phone_verification_required",
            "phone_verification_attempts",
            "phone_verification_started_at",
            "phone_verification_expires_at",
            "phone_verification_check_id",
            "phone_verification_call_phone",
            "phone_sms_code_hash",
            "phone_sms_sent_at",
        ]
    )
    return True, None


def _check_phone_call(profile):
    if not profile.phone_verification_check_id:
        return False, "Сначала запросите звонок."
    response = smsru_callcheck_status(profile.phone_verification_check_id)
    if not response.get("ok"):
        return False, response.get("error") or "Не удалось проверить звонок."
    check_status = str(response.get("check_status") or "")
    if check_status == "401":
        _mark_phone_confirmed(profile)
        return True, None
    if check_status == "402":
        return False, "Срок ожидания звонка истек. Запросите звонок снова."
    return False, response.get("check_status_text") or "Звонок еще не подтвержден."


def _send_phone_sms(profile, phone_digits):
    if _remaining_phone_attempts(profile) <= 0:
        return False, "Попытки подтверждения закончились."
    api_phone = _smsru_phone(phone_digits)
    if not api_phone:
        return False, "Не удалось определить номер телефона."
    code = get_random_string(4, allowed_chars="0123456789")
    text = f"Код подтверждения: {code}"
    response = smsru_send_sms(api_phone, text)
    if not response.get("ok"):
        return False, response.get("error") or "Не удалось отправить СМС."
    now = timezone.now()
    profile.phone_verification_required = True
    profile.phone_verification_attempts += 1
    profile.phone_verification_started_at = now
    profile.phone_verification_expires_at = now + timedelta(minutes=PHONE_VERIFY_TTL_MINUTES)
    profile.phone_sms_code_hash = make_password(code)
    profile.phone_sms_sent_at = now
    profile.save(
        update_fields=[
            "phone_verification_required",
            "phone_verification_attempts",
            "phone_verification_started_at",
            "phone_verification_expires_at",
            "phone_sms_code_hash",
            "phone_sms_sent_at",
        ]
    )
    return True, None


def _verify_phone_sms(profile, code):
    if not profile.phone_sms_code_hash:
        return False, "Сначала запросите СМС с кодом."
    if profile.phone_verification_expires_at and profile.phone_verification_expires_at <= timezone.now():
        return False, "Срок действия кода истек. Запросите новый."
    if not check_password(code, profile.phone_sms_code_hash):
        return False, "Неверный код. Попробуйте еще раз."
    _mark_phone_confirmed(profile)
    return True, None


def _redirect_if_access_blocked(request):
    if not is_org_access_blocked(request.user):
        return None
    messages.error(
        request,
        "\u041f\u0440\u043e\u0431\u043d\u044b\u0439 \u043f\u0435\u0440\u0438\u043e\u0434 \u0437\u0430\u0432\u0435\u0440\u0448\u0435\u043d. \u0414\u043b\u044f \u043f\u0440\u043e\u0434\u043e\u043b\u0436\u0435\u043d\u0438\u044f \u0440\u0430\u0431\u043e\u0442\u044b \u043e\u043f\u043b\u0430\u0442\u0438\u0442\u0435 \u0442\u0430\u0440\u0438\u0444.",
    )
    return redirect("billing")


def _deny_superuser_write(request):
    if not request.user.is_superuser:
        return None
    messages.error(request, "\u0421\u0443\u043f\u0435\u0440\u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u044c \u0434\u043e\u0441\u0442\u0443\u043f\u0435\u043d \u0442\u043e\u043b\u044c\u043a\u043e \u0434\u043b\u044f \u043f\u0440\u043e\u0441\u043c\u043e\u0442\u0440\u0430.")
    return redirect(request.META.get("HTTP_REFERER") or "pool_list")


def index(request):
    if request.user.is_authenticated:
        redirect_url = _personal_pool_redirect(request.user) or reverse("pool_list")
        return redirect(redirect_url)
    return render(request, "pool_service/index.html")


def _reading_edit_allowed(reading, user):
    if not user.is_authenticated:
        return False
    if reading.added_by_id != user.id:
        return False
    if not reading.date:
        return False
    reading_date = reading.date
    now = timezone.now()
    if timezone.is_aware(reading_date):
        now = timezone.localtime(now)
    else:
        now = now.replace(tzinfo=None)
    return now - reading_date <= timedelta(minutes=30)


@login_required
def pool_list(request):
    """Список бассейнов доступных пользователю."""
    if is_personal_user(request.user):
        redirect_url = _personal_pool_redirect(request.user)
        if redirect_url:
            return redirect(redirect_url)
    if request.user.is_superuser:
        pools = Pool.objects.all()
    elif OrganizationAccess.objects.filter(user=request.user).exists():
        org_access = OrganizationAccess.objects.filter(user=request.user).first()
        if org_access:
            pools = Pool.objects.filter(
                Q(organization=org_access.organization) | Q(client__organization=org_access.organization)
            ).distinct()
        else:
            pools = Pool.objects.none()
    else:
        pools = Pool.objects.filter(accesses__user=request.user)

    search_query = request.GET.get("q", "").strip()
    use_python_search = bool(search_query) and connection.vendor == "sqlite"
    if search_query and not use_python_search:
        pools = pools.filter(
            Q(client__name__icontains=search_query)
            | Q(address__icontains=search_query)
            | Q(organization__name__icontains=search_query)
        )

    sort = request.GET.get("sort", "client_asc")
    sort_options = {
        "client_asc",
        "client_desc",
        "recent_desc",
        "recent_asc",
        "created_desc",
        "created_asc",
    }
    if sort not in sort_options:
        sort = "client_asc"

    pools = pools.annotate(
        num_readings=Count("waterreading"),
        last_reading=Max("waterreading__date"),
    ).select_related("client")

    if sort == "recent_desc":
        pools = pools.order_by("-last_reading", "client__name", "address")
    elif sort == "recent_asc":
        pools = pools.order_by("last_reading", "client__name", "address")
    elif sort == "client_desc":
        pools = pools.order_by("-client__name", "address")
    elif sort == "created_desc":
        pools = pools.order_by("-id")
    elif sort == "created_asc":
        pools = pools.order_by("id")
    else:
        pools = pools.order_by("client__name", "address")

    if use_python_search:
        query_cf = search_query.casefold()
        filtered = []
        for pool in pools:
            parts = [
                getattr(pool.client, "name", ""),
                getattr(pool, "address", ""),
                getattr(getattr(pool, "organization", None), "name", ""),
            ]
            haystack = " ".join(part for part in parts if part).casefold()
            if query_cf in haystack:
                filtered.append(pool)
        pools = filtered

    personal_user = is_personal_user(request.user)
    personal_pool_count = 0
    if personal_user:
        personal_client = Client.objects.filter(user=request.user, organization__isnull=True).first()
        if personal_client:
            personal_pool_count = Pool.objects.filter(client=personal_client).count()

    per_page = _parse_per_page(request.GET.get("per_page"), 20)
    paginator = Paginator(pools, per_page)
    page_number = request.GET.get("page")
    pools_page = paginator.get_page(page_number)
    query_params = request.GET.copy()
    query_params.pop("page", None)
    query_params.pop("partial", None)

    allow_pool_create = not (personal_user and personal_pool_count >= 1)
    if request.user.is_superuser:
        allow_pool_create = False
    page_title = "Мой бассейн" if personal_user else "Бассейны"
    page_action_label = "Добавить бассейн" if allow_pool_create else None
    page_action_url = reverse("pool_create") if allow_pool_create else None

    context = {
        "pools": pools_page,
        "page_title": page_title,
        "page_subtitle": "Управление объектами обслуживания",
        "page_action_label": page_action_label,
        "page_action_url": page_action_url,
        "show_search": False,
        "show_add_button": False,
        "add_url": None,
        "active_tab": "pools",
        "show_pool_controls": not personal_user,
        "search_query": search_query,
        "sort": sort,
        "per_page": per_page,
        "pagination_query": query_params.urlencode(),
    }

    if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.GET.get("partial") == "1":
        return render(request, "pool_service/partials/pool_list_results.html", context)

    return render(request, "pool_service/pool_list.html", context)


@login_required
def billing_info(request):
    return render(
        request,
        "pool_service/billing.html",
        {
            "page_title": "\u041e\u043f\u043b\u0430\u0442\u0430 \u0438 \u043f\u0440\u043e\u0434\u043b\u0435\u043d\u0438\u0435",
            "page_subtitle": "\u041f\u043e\u0434\u043a\u043b\u044e\u0447\u0435\u043d\u0438\u0435 \u0442\u0430\u0440\u0438\u0444\u0430",
            "active_tab": None,
            "show_search": False,
            "show_add_button": False,
            "add_url": None,
        },
    )


@login_required
def invite_create(request):
    readonly = _deny_superuser_write(request)
    if readonly:
        return readonly
    blocked = _redirect_if_access_blocked(request)
    if blocked:
        return blocked

    is_admin = request.user.is_superuser or OrganizationAccess.objects.filter(user=request.user, role="admin").exists()
    if not is_admin:
        return HttpResponseForbidden()

    if request.method == "POST":
        form = OrganizationInviteForm(request.POST)
        if form.is_valid():
            email = form.cleaned_data["email"].strip().lower()
            if User.objects.filter(email__iexact=email).exists():
                form.add_error("email", "\u041f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u044c \u0441 \u0442\u0430\u043a\u0438\u043c email \u0443\u0436\u0435 \u0437\u0430\u0440\u0435\u0433\u0438\u0441\u0442\u0440\u0438\u0440\u043e\u0432\u0430\u043d")
            else:
                org_access = (
                    OrganizationAccess.objects.filter(user=request.user, role="admin")
                    .select_related("organization")
                    .first()
                )
                if not org_access and not request.user.is_superuser:
                    return HttpResponseForbidden()
                organization = org_access.organization if org_access else None
                if not organization:
                    form.add_error("email", "\u041d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d\u0430 \u043e\u0440\u0433\u0430\u043d\u0438\u0437\u0430\u0446\u0438\u044f")
                else:
                    now = timezone.now()
                    expires_at = now + timedelta(hours=INVITE_EXPIRY_HOURS)
                    invite = OrganizationInvite.objects.filter(
                        organization=organization,
                        email__iexact=email,
                        accepted_at__isnull=True,
                    ).first()
                    if invite:
                        invite.first_name = form.cleaned_data["first_name"]
                        invite.last_name = form.cleaned_data["last_name"]
                        invite.phone = form.cleaned_data.get("phone", "")
                        invite.role = form.cleaned_data["role"]
                        invite.token = uuid.uuid4()
                        invite.expires_at = expires_at
                        invite.invited_by = request.user
                        invite.last_sent_at = now
                    else:
                        invite = OrganizationInvite.objects.create(
                            organization=organization,
                            invited_by=request.user,
                            email=email,
                            first_name=form.cleaned_data["first_name"],
                            last_name=form.cleaned_data["last_name"],
                            phone=form.cleaned_data.get("phone", ""),
                            role=form.cleaned_data["role"],
                            expires_at=expires_at,
                            last_sent_at=now,
                        )
                    invite.save()
                    if _send_invite_email(request, invite):
                        messages.success(request, "\u041f\u0440\u0438\u0433\u043b\u0430\u0448\u0435\u043d\u0438\u0435 \u043e\u0442\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u043e.")
                        return redirect("users")
                    messages.error(request, "\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u043e\u0442\u043f\u0440\u0430\u0432\u0438\u0442\u044c \u043f\u0438\u0441\u044c\u043c\u043e. \u041f\u0440\u043e\u0432\u0435\u0440\u044c\u0442\u0435 \u043f\u043e\u0447\u0442\u043e\u0432\u044b\u0435 \u043d\u0430\u0441\u0442\u0440\u043e\u0439\u043a\u0438.")
    else:
        form = OrganizationInviteForm()

    return render(
        request,
        "pool_service/invite_create.html",
        {
            "form": form,
            "page_title": "\u0414\u043e\u0431\u0430\u0432\u0438\u0442\u044c \u0441\u043e\u0442\u0440\u0443\u0434\u043d\u0438\u043a\u0430",
            "active_tab": "users",
            "show_search": False,
            "show_add_button": False,
            "add_url": None,
        },
    )


@login_required
def invite_resend(request, invite_id):
    readonly = _deny_superuser_write(request)
    if readonly:
        return readonly
    blocked = _redirect_if_access_blocked(request)
    if blocked:
        return blocked
    if request.method != "POST":
        return redirect("users")

    invite = get_object_or_404(OrganizationInvite, pk=invite_id)
    is_admin = request.user.is_superuser or OrganizationAccess.objects.filter(user=request.user, role="admin").exists()
    if not is_admin:
        return HttpResponseForbidden()
    if not request.user.is_superuser:
        allowed = OrganizationAccess.objects.filter(
            user=request.user,
            role="admin",
            organization=invite.organization,
        ).exists()
        if not allowed:
            return HttpResponseForbidden()

    if invite.accepted_at:
        messages.info(request, "\u041f\u0440\u0438\u0433\u043b\u0430\u0448\u0435\u043d\u0438\u0435 \u0443\u0436\u0435 \u043f\u0440\u0438\u043d\u044f\u0442\u043e.")
        return redirect("users")

    now = timezone.now()
    invite.token = uuid.uuid4()
    invite.expires_at = now + timedelta(hours=INVITE_EXPIRY_HOURS)
    invite.last_sent_at = now
    invite.save(update_fields=["token", "expires_at", "last_sent_at"])

    if _send_invite_email(request, invite):
        messages.success(request, "\u041f\u0440\u0438\u0433\u043b\u0430\u0448\u0435\u043d\u0438\u0435 \u043e\u0442\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u043e \u043f\u043e\u0432\u0442\u043e\u0440\u043d\u043e.")
    else:
        messages.error(request, "\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u043e\u0442\u043f\u0440\u0430\u0432\u0438\u0442\u044c \u043f\u0438\u0441\u044c\u043c\u043e.")
    return redirect("users")


def invite_accept(request, token):
    invite = OrganizationInvite.objects.filter(token=token).select_related("organization").first()
    if not invite:
        return render(
            request,
            "registration/invite_accept.html",
            {"invite": None, "error_message": "\u0421\u0441\u044b\u043b\u043a\u0430 \u043d\u0435\u0434\u0435\u0439\u0441\u0442\u0432\u0438\u0442\u0435\u043b\u044c\u043d\u0430."},
        )

    if invite.accepted_at:
        return render(
            request,
            "registration/invite_accept.html",
            {"invite": invite, "error_message": "\u041f\u0440\u0438\u0433\u043b\u0430\u0448\u0435\u043d\u0438\u0435 \u0443\u0436\u0435 \u043f\u0440\u0438\u043d\u044f\u0442\u043e."},
        )

    if invite.is_expired():
        return render(
            request,
            "registration/invite_accept.html",
            {"invite": invite, "error_message": "\u0421\u0441\u044b\u043b\u043a\u0430 \u043f\u0440\u043e\u0441\u0440\u043e\u0447\u0435\u043d\u0430. \u041f\u043e\u043f\u0440\u043e\u0441\u0438\u0442\u0435 \u0430\u0434\u043c\u0438\u043d\u0438\u0441\u0442\u0440\u0430\u0442\u043e\u0440\u0430 \u043e\u0442\u043f\u0440\u0430\u0432\u0438\u0442\u044c \u043f\u0440\u0438\u0433\u043b\u0430\u0448\u0435\u043d\u0438\u0435 \u043f\u043e\u0432\u0442\u043e\u0440\u043d\u043e."},
        )

    if request.method == "POST":
        form = InviteAcceptForm(request.POST)
        if form.is_valid():
            email = invite.email.strip().lower()
            if User.objects.filter(email__iexact=email).exists():
                form.add_error("password1", "\u0410\u043a\u043a\u0430\u0443\u043d\u0442 \u0441 \u044d\u0442\u0438\u043c email \u0443\u0436\u0435 \u0441\u0443\u0449\u0435\u0441\u0442\u0432\u0443\u0435\u0442")
            else:
                user = User.objects.create_user(
                    username=email,
                    email=email,
                    first_name=form.cleaned_data["first_name"],
                    last_name=form.cleaned_data["last_name"],
                )
                user.set_password(form.cleaned_data["password1"])
                user.save()
                OrganizationAccess.objects.create(
                    user=user,
                    organization=invite.organization,
                    role=invite.role,
                )
                invite.accepted_at = timezone.now()
                invite.accepted_user = user
                invite.save(update_fields=["accepted_at", "accepted_user"])
                login(request, user)
                messages.success(request, "\u0410\u043a\u043a\u0430\u0443\u043d\u0442 \u0430\u043a\u0442\u0438\u0432\u0438\u0440\u043e\u0432\u0430\u043d.")
                return redirect("pool_list")
    else:
        form = InviteAcceptForm(
            initial={
                "first_name": invite.first_name,
                "last_name": invite.last_name,
                "phone": invite.phone,
            }
        )

    return render(
        request,
        "registration/invite_accept.html",
        {"invite": invite, "form": form},
    )


@login_required
def staff_toggle_block(request, access_id):
    readonly = _deny_superuser_write(request)
    if readonly:
        return readonly
    blocked = _redirect_if_access_blocked(request)
    if blocked:
        return blocked
    if request.method != "POST":
        return redirect("users")

    access = get_object_or_404(OrganizationAccess, pk=access_id)
    is_admin = request.user.is_superuser or OrganizationAccess.objects.filter(
        user=request.user,
        role="admin",
        organization=access.organization,
    ).exists()
    if not is_admin:
        return HttpResponseForbidden()
    if access.user_id == request.user.id:
        messages.error(request, "\u041d\u0435\u043b\u044c\u0437\u044f \u0437\u0430\u0431\u043b\u043e\u043a\u0438\u0440\u043e\u0432\u0430\u0442\u044c \u0441\u0430\u043c\u043e\u0433\u043e \u0441\u0435\u0431\u044f.")
        return redirect("users")
    if access.user.is_superuser:
        return HttpResponseForbidden()

    access.user.is_active = not access.user.is_active
    access.user.save(update_fields=["is_active"])
    if access.user.is_active:
        messages.success(request, "\u0421\u043e\u0442\u0440\u0443\u0434\u043d\u0438\u043a \u0440\u0430\u0437\u0431\u043b\u043e\u043a\u0438\u0440\u043e\u0432\u0430\u043d.")
    else:
        messages.success(request, "\u0421\u043e\u0442\u0440\u0443\u0434\u043d\u0438\u043a \u0437\u0430\u0431\u043b\u043e\u043a\u0438\u0440\u043e\u0432\u0430\u043d.")
    return redirect("users")


@login_required
def staff_delete(request, access_id):
    readonly = _deny_superuser_write(request)
    if readonly:
        return readonly
    blocked = _redirect_if_access_blocked(request)
    if blocked:
        return blocked
    if request.method != "POST":
        return redirect("users")

    access = get_object_or_404(OrganizationAccess, pk=access_id)
    is_admin = request.user.is_superuser or OrganizationAccess.objects.filter(
        user=request.user,
        role="admin",
        organization=access.organization,
    ).exists()
    if not is_admin:
        return HttpResponseForbidden()
    if access.user_id == request.user.id:
        messages.error(request, "\u041d\u0435\u043b\u044c\u0437\u044f \u0443\u0434\u0430\u043b\u0438\u0442\u044c \u0441\u0430\u043c\u043e\u0433\u043e \u0441\u0435\u0431\u044f.")
        return redirect("users")
    if access.user.is_superuser:
        return HttpResponseForbidden()

    PoolAccess.objects.filter(user=access.user, pool__organization=access.organization).delete()
    PoolAccess.objects.filter(user=access.user, pool__client__organization=access.organization).delete()
    access.delete()
    messages.success(request, "\u0421\u043e\u0442\u0440\u0443\u0434\u043d\u0438\u043a \u0443\u0434\u0430\u043b\u0435\u043d.")
    return redirect("users")


@login_required
def users_view(request):
    """Список пользователей для суперюзеров/админов, сервисники видят только персонал бассейнов."""
    roles = list(OrganizationAccess.objects.filter(user=request.user).values_list("role", flat=True))
    is_org_admin = "admin" in roles
    is_org_service = "service" in roles

    if not (request.user.is_superuser or is_org_admin or is_org_service):
        return HttpResponseForbidden()

    org_filter = {}
    pool_filter = {}
    if not request.user.is_superuser and (is_org_admin or is_org_service):
        org_ids = list(
            OrganizationAccess.objects.filter(user=request.user).values_list("organization_id", flat=True)
        )
        org_filter = {"organization_id__in": org_ids}
        pool_filter = {"pool__organization_id__in": org_ids}

    org_staff = []
    org_invites = []
    organizations = []
    if request.user.is_superuser or is_org_admin:
        org_staff = (
            OrganizationAccess.objects.filter(**org_filter)
            .select_related("organization", "user")
            .order_by("organization__name", "user__last_name")
        )
        org_invites = (
            OrganizationInvite.objects.filter(**org_filter, accepted_at__isnull=True)
            .select_related("organization", "invited_by")
            .order_by("-created_at")
        )
    if request.user.is_superuser:
        organizations = Organization.objects.order_by("name")

    pool_staff = (
        PoolAccess.objects.filter(**pool_filter)
        .select_related("pool", "pool__client", "user")
        .order_by("pool__client__name")
    )

    return render(
        request,
        "pool_service/users.html",
        {
            "page_title": "Пользователи",
            "page_subtitle": "Сотрудники сервисной компании и роли на объектах",
            "org_staff": org_staff,
            "org_invites": org_invites,
            "organizations": organizations,
            "page_action_label": "\u0414\u043e\u0431\u0430\u0432\u0438\u0442\u044c \u0441\u043e\u0442\u0440\u0443\u0434\u043d\u0438\u043a\u0430" if is_org_admin else None,
            "page_action_url": reverse("invite_create") if is_org_admin else None,
            "pool_staff": pool_staff,
            "active_tab": "users",
            "show_search": False,
            "show_add_button": False,
            "add_url": None,
        },
    )


@login_required
def clients_list(request):
    is_admin = request.user.is_superuser or OrganizationAccess.objects.filter(user=request.user, role="admin").exists()
    if not is_admin:
        return HttpResponseForbidden()

    if request.user.is_superuser:
        clients = Client.objects.all()
    else:
        org_ids = OrganizationAccess.objects.filter(user=request.user, role="admin").values_list(
            "organization_id",
            flat=True,
        )
        clients = Client.objects.filter(organization_id__in=org_ids).distinct()

    clients = clients.annotate(pool_count=Count("pool")).select_related("organization").order_by("name")

    return render(
        request,
        "pool_service/clients.html",
        {
            "page_title": "\u041a\u043b\u0438\u0435\u043d\u0442\u044b",
            "page_subtitle": "\u041a\u043e\u043d\u0442\u0430\u043a\u0442\u044b \u0438 \u043e\u0431\u044a\u0435\u043a\u0442\u044b \u043a\u043b\u0438\u0435\u043d\u0442\u043e\u0432 \u0432 \u043e\u0434\u043d\u043e\u043c \u0441\u043f\u0438\u0441\u043a\u0435",
            "clients": clients,
            "active_tab": "clients",
            "page_action_label": None if request.user.is_superuser else "\u0414\u043e\u0431\u0430\u0432\u0438\u0442\u044c \u043a\u043b\u0438\u0435\u043d\u0442\u0430",
            "page_action_url": None if request.user.is_superuser else reverse("client_create"),
            "show_search": False,
            "show_add_button": False,
            "add_url": None,
        },
    )


class PoolForm(forms.ModelForm):
    class Meta:
        model = Pool
        fields = [
            "client",
            "address",
            "description",
            "shape",
            "pool_type",
            "length",
            "width",
            "diameter",
            "variable_depth",
            "depth",
            "depth_min",
            "depth_max",
            "overflow_volume",
            "surface_area",
            "volume",
            "dosing_station",
        ]
        widgets = {
            "client": forms.Select(attrs={"class": "form-select"}),
            "address": forms.TextInput(attrs={"class": "form-control rounded-3"}),
            "description": forms.Textarea(attrs={"class": "form-control rounded-3", "rows": 3}),
            "shape": forms.Select(attrs={"class": "form-select"}),
            "pool_type": forms.Select(attrs={"class": "form-select"}),
            "length": forms.NumberInput(attrs={"class": "form-control rounded-3", "step": "0.01"}),
            "width": forms.NumberInput(attrs={"class": "form-control rounded-3", "step": "0.01"}),
            "diameter": forms.NumberInput(attrs={"class": "form-control rounded-3", "step": "0.01"}),
            "variable_depth": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "depth": forms.NumberInput(attrs={"class": "form-control rounded-3", "step": "0.01"}),
            "depth_min": forms.NumberInput(attrs={"class": "form-control rounded-3", "step": "0.01"}),
            "depth_max": forms.NumberInput(attrs={"class": "form-control rounded-3", "step": "0.01"}),
            "overflow_volume": forms.NumberInput(attrs={"class": "form-control rounded-3", "step": "0.01"}),
            "surface_area": forms.NumberInput(attrs={"class": "form-control rounded-3", "step": "0.01"}),
            "volume": forms.NumberInput(attrs={"class": "form-control rounded-3", "step": "0.01"}),
            "dosing_station": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

    def __init__(self, *args, **kwargs):
        user = kwargs.pop("user", None)
        selected_client_id = kwargs.pop("selected_client_id", None)
        super().__init__(*args, **kwargs)
        if user:
            client_qs = Client.objects.none()
            client_self = Client.objects.filter(user=user)

            if user.is_superuser:
                client_qs = Client.objects.all()
                self.fields["client"].empty_label = "Выберите клиента"
            elif client_self.exists():
                client_qs = client_self
                self.fields["client"].empty_label = None
                self.fields["client"].initial = client_self.first()
                self.fields["client"].widget = forms.HiddenInput()
            else:
                org_ids = OrganizationAccess.objects.filter(user=user).values_list("organization_id", flat=True)
                if org_ids:
                    client_qs = Client.objects.filter(organization_id__in=org_ids).distinct()
                self.fields["client"].empty_label = "Выберите клиента"

            self.fields["client"].queryset = client_qs
            if selected_client_id:
                try:
                    selected_client = client_qs.get(pk=selected_client_id)
                    self.fields["client"].initial = selected_client
                except Client.DoesNotExist:
                    pass
            else:
                if not client_self.exists():
                    self.fields["client"].initial = None


@login_required
@never_cache
def pool_create(request):
    readonly = _deny_superuser_write(request)
    if readonly:
        return readonly
    blocked = _redirect_if_access_blocked(request)
    if blocked:
        return blocked

    if is_personal_user(request.user):
        client = Client.objects.filter(user=request.user, organization__isnull=True).first()
        if client and Pool.objects.filter(client=client).exists():
            messages.info(request, "Можно создать только один бассейн для личного аккаунта.")
            return redirect("pool_list")

    user_client = Client.objects.filter(user=request.user).first()
    selected_client_id = request.GET.get("client_id")

    if request.method == "POST":
        form = PoolForm(request.POST, user=request.user)
        if form.is_valid():
            pool = form.save(commit=False)
            if user_client:
                pool.client = user_client
            # если есть организация клиента или организация создателя — проставляем org_id
            if not pool.organization:
                if pool.client and getattr(pool.client, "organization_id", None):
                    pool.organization_id = pool.client.organization_id
                else:
                    org_access = (
                        OrganizationAccess.objects.filter(user=request.user, role__in=["admin", "service", "manager"])
                        .first()
                    )
                    if org_access:
                        pool.organization_id = org_access.organization_id
            pool.save()
            # дать доступ создателю
            PoolAccess.objects.get_or_create(user=request.user, pool=pool, defaults={"role": "viewer"})
            # дать доступ клиенту, к которому привязан бассейн
            if pool.client and pool.client.user:
                PoolAccess.objects.get_or_create(user=pool.client.user, pool=pool, defaults={"role": "viewer"})
            messages.success(request, "Бассейн создан")
            return redirect("pool_detail", pool_uuid=pool.uuid)
    else:
        form = PoolForm(user=request.user, selected_client_id=selected_client_id)

    return render(
        request,
        "pool_service/pool_create.html",
        {
            "form": form,
            "page_title": "Новый бассейн",
            "active_tab": "pools",
            "show_add_button": False,
            "add_url": None,
            "is_edit": False,
            "pool": None,
        },
    )


@login_required
@never_cache
def pool_edit(request, pool_uuid):
    readonly = _deny_superuser_write(request)
    if readonly:
        return readonly
    blocked = _redirect_if_access_blocked(request)
    if blocked:
        return blocked

    pool = get_object_or_404(Pool, uuid=pool_uuid)

    is_owner = pool.client and pool.client.user_id == request.user.id
    if request.user.is_superuser:
        role = "viewer"
    else:
        role = None
        pool_access = PoolAccess.objects.filter(user=request.user, pool=pool).first()
        if pool_access:
            role = pool_access.role

        org_access = OrganizationAccess.objects.filter(user=request.user, organization=pool.organization).first()
        if org_access:
            role = org_access.role

    if not role and not is_owner:
        return render(request, "403.html")
    if not is_owner and role != "admin" and not request.user.is_superuser:
        return render(request, "403.html")

    user_client = Client.objects.filter(user=request.user).first()

    if request.method == "POST":
        form = PoolForm(request.POST, instance=pool, user=request.user)
        if form.is_valid():
            updated = form.save(commit=False)
            if user_client:
                updated.client = user_client
            updated.save()
            messages.success(request, "Бассейн обновлен")
            return redirect("pool_detail", pool_uuid=pool.uuid)
    else:
        form = PoolForm(instance=pool, user=request.user)

    return render(
        request,
        "pool_service/pool_create.html",
        {
            "form": form,
            "page_title": "Редактирование бассейна",
            "active_tab": "pools",
            "show_add_button": False,
            "add_url": None,
            "is_edit": True,
            "pool": pool,
        },
    )


@login_required
def client_create_inline(request):
    readonly = _deny_superuser_write(request)
    if readonly:
        return readonly
    blocked = _redirect_if_access_blocked(request)
    if blocked:
        return blocked

    roles = list(OrganizationAccess.objects.filter(user=request.user).values_list("role", flat=True))
    if not request.user.is_superuser and not any(r in ["admin", "service", "manager"] for r in roles):
        return HttpResponseForbidden()

    if request.method == "POST":
        form = ClientCreateForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Клиент создан")
    return redirect("pool_create")


@login_required
@never_cache
def client_create(request):
    readonly = _deny_superuser_write(request)
    if readonly:
        return readonly
    blocked = _redirect_if_access_blocked(request)
    if blocked:
        return blocked

    roles = list(OrganizationAccess.objects.filter(user=request.user).values_list("role", flat=True))
    if not request.user.is_superuser and not any(r in ["admin", "service", "manager"] for r in roles):
        return HttpResponseForbidden()

    next_url = request.GET.get("next") or request.POST.get("next")
    if next_url and not next_url.startswith("/"):
        next_url = None

    if request.method == "POST":
        form = ClientCreateForm(request.POST)
        if form.is_valid():
            client = form.save()
            org_access = (
                OrganizationAccess.objects.filter(user=request.user, role__in=["admin", "service", "manager"])
                .select_related("organization")
                .first()
            )
            if org_access and org_access.organization_id:
                client.organization = org_access.organization
                client.save(update_fields=["organization"])
            messages.success(request, "Клиент создан")
            if next_url:
                return redirect(f"{next_url}?client_id={client.id}")
            return redirect("pool_list")
    else:
        form = ClientCreateForm()
    page_title = "\u0421\u043e\u0437\u0434\u0430\u043d\u0438\u0435 \u043a\u043b\u0438\u0435\u043d\u0442\u0430"
    active_tab = "clients" if not next_url else "pools"

    return render(
        request,
        "pool_service/client_create.html",
        {
            "form": form,
            "page_title": page_title,
            "active_tab": active_tab,
            "next_url": next_url,
            "is_edit": False,
        },
    )


@login_required
def client_edit(request, client_id):
    readonly = _deny_superuser_write(request)
    if readonly:
        return readonly
    blocked = _redirect_if_access_blocked(request)
    if blocked:
        return blocked

    client = get_object_or_404(Client, pk=client_id)
    if request.user.is_superuser:
        allowed = True
    else:
        org_ids = OrganizationAccess.objects.filter(user=request.user, role="admin").values_list(
            "organization_id",
            flat=True,
        )
        allowed = bool(client.organization_id and client.organization_id in org_ids)

    if not allowed:
        return HttpResponseForbidden()

    if request.method == "POST":
        form = ClientCreateForm(request.POST, instance=client)
        if form.is_valid():
            form.save()
            messages.success(request, "\u041a\u043b\u0438\u0435\u043d\u0442 \u043e\u0431\u043d\u043e\u0432\u043b\u0435\u043d.")
            return redirect("clients_list")
    else:
        form = ClientCreateForm(instance=client)

    return render(
        request,
        "pool_service/client_create.html",
        {
            "form": form,
            "page_title": "\u0420\u0435\u0434\u0430\u043a\u0442\u0438\u0440\u043e\u0432\u0430\u043d\u0438\u0435 \u043a\u043b\u0438\u0435\u043d\u0442\u0430",
            "active_tab": "clients",
            "next_url": None,
            "is_edit": True,
        },
    )


@login_required
def client_delete(request, client_id):
    readonly = _deny_superuser_write(request)
    if readonly:
        return readonly
    blocked = _redirect_if_access_blocked(request)
    if blocked:
        return blocked
    if request.method != "POST":
        return redirect("clients_list")

    client = get_object_or_404(Client, pk=client_id)
    if request.user.is_superuser:
        allowed = True
    else:
        org_ids = OrganizationAccess.objects.filter(user=request.user, role="admin").values_list(
            "organization_id",
            flat=True,
        )
        allowed = bool(client.organization_id and client.organization_id in org_ids)

    if not allowed:
        return HttpResponseForbidden()

    if Pool.objects.filter(client=client).exists():
        messages.error(
            request,
            "\u041d\u0435\u043b\u044c\u0437\u044f \u0443\u0434\u0430\u043b\u0438\u0442\u044c \u043a\u043b\u0438\u0435\u043d\u0442\u0430: \u0437\u0430 \u043d\u0438\u043c \u0437\u0430\u043a\u0440\u0435\u043f\u043b\u0435\u043d\u044b \u0431\u0430\u0441\u0441\u0435\u0439\u043d\u044b. \u0421\u043d\u0430\u0447\u0430\u043b\u0430 \u043f\u0435\u0440\u0435\u043d\u0435\u0441\u0438\u0442\u0435 \u0438\u043b\u0438 \u0443\u0434\u0430\u043b\u0438\u0442\u0435 \u0438\u0445.",
        )
        return redirect("clients_list")

    client.delete()
    messages.success(request, "\u041a\u043b\u0438\u0435\u043d\u0442 \u0443\u0434\u0430\u043b\u0435\u043d.")
    return redirect("clients_list")


def home(request):
    """    ??????? ???????? ??? ???????????????? ?????????????.
    """
    if request.user.is_authenticated:
        return redirect("pool_list")

    if request.method == "POST":
        form = EmailOrUsernameAuthenticationForm(data=request.POST)
        if form.is_valid():
            user = form.get_user()
            login(request, user)
            messages.success(
                request,
                "\u0412\u044b \u0443\u0441\u043f\u0435\u0448\u043d\u043e \u0432\u043e\u0448\u043b\u0438 \u0432 \u0441\u0438\u0441\u0442\u0435\u043c\u0443.",
            )
            redirect_url = _personal_pool_redirect(user) or reverse("pool_list")
            return redirect(redirect_url)
        messages.error(
            request,
            "\u041d\u0435\u0432\u0435\u0440\u043d\u044b\u0439 \u043b\u043e\u0433\u0438\u043d \u0438\u043b\u0438 \u043f\u0430\u0440\u043e\u043b\u044c. \u041f\u043e\u043f\u0440\u043e\u0431\u0443\u0439\u0442\u0435 \u0435\u0449\u0435 \u0440\u0430\u0437.",
        )
    else:
        form = EmailOrUsernameAuthenticationForm()

    context = {
        "form": form,
        "register_form": RegistrationForm(),
        "active_tab": "home",
    }
    return render(request, "pool_service/home.html", context)


def signup_personal(request):
    if request.user.is_authenticated:
        return redirect("pool_list")

    if request.method == "POST":
        form = PersonalSignupForm(request.POST)
        if form.is_valid():
            user = form.save()
            email_sent = _send_registration_confirmation(request, user)
            if email_sent:
                messages.success(
                    request,
                    "\u041f\u0438\u0441\u044c\u043c\u043e \u043e\u0442\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u043e \u043d\u0430 \u043f\u043e\u0447\u0442\u0443. \u041f\u043e\u0434\u0442\u0432\u0435\u0440\u0434\u0438\u0442\u0435 email \u043f\u043e \u0441\u0441\u044b\u043b\u043a\u0435 \u0438\u0437 \u043f\u0438\u0441\u044c\u043c\u0430.",
                )
            else:
                messages.error(
                    request,
                    "\u0410\u043a\u043a\u0430\u0443\u043d\u0442 \u0441\u043e\u0437\u0434\u0430\u043d, \u043d\u043e \u043f\u0438\u0441\u044c\u043c\u043e \u043d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u043e\u0442\u043f\u0440\u0430\u0432\u0438\u0442\u044c. \u0421\u0432\u044f\u0436\u0438\u0442\u0435\u0441\u044c \u0441 \u043f\u043e\u0434\u0434\u0435\u0440\u0436\u043a\u043e\u0439.",
                )
            profile, _ = Profile.objects.get_or_create(user=user)
            if not profile.phone_verification_required:
                profile.phone_verification_required = True
                profile.save(update_fields=["phone_verification_required"])
            phone_digits = _user_phone_digits(user)
            if phone_digits:
                ok, error = _start_phone_call(profile, phone_digits)
                if ok:
                    messages.info(
                        request,
                        "\u041f\u043e\u0434\u0442\u0432\u0435\u0440\u0434\u0438\u0442\u0435 \u0442\u0435\u043b\u0435\u0444\u043e\u043d, \u043f\u043e\u0437\u0432\u043e\u043d\u0438\u0432 \u043d\u0430 \u043d\u043e\u043c\u0435\u0440 \u0438\u0437 \u0438\u043d\u0441\u0442\u0440\u0443\u043a\u0446\u0438\u0438.",
                    )
                else:
                    messages.error(request, error)
            else:
                messages.error(request, "\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u043e\u043f\u0440\u0435\u0434\u0435\u043b\u0438\u0442\u044c \u0442\u0435\u043b\u0435\u0444\u043e\u043d \u0434\u043b\u044f \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d\u0438\u044f.")
            return redirect("confirm_phone", token=profile.phone_verification_token)
    else:
        form = PersonalSignupForm()

    return render(
        request,
        "registration/signup_personal.html",
        {
            "form": form,
            "active_tab": "home",
            "hide_header": True,
        },
    )


def signup_company(request):
    if request.user.is_authenticated:
        return redirect("pool_list")

    if request.method == "POST":
        form = CompanySignupForm(request.POST)
        if form.is_valid():
            user = form.save()
            email_sent = _send_registration_confirmation(request, user)
            if email_sent:
                messages.success(
                    request,
                    "\u041f\u0438\u0441\u044c\u043c\u043e \u043e\u0442\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u043e \u043d\u0430 \u043f\u043e\u0447\u0442\u0443. \u041f\u043e\u0434\u0442\u0432\u0435\u0440\u0434\u0438\u0442\u0435 email \u043f\u043e \u0441\u0441\u044b\u043b\u043a\u0435 \u0438\u0437 \u043f\u0438\u0441\u044c\u043c\u0430.",
                )
            else:
                messages.error(
                    request,
                    "\u0410\u043a\u043a\u0430\u0443\u043d\u0442 \u0441\u043e\u0437\u0434\u0430\u043d, \u043d\u043e \u043f\u0438\u0441\u044c\u043c\u043e \u043d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u043e\u0442\u043f\u0440\u0430\u0432\u0438\u0442\u044c. \u0421\u0432\u044f\u0436\u0438\u0442\u0435\u0441\u044c \u0441 \u043f\u043e\u0434\u0434\u0435\u0440\u0436\u043a\u043e\u0439.",
                )
            profile, _ = Profile.objects.get_or_create(user=user)
            if not profile.phone_verification_required:
                profile.phone_verification_required = True
                profile.save(update_fields=["phone_verification_required"])
            phone_digits = _user_phone_digits(user)
            if phone_digits:
                ok, error = _start_phone_call(profile, phone_digits)
                if ok:
                    messages.info(
                        request,
                        "\u041f\u043e\u0434\u0442\u0432\u0435\u0440\u0434\u0438\u0442\u0435 \u0442\u0435\u043b\u0435\u0444\u043e\u043d, \u043f\u043e\u0437\u0432\u043e\u043d\u0438\u0432 \u043d\u0430 \u043d\u043e\u043c\u0435\u0440 \u0438\u0437 \u0438\u043d\u0441\u0442\u0440\u0443\u043a\u0446\u0438\u0438.",
                    )
                else:
                    messages.error(request, error)
            else:
                messages.error(request, "\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u043e\u043f\u0440\u0435\u0434\u0435\u043b\u0438\u0442\u044c \u0442\u0435\u043b\u0435\u0444\u043e\u043d \u0434\u043b\u044f \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d\u0438\u044f.")
            return redirect("confirm_phone", token=profile.phone_verification_token)
    else:
        form = CompanySignupForm()

    return render(
        request,
        "registration/signup_company.html",
        {
            "form": form,
            "active_tab": "home",
            "hide_header": True,
        },
    )


def register(request):
    """Register form."""
    if request.method == "POST":
        form = RegistrationForm(request.POST)
        if form.is_valid():
            user = form.save()
            email_sent = _send_registration_confirmation(request, user)
            if email_sent:
                messages.success(
                    request,
                    "\u041f\u0438\u0441\u044c\u043c\u043e \u043e\u0442\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u043e \u043d\u0430 \u043f\u043e\u0447\u0442\u0443. \u041f\u043e\u0434\u0442\u0432\u0435\u0440\u0434\u0438\u0442\u0435 email \u043f\u043e \u0441\u0441\u044b\u043b\u043a\u0435 \u0438\u0437 \u043f\u0438\u0441\u044c\u043c\u0430.",
                )
            else:
                messages.error(
                    request,
                    "\u0420\u0435\u0433\u0438\u0441\u0442\u0440\u0430\u0446\u0438\u044f \u0437\u0430\u0432\u0435\u0440\u0448\u0435\u043d\u0430, \u043d\u043e \u043f\u0438\u0441\u044c\u043c\u043e \u043d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u043e\u0442\u043f\u0440\u0430\u0432\u0438\u0442\u044c. \u0421\u0432\u044f\u0436\u0438\u0442\u0435\u0441\u044c \u0441 \u043f\u043e\u0434\u0434\u0435\u0440\u0436\u043a\u043e\u0439.",
                )
            profile, _ = Profile.objects.get_or_create(user=user)
            if not profile.phone_verification_required:
                profile.phone_verification_required = True
                profile.save(update_fields=["phone_verification_required"])
            phone_digits = _user_phone_digits(user)
            if phone_digits:
                ok, error = _start_phone_call(profile, phone_digits)
                if ok:
                    messages.info(
                        request,
                        "\u041f\u043e\u0434\u0442\u0432\u0435\u0440\u0434\u0438\u0442\u0435 \u0442\u0435\u043b\u0435\u0444\u043e\u043d, \u043f\u043e\u0437\u0432\u043e\u043d\u0438\u0432 \u043d\u0430 \u043d\u043e\u043c\u0435\u0440 \u0438\u0437 \u0438\u043d\u0441\u0442\u0440\u0443\u043a\u0446\u0438\u0438.",
                    )
                else:
                    messages.error(request, error)
            else:
                messages.error(request, "\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u043e\u043f\u0440\u0435\u0434\u0435\u043b\u0438\u0442\u044c \u0442\u0435\u043b\u0435\u0444\u043e\u043d \u0434\u043b\u044f \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d\u0438\u044f.")
            return redirect("confirm_phone", token=profile.phone_verification_token)
    else:
        form = RegistrationForm()

    return render(
        request,
        "registration/register.html",
        {
            "form": form,
            "page_title": "\u0420\u0435\u0433\u0438\u0441\u0442\u0440\u0430\u0446\u0438\u044f",
            "active_tab": "home",
            "hide_header": True,
        },
    )

def _build_confirmation_link(request, user):
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    path = reverse("confirm_email", kwargs={"uidb64": uid, "token": token})
    base_url = getattr(settings, "SITE_URL", "")
    if base_url:
        return f"{base_url.rstrip("/")}{path}"
    return request.build_absolute_uri(path)


def _build_invite_link(request, token):
    path = reverse("invite_accept", kwargs={"token": token})
    base_url = getattr(settings, "SITE_URL", "")
    if base_url:
        return f"{base_url.rstrip('/')}{path}"
    return request.build_absolute_uri(path)


def _send_invite_email(request, invite):
    invite_url = _build_invite_link(request, invite.token)
    subject = "\u041f\u0440\u0438\u0433\u043b\u0430\u0448\u0435\u043d\u0438\u0435 \u0432 RovikPool"
    message = (
        "\u0412\u0430\u0441 \u043f\u0440\u0438\u0433\u043b\u0430\u0441\u0438\u043b\u0438 \u0432 \u043a\u043e\u043c\u043f\u0430\u043d\u0438\u044e \u0432 RovikPool.\n\n"
        "\u0414\u043b\u044f \u0430\u043a\u0442\u0438\u0432\u0430\u0446\u0438\u0438 \u0430\u043a\u043a\u0430\u0443\u043d\u0442\u0430 \u043f\u0435\u0440\u0435\u0439\u0434\u0438\u0442\u0435 \u043f\u043e \u0441\u0441\u044b\u043b\u043a\u0435:\n"
        f"{invite_url}\n\n"
        "\u0421\u0441\u044b\u043b\u043a\u0430 \u0434\u0435\u0439\u0441\u0442\u0432\u0443\u0435\u0442 24 \u0447\u0430\u0441\u0430."
    )
    try:
        send_mail(subject, message, settings.DEFAULT_FROM_EMAIL, [invite.email])
    except Exception:
        return False
    return True

def _send_registration_confirmation(request, user):
    if not user.email:
        return False
    confirm_url = _build_confirmation_link(request, user)
    site_url = getattr(settings, "SITE_URL", "").rstrip("/") or request.build_absolute_uri("/").rstrip("/")
    logo_url = f"{site_url}{static('assets/images/favicon.png')}"
    brand_url = f"{site_url}{static('assets/images/rovikpool.png')}"
    subject = html.unescape(render_to_string("registration/confirm_email_subject.txt", {}).strip())
    message = render_to_string(
        "registration/confirm_email.txt",
        {"confirm_url": confirm_url, "user": user, "site_url": site_url, "logo_url": logo_url},
    )
    html_message = render_to_string(
        "registration/confirm_email.html",
        {
            "confirm_url": confirm_url,
            "user": user,
            "site_url": site_url,
            "logo_url": logo_url,
            "brand_url": brand_url,
        },
    )
    try:
        send_mail(subject, message, settings.DEFAULT_FROM_EMAIL, [user.email], html_message=html_message)
    except Exception:
        return False
    return True

def confirm_email(request, uidb64, token):
    try:
        uid = force_str(urlsafe_base64_decode(uidb64))
        user = User.objects.get(pk=uid)
    except (TypeError, ValueError, OverflowError, User.DoesNotExist):
        user = None

    if user and default_token_generator.check_token(user, token):
        profile, _ = Profile.objects.get_or_create(user=user)
        if not profile.email_confirmed_at:
            profile.email_confirmed_at = timezone.now()
            profile.save(update_fields=["email_confirmed_at"])
        if not user.is_active:
            user.is_active = True
            user.save(update_fields=["is_active"])
        messages.success(
            request,
            "\u041f\u043e\u0447\u0442\u0430 \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d\u0430. \u0422\u0435\u043f\u0435\u0440\u044c \u043c\u043e\u0436\u043d\u043e \u0432\u043e\u0439\u0442\u0438.",
        )
        return redirect("login")

    messages.error(
        request,
        "\u0421\u0441\u044b\u043b\u043a\u0430 \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d\u0438\u044f \u043d\u0435\u0432\u0435\u0440\u043d\u0430 \u0438\u043b\u0438 \u0443\u0441\u0442\u0430\u0440\u0435\u043b\u0430.",
    )
    return redirect("login")


@login_required
def resend_email_confirmation(request):
    if request.method != "POST":
        return redirect("profile")

    profile, _ = Profile.objects.get_or_create(user=request.user)
    if profile.email_confirmed_at:
        messages.info(request, "\u041f\u043e\u0447\u0442\u0430 \u0443\u0436\u0435 \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d\u0430.")
        return redirect("profile")

    if not request.user.email:
        messages.error(request, "\u0423\u043a\u0430\u0436\u0438\u0442\u0435 email, \u0447\u0442\u043e\u0431\u044b \u043f\u043e\u043b\u0443\u0447\u0438\u0442\u044c \u0441\u0441\u044b\u043b\u043a\u0443 \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d\u0438\u044f.")
        return redirect("profile")

    sent = _send_registration_confirmation(request, request.user)
    if sent:
        messages.success(
            request,
            "\u041f\u0438\u0441\u044c\u043c\u043e \u0441\u043e \u0441\u0441\u044b\u043b\u043a\u043e\u0439 \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d\u0438\u044f \u043e\u0442\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u043e.",
        )
    else:
        messages.error(
            request,
            "\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u043e\u0442\u043f\u0440\u0430\u0432\u0438\u0442\u044c \u043f\u0438\u0441\u044c\u043c\u043e. \u041f\u043e\u043f\u0440\u043e\u0431\u0443\u0439\u0442\u0435 \u043f\u043e\u0437\u0436\u0435.",
        )
    return redirect("profile")


@csrf_protect
@never_cache
def confirm_phone(request, token):
    profile = Profile.objects.select_related("user").filter(phone_verification_token=token).first()
    if not profile:
        return render(
            request,
            "registration/confirm_phone.html",
            {
                "error_message": "\u0421\u0441\u044b\u043b\u043a\u0430 \u0434\u043b\u044f \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d\u0438\u044f \u0442\u0435\u043b\u0435\u0444\u043e\u043d\u0430 \u043d\u0435\u0434\u0435\u0439\u0441\u0442\u0432\u0438\u0442\u0435\u043b\u044c\u043d\u0430.",
                "active_tab": "home",
                "hide_header": True,
            },
        )

    user = profile.user
    phone_digits = _user_phone_digits(user)
    if not phone_digits:
        return render(
            request,
            "registration/confirm_phone.html",
            {
                "error_message": "\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u043e\u043f\u0440\u0435\u0434\u0435\u043b\u0438\u0442\u044c \u043d\u043e\u043c\u0435\u0440 \u0442\u0435\u043b\u0435\u0444\u043e\u043d\u0430.",
                "active_tab": "home",
                "hide_header": True,
            },
        )

    if request.method == "POST" and not profile.phone_confirmed_at:
        action = request.POST.get("action") or ""
        if action == "start_call":
            ok, error = _start_phone_call(profile, phone_digits)
            if ok:
                messages.success(request, "\u0417\u0432\u043e\u043d\u043e\u043a \u0434\u043b\u044f \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d\u0438\u044f \u043e\u0442\u043f\u0440\u0430\u0432\u043b\u0435\u043d.")
            else:
                messages.error(request, error)
        elif action == "check_call":
            ok, error = _check_phone_call(profile)
            if ok:
                messages.success(request, "\u0422\u0435\u043b\u0435\u0444\u043e\u043d \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d.")
            else:
                messages.error(request, error)
        elif action == "send_sms":
            ok, error = _send_phone_sms(profile, phone_digits)
            if ok:
                messages.success(request, "\u0421\u041c\u0421 \u0441 \u043a\u043e\u0434\u043e\u043c \u043e\u0442\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u0430.")
            else:
                messages.error(request, error)
        elif action == "verify_sms":
            code = (request.POST.get("sms_code") or "").strip()
            ok, error = _verify_phone_sms(profile, code)
            if ok:
                messages.success(request, "\u0422\u0435\u043b\u0435\u0444\u043e\u043d \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d.")
            else:
                messages.error(request, error)

    remaining_attempts = _remaining_phone_attempts(profile)
    call_phone = profile.phone_verification_call_phone or ""
    call_phone_display = f"+{call_phone}" if call_phone and call_phone.startswith("7") else call_phone

    return render(
        request,
        "registration/confirm_phone.html",
        {
            "profile": profile,
            "phone_confirmed": bool(profile.phone_confirmed_at),
            "call_phone": call_phone,
            "call_phone_display": call_phone_display,
            "remaining_attempts": remaining_attempts,
            "sms_sent": bool(profile.phone_sms_sent_at),
            "expires_at": profile.phone_verification_expires_at,
            "active_tab": "home",
            "hide_header": True,
        },
    )


@csrf_exempt
def smsru_callback(request):
    data = request.POST or request.GET
    check_id = data.get("check_id") or data.get("check") or data.get("id")
    status = data.get("check_status") or data.get("status") or ""
    if not check_id:
        return HttpResponse("missing check_id", status=400)
    profile = Profile.objects.filter(phone_verification_check_id=check_id).first()
    if not profile:
        return HttpResponse("not found", status=404)
    if str(status) == "401":
        _mark_phone_confirmed(profile)
    return HttpResponse("OK")

@login_required
def pool_detail(request, pool_uuid):
    """Детальная страница бассейна с показателями и доступами."""
    pool = get_object_or_404(Pool, uuid=pool_uuid)

    if request.user.is_superuser:
        role = "admin"
    else:
        role = None
        pool_access = PoolAccess.objects.filter(user=request.user, pool=pool).first()
        if pool_access:
            role = pool_access.role

        org_access = OrganizationAccess.objects.filter(user=request.user, organization=pool.organization).first()
        if org_access:
            role = org_access.role

    is_owner = pool.client and pool.client.user_id == request.user.id
    if not role and is_owner:
        role = "admin"
    if not role:
        return render(request, "403.html")

    readings_list = WaterReading.objects.filter(pool=pool).select_related("added_by").order_by("-date")

    per_page = _parse_per_page(request.GET.get("per_page"), 20)

    paginator = Paginator(readings_list, per_page)
    page_number = request.GET.get("page")
    readings = paginator.get_page(page_number)
    query_params = request.GET.copy()
    query_params.pop("page", None)

    editable_reading_ids = []
    for reading in readings:
        if _reading_edit_allowed(reading, request.user):
            editable_reading_ids.append(reading.id)

    context = {
        "pool": pool,
        "readings": readings,
        "per_page": per_page,
        "role": role,
        "pagination_query": query_params.urlencode(),
        "page_title": None,
        "page_subtitle": None,
        "show_search": False,
        "show_add_button": False,
        "add_url": None,
        "active_tab": "pools",
        "editable_reading_ids": editable_reading_ids,
    }
    return render(request, "pool_service/pool_detail.html", context)


@login_required
def yandex_suggest(request):
    query = (request.GET.get("text") or "").strip()
    if not query:
        return JsonResponse({"items": []})
    api_key = getattr(settings, "YANDEX_SUGGEST_API_KEY", "")
    if not api_key:
        return JsonResponse({"items": []}, status=500)
    params = {
        "apikey": api_key,
        "text": query,
        "lang": "ru_RU",
        "results": "7",
        "types": "geo",
    }
    url = "https://suggest-maps.yandex.ru/v1/suggest?" + urlencode(params)
    req = Request(url, headers={"User-Agent": "PoolService/1.0"})
    try:
        with urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception:
        return JsonResponse({"items": []}, status=502)
    results = data.get("results", [])
    items = []
    for item in results:
        title = item.get("title", {})
        text = title.get("text")
        if text:
            items.append(text)
    return JsonResponse({"items": items})


@login_required
def readings_all(request):
    """Все показания для доступных бассейнов."""
    if request.user.is_superuser:
        pools = Pool.objects.all()

    elif OrganizationAccess.objects.filter(user=request.user).exists():
        org_access = OrganizationAccess.objects.get(user=request.user)
        pools = Pool.objects.filter(organization=org_access.organization)

    else:
        pools = Pool.objects.filter(accesses__user=request.user)

    readings_list = (
        WaterReading.objects.filter(pool__in=pools)
        .select_related("pool", "added_by")
        .order_by("-date")
    )

    per_page = _parse_per_page(request.GET.get("per_page"), 50)
    paginator = Paginator(readings_list, per_page)
    page_number = request.GET.get("page")
    readings = paginator.get_page(page_number)
    query_params = request.GET.copy()
    query_params.pop("page", None)

    return render(
        request,
        "pool_service/readings_all.html",
        {
            "readings": readings,
            "page_title": "История посещений",
            "page_subtitle": "Записывайте показания",
            "show_search": False,
            "show_add_button": False,
            "add_url": None,
            "active_tab": "readings",
            "per_page": per_page,
            "pagination_query": query_params.urlencode(),
        },
    )


@csrf_protect
@never_cache
def water_reading_create(request, pool_uuid):
    readonly = _deny_superuser_write(request)
    if readonly:
        return readonly
    blocked = _redirect_if_access_blocked(request)
    if blocked:
        return blocked

    """Создание нового замера для выбранного бассейна."""
    pool = get_object_or_404(Pool, uuid=pool_uuid)

    if request.method == "POST":
        form = WaterReadingForm(request.POST)
        if form.is_valid():
            reading = form.save(commit=False)
            reading.date = reading.date.replace(tzinfo=None)
            reading.pool = pool
            reading.added_by = request.user
            reading.save()
            messages.success(request, "Показания добавлены")
            return redirect("pool_detail", pool_uuid=pool.uuid)
        else:
            messages.error(request, "Не удалось сохранить показания, проверьте данные")
    else:
        form = WaterReadingForm()

    return render(request, "pool_service/water_reading_form.html", {"form": form, "pool": pool, "active_tab": "pools"})


@login_required
def water_reading_edit(request, reading_uuid):
    readonly = _deny_superuser_write(request)
    if readonly:
        return readonly
    blocked = _redirect_if_access_blocked(request)
    if blocked:
        return blocked

    reading = get_object_or_404(WaterReading.objects.select_related("pool"), uuid=reading_uuid)

    if not _reading_edit_allowed(reading, request.user):
        messages.error(request, "Редактирование доступно только автору записи в течение 30 минут.")
        return redirect("pool_detail", pool_uuid=reading.pool.uuid)

    if request.method == "POST":
        form = WaterReadingForm(request.POST, instance=reading)
        if form.is_valid():
            updated = form.save(commit=False)
            updated.date = reading.date
            updated.pool = reading.pool
            updated.added_by = reading.added_by
            updated.save()
            messages.success(request, "Запись обновлена.")
            return redirect("pool_detail", pool_uuid=reading.pool.uuid)
        messages.error(request, "Не удалось обновить запись. Проверьте форму.")
    else:
        form = WaterReadingForm(instance=reading)

    return render(
        request,
        "pool_service/water_reading_form.html",
        {"form": form, "pool": reading.pool, "active_tab": "pools", "is_edit": True, "reading": reading},
    )


@login_required
def profile_view(request):
    """Профиль текущего пользователя с основными контактами и правами доступа."""
    profile, _ = Profile.objects.get_or_create(user=request.user)
    org_accesses = OrganizationAccess.objects.filter(user=request.user).select_related("organization")
    pool_accesses = PoolAccess.objects.filter(user=request.user).select_related("pool", "pool__client")

    phone = None
    if profile and hasattr(profile, "phone"):
        phone = profile.phone
    if not phone and request.user.username and request.user.username.isdigit():
        phone = request.user.username

    if request.user.is_superuser:
        role_level = "Администратор системы"
    elif org_accesses.exists():
        unique_roles = sorted({access.get_role_display() for access in org_accesses})
        role_level = ", ".join(unique_roles)
    elif pool_accesses.exists():
        unique_roles = sorted({access.get_role_display() for access in pool_accesses})
        role_level = ", ".join(unique_roles)
    else:
        role_level = "Пользователь"

    email_confirmed = bool(getattr(profile, "email_confirmed_at", None))
    phone_confirmed = bool(getattr(profile, "phone_confirmed_at", None))
    confirm_phone_url = None
    if not phone_confirmed and getattr(profile, "phone_verification_token", None):
        confirm_phone_url = reverse("confirm_phone", kwargs={"token": profile.phone_verification_token})

    context = {
        "page_title": "Профиль",
        "page_subtitle": "Данные аккаунта и уровни доступа",
        "active_tab": "profile",
        "show_search": False,
        "show_add_button": False,
        "add_url": None,
        "user_full_name": request.user.get_full_name() or request.user.username,
        "username": request.user.username,
        "email": request.user.email,
        "phone": phone,
        "timezone": getattr(profile, "timezone", "Не указан"),
        "last_login": request.user.last_login,
        "date_joined": request.user.date_joined,
        "role_level": role_level,
        "org_accesses": org_accesses,
        "pool_accesses": pool_accesses,
        "email_confirmed": email_confirmed,
        "phone_confirmed": phone_confirmed,
        "confirm_phone_url": confirm_phone_url,
    }
    return render(request, "pool_service/profile.html", context)


class CustomLoginView(LoginView):
    template_name = "registration/login.html"
    success_url = reverse_lazy("pool_list")
    extra_context = {"hide_header": True}
    authentication_form = EmailOrUsernameAuthenticationForm

    def form_valid(self, form):
        messages.success(self.request, "\u0412\u044b \u0443\u0441\u043f\u0435\u0448\u043d\u043e \u0432\u043e\u0448\u043b\u0438 \u0432 \u0441\u0438\u0441\u0442\u0435\u043c\u0443.")
        return super().form_valid(form)

    def get_success_url(self):
        personal_url = _personal_pool_redirect(self.request.user)
        if personal_url:
            return personal_url
        return super().get_success_url()

    def form_invalid(self, form):
        messages.error(self.request, "\u041d\u0435\u0432\u0435\u0440\u043d\u044b\u0439 \u043b\u043e\u0433\u0438\u043d \u0438\u043b\u0438 \u043f\u0430\u0440\u043e\u043b\u044c. \u041f\u043e\u043f\u0440\u043e\u0431\u0443\u0439\u0442\u0435 \u0435\u0449\u0435 \u0440\u0430\u0437.")
        return super().form_invalid(form)


@login_required
def password_change_inline(request):
    if request.method != "POST":
        return redirect("profile")

    new_password1 = (request.POST.get("new_password1") or "").strip()
    new_password2 = (request.POST.get("new_password2") or "").strip()
    if not new_password1 or not new_password2:
        messages.error(request, "Заполните оба поля пароля.")
        return redirect("profile")
    if new_password1 != new_password2:
        messages.error(request, "Пароли не совпадают.")
        return redirect("profile")

    try:
        validate_password(new_password1, user=request.user)
    except forms.ValidationError as exc:
        for error in exc:
            messages.error(request, error)
        return redirect("profile")

    request.user.set_password(new_password1)
    request.user.save(update_fields=["password"])
    update_session_auth_hash(request, request.user)
    messages.success(request, "Пароль обновлен.")
    return redirect("profile")
