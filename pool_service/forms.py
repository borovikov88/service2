from django import forms
from django.contrib.auth.models import User
from .models import WaterReading, Organization, Client, OrganizationAccess, Pool


class WaterReadingForm(forms.ModelForm):
    date = forms.DateTimeField(widget=forms.HiddenInput())

    class Meta:
        model = WaterReading
        fields = [
            "date",
            "temperature",
            "ph",
            "cl_free",
            "cl_total",
            "ph_dosing_station",
            "cl_free_dosing_station",
            "cl_total_dosing_station",
            "redox_dosing_station",
            "comment",
            "required_materials",
            "performed_works",
        ]


class RegistrationForm(forms.Form):
    USER_TYPE_CHOICES = [
        ("client", "Частный клиент / бюджетник"),
        ("organization", "Сервисная организация"),
    ]

    user_type = forms.ChoiceField(choices=USER_TYPE_CHOICES, widget=forms.RadioSelect, label="Тип регистрации")

    first_name = forms.CharField(label="Имя", required=False)
    last_name = forms.CharField(label="Фамилия", required=False)
    user_phone = forms.CharField(label="Телефон (логин, без +7/8)", required=False)
    email = forms.EmailField(label="Email", required=False)
    password1 = forms.CharField(label="Пароль", widget=forms.PasswordInput)
    password2 = forms.CharField(label="Повторите пароль", widget=forms.PasswordInput)

    org_name = forms.CharField(label="Наименование организации", required=False)
    org_inn = forms.CharField(label="ИНН", required=False)
    org_city = forms.CharField(label="Город", required=False)
    org_address = forms.CharField(label="Адрес", required=False)
    org_phone = forms.CharField(label="Телефон", required=False)
    org_email = forms.EmailField(label="Email", required=False)

    client_name = forms.CharField(label="Имя/название клиента", required=False)
    client_phone = forms.CharField(label="Телефон клиента", required=False)
    client_email = forms.EmailField(label="Email клиента", required=False)
    consent = forms.BooleanField(label="Я соглашаюсь с обработкой персональных данных", required=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            widget = field.widget
            if isinstance(widget, forms.RadioSelect):
                continue
            if isinstance(widget, forms.CheckboxInput):
                widget.attrs.update({"class": "form-check-input"})
                continue
            widget.attrs.update({"class": "form-control rounded-3", "placeholder": field.label})
        self.fields["user_type"].initial = "client"
        if "org_phone" in self.fields:
            self.fields["org_phone"].initial = "+7 "

    def clean(self):
        cleaned = super().clean()
        phone_raw = cleaned.get("user_phone") or cleaned.get("org_phone") or cleaned.get("client_phone")
        username = None
        if phone_raw:
            digits = "".join(filter(str.isdigit, phone_raw))
            if digits.startswith("7") and len(digits) == 11:
                digits = digits[1:]
            if digits.startswith("8") and len(digits) == 11:
                digits = digits[1:]
            if len(digits) != 10:
                self.add_error("user_phone", "Телефон должен быть 10 цифр (без +7/8)")
            else:
                username = digits
                cleaned["username_normalized"] = digits
        if username and User.objects.filter(username=username).exists():
            self.add_error("user_phone", "Пользователь с таким логином уже существует")

        if cleaned.get("password1") != cleaned.get("password2"):
            self.add_error("password2", "Пароли не совпадают")

        if cleaned.get("user_type") == "organization":
            if not cleaned.get("org_name"):
                self.add_error("org_name", "Укажите название организации")
            if not cleaned.get("org_city"):
                self.add_error("org_city", "Укажите город")
            if not cleaned.get("org_phone"):
                self.add_error("org_phone", "Укажите телефон")
            if not cleaned.get("first_name"):
                self.add_error("first_name", "Укажите имя")
            if not cleaned.get("last_name"):
                self.add_error("last_name", "Укажите фамилию")
        if cleaned.get("user_type") == "client":
            if not cleaned.get("first_name"):
                self.add_error("first_name", "Укажите имя")
            if not cleaned.get("last_name"):
                self.add_error("last_name", "Укажите фамилию")
            if not cleaned.get("user_phone"):
                self.add_error("user_phone", "Укажите телефон")
        return cleaned

    def save(self):
        data = self.cleaned_data
        username = data.get("username_normalized") or data.get("user_phone")
        user = User.objects.create_user(
            username=username,
            password=data["password1"],
            email=data.get("email") or data.get("client_email") or data.get("org_email"),
            first_name=data.get("first_name", ""),
            last_name=data.get("last_name", ""),
        )
        if data["user_type"] == "organization":
            org = Organization.objects.create(
                name=data["org_name"],
                inn=data.get("org_inn"),
                city=data.get("org_city"),
                address=data.get("org_address"),
                phone=data.get("org_phone"),
                email=data.get("org_email"),
            )
            OrganizationAccess.objects.create(user=user, organization=org, role="admin")
        else:
            Client.objects.create(
                user=user,
                name=f"{data.get('first_name','')} {data.get('last_name','')}".strip(),
                phone=data.get("client_phone") or data.get("user_phone"),
                email=data.get("client_email") or data.get("email"),
            )
        return user


class ClientCreateForm(forms.Form):
    first_name = forms.CharField(label="Имя")
    last_name = forms.CharField(label="Фамилия")
    phone = forms.CharField(label="Телефон (логин, без +7/8)")
    email = forms.EmailField(label="Email", required=False)
    client_name = forms.CharField(label="Имя/название клиента", required=False)
    client_phone = forms.CharField(label="Телефон клиента", required=False)
    client_email = forms.EmailField(label="Email клиента", required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in self.fields:
            self.fields[name].widget.attrs.update({"class": "form-control rounded-3", "placeholder": self.fields[name].label})

    def clean(self):
        cleaned = super().clean()
        phone_raw = cleaned.get("phone")
        if phone_raw:
            digits = "".join(filter(str.isdigit, phone_raw))
            if digits.startswith("7") and len(digits) == 11:
                digits = digits[1:]
            if digits.startswith("8") and len(digits) == 11:
                digits = digits[1:]
            if len(digits) != 10:
                self.add_error("phone", "Телефон должен быть 10 цифр (без +7/8)")
            cleaned["username_normalized"] = digits
            if User.objects.filter(username=digits).exists():
                self.add_error("phone", "Пользователь с таким телефоном уже существует")
        else:
            self.add_error("phone", "Укажите телефон")
        return cleaned

    def save(self):
        data = self.cleaned_data
        username = data.get("username_normalized")
        user = User.objects.create_user(
            username=username,
            password=User.objects.make_random_password(),
            email=data.get("email") or data.get("client_email"),
            first_name=data.get("first_name", ""),
            last_name=data.get("last_name", ""),
        )
        client = Client.objects.create(
            user=user,
            name=data.get("client_name") or f"{data.get('first_name','')} {data.get('last_name','')}".strip(),
            phone=data.get("client_phone") or data.get("phone"),
            email=data.get("client_email") or data.get("email"),
        )
        return user, client
