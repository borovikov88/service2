from django.contrib import messages
from django.contrib.auth import login, authenticate
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.views import LoginView
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_protect
from django.urls import reverse, reverse_lazy
from django.db.models import Count
from django.http import HttpResponseForbidden

from .forms import WaterReadingForm, RegistrationForm, ClientCreateForm
from .models import OrganizationAccess, Pool, PoolAccess, WaterReading, Client
from .models import Client
from django import forms


def index(request):
    return render(request, "pool_service/index.html")


@login_required
def pool_list(request):
    """Список бассейнов доступных пользователю."""
    if request.user.is_superuser:
        pools = Pool.objects.all()
    elif OrganizationAccess.objects.filter(user=request.user).exists():
        org_access = OrganizationAccess.objects.get(user=request.user)
        pools = Pool.objects.filter(organization=org_access.organization)
    else:
        pools = Pool.objects.filter(accesses__user=request.user)

    pools = pools.annotate(num_readings=Count("waterreading")).select_related("client")

    return render(
        request,
        "pool_service/pool_list.html",
        {
            "pools": pools,
            "page_title": "Бассейны",
            "page_subtitle": "Управление объектами обслуживания",
            "show_search": False,
            "show_add_button": False,
            "add_url": None,
            "active_tab": "pools",
        },
    )


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
    if request.user.is_superuser or is_org_admin:
        org_staff = (
            OrganizationAccess.objects.filter(**org_filter)
            .select_related("organization", "user")
            .order_by("organization__name", "user__last_name")
        )

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
            "page_subtitle": "Сотрудники сервисной компании и представители бассейнов",
            "org_staff": org_staff,
            "pool_staff": pool_staff,
            "active_tab": None,
            "show_search": False,
            "show_add_button": False,
            "add_url": None,
        },
    )


class PoolForm(forms.ModelForm):
    class Meta:
        model = Pool
        fields = ["client", "address", "description"]
        widgets = {
            "client": forms.Select(attrs={"class": "form-select"}),
            "address": forms.TextInput(attrs={"class": "form-control rounded-3"}),
            "description": forms.Textarea(attrs={"class": "form-control rounded-3", "rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)
        if user:
            client_qs = Client.objects.all()
            client_self = Client.objects.filter(user=user)
            if client_self.exists():
                client_qs = client_self
                self.fields["client"].empty_label = None
                self.fields["client"].initial = client_self.first()
            self.fields["client"].queryset = client_qs


@login_required
def pool_create(request):
    user_client = Client.objects.filter(user=request.user).first()

    if request.method == "POST":
        form = PoolForm(request.POST, user=request.user)
        if form.is_valid():
            pool = form.save(commit=False)
            if user_client:
                pool.client = user_client
            pool.save()
            # дать доступ создателю
            PoolAccess.objects.get_or_create(user=request.user, pool=pool, defaults={"role": "viewer"})
            messages.success(request, "Бассейн создан")
            return redirect("pool_detail", pool_id=pool.id)
    else:
        form = PoolForm(user=request.user)

    return render(
        request,
        "pool_service/pool_create.html",
        {
            "form": form,
            "page_title": "Новый бассейн",
            "active_tab": "pools",
            "show_add_button": False,
            "add_url": None,
        },
    )


@login_required
def client_create(request):
    roles = list(OrganizationAccess.objects.filter(user=request.user).values_list("role", flat=True))
    if not request.user.is_superuser and not any(r in ["admin", "service", "manager"] for r in roles):
        return HttpResponseForbidden()

    if request.method == "POST":
        form = ClientCreateForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Клиент создан")
            return redirect("pool_list")
    else:
        form = ClientCreateForm()

    return render(
        request,
        "pool_service/client_create.html",
        {
            "form": form,
            "page_title": "Создание клиента",
            "active_tab": "pools",
        },
    )


def home(request):
    """
    Домашняя страница с формой авторизации и кратким описанием сервиса.
    """
    if request.method == "POST":
        form = AuthenticationForm(data=request.POST)
        if form.is_valid():
            user = form.get_user()
            login(request, user)
            messages.success(request, "Вход выполнен успешно")
            return redirect("pool_list")
        else:
            messages.error(request, "Неверные учетные данные. Попробуйте ещё раз.")
    else:
        form = AuthenticationForm()

    context = {
        "form": form,
        "info": "Добро пожаловать на наш сервис обслуживания бассейнов и водоочистки. Здесь вы можете узнать об услугах и войти в личный кабинет.",
        "active_tab": "home",
    }
    return render(request, "pool_service/home.html", context)


def register(request):
    """Регистрация сервисной организации или частного клиента."""
    if request.method == "POST":
        form = RegistrationForm(request.POST)
        if form.is_valid():
            user = form.save()
            messages.success(request, "Регистрация выполнена успешно")
            auth_user = authenticate(username=user.username, password=form.cleaned_data["password1"])
            if auth_user:
                login(request, auth_user)
            return redirect("pool_create")
    else:
        form = RegistrationForm()

    return render(
        request,
        "registration/register.html",
        {
            "form": form,
            "page_title": "Регистрация",
            "active_tab": "home",
            "hide_header": True,
        },
    )


@login_required
def pool_detail(request, pool_id):
    """Детальная страница бассейна с показателями и доступами."""
    pool = get_object_or_404(Pool, id=pool_id)

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

    if not role:
        return render(request, "403.html")

    readings_list = WaterReading.objects.filter(pool=pool).select_related("added_by").order_by("-date")

    per_page = request.GET.get("per_page", 20)
    try:
        per_page = int(per_page)
    except ValueError:
        per_page = 20

    paginator = Paginator(readings_list, per_page)
    page_number = request.GET.get("page")
    readings = paginator.get_page(page_number)

    context = {
        "pool": pool,
        "readings": readings,
        "per_page": per_page,
        "role": role,
        "page_title": None,
        "page_subtitle": None,
        "show_search": False,
        "show_add_button": True if role in ["viewer", "editor", "service", "admin"] else False,
        "add_url": reverse("water_reading_create", args=[pool.id]),
        "active_tab": "pools",
    }
    return render(request, "pool_service/pool_detail.html", context)


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

    per_page = request.GET.get("per_page", 50)
    paginator = Paginator(readings_list, per_page)
    page_number = request.GET.get("page")
    readings = paginator.get_page(page_number)

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
        },
    )


@csrf_protect
def water_reading_create(request, pool_id):
    """Создание нового замера для выбранного бассейна."""
    pool = get_object_or_404(Pool, pk=pool_id)

    if request.method == "POST":
        form = WaterReadingForm(request.POST)
        if form.is_valid():
            reading = form.save(commit=False)
            reading.date = reading.date.replace(tzinfo=None)
            reading.pool = pool
            reading.added_by = request.user
            reading.save()
            messages.success(request, "Показания добавлены")
            return redirect("pool_detail", pool_id=pool.id)
        else:
            messages.error(request, "Не удалось сохранить показания, проверьте данные")
    else:
        form = WaterReadingForm()

    return render(request, "pool_service/water_reading_form.html", {"form": form, "pool": pool, "active_tab": "pools"})


@login_required
def profile_view(request):
    """Профиль текущего пользователя с основными контактами и правами доступа."""
    profile = getattr(request.user, "profile", None)
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
    }
    return render(request, "pool_service/profile.html", context)


class CustomLoginView(LoginView):
    template_name = "registration/login.html"
    success_url = reverse_lazy("pool_list")

    def form_valid(self, form):
        messages.success(self.request, "Вход выполнен успешно")
        return super().form_valid(form)

    def form_invalid(self, form):
        messages.error(self.request, "Неверные учетные данные. Попробуйте ещё раз.")
        return super().form_invalid(form)
