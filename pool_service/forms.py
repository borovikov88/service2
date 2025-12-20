from django import forms
from django.contrib.auth.models import User
from django.utils.crypto import get_random_string
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

    consent = forms.BooleanField(label="Я соглашаюсь с обработкой персональных данных", required=True)

    def _normalize_phone(self, raw):
        if not raw:
            return None
        digits = "".join(filter(str.isdigit, raw))
        if digits.startswith("7") and len(digits) == 11:
            digits = digits[1:]
        if digits.startswith("8") and len(digits) == 11:
            digits = digits[1:]
        if len(digits) != 10:
            return None
        return digits

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if isinstance(field.widget, forms.RadioSelect):
                continue
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs.update({"class": "form-check-input"})
                continue
            extra = {}
            if name in ["user_phone", "org_phone"]:
                extra = {"class": "form-control rounded-3 phone-mask", "placeholder": field.label}
            else:
                extra = {"class": "form-control rounded-3", "placeholder": field.label}
            field.widget.attrs.update(extra)
        self.fields["user_type"].initial = "client"
        if "org_phone" in self.fields:
            self.fields["org_phone"].initial = "+7 "
        if "user_phone" in self.fields:
            self.fields["user_phone"].initial = "+7 "

    def clean(self):
        cleaned = super().clean()
        phone_raw = cleaned.get("user_phone") or cleaned.get("org_phone")
        username = self._normalize_phone(phone_raw)
        self._normalized_username = username
        if phone_raw and not username:
            self.add_error("user_phone", "Телефон должен быть 10 цифр (без +7/8)")

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
        username = getattr(self, "_normalized_username", None) or self._normalize_phone(
            data.get("user_phone") or data.get("org_phone")
        )
        user = User.objects.create_user(
            username=username,
            password=data["password1"],
            email=data.get("email") or data.get("org_email"),
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
                email=data.get("org_email") or data.get("email"),
            )
            OrganizationAccess.objects.create(user=user, organization=org, role="admin")
        else:
            Client.objects.create(
                
                client_type="private",
                first_name=data.get("first_name"),
                last_name=data.get("last_name"),
                name=f"{data.get('first_name','')} {data.get('last_name','')}".strip(),
                phone=data.get("user_phone"),
                email=data.get("email"),
            )
        return user


class ClientCreateForm(forms.Form):
    CLIENT_TYPE_CHOICES = [("private", "Частный клиент"), ("legal", "Юрлицо")]
    client_type = forms.ChoiceField(choices=CLIENT_TYPE_CHOICES, widget=forms.RadioSelect, initial="private", label="Тип клиента")
    first_name = forms.CharField(label="Имя", required=False)
    last_name = forms.CharField(label="Фамилия", required=False)
    phone = forms.CharField(label="Телефон (логин, без +7/8)", required=False)
    email = forms.EmailField(label="Email", required=False)
    company_name = forms.CharField(label="Название компании", required=False)
    inn = forms.CharField(label="ИНН", required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if isinstance(field.widget, forms.RadioSelect):
                continue
            field.widget.attrs.update({"class": "form-control rounded-3", "placeholder": field.label})
        self.fields["phone"].widget.attrs.update({"class": "form-control rounded-3 phone-mask", "placeholder": self.fields["phone"].label})
        self.fields["phone"].initial = "+7 "

    def clean(self):
        cleaned = super().clean()
        phone_raw = cleaned.get("phone")
        ctype = cleaned.get("client_type") or "private"
        if phone_raw:
            digits = "".join(filter(str.isdigit, phone_raw))
            if digits.startswith("7") and len(digits) == 11:
                digits = digits[1:]
            if digits.startswith("8") and len(digits) == 11:
                digits = digits[1:]
            if len(digits) != 10:
                self.add_error("phone", "Номер телефона должен состоять из 10 цифр (без +7/8)")
        else:
            self.add_error("phone", "Обязательное поле")

        if ctype == "private":
            if not cleaned.get("first_name"):
                self.add_error("first_name", "Обязательное поле")
            if not cleaned.get("last_name"):
                self.add_error("last_name", "Обязательное поле")
        else:
            if not cleaned.get("company_name"):
                self.add_error("company_name", "Обязательное поле")
            cleaned["first_name"] = ""
            cleaned["last_name"] = ""
        return cleaned

    def save(self):
        data = self.cleaned_data
        name_val = data.get("company_name") or f"{data.get('first_name','')} {data.get('last_name','')}".strip()
        return Client.objects.create(
            client_type=data.get("client_type"),
            first_name=data.get("first_name"),
            last_name=data.get("last_name"),
            company_name=data.get("company_name"),
            name=name_val,
            inn=data.get("inn"),
            phone=data.get("phone"),
            email=data.get("email"),
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
            else:
                org_ids = OrganizationAccess.objects.filter(user=user).values_list("organization_id", flat=True)
                if org_ids:
                    client_qs = Client.objects.filter(organization_id__in=org_ids)
                self.fields["client"].empty_label = "Выберите клиента"

            self.fields["client"].queryset = client_qs
            if not client_self.exists():
                self.fields["client"].initial = None
