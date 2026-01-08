from django import forms
from django.contrib.auth.models import User
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.password_validation import validate_password
from django.utils.crypto import get_random_string
from django.utils import timezone
from .models import WaterReading, Organization, Client, OrganizationAccess, Pool, PoolAccess


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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            if isinstance(field.widget, forms.HiddenInput):
                continue
            classes = field.widget.attrs.get("class", "")
            extra = "form-control rounded-3"
            if isinstance(field.widget, forms.Textarea):
                field.widget.attrs.setdefault("rows", 3)
            field.widget.attrs["class"] = f"{classes} {extra}".strip()


class RegistrationForm(forms.Form):
    USER_TYPE_CHOICES = [
        ("client", "Частный владелец / клиент"),
        ("organization", "Сервисная организация"),
    ]

    user_type = forms.ChoiceField(
        choices=USER_TYPE_CHOICES,
        widget=forms.RadioSelect,
        label="Тип учетной записи",
    )

    first_name = forms.CharField(label="Имя", required=False)
    last_name = forms.CharField(label="Фамилия", required=False)
    user_phone = forms.CharField(label="Телефон (только цифры, код +7/8)", required=False)
    email = forms.EmailField(label="Email", required=False)
    password1 = forms.CharField(label="Пароль", widget=forms.PasswordInput)
    password2 = forms.CharField(label="Повторите пароль", widget=forms.PasswordInput)

    org_name = forms.CharField(label="Название организации", required=False)
    org_inn = forms.CharField(label="ИНН", required=False)
    org_city = forms.CharField(label="Город", required=False)
    org_address = forms.CharField(label="Адрес", required=False)
    org_phone = forms.CharField(label="Телефон организации", required=False)
    org_email = forms.EmailField(label="Email организации", required=False)

    consent = forms.BooleanField(
        label="Я соглашаюсь с обработкой персональных данных",
        required=True,
    )

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
            self.add_error("user_phone", "Телефон должен содержать 10 цифр (код +7/8)")

        if username and User.objects.filter(username=username).exists():
            self.add_error("user_phone", "Пользователь с таким телефоном уже существует")

        if cleaned.get("password1") != cleaned.get("password2"):
            self.add_error("password2", "Пароли не совпадают")

        password_value = cleaned.get("password1")
        if password_value:
            try:
                validate_password(password_value)
            except forms.ValidationError as exc:
                self.add_error("password1", exc)

        email_value = cleaned.get("email")
        org_email_value = cleaned.get("org_email")
        if cleaned.get("user_type") == "organization":
            if not (email_value or org_email_value):
                self.add_error("org_email", "Укажите email")
        else:
            if not email_value:
                self.add_error("email", "Укажите email")

        if email_value and User.objects.filter(email__iexact=email_value).exists():
            self.add_error("email", "Этот email уже зарегистрирован")
        if org_email_value and User.objects.filter(email__iexact=org_email_value).exists():
            self.add_error("org_email", "Этот email уже зарегистрирован")

        if cleaned.get("user_type") == "organization":
            if not cleaned.get("org_name"):
                self.add_error("org_name", "Укажите название организации")
            if not cleaned.get("org_city"):
                self.add_error("org_city", "Укажите город")
            if not cleaned.get("org_phone"):
                self.add_error("org_phone", "Укажите телефон организации")
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
            is_active=False,
        )
        if data["user_type"] == "organization":
            org = Organization.objects.create(
                name=data["org_name"],
                inn=data.get("org_inn"),
                city=data.get("org_city"),
                address=data.get("org_address"),
                phone=data.get("org_phone"),
                email=data.get("org_email") or data.get("email"),
                plan_type=Organization.PLAN_COMPANY_TRIAL,
                trial_started_at=timezone.now(),
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


def normalize_phone(raw):
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


class PersonalSignupForm(forms.Form):
    first_name = forms.CharField(required=True)
    last_name = forms.CharField(required=True)
    phone = forms.CharField(required=True)
    email = forms.EmailField(required=True)
    password1 = forms.CharField(widget=forms.PasswordInput, required=True)
    password2 = forms.CharField(widget=forms.PasswordInput, required=True)
    pool_address = forms.CharField(required=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            classes = "form-control rounded-3"
            if name in ["phone"]:
                classes = "form-control rounded-3 phone-mask"
            field.widget.attrs.update({"class": classes})
        self.fields["phone"].initial = "+7 "

    def clean(self):
        cleaned = super().clean()
        phone_raw = cleaned.get("phone")
        username = normalize_phone(phone_raw)
        self._normalized_username = username
        if phone_raw and not username:
            self.add_error("phone", "\u041d\u0435\u043a\u043e\u0440\u0440\u0435\u043a\u0442\u043d\u044b\u0439 \u0442\u0435\u043b\u0435\u0444\u043e\u043d")

        if username and User.objects.filter(username=username).exists():
            self.add_error("phone", "\u041f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u044c \u0441 \u0442\u0430\u043a\u0438\u043c \u0442\u0435\u043b\u0435\u0444\u043e\u043d\u043e\u043c \u0443\u0436\u0435 \u0441\u0443\u0449\u0435\u0441\u0442\u0432\u0443\u0435\u0442")

        if cleaned.get("password1") != cleaned.get("password2"):
            self.add_error("password2", "\u041f\u0430\u0440\u043e\u043b\u0438 \u043d\u0435 \u0441\u043e\u0432\u043f\u0430\u0434\u0430\u044e\u0442")

        password_value = cleaned.get("password1")
        if password_value:
            try:
                validate_password(password_value)
            except forms.ValidationError as exc:
                self.add_error("password1", exc)

        email_value = cleaned.get("email")
        if email_value and User.objects.filter(email__iexact=email_value).exists():
            self.add_error("email", "\u042d\u0442\u043e\u0442 email \u0443\u0436\u0435 \u0437\u0430\u0440\u0435\u0433\u0438\u0441\u0442\u0440\u0438\u0440\u043e\u0432\u0430\u043d")
        return cleaned

    def save(self):
        data = self.cleaned_data
        username = self._normalized_username or normalize_phone(data.get("phone"))
        user = User.objects.create_user(
            username=username,
            password=data["password1"],
            email=data.get("email"),
            first_name=data.get("first_name", ""),
            last_name=data.get("last_name", ""),
            is_active=False,
        )
        client = Client.objects.create(
            user=user,
            client_type="private",
            first_name=data.get("first_name"),
            last_name=data.get("last_name"),
            name=f"{data.get('first_name','')} {data.get('last_name','')}".strip(),
            phone=data.get("phone"),
            email=data.get("email"),
        )
        pool = Pool.objects.create(
            client=client,
            address=data.get("pool_address"),
        )
        PoolAccess.objects.get_or_create(user=user, pool=pool, defaults={"role": "viewer"})
        return user


class CompanySignupForm(forms.Form):
    org_name = forms.CharField(required=True)
    org_city = forms.CharField(required=True)
    org_address = forms.CharField(required=False)
    org_phone = forms.CharField(required=True)
    org_email = forms.EmailField(required=True)
    owner_first_name = forms.CharField(required=True)
    owner_last_name = forms.CharField(required=True)
    owner_phone = forms.CharField(required=True)
    password1 = forms.CharField(widget=forms.PasswordInput, required=True)
    password2 = forms.CharField(widget=forms.PasswordInput, required=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            classes = "form-control rounded-3"
            if name in ["org_phone", "owner_phone"]:
                classes = "form-control rounded-3 phone-mask"
            field.widget.attrs.update({"class": classes})
        self.fields["org_phone"].initial = "+7 "
        self.fields["owner_phone"].initial = "+7 "

    def clean(self):
        cleaned = super().clean()
        phone_raw = cleaned.get("owner_phone")
        username = normalize_phone(phone_raw)
        self._normalized_username = username
        if phone_raw and not username:
            self.add_error("owner_phone", "\u041d\u0435\u043a\u043e\u0440\u0440\u0435\u043a\u0442\u043d\u044b\u0439 \u0442\u0435\u043b\u0435\u0444\u043e\u043d")

        if username and User.objects.filter(username=username).exists():
            self.add_error("owner_phone", "\u041f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u044c \u0441 \u0442\u0430\u043a\u0438\u043c \u0442\u0435\u043b\u0435\u0444\u043e\u043d\u043e\u043c \u0443\u0436\u0435 \u0441\u0443\u0449\u0435\u0441\u0442\u0432\u0443\u0435\u0442")

        if cleaned.get("password1") != cleaned.get("password2"):
            self.add_error("password2", "\u041f\u0430\u0440\u043e\u043b\u0438 \u043d\u0435 \u0441\u043e\u0432\u043f\u0430\u0434\u0430\u044e\u0442")

        password_value = cleaned.get("password1")
        if password_value:
            try:
                validate_password(password_value)
            except forms.ValidationError as exc:
                self.add_error("password1", exc)

        email_value = cleaned.get("org_email")
        if email_value and User.objects.filter(email__iexact=email_value).exists():
            self.add_error("org_email", "\u042d\u0442\u043e\u0442 email \u0443\u0436\u0435 \u0437\u0430\u0440\u0435\u0433\u0438\u0441\u0442\u0440\u0438\u0440\u043e\u0432\u0430\u043d")
        return cleaned

    def save(self):
        data = self.cleaned_data
        username = self._normalized_username or normalize_phone(data.get("owner_phone"))
        user = User.objects.create_user(
            username=username,
            password=data["password1"],
            email=data.get("org_email"),
            first_name=data.get("owner_first_name", ""),
            last_name=data.get("owner_last_name", ""),
            is_active=False,
        )
        org = Organization.objects.create(
            name=data.get("org_name"),
            city=data.get("org_city"),
            address=data.get("org_address"),
            phone=data.get("org_phone"),
            email=data.get("org_email"),
            plan_type=Organization.PLAN_COMPANY_TRIAL,
            trial_started_at=timezone.now(),
        )
        OrganizationAccess.objects.create(user=user, organization=org, role="admin")
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
        self.instance = kwargs.pop("instance", None)
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if isinstance(field.widget, forms.RadioSelect):
                continue
            field.widget.attrs.update({"class": "form-control rounded-3", "placeholder": field.label})
        self.fields["phone"].widget.attrs.update({"class": "form-control rounded-3 phone-mask", "placeholder": self.fields["phone"].label})
        self.fields["phone"].initial = "+7 "
        if self.instance:
            self.initial.update(
                {
                    "client_type": self.instance.client_type or "private",
                    "first_name": self.instance.first_name or "",
                    "last_name": self.instance.last_name or "",
                    "phone": self.instance.phone or "+7 ",
                    "email": self.instance.email or "",
                    "company_name": self.instance.company_name or "",
                    "inn": self.instance.inn or "",
                }
            )

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
        client = self.instance or Client()
        client.client_type = data.get("client_type")
        client.first_name = data.get("first_name")
        client.last_name = data.get("last_name")
        client.company_name = data.get("company_name")
        client.name = name_val
        client.inn = data.get("inn")
        client.phone = data.get("phone")
        client.email = data.get("email")
        client.save()
        return client


class OrganizationInviteForm(forms.Form):
    first_name = forms.CharField(label="\u0418\u043c\u044f", required=True)
    last_name = forms.CharField(label="\u0424\u0430\u043c\u0438\u043b\u0438\u044f", required=True)
    email = forms.EmailField(label="Email", required=True)
    phone = forms.CharField(label="\u0422\u0435\u043b\u0435\u0444\u043e\u043d", required=False)
    role = forms.ChoiceField(choices=OrganizationAccess.ROLE_CHOICES, label="\u0420\u043e\u043b\u044c")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if isinstance(field.widget, forms.RadioSelect):
                continue
            classes = "form-control rounded-3"
            if name in ["role"]:
                field.widget.attrs.update({"class": "form-select"})
                field.widget.choices = field.choices
                continue
            if name in ["phone"]:
                field.widget.attrs.update({"class": f"{classes} phone-mask", "placeholder": field.label})
                self.fields["phone"].initial = "+7 "
                continue
            field.widget.attrs.update({"class": classes, "placeholder": field.label})


class InviteAcceptForm(forms.Form):
    first_name = forms.CharField(label="\u0418\u043c\u044f", required=True)
    last_name = forms.CharField(label="\u0424\u0430\u043c\u0438\u043b\u0438\u044f", required=True)
    phone = forms.CharField(label="\u0422\u0435\u043b\u0435\u0444\u043e\u043d", required=False)
    password1 = forms.CharField(label="\u041f\u0430\u0440\u043e\u043b\u044c", widget=forms.PasswordInput)
    password2 = forms.CharField(label="\u041f\u043e\u0432\u0442\u043e\u0440\u0438\u0442\u0435 \u043f\u0430\u0440\u043e\u043b\u044c", widget=forms.PasswordInput)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            classes = "form-control rounded-3"
            if name == "phone":
                field.widget.attrs.update({"class": f"{classes} phone-mask", "placeholder": field.label})
                field.initial = "+7 "
            else:
                field.widget.attrs.update({"class": classes, "placeholder": field.label})

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("password1") != cleaned.get("password2"):
            self.add_error("password2", "\u041f\u0430\u0440\u043e\u043b\u0438 \u043d\u0435 \u0441\u043e\u0432\u043f\u0430\u0434\u0430\u044e\u0442")
        return cleaned



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


class EmailOrUsernameAuthenticationForm(AuthenticationForm):
    def clean(self):
        username = self.cleaned_data.get("username")
        if username and "@" in username:
            users = User.objects.filter(email__iexact=username)
            if users.count() == 1:
                user = users.first()
                if not user.is_active:
                    raise forms.ValidationError(
                        "\u041f\u043e\u0434\u0442\u0432\u0435\u0440\u0434\u0438\u0442\u0435 email, \u0447\u0442\u043e\u0431\u044b \u0432\u043e\u0439\u0442\u0438.",
                    )
                self.cleaned_data["username"] = user.username
            elif users.count() > 1:
                raise forms.ValidationError("\u041d\u0435\u0441\u043a\u043e\u043b\u044c\u043a\u043e \u0430\u043a\u043a\u0430\u0443\u043d\u0442\u043e\u0432 \u0441 \u044d\u0442\u0438\u043c email. \u0418\u0441\u043f\u043e\u043b\u044c\u0437\u0443\u0439\u0442\u0435 \u043b\u043e\u0433\u0438\u043d.")
        elif username:
            user = User.objects.filter(username__iexact=username).first()
            if user and not user.is_active:
                raise forms.ValidationError(
                    "\u041f\u043e\u0434\u0442\u0432\u0435\u0440\u0434\u0438\u0442\u0435 email, \u0447\u0442\u043e\u0431\u044b \u0432\u043e\u0439\u0442\u0438.",
                )
        return super().clean()
