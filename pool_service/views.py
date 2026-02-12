from zoneinfo import ZoneInfo

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

from django.views.decorators.http import require_POST

from django.urls import reverse, reverse_lazy

from django.db import connection

from django.db.models import Count, Q, Max, Case, When, Value, IntegerField

from django.http import HttpResponse, HttpResponseForbidden, HttpResponseNotFound, JsonResponse

from django.utils import timezone

from django.utils.crypto import get_random_string

from django.utils.encoding import force_bytes, force_str

from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode

from django.template.loader import render_to_string

from django.views.decorators.csrf import csrf_exempt
from django.core.files.base import ContentFile
from io import BytesIO
from pathlib import Path
from PIL import Image, ImageOps
import html

from django.templatetags.static import static

from django.conf import settings

from django.contrib.sitemaps.views import sitemap as sitemap_view

import uuid

from urllib.parse import urlencode

from urllib.request import urlopen, Request

import json

import calendar
from datetime import date, timedelta, time

from calendar import monthrange



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

    ClientInviteForm,

    ClientInviteAcceptForm,

    normalize_phone,

    OrganizationWaterNormsForm,

    CrmItemForm,

    CrmServiceIssueForm,

    CRM_STAGE_CHOICES_BY_DIRECTION,

    ServiceTaskForm,

)

from .sitemaps import HomeSitemap

from .seo import is_indexable_host

from .services.phone_verification import (

    smsru_callcheck_add,

    smsru_callcheck_status,

    smsru_send_sms,

)

from .services.notifications import notify_reading_out_of_range, notify_superusers, notify_task_assignment





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





def _format_call_phone_display(phone):

    digits = "".join(filter(str.isdigit, phone or ""))

    if digits.startswith("8") and len(digits) == 11:

        digits = "7" + digits[1:]

    if len(digits) == 11 and digits.startswith("7"):

        return f"+7 {digits[1:4]} {digits[4:7]} {digits[7:9]} {digits[9:11]}"

    return phone





def _format_profile_phone_display(phone):

    digits = normalize_phone(phone) if phone else ""

    if not digits:

        digits = "".join(filter(str.isdigit, phone or ""))

        if len(digits) == 11 and digits.startswith(("7", "8")):

            digits = digits[1:]

    if len(digits) == 10:

        return f"+7 {digits[0:3]} {digits[3:6]} {digits[6:]}"

    return phone





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

from .models import (

    OrganizationAccess,

    Pool,

    PoolAccess,

    WaterReading,

    Client,

    Organization,

    OrganizationInvite,

    Profile,

    ClientAccess,

    ClientInvite,

    OrganizationPaymentRequest,

    Notification,

    OrganizationWaterNorms,

    PushSubscription,

    CrmItem,

    CrmItemPhoto,

    ServiceVisitPlan,

    ServiceTask,

    ServiceTaskChange,

)

from .services.permissions import (

    is_personal_free,

    is_personal_user,

    is_org_access_blocked,

    personal_pool,

    organization_for_user,

)

from django import forms



PER_PAGE_CHOICES = {20, 50, 100}

INVITE_EXPIRY_HOURS = 24

ADMIN_ROLES = ["owner", "admin"]

ORG_STAFF_ROLES = ["owner", "admin", "service", "manager"]

CRM_ALLOWED_ROLES = {"owner", "admin", "service"}



CRM_DIRECTION_META = {

    CrmItem.DIRECTION_SERVICE: {

        "label": "Сервис",

        "subtitle": "Работы, замены и контроль объектов",

        "icon": "bi-wrench-adjustable",

    },

    CrmItem.DIRECTION_PROJECT: {

        "label": "Проекты",

        "subtitle": "Проектные и строительные объекты",

        "icon": "bi-building",

    },

    CrmItem.DIRECTION_SALES: {

        "label": "Продажи",

        "subtitle": "Воронка продаж и сделки",

        "icon": "bi-graph-up",

    },

    CrmItem.DIRECTION_TENDER: {

        "label": "Торги",

        "subtitle": "Конкурсы и тендеры",

        "icon": "bi-journal-check",

    },

}

CRM_STAGE_LABELS = {value: label for direction in CRM_STAGE_CHOICES_BY_DIRECTION.values() for value, label in direction}

ISSUE_PHOTO_MAX_SIZE = 1600
ISSUE_PHOTO_JPEG_QUALITY = 82


def _compress_issue_photo(uploaded_file, max_size=ISSUE_PHOTO_MAX_SIZE, quality=ISSUE_PHOTO_JPEG_QUALITY):
    try:
        uploaded_file.seek(0)
        img = Image.open(uploaded_file)
        img = ImageOps.exif_transpose(img)
        if img.mode in ("RGBA", "LA"):
            background = Image.new("RGB", img.size, (255, 255, 255))
            background.paste(img, mask=img.split()[-1])
            img = background
        elif img.mode != "RGB":
            img = img.convert("RGB")
        resample = Image.Resampling.LANCZOS if hasattr(Image, "Resampling") else Image.LANCZOS
        img.thumbnail((max_size, max_size), resample)
        buffer = BytesIO()
        img.save(buffer, format="JPEG", quality=quality, optimize=True, progressive=True)
        buffer.seek(0)
        original_name = getattr(uploaded_file, "name", "") or "photo"
        stem = Path(original_name).stem or "photo"
        return ContentFile(buffer.read(), name=f"{stem}.jpg")
    except Exception:
        try:
            uploaded_file.seek(0)
        except Exception:
            pass
        return uploaded_file


def _can_access_crm(user):
    if not user or not user.is_authenticated:

        return False

    if user.is_superuser:

        return True

    roles = OrganizationAccess.objects.filter(user=user).values_list("role", flat=True)

    return any(role in CRM_ALLOWED_ROLES for role in roles)





def _parse_per_page(value, default):

    try:

        per_page = int(value)

    except (TypeError, ValueError):

        return default

    return per_page if per_page in PER_PAGE_CHOICES else default





def _add_months(dt, months):

    month_index = (dt.month - 1) + months

    year = dt.year + month_index // 12

    month = month_index % 12 + 1

    day = min(dt.day, monthrange(year, month)[1])

    return dt.replace(year=year, month=month, day=day)





def _extend_org_paid_until(org, months):

    now = timezone.now()

    base = org.paid_until if org.paid_until and org.paid_until > now else now

    new_until = _add_months(base, months)

    previous = org.paid_until

    org.paid_until = new_until

    org.plan_type = Organization.PLAN_COMPANY_PAID

    org.save(update_fields=["paid_until", "plan_type"])

    return previous, new_until





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





def _client_access_for_user(user):

    if not user or not getattr(user, "is_authenticated", False):

        return None

    return ClientAccess.objects.filter(user=user).select_related("client").first()





def _pool_role_for_user(user, pool):

    if user.is_superuser:

        return "admin"



    pool_access = PoolAccess.objects.filter(user=user, pool=pool).first()

    client_access = ClientAccess.objects.filter(user=user, client=pool.client).first()

    org_roles = list(

        OrganizationAccess.objects.filter(user=user, organization=pool.organization).values_list("role", flat=True)

    )



    pool_role = pool_access.role if pool_access else None

    client_role = None

    if client_access:

        client_role = client_access.role or "viewer"

        if client_role == "staff":

            client_role = "editor"



    org_role = None

    if org_roles:

        if any(role in ADMIN_ROLES for role in org_roles):

            org_role = "admin"

        elif "service" in org_roles:

            org_role = "service"

        elif "manager" in org_roles:

            org_role = "manager"



    role = org_role or client_role or pool_role

    if pool.client and pool.client.user_id == user.id:

        role = "admin"

    return role





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





def _deny_client_staff_write(request):

    if request.user.is_superuser:

        return None

    if _client_access_for_user(request.user) and not OrganizationAccess.objects.filter(user=request.user).exists():

        return HttpResponseForbidden()

    return None





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

    """Список объектов обслуживания."""

    if is_personal_user(request.user):

        redirect_url = _personal_pool_redirect(request.user)

        if redirect_url:

            return redirect(redirect_url)

    org_access = None
    client_access = None

    if request.user.is_superuser:

        pools = Pool.objects.all()

    else:

        org_access = (
            OrganizationAccess.objects.filter(user=request.user)
            .select_related("organization")
            .first()
        )

        if org_access:

            pools = Pool.objects.filter(

                Q(organization=org_access.organization) | Q(client__organization=org_access.organization)

            ).distinct()

        else:

            client_access = _client_access_for_user(request.user)

            if client_access:

                pools = Pool.objects.filter(client=client_access.client)

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

        pools = pools.order_by("service_suspended", "-last_reading", "client__name", "address")

    elif sort == "recent_asc":

        pools = pools.order_by("service_suspended", "last_reading", "client__name", "address")

    elif sort == "client_desc":

        pools = pools.order_by("service_suspended", "-client__name", "address")

    elif sort == "created_desc":

        pools = pools.order_by("service_suspended", "-id")

    elif sort == "created_asc":

        pools = pools.order_by("service_suspended", "id")

    else:

        pools = pools.order_by("service_suspended", "client__name", "address")



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

    if client_access:

        allow_pool_create = False

    if request.user.is_superuser:

        allow_pool_create = False

    if client_access:

        page_title = f"Объекты: {client_access.client.name}"

    else:

        page_title = "Мой объект" if personal_user else "Объекты"

    page_action_label = "Добавить объект" if allow_pool_create else None

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

    org_access = (

        OrganizationAccess.objects.filter(user=request.user, role__in=ADMIN_ROLES)

        .select_related("organization")

        .first()

    )

    organization = org_access.organization if org_access else None

    if not organization:

        return render(request, "403.html")

    payment_requests = []

    if organization:

        payment_requests = (

            OrganizationPaymentRequest.objects.filter(organization=organization)

            .select_related("requested_by", "decided_by")

            .order_by("-created_at")[:50]

        )

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

            "payment_requests": payment_requests,

            "can_request_payment": bool(org_access),

            "payment_org": organization,

        },

    )





@login_required

def billing_request(request):

    if request.method != "POST":

        return redirect("billing")



    org_access = (

        OrganizationAccess.objects.filter(user=request.user, role__in=ADMIN_ROLES)

        .select_related("organization")

        .first()

    )

    if not org_access:

        return HttpResponseForbidden()



    try:

        months = int(request.POST.get("months") or "0")

    except ValueError:

        months = 0

    if months not in {1, 3, 6, 12}:

        messages.error(request, "\u041d\u0435\u0432\u0435\u0440\u043d\u044b\u0439 \u0441\u0440\u043e\u043a \u043f\u0440\u043e\u0434\u043b\u0435\u043d\u0438\u044f.")

        return redirect("billing")



    organization = org_access.organization

    if OrganizationPaymentRequest.objects.filter(

        organization=organization,

        status=OrganizationPaymentRequest.STATUS_PENDING,

    ).exists():

        messages.info(request, "\u0417\u0430\u044f\u0432\u043a\u0430 \u0443\u0436\u0435 \u043e\u0442\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u0430.")

        return redirect("billing")



    note = (request.POST.get("note") or "").strip()

    OrganizationPaymentRequest.objects.create(

        organization=organization,

        requested_by=request.user,

        months=months,

        status=OrganizationPaymentRequest.STATUS_PENDING,

        note=note,

    )

    messages.success(request, "\u0417\u0430\u044f\u0432\u043a\u0430 \u043d\u0430 \u043f\u0440\u043e\u0434\u043b\u0435\u043d\u0438\u0435 \u043e\u0442\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u0430.")

    return redirect("billing")





@login_required

def billing_admin(request):

    if not request.user.is_superuser:

        return HttpResponseForbidden()



    if request.method == "POST":

        action = (request.POST.get("action") or "").strip()

        if action == "approve_request":

            request_id = request.POST.get("request_id")

            req = get_object_or_404(

                OrganizationPaymentRequest,

                pk=request_id,

                status=OrganizationPaymentRequest.STATUS_PENDING,

            )

            before, after = _extend_org_paid_until(req.organization, req.months)

            req.status = OrganizationPaymentRequest.STATUS_APPROVED

            req.decided_at = timezone.now()

            req.decided_by = request.user

            req.paid_until_before = before

            req.paid_until_after = after

            req.save(

                update_fields=[

                    "status",

                    "decided_at",

                    "decided_by",

                    "paid_until_before",

                    "paid_until_after",

                ]

            )

            messages.success(request, "\u041f\u0440\u043e\u0434\u043b\u0435\u043d\u0438\u0435 \u043e\u0434\u043e\u0431\u0440\u0435\u043d\u043e.")

            return redirect("billing_admin")



        if action == "reject_request":

            request_id = request.POST.get("request_id")

            req = get_object_or_404(

                OrganizationPaymentRequest,

                pk=request_id,

                status=OrganizationPaymentRequest.STATUS_PENDING,

            )

            req.status = OrganizationPaymentRequest.STATUS_REJECTED

            req.decided_at = timezone.now()

            req.decided_by = request.user

            req.save(update_fields=["status", "decided_at", "decided_by"])

            messages.info(request, "\u0417\u0430\u044f\u0432\u043a\u0430 \u043e\u0442\u043a\u043b\u043e\u043d\u0435\u043d\u0430.")

            return redirect("billing_admin")



        if action == "manual_extend":

            org_id = request.POST.get("organization_id")

            try:

                months = int(request.POST.get("months") or "0")

            except ValueError:

                months = 0

            if months not in {1, 3, 6, 12}:

                messages.error(request, "\u041d\u0435\u0432\u0435\u0440\u043d\u044b\u0439 \u0441\u0440\u043e\u043a \u043f\u0440\u043e\u0434\u043b\u0435\u043d\u0438\u044f.")

                return redirect("billing_admin")

            organization = get_object_or_404(Organization, pk=org_id)

            before, after = _extend_org_paid_until(organization, months)

            note = (request.POST.get("note") or "").strip()

            OrganizationPaymentRequest.objects.create(

                organization=organization,

                requested_by=request.user,

                months=months,

                status=OrganizationPaymentRequest.STATUS_APPROVED,

                decided_at=timezone.now(),

                decided_by=request.user,

                paid_until_before=before,

                paid_until_after=after,

                note=note,

            )

            messages.success(request, "\u041f\u0440\u043e\u0434\u043b\u0435\u043d\u0438\u0435 \u0432\u044b\u043f\u043e\u043b\u043d\u0435\u043d\u043e.")

            return redirect("billing_admin")



    pending_requests = (

        OrganizationPaymentRequest.objects.filter(status=OrganizationPaymentRequest.STATUS_PENDING)

        .select_related("organization", "requested_by")

        .order_by("created_at")

    )

    history_requests = (

        OrganizationPaymentRequest.objects.exclude(status=OrganizationPaymentRequest.STATUS_PENDING)

        .select_related("organization", "requested_by", "decided_by")

        .order_by("-created_at")[:50]

    )

    organizations = Organization.objects.order_by("name")



    return render(

        request,

        "pool_service/billing_admin.html",

        {

            "page_title": "\u041f\u0440\u043e\u0434\u043b\u0435\u043d\u0438\u044f \u0442\u0430\u0440\u0438\u0444\u0430",

            "page_subtitle": "\u0417\u0430\u044f\u0432\u043a\u0438 \u0438 \u0440\u0443\u0447\u043d\u044b\u0435 \u043f\u0440\u043e\u0434\u043b\u0435\u043d\u0438\u044f",

            "active_tab": "billing_admin",

            "pending_requests": pending_requests,

            "history_requests": history_requests,

            "organizations": organizations,

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



    is_admin = request.user.is_superuser or OrganizationAccess.objects.filter(user=request.user, role__in=ADMIN_ROLES).exists()

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

                    OrganizationAccess.objects.filter(user=request.user, role__in=ADMIN_ROLES)

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

                    roles = form.cleaned_data.get("roles") or []

                    if invite:

                        invite.first_name = form.cleaned_data["first_name"]

                        invite.last_name = form.cleaned_data["last_name"]

                        invite.phone = form.cleaned_data.get("phone", "")

                        invite.roles = roles

                        invite.role = roles[0] if roles else invite.role

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

                            role=roles[0] if roles else "service",

                            roles=roles,

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

    is_admin = request.user.is_superuser or OrganizationAccess.objects.filter(user=request.user, role__in=ADMIN_ROLES).exists()

    if not is_admin:

        return HttpResponseForbidden()

    if not request.user.is_superuser:

        allowed = OrganizationAccess.objects.filter(

            user=request.user,

            role__in=ADMIN_ROLES,

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





@login_required

def invite_delete(request, invite_id):

    readonly = _deny_superuser_write(request)

    if readonly:

        return readonly

    blocked = _redirect_if_access_blocked(request)

    if blocked:

        return blocked

    if request.method != "POST":

        return redirect("users")



    invite = get_object_or_404(OrganizationInvite, pk=invite_id)

    is_admin = request.user.is_superuser or OrganizationAccess.objects.filter(user=request.user, role__in=ADMIN_ROLES).exists()

    if not is_admin:

        return HttpResponseForbidden()

    if not request.user.is_superuser:

        allowed = OrganizationAccess.objects.filter(

            user=request.user,

            role__in=ADMIN_ROLES,

            organization=invite.organization,

        ).exists()

        if not allowed:

            return HttpResponseForbidden()

    if invite.accepted_at:

        messages.info(request, "Приглашение уже принято.")

        return redirect("users")



    invite.delete()

    messages.success(request, "Приглашение удалено.")

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

                roles = list(invite.roles or [])

                if not roles and invite.role:

                    roles = [invite.role]

                allowed_roles = {"admin", "service", "manager"}

                roles = [role for role in roles if role in allowed_roles] or ["service"]

                for role in roles:

                    OrganizationAccess.objects.get_or_create(

                        user=user,

                        organization=invite.organization,

                        role=role,

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

def client_staff(request, client_id):

    client = get_object_or_404(Client, pk=client_id)

    is_org_staff = OrganizationAccess.objects.filter(

        user=request.user,

        organization=client.organization,

        role__in=ORG_STAFF_ROLES,

    ).exists()

    is_org_admin = OrganizationAccess.objects.filter(

        user=request.user,

        organization=client.organization,

        role__in=ADMIN_ROLES,

    ).exists()



    if not request.user.is_superuser and not is_org_staff:

        return HttpResponseForbidden()



    staff_accesses = (

        ClientAccess.objects.filter(client=client)

        .select_related("user")

        .order_by("user__last_name", "user__first_name")

    )

    invites = (

        ClientInvite.objects.filter(client=client, accepted_at__isnull=True)

        .select_related("invited_by")

        .order_by("-created_at")

    )

    can_manage = is_org_staff and not request.user.is_superuser



    return render(

        request,

        "pool_service/client_staff.html",

        {

            "client": client,

            "staff_accesses": staff_accesses,

            "invites": invites,

            "page_title": "Сотрудники клиента",

            "page_subtitle": client.name,

            "page_action_label": None,

            "page_action_url": None,

            "active_tab": "clients",

            "show_search": False,

            "show_add_button": False,

            "add_url": None,

            "can_manage": can_manage,

            "can_manage_roles": is_org_admin and not request.user.is_superuser,

        },

    )





@login_required

def client_staff_toggle_block(request, access_id):

    readonly = _deny_superuser_write(request)

    if readonly:

        return readonly

    blocked = _redirect_if_access_blocked(request)

    if blocked:

        return blocked

    if request.method != "POST":

        return redirect("clients_list")



    access = get_object_or_404(

        ClientAccess.objects.select_related("client", "client__organization", "user"),

        pk=access_id,

    )

    is_org_staff = OrganizationAccess.objects.filter(

        user=request.user,

        organization=access.client.organization,

        role__in=ORG_STAFF_ROLES,

    ).exists()

    if not is_org_staff:

        return HttpResponseForbidden()

    if access.user_id == request.user.id:

        messages.error(request, "Нельзя заблокировать самого себя.")

        return redirect("client_staff", client_id=access.client_id)

    if access.user.is_superuser:

        return HttpResponseForbidden()



    access.user.is_active = not access.user.is_active

    access.user.save(update_fields=["is_active"])

    if access.user.is_active:

        messages.success(request, "Сотрудник разблокирован.")

    else:

        messages.success(request, "Сотрудник заблокирован.")

    return redirect("client_staff", client_id=access.client_id)





@login_required

def client_staff_delete(request, access_id):

    readonly = _deny_superuser_write(request)

    if readonly:

        return readonly

    blocked = _redirect_if_access_blocked(request)

    if blocked:

        return blocked

    if request.method != "POST":

        return redirect("clients_list")



    access = get_object_or_404(

        ClientAccess.objects.select_related("client", "client__organization", "user"),

        pk=access_id,

    )

    is_org_staff = OrganizationAccess.objects.filter(

        user=request.user,

        organization=access.client.organization,

        role__in=ORG_STAFF_ROLES,

    ).exists()

    if not is_org_staff:

        return HttpResponseForbidden()

    if access.user_id == request.user.id:

        messages.error(request, "Нельзя удалить самого себя.")

        return redirect("client_staff", client_id=access.client_id)

    if access.user.is_superuser:

        return HttpResponseForbidden()



    PoolAccess.objects.filter(user=access.user, pool__client=access.client).delete()

    ClientAccess.objects.filter(user=access.user, client=access.client).delete()

    messages.success(request, "Сотрудник удален.")

    return redirect("client_staff", client_id=access.client_id)





@login_required

def client_staff_change_role(request, access_id):

    readonly = _deny_superuser_write(request)

    if readonly:

        return readonly

    blocked = _redirect_if_access_blocked(request)

    if blocked:

        return blocked

    if request.method != "POST":

        return redirect("clients_list")



    access = get_object_or_404(

        ClientAccess.objects.select_related("client", "client__organization", "user"),

        pk=access_id,

    )

    is_org_admin = OrganizationAccess.objects.filter(

        user=request.user,

        organization=access.client.organization,

        role__in=ADMIN_ROLES,

    ).exists()

    if not is_org_admin:

        return HttpResponseForbidden()

    if access.user_id == request.user.id:

        messages.error(request, "Нельзя менять свою роль.")

        return redirect("client_staff", client_id=access.client_id)

    if access.user.is_superuser:

        return HttpResponseForbidden()



    new_role = (request.POST.get("role") or "").strip()

    allowed_roles = ["editor", "viewer"]

    if new_role not in allowed_roles:

        messages.error(request, "Недопустимая роль.")

        return redirect("client_staff", client_id=access.client_id)

    if access.role == new_role:

        messages.info(request, "Роль уже установлена.")

        return redirect("client_staff", client_id=access.client_id)



    access.role = new_role

    access.save(update_fields=["role"])

    messages.success(request, "Роль обновлена.")

    return redirect("client_staff", client_id=access.client_id)





@login_required

def client_invite_create(request, client_id):

    readonly = _deny_superuser_write(request)

    if readonly:

        return readonly

    blocked = _redirect_if_access_blocked(request)

    if blocked:

        return blocked



    client = get_object_or_404(Client, pk=client_id)

    is_org_staff = OrganizationAccess.objects.filter(

        user=request.user,

        organization=client.organization,

        role__in=ORG_STAFF_ROLES,

    ).exists()

    if not is_org_staff:

        return HttpResponseForbidden()



    if request.method == "POST":

        form = ClientInviteForm(request.POST)

        if form.is_valid():

            email = form.cleaned_data["email"].strip().lower()

            if User.objects.filter(email__iexact=email).exists():

                form.add_error("email", "Пользователь с таким email уже зарегистрирован")

            else:

                now = timezone.now()

                expires_at = now + timedelta(hours=INVITE_EXPIRY_HOURS)

                invite = ClientInvite.objects.filter(

                    client=client,

                    email__iexact=email,

                    accepted_at__isnull=True,

                ).first()

                if invite:

                    invite.first_name = form.cleaned_data["first_name"]

                    invite.last_name = form.cleaned_data["last_name"]

                    invite.phone = form.cleaned_data["phone"]

                    invite.role = form.cleaned_data["role"]

                    invite.token = uuid.uuid4()

                    invite.expires_at = expires_at

                    invite.invited_by = request.user

                    invite.last_sent_at = now

                else:

                    invite = ClientInvite.objects.create(

                        client=client,

                        invited_by=request.user,

                        email=email,

                        first_name=form.cleaned_data["first_name"],

                        last_name=form.cleaned_data["last_name"],

                        phone=form.cleaned_data["phone"],

                        role=form.cleaned_data["role"],

                        expires_at=expires_at,

                        last_sent_at=now,

                    )

                invite.save()

                if _send_client_invite_email(request, invite):

                    messages.success(request, "Приглашение отправлено.")

                    return redirect("client_staff", client_id=client.id)

                messages.error(request, "Не удалось отправить письмо.")

    else:

        form = ClientInviteForm()



    return render(

        request,

        "pool_service/client_invite_create.html",

        {

            "form": form,

            "client": client,

            "page_title": "Приглашение сотрудника клиента",

            "active_tab": "clients",

            "show_search": False,

            "show_add_button": False,

            "add_url": None,

        },

    )





@login_required

def client_invite_resend(request, invite_id, client_id=None):

    readonly = _deny_superuser_write(request)

    if readonly:

        return readonly

    blocked = _redirect_if_access_blocked(request)

    if blocked:

        return blocked

    if request.method != "POST":

        return redirect("clients_list")



    invite = get_object_or_404(ClientInvite, pk=invite_id)

    is_org_staff = OrganizationAccess.objects.filter(

        user=request.user,

        organization=invite.client.organization,

        role__in=ORG_STAFF_ROLES,

    ).exists()

    if not is_org_staff:

        return HttpResponseForbidden()



    if invite.accepted_at:

        messages.info(request, "Приглашение уже принято.")

        return redirect("client_staff", client_id=invite.client_id)



    now = timezone.now()

    invite.token = uuid.uuid4()

    invite.expires_at = now + timedelta(hours=INVITE_EXPIRY_HOURS)

    invite.last_sent_at = now

    invite.save(update_fields=["token", "expires_at", "last_sent_at"])



    if _send_client_invite_email(request, invite):

        messages.success(request, "Приглашение отправлено повторно.")

    else:

        messages.error(request, "Не удалось отправить письмо.")

    return redirect("client_staff", client_id=invite.client_id)





@login_required

def client_invite_delete(request, invite_id, client_id=None):

    readonly = _deny_superuser_write(request)

    if readonly:

        return readonly

    blocked = _redirect_if_access_blocked(request)

    if blocked:

        return blocked

    if request.method != "POST":

        return redirect("clients_list")



    invite = get_object_or_404(ClientInvite, pk=invite_id)

    is_org_staff = OrganizationAccess.objects.filter(

        user=request.user,

        organization=invite.client.organization,

        role__in=ORG_STAFF_ROLES,

    ).exists()

    if not is_org_staff:

        return HttpResponseForbidden()

    if invite.accepted_at:

        messages.info(request, "Приглашение уже принято.")

        return redirect("client_staff", client_id=invite.client_id)



    invite.delete()

    messages.success(request, "Приглашение удалено.")

    return redirect("client_staff", client_id=invite.client_id)





def client_invite_accept(request, token):

    invite = ClientInvite.objects.filter(token=token).select_related("client").first()

    if not invite:

        return render(

            request,

            "registration/client_invite_accept.html",

            {"invite": None, "error_message": "Ссылка недействительна."},

        )



    if invite.accepted_at:

        return render(

            request,

            "registration/client_invite_accept.html",

            {"invite": invite, "error_message": "Приглашение уже принято."},

        )



    if invite.is_expired():

        return render(

            request,

            "registration/client_invite_accept.html",

            {"invite": invite, "error_message": "Ссылка просрочена. Попросите администратора отправить приглашение повторно."},

        )



    if request.method == "POST":

        form = ClientInviteAcceptForm(request.POST)

        if form.is_valid():

            email = invite.email.strip().lower()

            if User.objects.filter(email__iexact=email).exists():

                form.add_error("password1", "Аккаунт с этим email уже существует")

            else:

                user = User.objects.create_user(

                    username=email,

                    email=email,

                    first_name=form.cleaned_data["first_name"],

                    last_name=form.cleaned_data["last_name"],

                )

                user.set_password(form.cleaned_data["password1"])

                user.save()

                invite_role = invite.role

                if invite_role == "staff":

                    invite_role = "editor"

                ClientAccess.objects.create(

                    user=user,

                    client=invite.client,

                    role=invite_role,

                    phone=form.cleaned_data["phone"],

                )

                invite.accepted_at = timezone.now()

                invite.accepted_user = user

                invite.save(update_fields=["accepted_at", "accepted_user"])

                login(request, user)

                messages.success(request, "Аккаунт активирован.")

                return redirect("pool_list")

    else:

        form = ClientInviteAcceptForm(

            initial={

                "first_name": invite.first_name,

                "last_name": invite.last_name,

                "phone": invite.phone,

            }

        )



    return render(

        request,

        "registration/client_invite_accept.html",

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

        role__in=ADMIN_ROLES,

        organization=access.organization,

    ).exists()

    if not is_admin:

        return HttpResponseForbidden()

    if access.user_id == request.user.id:

        messages.error(request, "\u041d\u0435\u043b\u044c\u0437\u044f \u0437\u0430\u0431\u043b\u043e\u043a\u0438\u0440\u043e\u0432\u0430\u0442\u044c \u0441\u0430\u043c\u043e\u0433\u043e \u0441\u0435\u0431\u044f.")

        return redirect("users")

    if OrganizationAccess.objects.filter(

        user=access.user,

        organization=access.organization,

        role="owner",

    ).exists():

        messages.error(request, "\u041d\u0435\u043b\u044c\u0437\u044f \u0437\u0430\u0431\u043b\u043e\u043a\u0438\u0440\u043e\u0432\u0430\u0442\u044c \u0432\u043b\u0430\u0434\u0435\u043b\u044c\u0446\u0430.")

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

        role__in=ADMIN_ROLES,

        organization=access.organization,

    ).exists()

    if not is_admin:

        return HttpResponseForbidden()

    if access.user_id == request.user.id:

        messages.error(request, "\u041d\u0435\u043b\u044c\u0437\u044f \u0443\u0434\u0430\u043b\u0438\u0442\u044c \u0441\u0430\u043c\u043e\u0433\u043e \u0441\u0435\u0431\u044f.")

        return redirect("users")

    if OrganizationAccess.objects.filter(

        user=access.user,

        organization=access.organization,

        role="owner",

    ).exists():

        messages.error(request, "\u041d\u0435\u043b\u044c\u0437\u044f \u0443\u0434\u0430\u043b\u0438\u0442\u044c \u0432\u043b\u0430\u0434\u0435\u043b\u044c\u0446\u0430.")

        return redirect("users")

    if access.user.is_superuser:

        return HttpResponseForbidden()



    PoolAccess.objects.filter(user=access.user, pool__organization=access.organization).delete()

    PoolAccess.objects.filter(user=access.user, pool__client__organization=access.organization).delete()

    OrganizationAccess.objects.filter(user=access.user, organization=access.organization).delete()

    messages.success(request, "\u0421\u043e\u0442\u0440\u0443\u0434\u043d\u0438\u043a \u0443\u0434\u0430\u043b\u0435\u043d.")

    return redirect("users")





@login_required

def staff_change_role(request, access_id):

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

        role__in=ADMIN_ROLES,

        organization=access.organization,

    ).exists()

    if not is_admin:

        return HttpResponseForbidden()

    if access.user_id == request.user.id:

        messages.error(request, "\u041d\u0435\u043b\u044c\u0437\u044f \u043c\u0435\u043d\u044f\u0442\u044c \u0441\u0432\u043e\u044e \u0440\u043e\u043b\u044c.")

        return redirect("users")

    if OrganizationAccess.objects.filter(

        user=access.user,

        organization=access.organization,

        role="owner",

    ).exists():

        messages.error(request, "\u041d\u0435\u043b\u044c\u0437\u044f \u043c\u0435\u043d\u044f\u0442\u044c \u0440\u043e\u043b\u044c \u0432\u043b\u0430\u0434\u0435\u043b\u044c\u0446\u0430.")

        return redirect("users")

    if access.user.is_superuser:

        return HttpResponseForbidden()



    allowed_roles = ["admin", "service", "manager"]

    roles = request.POST.getlist("roles")

    if not roles:

        single = (request.POST.get("role") or "").strip()

        if single:

            roles = [single]

    roles = [role for role in roles if role in allowed_roles]

    if not roles:

        messages.error(request, "\u041d\u0443\u0436\u043d\u043e \u0432\u044b\u0431\u0440\u0430\u0442\u044c \u0445\u043e\u0442\u044f \u0431\u044b \u043e\u0434\u043d\u0443 \u0440\u043e\u043b\u044c.")

        return redirect("users")



    OrganizationAccess.objects.filter(

        user=access.user,

        organization=access.organization,

        role__in=allowed_roles,

    ).delete()

    for role in roles:

        OrganizationAccess.objects.get_or_create(

            user=access.user,

            organization=access.organization,

            role=role,

        )

    messages.success(request, "\u0420\u043e\u043b\u0438 \u043e\u0431\u043d\u043e\u0432\u043b\u0435\u043d\u044b.")

    return redirect("users")





@login_required

def pool_staff_change_role(request, access_id):

    readonly = _deny_superuser_write(request)

    if readonly:

        return readonly

    blocked = _redirect_if_access_blocked(request)

    if blocked:

        return blocked

    if request.method != "POST":

        return redirect("users")



    access = get_object_or_404(

        PoolAccess.objects.select_related("pool", "pool__organization", "user"),

        pk=access_id,

    )

    is_admin = request.user.is_superuser or OrganizationAccess.objects.filter(

        user=request.user,

        role__in=ADMIN_ROLES,

        organization=access.pool.organization,

    ).exists()

    if not is_admin:

        return HttpResponseForbidden()

    if access.user_id == request.user.id:

        messages.error(request, "\u041d\u0435\u043b\u044c\u0437\u044f \u043c\u0435\u043d\u044f\u0442\u044c \u0441\u0432\u043e\u044e \u0440\u043e\u043b\u044c.")

        return redirect("users")

    if access.user.is_superuser:

        return HttpResponseForbidden()



    new_role = (request.POST.get("role") or "").strip()

    allowed_roles = ["editor", "viewer"]

    if new_role not in allowed_roles:

        messages.error(request, "\u041d\u0435\u0434\u043e\u043f\u0443\u0441\u0442\u0438\u043c\u0430\u044f \u0440\u043e\u043b\u044c.")

        return redirect("users")

    if access.role == new_role:

        messages.info(request, "\u0420\u043e\u043b\u044c \u0443\u0436\u0435 \u0443\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d\u0430.")

        return redirect("users")



    access.role = new_role

    access.save(update_fields=["role"])

    messages.success(request, "\u0420\u043e\u043b\u044c \u043e\u0431\u043d\u043e\u0432\u043b\u0435\u043d\u0430.")

    return redirect("users")





@login_required

def users_view(request):

    """Список пользователей для суперюзеров/админов, сервисники видят только персонал объектов."""

    roles = list(OrganizationAccess.objects.filter(user=request.user).values_list("role", flat=True))

    is_org_owner = "owner" in roles

    is_org_admin = "admin" in roles or is_org_owner

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

        org_staff_qs = (

            OrganizationAccess.objects.filter(**org_filter)

            .select_related("organization", "user")

            .order_by("organization__name", "user__last_name")

        )

        role_labels = dict(OrganizationAccess.ROLE_CHOICES)

        role_order = ["owner", "admin", "manager", "service"]

        grouped = {}

        for access in org_staff_qs:

            key = (access.organization_id, access.user_id)

            if key not in grouped:

                grouped[key] = {

                    "id": access.id,

                    "user": access.user,

                    "organization": access.organization,

                    "roles": set(),

                    "is_owner": False,

                }

            grouped[key]["roles"].add(access.role)

            if access.role == "owner":

                grouped[key]["is_owner"] = True



        org_staff = list(grouped.values())

        for item in org_staff:

            roles = item["roles"]

            ordered = [role for role in role_order if role in roles]

            ordered += sorted([role for role in roles if role not in role_order])

            item["roles"] = ordered

            item["role_labels"] = ", ".join(role_labels.get(role, role) for role in ordered)



        org_invites = (

            OrganizationInvite.objects.filter(**org_filter, accepted_at__isnull=True)

            .select_related("organization", "invited_by")

            .order_by("-created_at")

        )

        for invite in org_invites:

            invite_roles = list(invite.roles or [])

            if not invite_roles and invite.role:

                invite_roles = [invite.role]

            ordered = [role for role in role_order if role in invite_roles]

            ordered += sorted([role for role in invite_roles if role not in role_order])

            invite.role_labels = ", ".join(role_labels.get(role, role) for role in ordered)

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

            "page_subtitle": "?????????? ????????? ???????? ? ???? ?? ????????",

            "org_staff": org_staff,

            "org_invites": org_invites,

            "organizations": organizations,

            "page_action_label": "\u0414\u043e\u0431\u0430\u0432\u0438\u0442\u044c \u0441\u043e\u0442\u0440\u0443\u0434\u043d\u0438\u043a\u0430" if is_org_admin else None,

            "page_action_url": reverse("invite_create") if is_org_admin else None,

            "can_manage_roles": is_org_admin and not request.user.is_superuser,

            "pool_staff": pool_staff,

            "active_tab": "users",

            "show_search": False,

            "show_add_button": False,

            "add_url": None,

        },

    )





@login_required

def clients_list(request):

    allowed_roles = ORG_STAFF_ROLES

    is_allowed = request.user.is_superuser or OrganizationAccess.objects.filter(

        user=request.user, role__in=allowed_roles

    ).exists()

    if not is_allowed:

        return HttpResponseForbidden()



    if request.user.is_superuser:

        clients_qs = Client.objects.all()

        pool_staff_qs = PoolAccess.objects.all()

    else:

        org_ids = OrganizationAccess.objects.filter(user=request.user).values_list(

            "organization_id",

            flat=True,

        )

        clients_qs = Client.objects.filter(organization_id__in=org_ids).distinct()

        pool_staff_qs = PoolAccess.objects.filter(pool__organization_id__in=org_ids)



    clients = list(

        clients_qs.annotate(pool_count=Count("pool")).select_related("organization").order_by("name")

    )

    companies = [client for client in clients if client.client_type == "legal"]

    private_contacts = [client for client in clients if client.client_type != "legal"]



    staff_by_client = {}

    company_ids = [client.id for client in companies]

    if company_ids:

        staff_accesses = (

            ClientAccess.objects.filter(client_id__in=company_ids)

            .select_related("user")

            .order_by("user__last_name", "user__first_name")

        )

        for access in staff_accesses:

            staff_by_client.setdefault(access.client_id, []).append(access)



    for company in companies:

        contact_name = " ".join(part for part in [company.first_name, company.last_name] if part).strip()

        primary_contact = {

            "name": contact_name,

            "position": company.contact_position,

            "phone": company.phone,

            "email": company.email,

        }

        if not any(primary_contact.values()):

            primary_contact = None

        company.primary_contact = primary_contact

        company.staff_contacts = staff_by_client.get(company.id, [])



    pool_staff = (

        pool_staff_qs.select_related("pool", "pool__client", "pool__organization", "user")

        .order_by("pool__client__name", "pool__address", "user__last_name", "user__first_name")

    )



    return render(

        request,

        "pool_service/clients.html",

        {

            "page_title": "\u041a\u043b\u0438\u0435\u043d\u0442\u044b",

            "page_subtitle": "\u041a\u043e\u043d\u0442\u0430\u043a\u0442\u044b \u0438 \u043e\u0431\u044a\u0435\u043a\u0442\u044b \u043a\u043b\u0438\u0435\u043d\u0442\u043e\u0432 \u0432 \u043e\u0434\u043d\u043e\u043c \u0441\u043f\u0438\u0441\u043a\u0435",

            "companies": companies,

            "private_contacts": private_contacts,

            "pool_staff": pool_staff,

            "active_tab": "clients",

            "page_action_label": None if request.user.is_superuser else "\u0414\u043e\u0431\u0430\u0432\u0438\u0442\u044c \u043a\u043b\u0438\u0435\u043d\u0442\u0430",

            "page_action_url": None if request.user.is_superuser else reverse("client_create"),

            "show_search": False,

            "show_add_button": False,

            "add_url": None,

        },

    )





@login_required

def crm_index(request):

    if not _can_access_crm(request.user):

        return HttpResponseForbidden()



    primary_cards = []

    for key, meta in CRM_DIRECTION_META.items():

        primary_cards.append(

            {

                "label": meta["label"],

                "subtitle": meta["subtitle"],

                "url": reverse("crm_list", kwargs={"direction": key}),

                "direction": key,

                "icon": meta["icon"],

            }

        )



    secondary_cards = [

        {

            "label": "Клиенты",

            "subtitle": "Контакты и объекты",

            "url": reverse("clients_list"),

            "icon": "bi-people",

        },

        {

            "label": "Задачи",

            "subtitle": "Планирование работ",

            "url": reverse("crm_tasks"),

            "icon": "bi-list-check",

        },

    ]



    return render(

        request,

        "pool_service/crm_index.html",

        {

            "page_title": "CRM",

            "page_subtitle": "\u0421\u0435\u0440\u0432\u0438\u0441, \u043f\u0440\u043e\u0435\u043a\u0442\u044b, \u043f\u0440\u043e\u0434\u0430\u0436\u0438 \u0438 \u0442\u043e\u0440\u0433\u0438",

            "active_tab": "crm",

            "primary_cards": primary_cards,

            "secondary_cards": secondary_cards,

            "show_search": False,

            "show_add_button": False,

            "add_url": None,

        },

    )





@login_required

def crm_tasks(request):

    if not _can_access_crm(request.user):

        return HttpResponseForbidden()



    return render(

        request,

        "pool_service/crm_tasks.html",

        {

            "page_title": "\u0417\u0430\u0434\u0430\u0447\u0438",

            "page_subtitle": "\u041f\u043b\u0430\u043d\u0438\u0440\u043e\u0432\u0430\u043d\u0438\u0435 \u0438 \u043a\u043e\u043d\u0442\u0440\u043e\u043b\u044c \u0440\u0430\u0431\u043e\u0442",

            "active_tab": "crm",

            "show_search": False,

            "show_add_button": False,

            "add_url": None,

        },

    )





def _crm_get_org_for_request(request):

    org = organization_for_user(request.user)

    if org:

        return org

    if not request.user.is_superuser:

        return None

    org_id = request.GET.get("org_id") or request.POST.get("org_id")

    if not org_id:

        return None

    return Organization.objects.filter(id=org_id).first()





@login_required

def crm_list(request, direction):

    if not _can_access_crm(request.user):

        return HttpResponseForbidden()

    if direction not in CRM_DIRECTION_META:

        return HttpResponseNotFound("Unknown CRM direction")



    org = _crm_get_org_for_request(request)

    items = CrmItem.objects.filter(direction=direction)

    if org:

        items = items.filter(organization=org)

    elif not request.user.is_superuser:

        return HttpResponseForbidden()



    items = items.select_related("client", "pool", "responsible", "organization")

    search_query = (request.GET.get("q") or "").strip()

    if search_query:

        search_filter = (

            Q(title__icontains=search_query)

            | Q(client__name__icontains=search_query)

            | Q(client__company_name__icontains=search_query)

            | Q(pool__address__icontains=search_query)

            | Q(description__icontains=search_query)

            | Q(service_works__icontains=search_query)

            | Q(equipment_replacement__icontains=search_query)

            | Q(photo_url__icontains=search_query)

            | Q(stage__icontains=search_query)

            | Q(urgency__icontains=search_query)

            | Q(responsible__first_name__icontains=search_query)

            | Q(responsible__last_name__icontains=search_query)

            | Q(responsible__username__icontains=search_query)

        )

        if request.user.is_superuser:

            search_filter |= Q(organization__name__icontains=search_query)

        items = items.filter(search_filter)



    sort_key = (request.GET.get("sort") or "").strip()

    sort_dir = (request.GET.get("dir") or "asc").strip().lower()

    if sort_dir not in {"asc", "desc"}:

        sort_dir = "asc"

    sort_fields = {

        "title": ["title"],

        "client": ["client__name"],

        "pool": ["pool__address"],

        "stage": ["stage"],

        "urgency": ["urgency"],

        "amount": ["amount"],

        "responsible": ["responsible__last_name", "responsible__first_name", "responsible__username"],

        "description": ["description"],

        "service_works": ["service_works"],

        "equipment_replacement": ["equipment_replacement"],

        "photo_url": ["photo_url"],

        "organization": ["organization__name"],

    }

    sort_field_list = sort_fields.get(sort_key)



    service_done_stage = None

    if direction == CrmItem.DIRECTION_SERVICE:

        service_done_stage = CrmItem.STAGE_SERVICE_DONE

        items = items.annotate(

            is_done=Case(

                When(stage=service_done_stage, then=Value(1)),

                default=Value(0),

                output_field=IntegerField(),

            )

        )

    order_fields = []

    if direction == CrmItem.DIRECTION_SERVICE:

        order_fields.append("is_done")

    if sort_field_list:

        if sort_dir == "desc":

            order_fields += [f"-{field}" for field in sort_field_list]

        else:

            order_fields += sort_field_list

        order_fields.append("-updated_at")

    else:

        order_fields.append("-updated_at")

    items = items.order_by(*order_fields)

    for item in items:

        item.stage_label = CRM_STAGE_LABELS.get(item.stage, item.stage)



    query_params = request.GET.copy()

    query_params.pop("sort", None)

    query_params.pop("dir", None)

    base_query = query_params.urlencode()

    table_columns = [

        "title",

        "client",

        "pool",

        "stage",

        "amount",

        "responsible",

        "description",

    ]

    if direction == CrmItem.DIRECTION_SERVICE:

        table_columns.extend(["urgency", "service_works", "equipment_replacement", "photo_url"])

    if request.user.is_superuser:

        table_columns.append("organization")

    crm_table_colspan = len(table_columns) + 1



    return render(

        request,

        "pool_service/crm_list.html",

        {

            "page_title": CRM_DIRECTION_META[direction]["label"],

            "page_subtitle": CRM_DIRECTION_META[direction]["subtitle"],

            "active_tab": "crm",

            "direction": direction,

            "direction_label": CRM_DIRECTION_META[direction]["label"],

            "items": items,

            "page_action_label": "\u0421\u043e\u0437\u0434\u0430\u0442\u044c",
            "page_action_url": reverse("crm_create", kwargs={"direction": direction}),

            "service_done_stage": service_done_stage,

            "search_query": search_query,

            "current_sort": sort_key,

            "current_dir": sort_dir,

            "base_query": base_query,

            "crm_table_colspan": crm_table_colspan,

        },

    )

@login_required

def crm_create(request, direction):

    readonly = _deny_superuser_write(request)

    if readonly:

        return readonly

    blocked = _redirect_if_access_blocked(request)

    if blocked:

        return blocked

    if not _can_access_crm(request.user):

        return HttpResponseForbidden()

    if direction not in CRM_DIRECTION_META:

        return HttpResponseNotFound("Unknown CRM direction")



    org = _crm_get_org_for_request(request)

    if not org:

        messages.error(request, "Не найдена организация для CRM.")

        return redirect("crm_index")



    if request.method == "POST":

        form = CrmItemForm(request.POST, direction=direction, organization=org)

        if form.is_valid():

            item = form.save(commit=False)

            item.organization = org

            item.direction = direction

            item.created_by = request.user

            if not item.stage:

                item.stage = CRM_STAGE_CHOICES_BY_DIRECTION[direction][0][0]

            item.save()

            messages.success(request, "Запись CRM создана.")

            return redirect("crm_list", direction=direction)

    else:

        form = CrmItemForm(direction=direction, organization=org)



    return render(

        request,

        "pool_service/crm_form.html",

        {

            "page_title": f"{CRM_DIRECTION_META[direction]['label']}: создание",

            "page_subtitle": CRM_DIRECTION_META[direction]["subtitle"],

            "active_tab": "crm",

            "direction": direction,

            "direction_label": CRM_DIRECTION_META[direction]["label"],

            "form": form,

        },

    )





@login_required

def crm_edit(request, direction, item_id):

    readonly = _deny_superuser_write(request)

    if readonly:

        return readonly

    blocked = _redirect_if_access_blocked(request)

    if blocked:

        return blocked

    if not _can_access_crm(request.user):

        return HttpResponseForbidden()

    if direction not in CRM_DIRECTION_META:

        return HttpResponseNotFound("Unknown CRM direction")



    item = get_object_or_404(CrmItem, pk=item_id, direction=direction)

    if not request.user.is_superuser:

        org = organization_for_user(request.user)

        if not org or item.organization_id != org.id:

            return HttpResponseForbidden()

    else:

        org = item.organization



    if request.method == "POST":

        form = CrmItemForm(request.POST, instance=item, direction=direction, organization=org)

        if form.is_valid():

            form.save()

            messages.success(request, "Запись CRM обновлена.")

            return redirect("crm_list", direction=direction)

    else:

        form = CrmItemForm(instance=item, direction=direction, organization=org)



    item_photo_urls = []

    for photo in item.photos.all():

        if photo.image:

            item_photo_urls.append(photo.image.url)

    if item.photo:

        item_photo_urls.append(item.photo.url)

    if item.photo_url:

        item_photo_urls.append(item.photo_url)



    return render(

        request,

        "pool_service/crm_form.html",

        {

            "page_title": f"{CRM_DIRECTION_META[direction]['label']}: редактирование",

            "page_subtitle": CRM_DIRECTION_META[direction]["subtitle"],

            "active_tab": "crm",

            "direction": direction,

            "direction_label": CRM_DIRECTION_META[direction]["label"],

            "form": form,

            "item": item,

            "item_photo_urls": item_photo_urls,

            "item_photo_urls_json": json.dumps(item_photo_urls, ensure_ascii=False),

        },

    )







@login_required

@never_cache

def pool_create(request):

    readonly = _deny_superuser_write(request)

    if readonly:

        return readonly

    client_staff_block = _deny_client_staff_write(request)

    if client_staff_block:

        return client_staff_block

    blocked = _redirect_if_access_blocked(request)

    if blocked:

        return blocked



    if is_personal_user(request.user):

        client = Client.objects.filter(user=request.user, organization__isnull=True).first()

        if client and Pool.objects.filter(client=client).exists():

            messages.info(request, "Можно создать только один объект для личного аккаунта.")

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

                        OrganizationAccess.objects.filter(user=request.user, role__in=ORG_STAFF_ROLES)

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

            messages.success(request, "Объект создан")

            return redirect("pool_detail", pool_uuid=pool.uuid)

    else:

        form = PoolForm(user=request.user, selected_client_id=selected_client_id)



    return render(

        request,

        "pool_service/pool_create.html",

        {

            "form": form,

            "page_title": "Новый объект",

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

    client_staff_block = _deny_client_staff_write(request)

    if client_staff_block:

        return client_staff_block

    blocked = _redirect_if_access_blocked(request)

    if blocked:

        return blocked



    pool = get_object_or_404(Pool, uuid=pool_uuid)
    is_water_object = pool.object_type == Pool.OBJECT_TYPE_WATER

    role = _pool_role_for_user(request.user, pool)

    if role not in {"admin", "service"}:

        return render(request, "403.html")



    user_client = Client.objects.filter(user=request.user).first()



    if request.method == "POST":

        form = PoolForm(request.POST, instance=pool, user=request.user)

        if form.is_valid():

            updated = form.save(commit=False)

            if user_client:

                updated.client = user_client

            updated.save()

            messages.success(request, "Объект обновлен")

            return redirect("pool_detail", pool_uuid=pool.uuid)

    else:

        form = PoolForm(instance=pool, user=request.user)



    return render(

        request,

        "pool_service/pool_create.html",

        {

            "form": form,

            "page_title": "Редактирование объекта",

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

    client_staff_block = _deny_client_staff_write(request)

    if client_staff_block:

        return client_staff_block

    blocked = _redirect_if_access_blocked(request)

    if blocked:

        return blocked



    roles = list(OrganizationAccess.objects.filter(user=request.user).values_list("role", flat=True))

    if not request.user.is_superuser and not any(r in ORG_STAFF_ROLES for r in roles):

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

    client_staff_block = _deny_client_staff_write(request)

    if client_staff_block:

        return client_staff_block

    blocked = _redirect_if_access_blocked(request)

    if blocked:

        return blocked



    roles = list(OrganizationAccess.objects.filter(user=request.user).values_list("role", flat=True))

    if not request.user.is_superuser and not any(r in ORG_STAFF_ROLES for r in roles):

        return HttpResponseForbidden()



    next_url = request.GET.get("next") or request.POST.get("next")

    if next_url and not next_url.startswith("/"):

        next_url = None



    if request.method == "POST":

        form = ClientCreateForm(request.POST)

        if form.is_valid():

            client = form.save()

            org_access = (

                OrganizationAccess.objects.filter(user=request.user, role__in=ORG_STAFF_ROLES)

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

    client_staff_block = _deny_client_staff_write(request)

    if client_staff_block:

        return client_staff_block

    blocked = _redirect_if_access_blocked(request)

    if blocked:

        return blocked



    client = get_object_or_404(Client, pk=client_id)

    if request.user.is_superuser:

        allowed = True

    else:

        org_ids = OrganizationAccess.objects.filter(user=request.user, role__in=ADMIN_ROLES).values_list(

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

    client_staff_block = _deny_client_staff_write(request)

    if client_staff_block:

        return client_staff_block

    blocked = _redirect_if_access_blocked(request)

    if blocked:

        return blocked

    if request.method != "POST":

        return redirect("clients_list")



    client = get_object_or_404(Client, pk=client_id)

    if request.user.is_superuser:

        allowed = True

    else:

        org_ids = OrganizationAccess.objects.filter(user=request.user, role__in=ADMIN_ROLES).values_list(

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

    """??????? ???????? ??? ???????? ???????."""


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

            notify_superusers(

                title="Новый частный пользователь",

                message=f"{user.get_full_name() or user.username} ({user.email or user.username})",

                kind="new_personal",

                level="info",

                action_url=reverse("users"),

            )

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

            org_access = OrganizationAccess.objects.filter(user=user, role="owner").select_related("organization").first()

            organization = org_access.organization if org_access else None

            notify_superusers(

                title="Новая компания",

                message=f"{organization.name if organization else 'Организация'} — {user.get_full_name() or user.username}",

                kind="new_company",

                level="info",

                action_url=reverse("users"),

            )

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





def _build_client_invite_link(request, token):

    path = reverse("client_invite_accept", kwargs={"token": token})

    return request.build_absolute_uri(path)





def _send_client_invite_email(request, invite):

    invite_url = _build_client_invite_link(request, invite.token)

    subject = "\u041f\u0440\u0438\u0433\u043b\u0430\u0448\u0435\u043d\u0438\u0435 \u0432 RovikPool"

    message = (

        f"\u0412\u0430\u0441 \u043f\u0440\u0438\u0433\u043b\u0430\u0441\u0438\u043b\u0438 \u043a\u0430\u043a \u0441\u043e\u0442\u0440\u0443\u0434\u043d\u0438\u043a\u0430 \u043a\u043b\u0438\u0435\u043d\u0442\u0430: {invite.client.name}.\n\n"

        f"\u0414\u043b\u044f \u0430\u043a\u0442\u0438\u0432\u0430\u0446\u0438\u0438 \u0430\u043a\u043a\u0430\u0443\u043d\u0442\u0430 \u043f\u0435\u0440\u0435\u0439\u0434\u0438\u0442\u0435 \u043f\u043e \u0441\u0441\u044b\u043b\u043a\u0435:\n"

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

        messages.success(request, "\u041f\u043e\u0447\u0442\u0430 \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d\u0430.")

        if request.user.is_authenticated:

            return redirect("profile")

        login(request, user)

        personal_url = _personal_pool_redirect(user)

        return redirect(personal_url or "pool_list")



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

    call_phone_display = _format_call_phone_display(call_phone)



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

            "hide_install_banner": True,

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

    """Детальная страница объекта с показателями и доступами."""

    pool = get_object_or_404(Pool, uuid=pool_uuid)



    role = _pool_role_for_user(request.user, pool)

    if not role:

        return render(request, "403.html")



    can_edit_pool = role in {"admin", "service"}

    can_add_reading = role in {"editor", "service", "admin"}



    readings_list = WaterReading.objects.filter(pool=pool).select_related("added_by").order_by("-date")



    per_page = _parse_per_page(request.GET.get("per_page"), 20)



    paginator = Paginator(readings_list, per_page)

    page_number = request.GET.get("page")

    readings = paginator.get_page(page_number)

    query_params = request.GET.copy()

    query_params.pop("page", None)



    editable_reading_ids = []

    if can_add_reading:

        for reading in readings:

            if _reading_edit_allowed(reading, request.user):

                editable_reading_ids.append(reading.id)



    show_service_issues = False

    can_manage_service_issues = False

    service_issues = []

    service_issue_form = None

    service_issue_stage_choices = CRM_STAGE_CHOICES_BY_DIRECTION.get(CrmItem.DIRECTION_SERVICE, [])

    if pool.organization_id:

        org_staff_access = OrganizationAccess.objects.filter(

            user=request.user,

            organization=pool.organization,


        ).exists()

        show_service_issues = org_staff_access or request.user.is_superuser

        can_manage_service_issues = org_staff_access and role in {"admin", "service", "manager"}

        if show_service_issues:

            service_issues = (

                CrmItem.objects.filter(

                    direction=CrmItem.DIRECTION_SERVICE,

                    pool=pool,

                    organization=pool.organization,

                )

                .select_related("responsible", "created_by")

                .prefetch_related("photos")

                .order_by("-created_at")

            )

            for issue in service_issues:

                issue.stage_label = CRM_STAGE_LABELS.get(issue.stage, issue.stage)

                photo_urls = []

                if issue.photo:

                    photo_urls.append(issue.photo.url)

                if issue.photo_url:

                    photo_urls.append(issue.photo_url)

                for photo in issue.photos.all():

                    if photo.image:

                        photo_urls.append(photo.image.url)

                issue.photo_urls = photo_urls

                issue.photo_count = len(photo_urls)

                issue.photo_extra_count = max(0, len(photo_urls) - 3)

                issue.photo_urls_json = json.dumps(photo_urls, ensure_ascii=False)

            if can_manage_service_issues:

                service_issue_form = CrmServiceIssueForm()



    context = {

        "pool": pool,
        "is_water_object": pool.object_type == Pool.OBJECT_TYPE_WATER,

        "readings": readings,

        "per_page": per_page,

        "role": role,

        "can_edit_pool": can_edit_pool,

        "can_add_reading": can_add_reading,

        "pagination_query": query_params.urlencode(),

        "page_title": None,

        "page_subtitle": None,

        "show_search": False,

        "show_add_button": False,

        "add_url": None,

        "active_tab": "pools",

        "editable_reading_ids": editable_reading_ids,

        "show_service_issues": show_service_issues,

        "can_manage_service_issues": can_manage_service_issues,

        "service_issues": service_issues,

        "service_issue_form": service_issue_form,

        "service_issue_stage_choices": service_issue_stage_choices,

        "service_issue_done_stage": CrmItem.STAGE_SERVICE_DONE,

    }

    return render(request, "pool_service/pool_detail.html", context)





@login_required

@require_POST

def pool_issue_create(request, pool_uuid):

    readonly = _deny_superuser_write(request)

    if readonly:

        return readonly

    blocked = _redirect_if_access_blocked(request)

    if blocked:

        return blocked



    pool = get_object_or_404(Pool, uuid=pool_uuid)

    if not pool.organization_id:

        return HttpResponseForbidden()



    is_org_staff = OrganizationAccess.objects.filter(

        user=request.user,

        organization=pool.organization,

        role__in=ORG_STAFF_ROLES,

    ).exists()

    if not is_org_staff:

        return HttpResponseForbidden()



    form = CrmServiceIssueForm(request.POST, request.FILES)

    if form.is_valid():

        issue = form.save(commit=False)

        issue.organization = pool.organization

        issue.direction = CrmItem.DIRECTION_SERVICE

        issue.pool = pool

        issue.client = pool.client

        issue.stage = CrmItem.STAGE_SERVICE_NEW

        issue.created_by = request.user

        if not issue.responsible:

            issue.responsible = request.user

        issue.save()

        for photo in form.cleaned_data.get("photos", []):

            processed = _compress_issue_photo(photo)
            CrmItemPhoto.objects.create(item=issue, image=processed)

        messages.success(request, "Неисправность добавлена.")

    else:

        errors = []
        for field_errors in form.errors.values():
            errors.extend(field_errors)
        if errors:
            messages.error(request, "; ".join(errors))
        if not errors:
            messages.error(request, "Проверьте поля неисправности.")

    return redirect("pool_detail", pool_uuid=pool.uuid)





@login_required

@require_POST

def pool_issue_update(request, pool_uuid, item_id):

    readonly = _deny_superuser_write(request)

    if readonly:

        return readonly

    blocked = _redirect_if_access_blocked(request)

    if blocked:

        return blocked



    pool = get_object_or_404(Pool, uuid=pool_uuid)

    if not pool.organization_id:

        return HttpResponseForbidden()



    is_org_staff = OrganizationAccess.objects.filter(

        user=request.user,

        organization=pool.organization,

        role__in=ORG_STAFF_ROLES,

    ).exists()

    if not is_org_staff:

        return HttpResponseForbidden()



    issue = get_object_or_404(

        CrmItem,

        id=item_id,

        direction=CrmItem.DIRECTION_SERVICE,

        pool=pool,

        organization=pool.organization,

    )

    stage = (request.POST.get("stage") or "").strip()

    allowed = {choice[0] for choice in CRM_STAGE_CHOICES_BY_DIRECTION.get(CrmItem.DIRECTION_SERVICE, [])}

    if stage not in allowed:

        messages.error(request, "Выберите корректный статус.")

        return redirect("pool_detail", pool_uuid=pool.uuid)



    issue.stage = stage

    issue.save(update_fields=["stage", "updated_at"])

    messages.success(request, "Статус обновлен.")

    return redirect("pool_detail", pool_uuid=pool.uuid)





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


def _task_user_label(user):
    if not user:
        return ""
    return user.get_full_name() or user.username or str(user.id)


def _is_modal_request(request):
    return (
        request.GET.get("modal") == "1"
        or request.POST.get("modal") == "1"
        or request.headers.get("x-requested-with") == "XMLHttpRequest"
    )


def _task_responsible_context(form):
    options = []
    field = form.fields.get("responsibles")
    if field:
        options = [{"id": user.id, "name": _task_user_label(user)} for user in field.queryset]
    selected = form["responsibles"].value() if "responsibles" in form.fields else []
    if not selected:
        selected = []
    selected = [str(value) for value in selected]
    return options, selected


def _is_org_owner(user, organization):
    if not user or not organization:
        return False
    if user.is_superuser:
        return True
    return OrganizationAccess.objects.filter(user=user, organization=organization, role="owner").exists()


def _task_can_edit(task, user):
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    return task.responsibles.filter(id=user.id).exists()


def _record_task_change(task, user, action, field_name="", old_value="", new_value=""):
    ServiceTaskChange.objects.create(
        task=task,
        changed_by=user,
        action=action,
        field_name=field_name or "",
        old_value=old_value or "",
        new_value=new_value or "",
    )


@login_required
def task_create(request):
    readonly = _deny_superuser_write(request)
    if readonly:
        return readonly
    blocked = _redirect_if_access_blocked(request)
    if blocked:
        return blocked

    is_modal = _is_modal_request(request)

    org = organization_for_user(request.user)
    if not org:
        return HttpResponseForbidden()
    is_staff = OrganizationAccess.objects.filter(
        user=request.user,
        organization=org,
        role__in=ORG_STAFF_ROLES,
    ).exists()
    if not is_staff:
        return HttpResponseForbidden()

    allowed_responsible_ids = set(
        User.objects.filter(
            organizationaccess__organization=org,
            organizationaccess__role__in=ORG_STAFF_ROLES,
            is_active=True,
        )
        .values_list("id", flat=True)
        .distinct()
    )
    required_responsible_id = request.user.id
    if required_responsible_id not in allowed_responsible_ids:
        required_responsible_id = None

    next_url = request.GET.get("next") or request.POST.get("next") or ""
    initial = {}
    date_param = request.GET.get("date")
    if date_param:
        try:
            initial_date = date.fromisoformat(date_param)
            initial["start_date"] = initial_date
            initial["end_date"] = initial_date
        except ValueError:
            pass
    initial["responsibles"] = [required_responsible_id] if required_responsible_id else []

    if request.method == "POST":
        post_data = request.POST.copy()
        selected = post_data.getlist("responsibles") or []
        if required_responsible_id and str(required_responsible_id) not in selected:
            selected.append(str(required_responsible_id))
        post_data.setlist("responsibles", selected)
        form = ServiceTaskForm(post_data, organization=org)
        if form.is_valid():
            task = form.save(commit=False)
            task.organization = org
            task.created_by = request.user
            task.visibility = ServiceTask.VISIBILITY_PRIVATE
            task.save()
            form.save_m2m()
            notify_task_assignment(task, task.responsibles.all(), added_by=request.user)

            _record_task_change(
                task,
                request.user,
                ServiceTaskChange.ACTION_CREATED,
                new_value=task.title,
            )
            if form.cleaned_data.get("is_completed"):
                task.completed_at = timezone.now()
                task.completed_by = request.user
                task.save(update_fields=["completed_at", "completed_by", "updated_at"])
                _record_task_change(
                    task,
                    request.user,
                    ServiceTaskChange.ACTION_COMPLETED,
                )
            if is_modal:
                return JsonResponse({"ok": True})
            messages.success(request, "Задача добавлена.")
            if next_url:
                return redirect(next_url)
            return redirect("readings_all")
    else:
        form = ServiceTaskForm(initial=initial, organization=org)

    responsible_options, selected_responsibles = _task_responsible_context(form)
    if required_responsible_id and str(required_responsible_id) not in selected_responsibles:
        selected_responsibles.append(str(required_responsible_id))
    has_time = bool(form["start_time"].value() or form["end_time"].value())
    modal_title = "" if is_modal else "\u041d\u043e\u0432\u0430\u044f \u0437\u0430\u0434\u0430\u0447\u0430"
    context = {
        "form": form,
        "task": None,
        "page_title": "\u041d\u043e\u0432\u0430\u044f \u0437\u0430\u0434\u0430\u0447\u0430",
        "page_subtitle": "\u0421\u043e\u0437\u0434\u0430\u043d\u0438\u0435 \u0437\u0430\u0434\u0430\u0447\u0438 \u0434\u043b\u044f \u043a\u0430\u043b\u0435\u043d\u0434\u0430\u0440\u044f",
        "active_tab": "readings",
        "next_url": next_url,
        "form_action": request.path,
        "show_history": False,
        "responsible_options": responsible_options,
        "selected_responsibles": selected_responsibles,
        "is_modal": is_modal,
        "required_responsible_id": required_responsible_id,
        "has_time": has_time,
        "modal_title": modal_title,
    }
    template_name = "pool_service/task_form_modal.html" if is_modal else "pool_service/task_form.html"
    status_code = 400 if request.method == "POST" and not form.is_valid() and is_modal else 200
    return render(request, template_name, context, status=status_code)


@login_required
def task_edit(request, task_id):
    readonly = _deny_superuser_write(request)
    if readonly:
        return readonly
    blocked = _redirect_if_access_blocked(request)
    if blocked:
        return blocked

    is_modal = _is_modal_request(request)

    task = get_object_or_404(ServiceTask, pk=task_id)
    if not _task_can_edit(task, request.user):
        return HttpResponseForbidden()

    next_url = request.GET.get("next") or request.POST.get("next") or ""
    allowed_responsible_ids = set(
        User.objects.filter(
            organizationaccess__organization=task.organization,
            organizationaccess__role__in=ORG_STAFF_ROLES,
            is_active=True,
        )
        .values_list("id", flat=True)
        .distinct()
    )
    required_responsible_id = task.created_by_id or request.user.id
    if required_responsible_id not in allowed_responsible_ids:
        required_responsible_id = None

    old_values = {
        "title": task.title,
        "description": task.description,
        "start_date": task.start_date,
        "end_date": task.end_date,
        "start_time": task.start_time,
        "end_time": task.end_time,
        "is_important": task.priority == ServiceTask.PRIORITY_HIGH,
    }
    old_responsibles = list(task.responsibles.all())
    old_responsible_ids = {user.id for user in old_responsibles}
    old_completed = bool(task.completed_at)

    if request.method == "POST":
        post_data = request.POST.copy()
        selected = post_data.getlist("responsibles") or []
        if required_responsible_id and str(required_responsible_id) not in selected:
            selected.append(str(required_responsible_id))
        post_data.setlist("responsibles", selected)
        form = ServiceTaskForm(post_data, instance=task, organization=task.organization)
        if form.is_valid():
            task = form.save(commit=False)
            task.organization = task.organization
            task.visibility = ServiceTask.VISIBILITY_PRIVATE
            task.save()
            form.save_m2m()

            def _format_value(field, value):
                if value is None:
                    return ""
                if field in {"start_date", "end_date"} and isinstance(value, date):
                    return value.strftime("%d.%m.%Y")
                if field in {"start_time", "end_time"} and hasattr(value, "strftime"):
                    return value.strftime("%H:%M")
                if field == "is_important":
                    return "Да" if value else "Нет"
                return str(value)

            for field in ["title", "description", "start_date", "end_date", "start_time", "end_time", "is_important"]:
                old_val = old_values.get(field)
                new_val = task.priority == ServiceTask.PRIORITY_HIGH if field == "is_important" else getattr(task, field)
                if old_val != new_val:
                    action = ServiceTaskChange.ACTION_MOVED if field in {"start_date", "end_date"} else ServiceTaskChange.ACTION_UPDATED
                    _record_task_change(
                        task,
                        request.user,
                        action,
                        field_name=field,
                        old_value=_format_value(field, old_val),
                        new_value=_format_value(field, new_val),
                    )

            new_responsibles = list(task.responsibles.all())
            new_responsible_ids = {user.id for user in new_responsibles}
            if new_responsible_ids != old_responsible_ids:
                old_names = ", ".join(sorted(_task_user_label(user) for user in old_responsibles))
                new_names = ", ".join(sorted(_task_user_label(user) for user in new_responsibles))
                _record_task_change(
                    task,
                    request.user,
                    ServiceTaskChange.ACTION_UPDATED,
                    field_name="responsibles",
                    old_value=old_names,
                    new_value=new_names,
                )
                added_users = [user for user in new_responsibles if user.id not in old_responsible_ids]
                notify_task_assignment(task, added_users, added_by=request.user)

            is_completed = bool(form.cleaned_data.get("is_completed"))
            if is_completed and not old_completed:
                task.completed_at = timezone.now()
                task.completed_by = request.user
                task.save(update_fields=["completed_at", "completed_by", "updated_at"])
                _record_task_change(task, request.user, ServiceTaskChange.ACTION_COMPLETED)
            elif not is_completed and old_completed:
                task.completed_at = None
                task.completed_by = None
                task.save(update_fields=["completed_at", "completed_by", "updated_at"])
                _record_task_change(task, request.user, ServiceTaskChange.ACTION_REOPENED)

            if is_modal:
                return JsonResponse({"ok": True})
            messages.success(request, "Задача обновлена.")
            if next_url:
                return redirect(next_url)
            return redirect("task_edit", task_id=task.id)
    else:
        form = ServiceTaskForm(instance=task, organization=task.organization)

    history = []
    field_labels = {
        "title": "Название",
        "description": "Описание",
        "start_date": "Дата начала",
        "end_date": "Дата окончания",
        "start_time": "Время начала",
        "end_time": "Время окончания",
        "is_important": "Важная задача",
        "responsibles": "Участники",
    }
    action_labels = dict(ServiceTaskChange.ACTION_CHOICES)
    for change in task.changes.select_related("changed_by").order_by("-created_at")[:100]:
        history.append(
            {
                "created_at": change.created_at,
                "user": _task_user_label(change.changed_by),
                "action": action_labels.get(change.action, change.action),
                "field": field_labels.get(change.field_name, change.field_name),
                "old_value": change.old_value,
                "new_value": change.new_value,
            }
        )

    responsible_options, selected_responsibles = _task_responsible_context(form)
    if required_responsible_id and str(required_responsible_id) not in selected_responsibles:
        selected_responsibles.append(str(required_responsible_id))
    has_time = bool(form["start_time"].value() or form["end_time"].value())
    modal_title = "" if is_modal else "\u0420\u0435\u0434\u0430\u043a\u0442\u0438\u0440\u043e\u0432\u0430\u043d\u0438\u0435 \u0437\u0430\u0434\u0430\u0447\u0438"
    context = {
        "form": form,
        "task": task,
        "page_title": "\u0420\u0435\u0434\u0430\u043a\u0442\u0438\u0440\u043e\u0432\u0430\u043d\u0438\u0435 \u0437\u0430\u0434\u0430\u0447\u0438",
        "page_subtitle": task.title,
        "active_tab": "readings",
        "next_url": next_url,
        "form_action": request.path,
        "show_history": True,
        "history": history,
        "responsible_options": responsible_options,
        "selected_responsibles": selected_responsibles,
        "is_modal": is_modal,
        "required_responsible_id": required_responsible_id,
        "has_time": has_time,
        "modal_title": modal_title,
    }
    template_name = "pool_service/task_form_modal.html" if is_modal else "pool_service/task_form.html"
    status_code = 400 if request.method == "POST" and not form.is_valid() and is_modal else 200
    return render(request, template_name, context, status=status_code)


@login_required
def task_delete(request, task_id):
    if request.method != "POST":
        return redirect("task_edit", task_id=task_id)

    readonly = _deny_superuser_write(request)
    if readonly:
        return readonly
    blocked = _redirect_if_access_blocked(request)
    if blocked:
        return blocked

    task = get_object_or_404(ServiceTask, pk=task_id)
    if not _task_can_edit(task, request.user):
        return HttpResponseForbidden()

    next_url = request.POST.get("next") or ""
    if next_url and not next_url.startswith("/"):
        next_url = ""

    task.delete()
    messages.success(request, "Р—Р°РґР°С‡Р° СѓРґР°Р»РµРЅР°.")

    if request.POST.get("modal") == "1":
        return JsonResponse({"ok": True, "deleted": True})

    if next_url:
        return redirect(next_url)
    return redirect("readings_all")


@csrf_protect
@login_required
def task_move(request):
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "method_not_allowed"}, status=405)

    try:
        payload = json.loads(request.body.decode("utf-8")) if request.body else request.POST
    except (TypeError, ValueError):
        payload = request.POST

    task_id = payload.get("task_id")
    target_date_raw = payload.get("target_date") or payload.get("date")
    if not task_id or not target_date_raw:
        return JsonResponse({"ok": False, "error": "missing_fields"}, status=400)

    try:
        task_id = int(task_id)
    except (TypeError, ValueError):
        return JsonResponse({"ok": False, "error": "invalid_task"}, status=400)

    try:
        target_date = date.fromisoformat(target_date_raw)
    except ValueError:
        return JsonResponse({"ok": False, "error": "invalid_date"}, status=400)

    task = get_object_or_404(ServiceTask, pk=task_id)
    if not _task_can_edit(task, request.user):
        return JsonResponse({"ok": False, "error": "forbidden"}, status=403)
    if task.completed_at:
        return JsonResponse({"ok": False, "error": "completed_task"}, status=400)

    old_start = task.start_date
    old_end = task.end_date or task.start_date
    delta = target_date - old_start
    new_start = old_start + delta
    new_end = old_end + delta

    if new_start == old_start and new_end == old_end:
        return JsonResponse({"ok": True})

    task.start_date = new_start
    task.end_date = new_end
    task.save(update_fields=["start_date", "end_date", "updated_at"])

    def _fmt(value):
        return value.strftime("%d.%m.%Y") if value else ""

    if old_start != new_start:
        _record_task_change(
            task,
            request.user,
            ServiceTaskChange.ACTION_MOVED,
            field_name="start_date",
            old_value=_fmt(old_start),
            new_value=_fmt(new_start),
        )
    if old_end != new_end:
        _record_task_change(
            task,
            request.user,
            ServiceTaskChange.ACTION_MOVED,
            field_name="end_date",
            old_value=_fmt(old_end),
            new_value=_fmt(new_end),
        )

    return JsonResponse(
        {
            "ok": True,
            "start_date": new_start.isoformat(),
            "end_date": new_end.isoformat(),
        }
    )


@login_required
def readings_all(request):

    """Service visit calendar."""

    if request.user.is_superuser:
        pools = Pool.objects.all()
    elif OrganizationAccess.objects.filter(user=request.user).exists():
        org_ids = OrganizationAccess.objects.filter(user=request.user).values_list("organization_id", flat=True)
        pools = Pool.objects.filter(organization_id__in=org_ids)
    elif ClientAccess.objects.filter(user=request.user).exists():
        client_access = _client_access_for_user(request.user)
        pools = Pool.objects.filter(client=client_access.client) if client_access else Pool.objects.none()
    else:
        pools = Pool.objects.filter(accesses__user=request.user)

    pools = pools.select_related("client", "organization").order_by("client__name", "address")
    pool_list = list(pools)

    today = timezone.localdate() if settings.USE_TZ else date.today()

    def _add_month(d, months):
        month_index = d.month - 1 + months
        year = d.year + (month_index // 12)
        month = (month_index % 12) + 1
        day = min(d.day, calendar.monthrange(year, month)[1])
        return date(year, month, day)

    def _week_start(d):
        return d - timedelta(days=d.weekday())

    def _shift_from_weekend(d):
        if d.weekday() == 5:
            return d - timedelta(days=1)
        if d.weekday() == 6:
            return d - timedelta(days=2)
        return d

    month_labels = {
        1: "\u042f\u043d\u0432\u0430\u0440\u044c",
        2: "\u0424\u0435\u0432\u0440\u0430\u043b\u044c",
        3: "\u041c\u0430\u0440\u0442",
        4: "\u0410\u043f\u0440\u0435\u043b\u044c",
        5: "\u041c\u0430\u0439",
        6: "\u0418\u044e\u043d\u044c",
        7: "\u0418\u044e\u043b\u044c",
        8: "\u0410\u0432\u0433\u0443\u0441\u0442",
        9: "\u0421\u0435\u043d\u0442\u044f\u0431\u0440\u044c",
        10: "\u041e\u043a\u0442\u044f\u0431\u0440\u044c",
        11: "\u041d\u043e\u044f\u0431\u0440\u044c",
        12: "\u0414\u0435\u043a\u0430\u0431\u0440\u044c",
    }

    month_param = request.GET.get("month")
    target_month = None
    if month_param:
        try:
            year_str, month_str = month_param.split("-")
            target_month = date(int(year_str), int(month_str), 1)
        except (TypeError, ValueError):
            target_month = None
    if not target_month:
        target_month = today.replace(day=1)

    selected_responsible_ids = []
    for raw_id in request.GET.getlist("responsible"):
        try:
            selected_responsible_ids.append(int(raw_id))
        except (TypeError, ValueError):
            continue

    month_label = f"{month_labels.get(target_month.month, target_month.month)} {target_month.year}"
    prev_month_date = _add_month(target_month, -1)
    next_month_date = _add_month(target_month, 1)
    target_month_value = f"{target_month.year}-{target_month.month:02d}"
    prev_month_value = f"{prev_month_date.year}-{prev_month_date.month:02d}"
    next_month_value = f"{next_month_date.year}-{next_month_date.month:02d}"
    today_month_value = f"{today.year}-{today.month:02d}"

    responsible_params = [("responsible", str(rid)) for rid in selected_responsible_ids]
    responsible_filter_set = set(selected_responsible_ids)
    prev_month_query = urlencode([("month", prev_month_value), *responsible_params])
    next_month_query = urlencode([("month", next_month_value), *responsible_params])
    today_month_query = urlencode([("month", today_month_value), *responsible_params])
    current_query = urlencode([("month", target_month_value), *responsible_params])

    first_day = target_month
    last_day = date(target_month.year, target_month.month, calendar.monthrange(target_month.year, target_month.month)[1])
    range_start = _week_start(first_day)
    range_end = last_day + timedelta(days=(6 - last_day.weekday()))

    calendar_days = []
    cursor = range_start
    while cursor <= range_end:
        calendar_days.append(
            {
                "date": cursor,
                "day": cursor.day,
                "is_current_month": cursor.month == target_month.month,
                "is_today": cursor == today,
                "week_start": _week_start(cursor),
            }
        )
        cursor += timedelta(days=1)

    schedule_by_date = {}
    unscheduled_pools = []
    paused_pools = []

    pool_ids = [pool.id for pool in pool_list]
    org_ids = {pool.organization_id for pool in pool_list if pool.organization_id}

    task_org = organization_for_user(request.user)
    responsible_options = []
    can_create_tasks = False
    selected_responsible_label = None
    if task_org:
        task_staff = (
            User.objects.filter(
                organizationaccess__organization=task_org,
                organizationaccess__role__in=ORG_STAFF_ROLES,
                is_active=True,
            )
            .distinct()
            .order_by("last_name", "first_name", "username")
        )
        responsible_options = [
            {"id": user.id, "name": _task_user_label(user)} for user in task_staff
        ]
        active_responsible_ids = {user["id"] for user in responsible_options}
        if active_responsible_ids:
            selected_responsible_ids = [
                rid for rid in selected_responsible_ids if rid in active_responsible_ids
            ]
            responsible_filter_set = set(selected_responsible_ids)
        can_create_tasks = OrganizationAccess.objects.filter(
            user=request.user,
            organization=task_org,
            role__in=ORG_STAFF_ROLES,
        ).exists()
        if selected_responsible_ids:
            if len(selected_responsible_ids) == 1:
                selected_id = selected_responsible_ids[0]
                for option in responsible_options:
                    if option["id"] == selected_id:
                        selected_responsible_label = option["name"]
                        break
            else:
                selected_responsible_label = "\u041d\u0435\u0441\u043a\u043e\u043b\u044c\u043a\u043e"

    org_user_ids = set(
        OrganizationAccess.objects.filter(organization_id__in=org_ids, user__is_active=True)
        .values_list("user_id", flat=True)
    )

    base_readings = WaterReading.objects.filter(pool_id__in=pool_ids)
    if org_user_ids:
        base_readings = base_readings.filter(added_by_id__in=org_user_ids)
    else:
        base_readings = base_readings.none()

    last_reading_before_range = {}
    for reading in (
        base_readings.filter(date__date__lt=range_start)
        .select_related("added_by")
        .order_by("pool_id", "-date", "-id")
    ):
        if reading.pool_id not in last_reading_before_range:
            last_reading_before_range[reading.pool_id] = reading

    readings_in_range = list(
        base_readings.filter(date__date__gte=range_start, date__date__lte=range_end)
        .select_related("added_by")
        .order_by("date", "id")
    )

    readings_by_pool_week = {}
    readings_by_pool_month = {}
    for reading in readings_in_range:
        if not reading.date:
            continue
        reading_date = reading.date.date()
        reading._calendar_date = reading_date
        week_key = _week_start(reading_date)
        month_key = f"{reading_date.year}-{reading_date.month:02d}"
        readings_by_pool_week.setdefault(reading.pool_id, {}).setdefault(week_key, []).append(reading)
        readings_by_pool_month.setdefault(reading.pool_id, {}).setdefault(month_key, []).append(reading)

    responsible_by_pool = {}
    org_staff_ids = set(
        OrganizationAccess.objects.filter(
            organization_id__in=org_ids,
            role__in=ORG_STAFF_ROLES,
            user__is_active=True,
        )
        .values_list("user_id", flat=True)
    )
    if org_staff_ids:
        for access in (
            PoolAccess.objects.filter(pool_id__in=pool_ids, user_id__in=org_staff_ids, user__is_active=True)
            .select_related("user")
            .order_by("pool_id", "user__last_name", "user__first_name")
        ):
            if access.pool_id not in responsible_by_pool:
                responsible_by_pool[access.pool_id] = access.user

    plan_by_pool_week = {}
    for plan in ServiceVisitPlan.objects.filter(
        pool_id__in=pool_ids,
        week_start__gte=range_start,
        week_start__lte=range_end,
    ):
        plan_by_pool_week[(plan.pool_id, plan.week_start)] = plan

    def _frequency_days(pool):
        if pool.service_interval_days:
            return int(pool.service_interval_days)
        mapping = {
            Pool.SERVICE_FREQ_WEEKLY: 7,
            Pool.SERVICE_FREQ_TWICE_MONTHLY: 14,
        }
        return mapping.get(pool.service_frequency)

    def _frequency_months(pool):
        mapping = {
            Pool.SERVICE_FREQ_MONTHLY: 1,
            Pool.SERVICE_FREQ_BIMONTHLY: 2,
            Pool.SERVICE_FREQ_QUARTERLY: 3,
            Pool.SERVICE_FREQ_TWICE_YEARLY: 6,
            Pool.SERVICE_FREQ_YEARLY: 12,
        }
        return mapping.get(pool.service_frequency)

    def _frequency_label(pool):
        if pool.service_interval_days:
            return f"\u041a\u0430\u0436\u0434\u044b\u0435 {pool.service_interval_days} \u0434\u043d\u0435\u0439"
        if pool.service_frequency:
            return pool.get_service_frequency_display()
        return "\u041d\u0435 \u0437\u0430\u0434\u0430\u043d\u0430"

    used_reading_ids = set()

    def _period_kind(pool):
        if pool.service_interval_days:
            return "week" if int(pool.service_interval_days) <= 14 else "month"
        if pool.service_frequency in {Pool.SERVICE_FREQ_WEEKLY, Pool.SERVICE_FREQ_TWICE_MONTHLY}:
            return "week"
        return "month"

    def _period_key(kind, d):
        return _week_start(d) if kind == "week" else f"{d.year}-{d.month:02d}"

    def _select_reading(pool, expected_date):
        kind = _period_kind(pool)
        key = _period_key(kind, expected_date)
        if kind == "week":
            readings = readings_by_pool_week.get(pool.id, {}).get(key, [])
        else:
            readings = readings_by_pool_month.get(pool.id, {}).get(key, [])
        available = [reading for reading in readings if reading.id not in used_reading_ids]
        if not available:
            return None
        chosen = min(
            available,
            key=lambda reading: (
                abs((reading._calendar_date - expected_date).days),
                reading._calendar_date,
                reading.id,
            ),
        )
        used_reading_ids.add(chosen.id)
        return chosen

    pool_events = []

    def _allow_month_move(pool):
        return _period_kind(pool) == "month"

    def _record_pool_event(
        pool,
        source_week_start,
        due_date,
        planned_date,
        plan_id,
        status,
        actual_date,
        group_key=None,
        is_extra=False,
    ):
        responsible = responsible_by_pool.get(pool.id)
        responsible_name = None
        if responsible:
            responsible_name = responsible.get_full_name() or responsible.username
        if responsible_filter_set:
            if not responsible or responsible.id not in responsible_filter_set:
                return

        display_date = actual_date or planned_date or due_date
        display_week_start = _week_start(display_date)
        display_week_end = display_week_start + timedelta(days=6)
        if group_key is None:
            group_key = display_week_start
        group_token = ("extra", group_key) if is_extra else ("base", group_key)

        pool_events.append(
            {
                "pool": pool,
                "client_id": pool.client_id,
                "client_name": pool.client.name,
                "pool_address": pool.address,
                "object_type": pool.object_type or Pool.OBJECT_TYPE_POOL,
                "source_week_start": source_week_start,
                "display_week_start": display_week_start,
                "display_week_end": display_week_end,
                "group_key": group_token,
                "is_extra": is_extra,
                "due_date": due_date,
                "plan_date": planned_date,
                "plan_id": plan_id,
                "status": status,
                "actual_date": actual_date,
                "frequency_label": _frequency_label(pool),
                "responsible_name": responsible_name,
                "allow_month_move": _allow_month_move(pool),
            }
        )

    for pool in pool_list:
        if getattr(pool, "service_suspended", False):
            paused_pools.append(pool)
            continue

        interval_days = _frequency_days(pool)
        interval_months = _frequency_months(pool)

        if not interval_days and not interval_months:
            unscheduled_pools.append(pool)
            continue

        last_reading = last_reading_before_range.get(pool.id)
        last_visit_date = last_reading.date.date() if last_reading else None
        anchor_date = last_visit_date or (pool.created_at.date() if pool.created_at else None)
        if not anchor_date:
            continue

        if interval_days:
            due_date = anchor_date + timedelta(days=interval_days)
            if due_date < range_start:
                delta_days = (range_start - due_date).days
                steps = delta_days // interval_days
                if due_date + timedelta(days=steps * interval_days) < range_start:
                    steps += 1
                due_date = due_date + timedelta(days=steps * interval_days)

            while due_date <= range_end:
                week_key = _week_start(due_date)
                plan = plan_by_pool_week.get((pool.id, week_key))
                planned_date = plan.planned_date if plan else due_date
                actual_reading = _select_reading(pool, planned_date)

                if actual_reading:
                    event_date = actual_reading._calendar_date
                    _record_pool_event(
                        pool,
                        week_key,
                        due_date,
                        planned_date,
                        plan.id if plan else None,
                        "done",
                        event_date,
                    )
                    anchor_date = event_date
                    due_date = anchor_date + timedelta(days=interval_days)
                    continue

                status = "overdue" if (week_key + timedelta(days=6)) < today else "planned"
                _record_pool_event(
                    pool,
                    week_key,
                    due_date,
                    planned_date,
                    plan.id if plan else None,
                    status,
                    None,
                )
                due_date = due_date + timedelta(days=interval_days)
        else:
            step = 1
            due_date = _shift_from_weekend(_add_month(anchor_date, interval_months * step))
            if due_date < range_start:
                delta_months = (range_start.year - anchor_date.year) * 12 + (range_start.month - anchor_date.month)
                step = max(1, delta_months // interval_months)
                while _shift_from_weekend(_add_month(anchor_date, interval_months * step)) < range_start:
                    step += 1
                due_date = _shift_from_weekend(_add_month(anchor_date, interval_months * step))

            while due_date <= range_end:
                week_key = _week_start(due_date)
                plan = plan_by_pool_week.get((pool.id, week_key))
                planned_date = plan.planned_date if plan else due_date
                actual_reading = _select_reading(pool, planned_date)

                if actual_reading:
                    event_date = actual_reading._calendar_date
                    _record_pool_event(
                        pool,
                        week_key,
                        due_date,
                        planned_date,
                        plan.id if plan else None,
                        "done",
                        event_date,
                    )
                    anchor_date = event_date
                    step = 1
                    due_date = _shift_from_weekend(_add_month(anchor_date, interval_months * step))
                    continue

                status = "overdue" if (week_key + timedelta(days=6)) < today else "planned"
                _record_pool_event(
                    pool,
                    week_key,
                    due_date,
                    planned_date,
                    plan.id if plan else None,
                    status,
                    None,
                )
                step += 1
                due_date = _shift_from_weekend(_add_month(anchor_date, interval_months * step))

    unscheduled_ids = {pool.id for pool in unscheduled_pools}
    paused_ids = {pool.id for pool in paused_pools}
    extra_seen = set()
    for pool in pool_list:
        if pool.id in unscheduled_ids or pool.id in paused_ids:
            continue
        period_kind = _period_kind(pool)
        if period_kind == "week":
            period_map = readings_by_pool_week.get(pool.id, {})
        else:
            period_map = readings_by_pool_month.get(pool.id, {})
        for readings in period_map.values():
            for reading in readings:
                if reading.id in used_reading_ids:
                    continue
                actual_date = reading._calendar_date
                extra_key = (pool.id, actual_date)
                if extra_key in extra_seen:
                    continue
                extra_seen.add(extra_key)
                _record_pool_event(
                    pool,
                    _week_start(actual_date),
                    actual_date,
                    None,
                    None,
                    "done",
                    actual_date,
                    group_key=actual_date,
                    is_extra=True,
                )
                used_reading_ids.add(reading.id)

    grouped = {}
    for event in pool_events:
        key = (event["client_id"], event["group_key"], event["object_type"])
        group = grouped.setdefault(
            key,
            {
                "client_id": event["client_id"],
                "client_name": event["client_name"],
                "week_start": event["display_week_start"],
                "week_end": event["display_week_end"],
                "pool_ids": [],
                "pool_addresses": [],
                "plan_dates": [],
                "due_dates": [],
                "actual_dates": [],
                "frequency_labels": set(),
                "responsibles": set(),
                "allow_month_move": True,
                "source_weeks": {},
                "is_extra": event["is_extra"],
                "object_type": event["object_type"],
            },
        )
        group["pool_ids"].append(event["pool"].id)
        group["pool_addresses"].append(event["pool_address"])
        group["due_dates"].append(event["due_date"])
        group["source_weeks"][event["pool"].id] = event["source_week_start"]
        if event["plan_date"]:
            group["plan_dates"].append(event["plan_date"])
        if event["actual_date"]:
            group["actual_dates"].append(event["actual_date"])
        if event["frequency_label"]:
            group["frequency_labels"].add(event["frequency_label"])
        if event["responsible_name"]:
            group["responsibles"].add(event["responsible_name"])
        group["allow_month_move"] = group["allow_month_move"] and event["allow_month_move"]

    overdue_count = 0
    planned_count = 0
    done_count = 0

    for group in grouped.values():
        is_extra = group.get("is_extra", False)
        if group["actual_dates"]:
            event_date = min(group["actual_dates"])
            status = "done"
            draggable = False
        else:
            status = "overdue" if group["week_end"] < today else "planned"
            planned_date = min(group["plan_dates"]) if group["plan_dates"] else min(group["due_dates"])
            event_date = planned_date
            draggable = status == "planned"

        if event_date < range_start or event_date > range_end:
            continue

        if not is_extra:
            if status == "overdue":
                overdue_count += 1
            elif status == "planned":
                planned_count += 1
            else:
                done_count += 1

        pool_count = len(group["pool_ids"])
        display_name = group["client_name"]
        if pool_count > 1:
            display_name = f"{display_name} ({pool_count})"

        addresses = group["pool_addresses"]
        addresses_display = ", ".join(addresses[:3])
        if len(addresses) > 3:
            addresses_display = f"{addresses_display} \u0438 \u0435\u0449\u0435 {len(addresses) - 3}"

        frequency_label = None
        if len(group["frequency_labels"]) == 1:
            frequency_label = next(iter(group["frequency_labels"]))
        elif len(group["frequency_labels"]) > 1:
            frequency_label = "\u041d\u0435\u0441\u043a\u043e\u043b\u044c\u043a\u043e \u0447\u0430\u0441\u0442\u043e\u0442"

        responsible_label = None
        if len(group["responsibles"]) == 1:
            responsible_label = next(iter(group["responsibles"]))
        elif len(group["responsibles"]) > 1:
            responsible_label = "\u041d\u0435\u0441\u043a\u043e\u043b\u044c\u043a\u043e"

        title_parts = [f"\u0411\u0430\u0441\u0441\u0435\u0439\u043d\u043e\u0432: {pool_count}"]
        if addresses_display:
            title_parts.append(f"\u0410\u0434\u0440\u0435\u0441\u0430: {addresses_display}")
        if responsible_label:
            title_parts.append(f"\u041e\u0442\u0432\u0435\u0442\u0441\u0442\u0432\u0435\u043d\u043d\u044b\u0439: {responsible_label}")
        if status == "done":
            title_parts.append(f"\u0412\u044b\u0435\u0437\u0434: {event_date:%d.%m.%Y}")
        else:
            title_parts.append(f"\u041f\u043b\u0430\u043d: {event_date:%d.%m.%Y}")
        if is_extra:
            title_parts.append("\u0414\u043e\u043f\u043e\u043b\u043d\u0438\u0442\u0435\u043b\u044c\u043d\u044b\u0439 \u0432\u044b\u0435\u0437\u0434")
        if frequency_label:
            title_parts.append(f"\u0427\u0430\u0441\u0442\u043e\u0442\u0430: {frequency_label}")
        if status == "overdue":
            title_parts.append("\u041f\u0440\u043e\u0441\u0440\u043e\u0447\u0435\u043d\u043e")

        source_weeks = {str(pid): week.isoformat() for pid, week in group["source_weeks"].items()}
        source_weeks_json = json.dumps(source_weeks)
        source_week_values = list({week.isoformat() for week in group["source_weeks"].values()})
        source_week_start = source_week_values[0] if len(source_week_values) == 1 else ""

        schedule_by_date.setdefault(event_date, []).append(
            {
                "client_name": group["client_name"],
                "display_name": display_name,
                "pool_ids": group["pool_ids"],
                "status": status,
                "date": event_date,
                "week_start": group["week_start"],
                "source_week_start": source_week_start,
                "source_weeks": source_weeks_json,
                "source_month": event_date.strftime("%Y-%m"),
                "allow_month_move": group["allow_month_move"],
                "is_draggable": draggable,
                "title": " | ".join(title_parts),
                "object_type": group["object_type"],
                "item_type": "auto",
                "kind_order": 0,
            }
        )

    calendar_return_url = f"{reverse('readings_all')}?{current_query}" if current_query else reverse("readings_all")
    task_search_index = []

    if task_org:
        task_qs = ServiceTask.objects.filter(organization=task_org)
        if not request.user.is_superuser:
            task_qs = task_qs.filter(responsibles=request.user)
        if responsible_filter_set:
            task_qs = task_qs.filter(responsibles__in=responsible_filter_set)

        task_qs = task_qs.filter(
            Q(end_date__isnull=True, start_date__lte=range_end, start_date__gte=range_start)
            | Q(end_date__isnull=False, start_date__lte=range_end, end_date__gte=range_start)
        )

        task_qs = task_qs.select_related("created_by").prefetch_related("responsibles").distinct()
        for task in task_qs:
            task_start = task.start_date
            task_end = task.end_date or task.start_date
            if task_end < range_start or task_start > range_end:
                continue

            is_completed = bool(task.completed_at)
            if is_completed:
                status = "done"
            elif task_end < today:
                status = "overdue"
            else:
                status = "planned"

            if status == "overdue":
                overdue_count += 1
            elif status == "planned":
                planned_count += 1
            else:
                done_count += 1

            task_responsibles = list(task.responsibles.all())
            responsible_names = [_task_user_label(user) for user in task_responsibles]
            responsible_names = [name for name in responsible_names if name]
            responsible_label = ", ".join(responsible_names) if responsible_names else ""

            title_parts = []
            if responsible_label:
                title_parts.append(f"Участники: {responsible_label}")
            if task.priority == ServiceTask.PRIORITY_HIGH:
                title_parts.append("Важная задача")
            if task_start == task_end:
                title_parts.append(f"Дата: {task_start:%d.%m.%Y}")
            else:
                title_parts.append(f"Период: {task_start:%d.%m.%Y} — {task_end:%d.%m.%Y}")
            if task.start_time or task.end_time:
                if task.start_time and task.end_time:
                    title_parts.append(f"Время: {task.start_time:%H:%M} — {task.end_time:%H:%M}")
                elif task.start_time:
                    title_parts.append(f"Время начала: {task.start_time:%H:%M}")
                elif task.end_time:
                    title_parts.append(f"Время окончания: {task.end_time:%H:%M}")
            if is_completed and task.completed_at:
                title_parts.append(f"Выполнено: {task.completed_at:%d.%m.%Y}")
            if status == "overdue":
                title_parts.append("Просрочено")

            responsible_ids = {user.id for user in task_responsibles}
            can_edit = request.user.is_superuser or request.user.id in responsible_ids
            is_draggable = can_edit and not is_completed
            edit_url = reverse("task_edit", kwargs={"task_id": task.id})
            if calendar_return_url:
                edit_url = f"{edit_url}?{urlencode({'next': calendar_return_url})}"

            display_start = max(task_start, range_start)
            display_end = min(task_end, range_end)
            span_days = (task_end - task_start).days + 1
            start_time_label = task.start_time.strftime("%H:%M") if task.start_time else ""
            date_label = (
                f"{task_start:%d.%m.%Y}"
                if task_start == task_end
                else f"{task_start:%d.%m.%Y} \u2014 {task_end:%d.%m.%Y}"
            )

            schedule_by_date.setdefault(display_start, []).append(
                {
                    "client_name": task.title,
                    "display_name": task.title,
                    "status": status,
                    "date": display_start,
                    "priority": task.priority,
                    "visibility": task.visibility,
                    "is_completed": is_completed,
                    "is_multi": span_days > 1,
                    "span_days": span_days,
                    "is_continued": task_start < range_start,
                    "title": " | ".join(title_parts),
                    "task_id": task.id,
                    "start_date": task_start,
                    "end_date": task_end,
                    "start_time": task.start_time,
                    "start_time_label": start_time_label,
                    "is_draggable": is_draggable,
                    "item_type": "task",
                    "edit_url": edit_url if can_edit else "",
                    "kind_order": 1,
                }
            )
            task_search_index.append(
                {
                    "id": task.id,
                    "title": task.title,
                    "date": task_start.isoformat(),
                    "date_label": date_label,
                    "time_label": start_time_label,
                    "edit_url": edit_url if can_edit else "",
                    "priority": task.priority,
                    "status": status,
                }
            )

            if span_days > 1:
                cursor = display_start + timedelta(days=1)
                while cursor <= display_end:
                    schedule_by_date.setdefault(cursor, []).append(
                        {
                            "client_name": task.title,
                            "display_name": task.title,
                            "status": status,
                            "date": cursor,
                            "priority": task.priority,
                            "visibility": task.visibility,
                            "is_completed": is_completed,
                            "title": " | ".join(title_parts),
                            "item_type": "task_continuation",
                            "kind_order": 2,
                        }
                    )
                    cursor += timedelta(days=1)

    status_order = {"overdue": 0, "planned": 1, "done": 2}
    for day in calendar_days:
        items = schedule_by_date.get(day["date"], [])
        items.sort(
            key=lambda item: (
                status_order.get(item["status"], 9),
                item.get("kind_order", 0),
                item.get("start_time") or time.max,
                item.get("client_name") or "",
            )
        )
        day["items"] = items

    return render(
        request,
        "pool_service/readings_all.html",
        {
            "calendar_days": calendar_days,
            "month_label": month_label,
            "prev_month_query": prev_month_query,
            "next_month_query": next_month_query,
            "today_month_query": today_month_query,
            "current_month": target_month_value,
            "calendar_return_url": calendar_return_url,
            "page_title": "\u041a\u0430\u043b\u0435\u043d\u0434\u0430\u0440\u044c \u0437\u0430\u0434\u0430\u0447",
            "page_subtitle": "\u0412\u044b\u0435\u0437\u0434\u044b \u0438 \u0440\u0443\u0447\u043d\u044b\u0435 \u0437\u0430\u0434\u0430\u0447\u0438 \u043a\u043e\u043c\u0430\u043d\u0434\u044b",
            "active_tab": "readings",
            "overdue_count": overdue_count,
            "planned_count": planned_count,
            "done_count": done_count,
            "unscheduled_pools": unscheduled_pools,
            "paused_pools": paused_pools,
            "responsible_options": responsible_options,
            "selected_responsibles": [str(rid) for rid in selected_responsible_ids],
            "selected_responsible_label": selected_responsible_label,
            "can_create_tasks": can_create_tasks,
            "task_search_index": task_search_index,
        },
    )


@csrf_protect
@login_required
def visit_plan_move(request):
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "method_not_allowed"}, status=405)

    try:
        payload = json.loads(request.body.decode("utf-8")) if request.body else request.POST
    except (TypeError, ValueError):
        payload = request.POST

    pool_id = payload.get("pool_id")
    pool_ids = payload.get("pool_ids")
    source_weeks_raw = payload.get("source_weeks")
    source_week_start_raw = payload.get("source_week_start")
    display_week_start_raw = payload.get("week_start")
    source_month = payload.get("source_month")
    planned_date_raw = payload.get("planned_date")

    if (not pool_id and not pool_ids) or not planned_date_raw:
        return JsonResponse({"ok": False, "error": "missing_fields"}, status=400)

    try:
        planned_date = date.fromisoformat(planned_date_raw)
    except ValueError:
        return JsonResponse({"ok": False, "error": "invalid_date"}, status=400)

    if pool_ids:
        if isinstance(pool_ids, str):
            pool_ids = [pid for pid in pool_ids.split(",") if pid]
        elif not isinstance(pool_ids, (list, tuple)):
            pool_ids = [pool_ids]
    else:
        pool_ids = [pool_id]

    try:
        pool_ids = [int(pid) for pid in pool_ids]
    except (TypeError, ValueError):
        return JsonResponse({"ok": False, "error": "invalid_pool_ids"}, status=400)

    source_weeks = {}
    if source_weeks_raw:
        if isinstance(source_weeks_raw, str):
            try:
                source_weeks = json.loads(source_weeks_raw)
            except (TypeError, ValueError):
                source_weeks = {}
        elif isinstance(source_weeks_raw, dict):
            source_weeks = source_weeks_raw

    pool_week_map = {}
    if source_weeks:
        for key, value in source_weeks.items():
            try:
                pid = int(key)
                pool_week_map[pid] = date.fromisoformat(value)
            except (TypeError, ValueError):
                return JsonResponse({"ok": False, "error": "invalid_source_weeks"}, status=400)
    elif source_week_start_raw:
        try:
            source_week_start = date.fromisoformat(source_week_start_raw)
        except ValueError:
            return JsonResponse({"ok": False, "error": "invalid_week"}, status=400)
        pool_week_map = {pid: source_week_start for pid in pool_ids}
    else:
        return JsonResponse({"ok": False, "error": "missing_fields"}, status=400)

    pools = list(Pool.objects.filter(id__in=pool_ids).select_related("organization"))
    if not pools or len({pool.id for pool in pools}) != len(set(pool_ids)):
        return JsonResponse({"ok": False, "error": "pool_not_found"}, status=404)

    if not request.user.is_superuser:
        org_ids = {pool.organization_id for pool in pools}
        if None in org_ids:
            return JsonResponse({"ok": False, "error": "forbidden"}, status=403)
        access_org_ids = set(
            OrganizationAccess.objects.filter(user=request.user, organization_id__in=org_ids)
            .values_list("organization_id", flat=True)
        )
        if access_org_ids != org_ids:
            return JsonResponse({"ok": False, "error": "forbidden"}, status=403)

    def _allow_month_move(pool):
        if pool.service_interval_days:
            return int(pool.service_interval_days) > 14
        if not pool.service_frequency:
            return False
        return pool.service_frequency not in {
            Pool.SERVICE_FREQ_WEEKLY,
            Pool.SERVICE_FREQ_TWICE_MONTHLY,
        }

    allow_month_move = all(_allow_month_move(pool) for pool in pools)

    if allow_month_move:
        if not source_month:
            return JsonResponse({"ok": False, "error": "invalid_month"}, status=400)
        if planned_date.strftime("%Y-%m") != source_month:
            return JsonResponse({"ok": False, "error": "invalid_month"}, status=400)
    else:
        display_week_start = None
        if display_week_start_raw:
            try:
                display_week_start = date.fromisoformat(display_week_start_raw)
            except ValueError:
                return JsonResponse({"ok": False, "error": "invalid_week"}, status=400)
        if not display_week_start and pool_week_map:
            unique_weeks = {week for week in pool_week_map.values() if week}
            if len(unique_weeks) == 1:
                display_week_start = next(iter(unique_weeks))
        if not display_week_start:
            return JsonResponse({"ok": False, "error": "invalid_week"}, status=400)
        display_week_end = display_week_start + timedelta(days=6)
        if planned_date < display_week_start or planned_date > display_week_end:
            return JsonResponse({"ok": False, "error": "invalid_week"}, status=400)

    target_week_start = planned_date - timedelta(days=planned_date.weekday())
    target_week_end = target_week_start + timedelta(days=6)

    org_user_ids = set(
        OrganizationAccess.objects.filter(organization_id__in={pool.organization_id for pool in pools})
        .values_list("user_id", flat=True)
    )
    if org_user_ids:
        has_actual = WaterReading.objects.filter(
            pool_id__in=pool_ids,
            added_by_id__in=org_user_ids,
            date__date__gte=target_week_start,
            date__date__lte=target_week_end,
        ).exists()
        if has_actual:
            return JsonResponse({"ok": False, "error": "already_completed"}, status=409)

    for pool in pools:
        source_week = pool_week_map.get(pool.id)
        if not source_week:
            return JsonResponse({"ok": False, "error": "invalid_source_weeks"}, status=400)
        plan, created = ServiceVisitPlan.objects.get_or_create(
            pool=pool,
            week_start=source_week,
            defaults={"planned_date": planned_date, "created_by": request.user},
        )
        if not created and plan.planned_date != planned_date:
            plan.planned_date = planned_date
            plan.save(update_fields=["planned_date", "updated_at"])

    return JsonResponse({"ok": True, "planned_date": planned_date.isoformat()})

@csrf_protect

@never_cache

def water_reading_create(request, pool_uuid):

    readonly = _deny_superuser_write(request)

    if readonly:

        return readonly

    blocked = _redirect_if_access_blocked(request)

    if blocked:

        return blocked



    """Создание нового замера для выбранного объекта."""

    pool = get_object_or_404(Pool, uuid=pool_uuid)
    is_water_object = pool.object_type == Pool.OBJECT_TYPE_WATER

    role = _pool_role_for_user(request.user, pool)

    if role not in {"editor", "service", "admin"}:

        return render(request, "403.html")



    if request.method == "POST":

        form = WaterReadingForm(request.POST)

        if form.is_valid():

            reading = form.save(commit=False)

            reading.date = reading.date.replace(tzinfo=None)

            reading.pool = pool

            reading.added_by = request.user

            reading.save()

            notify_reading_out_of_range(reading)

            if is_water_object:
                messages.success(request, "Запись добавлена")
            else:
                messages.success(request, "Показания добавлены")

            return redirect("pool_detail", pool_uuid=pool.uuid)

        else:
            if is_water_object:
                messages.error(request, "Проверьте поля записи.")
            else:
                messages.error(request, "Проверьте поля показаний.")

    else:

        form = WaterReadingForm()



    return render(request, "pool_service/water_reading_form.html", {"form": form, "pool": pool, "active_tab": "pools"})





@csrf_protect
@never_cache
@login_required
def water_object_visit_create(request, pool_uuid):
    pool = get_object_or_404(Pool, uuid=pool_uuid)
    if pool.object_type != Pool.OBJECT_TYPE_WATER:
        return redirect("water_reading_create", pool_uuid=pool.uuid)
    return water_reading_create(request, pool_uuid=pool.uuid)


@login_required

def water_reading_edit(request, reading_uuid):

    readonly = _deny_superuser_write(request)

    if readonly:

        return readonly

    blocked = _redirect_if_access_blocked(request)

    if blocked:

        return blocked



    reading = get_object_or_404(WaterReading.objects.select_related("pool"), uuid=reading_uuid)
    is_water_object = reading.pool.object_type == Pool.OBJECT_TYPE_WATER



    role = _pool_role_for_user(request.user, reading.pool)

    if role not in {"editor", "service", "admin"}:

        messages.error(request, "Редактирование доступно только пользователям с правами редактора.")

        return redirect("pool_detail", pool_uuid=reading.pool.uuid)



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

            notify_reading_out_of_range(updated)

            if is_water_object:
                messages.success(request, "Запись обновлена.")
            else:
                messages.success(request, "Показания обновлены.")

            return redirect("pool_detail", pool_uuid=reading.pool.uuid)

        if is_water_object:
            messages.error(request, "Не удалось обновить запись. Проверьте форму.")
        else:
            messages.error(request, "Не удалось обновить показания. Проверьте форму.")

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

    notification_access = (

        org_accesses.filter(role__in={"owner", "admin"}).select_related("organization").first()

    )



    if request.method == "POST" and request.POST.get("notification_settings") == "1":

        if not notification_access:

            return render(request, "403.html")

        organization = notification_access.organization

        organization.notify_limits = bool(request.POST.get("notify_limits"))

        organization.notify_missed_visits = bool(request.POST.get("notify_missed_visits"))

        organization.notify_pool_staff_daily = bool(request.POST.get("notify_pool_staff_daily"))

        organization.save(

            update_fields=[

                "notify_limits",

                "notify_missed_visits",

                "notify_pool_staff_daily",

            ]

        )

        messages.success(request, "\u041d\u0430\u0441\u0442\u0440\u043e\u0439\u043a\u0438 \u0443\u0432\u0435\u0434\u043e\u043c\u043b\u0435\u043d\u0438\u0439 \u0441\u043e\u0445\u0440\u0430\u043d\u0435\u043d\u044b.")

        return redirect("profile")



    phone = None

    if profile and hasattr(profile, "phone"):

        phone = profile.phone

    if not phone and request.user.username and request.user.username.isdigit():

        phone = request.user.username

    phone_display = _format_profile_phone_display(phone)



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

        "phone": phone_display,

        "last_login": request.user.last_login,

        "date_joined": request.user.date_joined,

        "role_level": role_level,

        "org_accesses": org_accesses,

        "pool_accesses": pool_accesses,

        "email_confirmed": email_confirmed,

        "phone_confirmed": phone_confirmed,

        "confirm_phone_url": confirm_phone_url,

        "can_manage_notifications": bool(notification_access),

        "notification_org": notification_access.organization if notification_access else None,

        "notify_limits": notification_access.organization.notify_limits if notification_access else False,

        "notify_missed_visits": notification_access.organization.notify_missed_visits if notification_access else False,

        "notify_pool_staff_daily": notification_access.organization.notify_pool_staff_daily if notification_access else False,

    }

    return render(request, "pool_service/profile.html", context)





@login_required

def organization_norms(request):

    org_access = OrganizationAccess.objects.filter(user=request.user).select_related("organization").first()

    if not org_access:

        return render(request, "403.html")



    if not request.user.is_superuser and org_access.role not in {"owner", "admin"}:

        return render(request, "403.html")



    organization = org_access.organization



    norms, _ = OrganizationWaterNorms.objects.get_or_create(organization=organization)



    if request.method == "POST":

        form = OrganizationWaterNormsForm(request.POST, instance=norms)

        if form.is_valid():

            form.save()

            messages.success(request, "\u041d\u043e\u0440\u043c\u0430\u0442\u0438\u0432\u044b \u043e\u0431\u043d\u043e\u0432\u043b\u0435\u043d\u044b.")

            return redirect("organization_norms")

    else:

        form = OrganizationWaterNormsForm(instance=norms)



    return render(

        request,

        "pool_service/organization_norms.html",

        {

            "form": form,

            "page_title": "\u041d\u043e\u0440\u043c\u0430\u0442\u0438\u0432\u044b \u0432\u043e\u0434\u044b",

            "page_subtitle": "\u041e\u0431\u0449\u0438\u0435 \u043f\u0430\u0440\u0430\u043c\u0435\u0442\u0440\u044b \u0434\u043b\u044f \u0432\u0441\u0435\u0445 \u0431\u0430\u0441\u0441\u0435\u0439\u043d\u043e\u0432 \u043e\u0440\u0433\u0430\u043d\u0438\u0437\u0430\u0446\u0438\u0438",

            "active_tab": "norms",

            "show_search": False,

            "show_add_button": False,

            "add_url": None,

        },

    )





@login_required
def notifications_list(request):
    qs = Notification.objects.filter(user=request.user, is_resolved=False).order_by("-created_at")
    notifications = list(qs)

    current_tz = timezone.get_current_timezone()
    default_tz = timezone.get_default_timezone()
    if not settings.USE_TZ and request.user.is_authenticated:
        tz_name = None
        try:
            tz_name = request.user.profile.timezone
        except Profile.DoesNotExist:
            tz_name = None
        if tz_name:
            try:
                current_tz = ZoneInfo(tz_name)
            except Exception:
                current_tz = timezone.get_current_timezone()

    for note in notifications:
        created_at = note.created_at
        if timezone.is_naive(created_at):
            created_at = timezone.make_aware(created_at, default_tz)
        note.display_time = created_at.astimezone(current_tz)

    def _parse_task_message(message):
        if not message:
            return "", ""
        base = message
        details = ""
        if message.endswith(")") and " (" in message:
            head, _, tail = message.rpartition(" (")
            if head and tail.endswith(")"):
                base = head
                details = tail[:-1]
        return base, details

    def _parse_limits_message(message):
        if not message:
            return "", ""
        object_name = ""
        details = message
        if ":" in message:
            object_name, _, details = message.partition(":")
            object_name = object_name.strip()
            details = details.strip()
        parts = [part.strip() for part in details.split(";") if part.strip()]
        converted = []
        for part in parts:
            if ":" not in part:
                converted.append(part)
                continue
            label, _, expr = part.partition(":")
            label = label.strip()
            expr = expr.strip()
            if "<" in expr:
                converted.append(f"низкий уровень {label} ({expr})")
            elif ">" in expr:
                converted.append(f"высокий уровень {label} ({expr})")
            else:
                converted.append(f"{label}: {expr}")
        detail_text = "; ".join(converted) if converted else details
        return object_name, detail_text

    object_kinds = {"limits", "missed_visit", "daily_missing"}
    task_notifications = []
    deviation_notifications = []
    for note in notifications:
        if note.kind == "task_assignment":
            title, details = _parse_task_message(note.message or note.title)
            note.task_title = title or note.title or ""
            note.task_details = details
            task_notifications.append(note)
        else:
            if note.kind in object_kinds:
                obj_name, detail_text = _parse_limits_message(note.message)
                note.deviation_prefix = "\u041d\u0430 \u043e\u0431\u044a\u0435\u043a\u0442\u0435"
                note.object_name = obj_name or note.title or ""
                note.deviation_details = detail_text
            else:
                note.deviation_prefix = ""
                note.object_name = note.title or ""
                note.deviation_details = note.message or ""
            deviation_notifications.append(note)

    task_unread_count = sum(1 for note in task_notifications if not note.is_read)
    deviation_unread_count = sum(1 for note in deviation_notifications if not note.is_read)

    return render(
        request,
        "pool_service/notifications.html",
        {
            "notifications": notifications,
            "task_notifications": task_notifications,
            "deviation_notifications": deviation_notifications,
            "task_unread_count": task_unread_count,
            "deviation_unread_count": deviation_unread_count,
            "page_title": None,
            "page_subtitle": None,
            "active_tab": "notifications",
            "show_search": False,
            "show_add_button": False,
            "add_url": None,
        },
    )


@login_required
def notification_mark_read(request, notification_id):

    if request.method != "POST":

        return redirect("notifications")

    note = get_object_or_404(Notification, id=notification_id, user=request.user)

    if not note.is_read:

        note.is_read = True

        note.save(update_fields=["is_read"])

    return redirect(request.POST.get("next") or "notifications")





@login_required

def notifications_mark_all(request):

    if request.method == "POST":

        kind = (request.POST.get("kind") or "").strip()
        qs = Notification.objects.filter(user=request.user, is_read=False)
        if kind == "task":
            qs = qs.filter(kind="task_assignment")
        elif kind == "service":
            qs = qs.exclude(kind="task_assignment")
        qs.update(is_read=True)

    return redirect("notifications")





@login_required

def notifications_resolve_all(request):
    if request.method != "POST":
        return redirect("notifications")

    now = timezone.now()
    kind = (request.POST.get("kind") or "").strip()
    qs = Notification.objects.filter(user=request.user, is_resolved=False)
    if kind == "task":
        qs = qs.filter(kind="task_assignment")
    elif kind == "service":
        qs = qs.exclude(kind="task_assignment")
    qs.update(
        is_resolved=True,
        is_read=True,
        resolved_at=now,
    )
    return redirect("notifications")


@login_required
def notification_resolve(request, notification_id):

    if request.method != "POST":

        return redirect("notifications")

    note = get_object_or_404(Notification, id=notification_id, user=request.user)

    if not note.is_resolved:

        note.is_resolved = True

        note.is_read = True

        note.resolved_at = timezone.now()

        note.save(update_fields=["is_resolved", "is_read", "resolved_at"])

    return redirect(request.POST.get("next") or "notifications")





@login_required

@require_POST

def push_subscribe(request):

    try:

        payload = json.loads(request.body.decode("utf-8"))

    except (json.JSONDecodeError, UnicodeDecodeError):

        return JsonResponse({"ok": False, "error": "invalid_payload"}, status=400)



    endpoint = payload.get("endpoint")

    keys = payload.get("keys") or {}

    p256dh = keys.get("p256dh")

    auth = keys.get("auth")

    if not endpoint or not p256dh or not auth:

        return JsonResponse({"ok": False, "error": "missing_fields"}, status=400)



    user_agent = request.META.get("HTTP_USER_AGENT", "")[:255]

    PushSubscription.objects.update_or_create(

        endpoint=endpoint,

        defaults={

            "user": request.user,

            "p256dh": p256dh,

            "auth": auth,

            "user_agent": user_agent,

        },

    )

    return JsonResponse({"ok": True})





@login_required

@require_POST

def push_unsubscribe(request):

    try:

        payload = json.loads(request.body.decode("utf-8"))

    except (json.JSONDecodeError, UnicodeDecodeError):

        return JsonResponse({"ok": False, "error": "invalid_payload"}, status=400)



    endpoint = payload.get("endpoint")

    if not endpoint:

        return JsonResponse({"ok": False, "error": "missing_fields"}, status=400)



    PushSubscription.objects.filter(user=request.user, endpoint=endpoint).delete()

    return JsonResponse({"ok": True})





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
