from urllib.parse import urlsplit

from django import forms


class CommunicationConnectionForm(forms.Form):
    name = forms.CharField(
        label="Название подключения",
        max_length=120,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "Например, Основной сайт"}),
    )
    external_id = forms.CharField(
        label="Идентификатор",
        max_length=255,
        help_text="Сайт: домен или внутреннее имя. Авито: числовой ID аккаунта.",
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    is_active = forms.BooleanField(
        label="Подключение активно",
        required=False,
        initial=True,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )
    client_id = forms.CharField(
        label="Avito client_id",
        max_length=500,
        required=False,
        widget=forms.PasswordInput(
            render_value=False,
            attrs={"class": "form-control", "autocomplete": "new-password"},
        ),
    )
    client_secret = forms.CharField(
        label="Avito client_secret",
        max_length=1000,
        required=False,
        widget=forms.PasswordInput(
            render_value=False,
            attrs={"class": "form-control", "autocomplete": "new-password"},
        ),
    )

    def __init__(self, *args, kind, require_avito_credentials=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.kind = kind
        self.require_avito_credentials = require_avito_credentials
        if kind != "avito":
            self.fields.pop("client_id")
            self.fields.pop("client_secret")

    def clean(self):
        cleaned = super().clean()
        if self.kind != "avito":
            return cleaned
        external_id = cleaned.get("external_id", "")
        if external_id and not external_id.isascii() or (external_id and not external_id.isdecimal()):
            self.add_error("external_id", "ID аккаунта Авито должен состоять только из цифр.")
        client_id = cleaned.get("client_id", "")
        client_secret = cleaned.get("client_secret", "")
        if self.require_avito_credentials and (not client_id or not client_secret):
            if not client_id:
                self.add_error("client_id", "Укажите client_id.")
            if not client_secret:
                self.add_error("client_secret", "Укажите client_secret.")
        elif bool(client_id) != bool(client_secret):
            message = "Для обновления учётных данных заполните оба поля."
            if client_id:
                self.add_error("client_secret", message)
            else:
                self.add_error("client_id", message)
        return cleaned


class TelephonyConnectionForm(forms.Form):
    name = forms.CharField(
        label="Название подключения",
        max_length=120,
        initial="Мегафон",
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    external_id = forms.CharField(
        label="Внутренний ID линии",
        max_length=255,
        initial="megafon-main",
        help_text=(
            "Это внутренний идентификатор Service2. Для основной ВАТС оставьте "
            "megafon-main — искать этот ID в кабинете МегаФона не нужно."
        ),
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    ats_base_url = forms.CharField(
        label="Адрес АТС",
        max_length=500,
        help_text=(
            "Скопируйте неизменяемое поле «Адрес АТС» из кабинета МегаФона. "
            "Например: https://aqualine22.megapbx.ru/crmapi/v1"
        ),
        widget=forms.URLInput(
            attrs={
                "class": "form-control",
                "placeholder": "https://aqualine22.megapbx.ru/crmapi/v1",
            }
        ),
    )
    ats_api_key = forms.CharField(
        label="Ключ для авторизации в АТС",
        max_length=2000,
        required=False,
        help_text=(
            "Скопируйте неизменяемый ключ из кабинета МегаФона. "
            "Service2 хранит его только в зашифрованном виде."
        ),
        widget=forms.PasswordInput(
            render_value=False,
            attrs={"class": "form-control", "autocomplete": "new-password"},
        ),
    )
    recording_allowed_hosts = forms.CharField(
        label="Разрешённые хосты записей разговоров",
        required=False,
        help_text="По одному доменному имени в строке, без https://, пути и порта.",
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 4, "placeholder": "records.example.ru"}),
    )
    is_active = forms.BooleanField(
        label="Подключение активно",
        required=False,
        initial=True,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )


    def __init__(self, *args, require_ats_api_key=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.require_ats_api_key = require_ats_api_key

    def clean_ats_base_url(self):
        raw = (self.cleaned_data.get("ats_base_url") or "").strip().rstrip("/")
        try:
            parsed = urlsplit(raw)
            parsed.port
        except ValueError as exc:
            raise forms.ValidationError("Некорректный адрес АТС.") from exc
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.port not in (None, 443)
        ):
            raise forms.ValidationError(
                "Укажите HTTPS-адрес АТС без логина, пароля, query-параметров и нестандартного порта."
            )
        return raw

    def clean(self):
        cleaned = super().clean()
        if self.require_ats_api_key and not cleaned.get("ats_api_key"):
            self.add_error(
                "ats_api_key",
                "Скопируйте ключ для авторизации в АТС из кабинета МегаФона.",
            )
        return cleaned

    def clean_recording_allowed_hosts(self):
        raw = self.cleaned_data.get("recording_allowed_hosts", "")
        hosts = []
        for line in raw.replace(",", "\n").splitlines():
            host = line.strip().lower()
            if not host:
                continue
            if (
                "://" in host
                or ":" in host
                or "/" in host
                or "@" in host
                or any(character.isspace() for character in host)
            ):
                raise forms.ValidationError(
                    "Указывайте только доменное имя без схемы, пути и порта."
                )
            if host not in hosts:
                hosts.append(host)
        return hosts
