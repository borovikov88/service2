from django.db import models
from django.contrib.auth.models import User
from django_ckeditor_5.fields import CKEditor5Field


class Organization(models.Model):
    name = models.CharField(max_length=255, unique=True)
    inn = models.CharField(max_length=20, blank=True, null=True)
    city = models.CharField(max_length=100, blank=True, null=True)
    address = models.CharField(max_length=255, blank=True, null=True)
    phone = models.CharField(max_length=20, blank=True, null=True)
    email = models.EmailField(blank=True, null=True)

    def __str__(self):
        return self.name


class Profile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    timezone = models.CharField(
        max_length=50,
        default="Europe/Moscow",
        help_text="Часовой пояс пользователя, например, Europe/Moscow",
    )

    def __str__(self):
        return f"{self.user.username}'s profile"


class Client(models.Model):
    CLIENT_TYPE_CHOICES = [
        ("private", "Частный клиент"),
        ("legal", "Юридическое лицо"),
    ]

    user = models.OneToOneField(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="client_profile")
    client_type = models.CharField(max_length=20, choices=CLIENT_TYPE_CHOICES, default="private")
    first_name = models.CharField(max_length=100, blank=True, null=True)
    last_name = models.CharField(max_length=100, blank=True, null=True)
    company_name = models.CharField(max_length=255, blank=True, null=True)
    name = models.CharField(max_length=255, verbose_name="Имя/название")
    phone = models.CharField(max_length=20, blank=True, null=True)
    email = models.EmailField(blank=True, null=True)
    inn = models.CharField(max_length=20, blank=True, null=True)
    organization = models.ForeignKey(Organization, on_delete=models.SET_NULL, null=True, blank=True, related_name="clients")

    def __str__(self):
        return self.name


class Pool(models.Model):
    SHAPE_CHOICES = [
        ("rect", "Прямоугольный"),
        ("round", "Круглый"),
        ("oval", "Овальный"),
        ("free", "Произвольная форма"),
    ]
    TYPE_CHOICES = [
        ("overflow", "Переливной"),
        ("skimmer", "Скиммерный"),
    ]

    client = models.ForeignKey(Client, on_delete=models.CASCADE)
    address = models.CharField(max_length=255)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="pools",
        null=True,
        blank=True,
    )
    description = CKEditor5Field(blank=True, null=True, verbose_name="Описание бассейна")
    shape = models.CharField(max_length=20, choices=SHAPE_CHOICES, default="rect")
    pool_type = models.CharField(max_length=20, choices=TYPE_CHOICES, default="skimmer")
    length = models.FloatField(null=True, blank=True)
    width = models.FloatField(null=True, blank=True)
    diameter = models.FloatField(null=True, blank=True)
    variable_depth = models.BooleanField(default=False)
    depth = models.FloatField(null=True, blank=True)
    depth_min = models.FloatField(null=True, blank=True)
    depth_max = models.FloatField(null=True, blank=True)
    overflow_volume = models.FloatField(null=True, blank=True)
    surface_area = models.FloatField(null=True, blank=True)
    volume = models.FloatField(null=True, blank=True)
    dosing_station = models.BooleanField(default=False)

    def __str__(self):
        org_name = self.organization.name if self.organization else "без организации"
        return f"Бассейн: {self.address} ({org_name})"


class PoolAccess(models.Model):
    ROLE_CHOICES = [
        ("viewer", "Просмотр"),
        ("editor", "Редактор"),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE)
    pool = models.ForeignKey(Pool, on_delete=models.CASCADE, related_name="accesses")
    role = models.CharField(max_length=20, choices=ROLE_CHOICES)

    def __str__(self):
        return f"{self.user.get_full_name()} - {self.pool.address} ({self.role})"


class OrganizationAccess(models.Model):
    ROLE_CHOICES = [
        ("manager", "Менеджер"),
        ("service", "Сервисник"),
        ("admin", "Администратор"),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, verbose_name="Пользователь")
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="accesses", verbose_name="Организация")
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, verbose_name="Роль")

    def __str__(self):
        return f"{self.user.get_full_name()} - {self.organization.name} ({self.role})"


class WaterReading(models.Model):
    pool = models.ForeignKey(Pool, on_delete=models.CASCADE, related_name="waterreading")
    date = models.DateTimeField()
    added_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    temperature = models.FloatField(null=True, blank=True)
    ph = models.FloatField(null=True, blank=True)
    cl_free = models.FloatField(null=True, blank=True)
    cl_total = models.FloatField(null=True, blank=True)
    ph_dosing_station = models.FloatField(null=True, blank=True)
    cl_free_dosing_station = models.FloatField(null=True, blank=True)
    cl_total_dosing_station = models.FloatField(null=True, blank=True)
    redox_dosing_station = models.FloatField(null=True, blank=True)
    comment = models.TextField(null=True, blank=True)
    required_materials = models.TextField(null=True, blank=True)
    performed_works = models.TextField(null=True, blank=True)

    def __str__(self):
        return f"{self.pool.address} - {self.date.strftime('%d.%m.%Y %H:%M')}"
