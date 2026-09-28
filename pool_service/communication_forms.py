from django import forms


class BootstrapCommunicationForm(forms.Form):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            widget = field.widget
            css_class = "form-select" if isinstance(widget, forms.Select) else "form-control"
            widget.attrs["class"] = f"{widget.attrs.get('class', '')} {css_class}".strip()


class WebsiteConnectionForm(BootstrapCommunicationForm):
    name = forms.CharField(
        label="Название подключения",
        max_length=120,
        help_text="Например: Основной сайт.",
    )
    external_id = forms.CharField(
        label="Идентификатор сайта",
        max_length=255,
        help_text="Внутренний уникальный идентификатор, например aqualine22.ru.",
    )


class AvitoConnectionForm(BootstrapCommunicationForm):
    name = forms.CharField(
        label="Название подключения",
        max_length=120,
        help_text="Например: Основной аккаунт Авито.",
    )
    external_id = forms.CharField(
        label="ID аккаунта Авито",
        max_length=255,
        help_text="Числовой ID аккаунта из Авито.",
    )
    client_id = forms.CharField(
        label="Client ID",
        max_length=500,
        required=False,
        widget=forms.PasswordInput(
            render_value=False,
            attrs={"autocomplete": "new-password"},
        ),
        help_text="При редактировании оставьте пустым, чтобы не менять сохранённый секрет.",
    )
    client_secret = forms.CharField(
        label="Client Secret",
        max_length=1000,
        required=False,
        widget=forms.PasswordInput(
            render_value=False,
            attrs={"autocomplete": "new-password"},
        ),
        help_text="Хранится только в зашифрованном виде.",
    )

    def __init__(self, *args, require_credentials=False, **kwargs):
        self.require_credentials = require_credentials
        super().__init__(*args, **kwargs)
        if require_credentials:
            self.fields["client_id"].required = True
            self.fields["client_secret"].required = True

    def clean_external_id(self):
        value = self.cleaned_data["external_id"].strip()
        if not value.isascii() or not value.isdecimal():
            raise forms.ValidationError("ID аккаунта Авито должен содержать только цифры.")
        return value

    def clean(self):
        cleaned = super().clean()
        client_id = cleaned.get("client_id")
        client_secret = cleaned.get("client_secret")
        if bool(client_id) != bool(client_secret):
            message = "Client ID и Client Secret нужно указывать вместе."
            if not client_id:
                self.add_error("client_id", message)
            if not client_secret:
                self.add_error("client_secret", message)
        return cleaned


class MegafonConnectionForm(BootstrapCommunicationForm):
    name = forms.CharField(
        label="Название подключения",
        max_length=120,
        initial="Мегафон",
    )
    external_id = forms.CharField(
        label="Идентификатор ВАТС / аккаунта",
        max_length=255,
        help_text="Идентификатор подключения в Мегафоне.",
    )
    recording_hosts = forms.CharField(
        label="Разрешённые домены записей разговоров",
        required=False,
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text="По одному домену на строку, без https://, пути и порта.",
    )

    def clean_recording_hosts(self):
        raw = self.cleaned_data.get("recording_hosts", "")
        result = []
        for line in raw.splitlines():
            host = line.strip().lower()
            if not host:
                continue
            if any(character.isspace() for character in host):
                raise forms.ValidationError("Домен не должен содержать пробелы.")
            if ":" in host or "/" in host or "@" in host:
                raise forms.ValidationError(
                    "Укажите только имя домена без схемы, пути, порта и логина."
                )
            if host not in result:
                result.append(host)
        return result
