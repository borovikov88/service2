from django.db import models
from django.contrib.auth.models import User
from django_ckeditor_5.fields import CKEditor5Field

# Организация (Обслуживающая компания или владелец бассейна)
class Organization(models.Model):
    name = models.CharField(max_length=255, unique=True)

    def __str__(self):
        return self.name

class Profile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    timezone = models.CharField(
        max_length=50,
        default='Europe/Moscow',  # или другой часовой пояс по умолчанию
        help_text='Укажите ваш часовой пояс, например, Europe/Moscow'
    )

    def __str__(self):
        return f"{self.user.username}'s profile"

# Клиенты (владельцы бассейнов)
class Client(models.Model):
    name = models.CharField(max_length=100, verbose_name="Имя")
    phone = models.CharField(max_length=20, blank=True, null=True)  # Телефон
    email = models.EmailField(blank=True, null=True)  # Почта
    def __str__(self):
        return f"{self.name}"  # Отображаем фамилию и имя

# Бассейн
class Pool(models.Model):
    client = models.ForeignKey(Client, on_delete=models.CASCADE)
    address = models.CharField(max_length=255)
    organization = models.ForeignKey(
        
        Organization,
        on_delete=models.CASCADE,
        related_name="pools",
        null=True,
        blank=True
    )
    description = CKEditor5Field(blank=True, null=True, verbose_name="Описание бассейна")

    def __str__(self):
        return f"Бассейн: {self.address} ({self.organization.name if self.organization else 'Без организации'})"

# Доступ к бассейну (Клиенты, тренеры и т. д.)
class PoolAccess(models.Model):
    ROLE_CHOICES = [
        ('viewer', 'Просмотр'),
        ('editor', 'Редактирование'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE)
    pool = models.ForeignKey(Pool, on_delete=models.CASCADE, related_name="accesses")
    role = models.CharField(max_length=20, choices=ROLE_CHOICES)

    def __str__(self):
        return f"{self.user.get_full_name()} - {self.pool.address} ({self.role})"
        
# Доступ к организации (Менеджеры, сервисники и т. д.)
class OrganizationAccess(models.Model):
    ROLE_CHOICES = [
        ('manager', 'Менеджер (просмотр всех бассейнов)'),
        ('service', 'Сервисник (внесение данных)'),
        ('admin', 'Администратор (управление бассейнами и пользователями)'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, verbose_name="Пользователь")
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="accesses", verbose_name="Организация")
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, verbose_name="Роль")

    def __str__(self):
        return f"{self.user.get_full_name()} - {self.organization.name} ({self.role})"

# Записи о показаниях воды
class WaterReading(models.Model):
    pool = models.ForeignKey(Pool, on_delete=models.CASCADE, related_name="waterreading")
    date = models.DateTimeField()
    added_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    temperature = models.FloatField(null=True, blank=True)
    ph = models.FloatField(null=True, blank=True)
    chlorine = models.FloatField(null=True, blank=True)
    cltotal = models.FloatField(null=True, blank=True)
    redox = models.FloatField(null=True, blank=True)
    comment = models.TextField(null=True, blank=True)
    required_materials = models.TextField(null=True, blank=True)
    performed_works = models.TextField(null=True, blank=True)

    def __str__(self):
        return f"{self.pool.address} - {self.date.strftime('%d.%m.%Y %H:%M')}"
