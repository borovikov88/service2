import re

from django import forms
from django.core.exceptions import ValidationError
from django.core.validators import validate_email

from .mailbox_models import CrmMailbox
from .services.mailbox_transport import PRESETS, public_hostname
from .services.mailbox_mime import display_address, smtp_address


class MailboxSetupForm(forms.Form):
    address = forms.EmailField(label="Адрес почтового ящика", max_length=254)
    provider = forms.ChoiceField(label="Почтовый сервис", choices=CrmMailbox.Provider.choices)
    login = forms.CharField(label="Логин для входа", max_length=254, required=False,
                            help_text="Оставьте пустым, если логин совпадает с адресом ящика.")
    imap_host = forms.CharField(label="Сервер входящей почты (IMAP)", required=False, max_length=253)
    imap_port = forms.TypedChoiceField(label="Порт IMAP", choices=[(993, "993 · SSL/TLS"), (143, "143 · STARTTLS")],
                                       required=False, coerce=int)
    smtp_host = forms.CharField(label="Сервер исходящей почты (SMTP)", required=False, max_length=253)
    smtp_port = forms.TypedChoiceField(label="Порт SMTP", choices=[(465, "465 · SSL/TLS"), (587, "587 · STARTTLS")],
                                       required=False, coerce=int)
    version = forms.IntegerField(widget=forms.HiddenInput, min_value=0, initial=0)
    password = forms.CharField(
        label="Пароль приложения или почтовый пароль",
        required=False,
        strip=False,
        max_length=256,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )

    def __init__(self, *args, mailbox=None, **kwargs):
        self.mailbox = mailbox
        initial = dict(kwargs.pop("initial", {}))
        initial.update(provider=CrmMailbox.Provider.MAIL, imap_port=993, smtp_port=465, version=0)
        if mailbox:
            for name in ("address", "provider", "login", "imap_host", "imap_port", "smtp_host", "smtp_port"):
                initial[name] = getattr(mailbox, name)
            initial["version"] = mailbox.config_version
        super().__init__(*args, initial=initial, **kwargs)
        for name in ("provider", "imap_port", "smtp_port"):
            self.fields[name].widget.attrs["class"] = "form-select"
        self.fields["address"].disabled = bool(mailbox)
        self.fields["password"].required = not (mailbox and mailbox.enabled and mailbox.credential)
        self.fields["password"].help_text = (
            "Чтобы сохранить действующее подключение, оставьте поле пустым."
            if not self.fields["password"].required else "Используйте пароль приложения, если его требует почтовый сервис."
        )

    def clean_login(self):
        login = self.cleaned_data["login"]
        if any(ord(c) < 32 or ord(c) == 127 for c in login):
            raise ValidationError("Укажите логин без управляющих символов.")
        return login

    def clean(self):
        data = super().clean()
        provider = data.get("provider")
        if provider in PRESETS:
            data["imap_host"], data["smtp_host"] = PRESETS[provider]
            data.update(imap_port=993, smtp_port=465)
        elif provider == CrmMailbox.Provider.CUSTOM:
            for field in ("imap_host", "smtp_host"):
                try:
                    data[field] = public_hostname(data.get(field, ""))
                except ValueError:
                    self.add_error(field, "Введите полное доменное имя публичного почтового сервера.")
            for field in ("imap_port", "smtp_port"):
                if not data.get(field) and field not in self.errors:
                    self.add_error(field, "Выберите защищённый порт.")
        if self.mailbox and not self.errors and not data.get("password"):
            fields = ("imap_host", "imap_port", "smtp_host", "smtp_port")
            if (any(data[k] != getattr(self.mailbox, k) for k in fields)
                    or (data["login"] or data["address"]) != (self.mailbox.login or self.mailbox.address)):
                self.add_error("password", "Для смены сервера или логина введите пароль для нового подключения.")
        return data

    def clean_address(self):
        return smtp_address(self.cleaned_data["address"].casefold())


class AddressList(forms.CharField):
    def to_python(self, value):
        items = []
        for item in re.split("[,;]", super().to_python(value)):
            address = item.strip().casefold()
            if address:
                address = display_address(address)
                validate_email(smtp_address(address))
                if address not in items:
                    items.append(address)
        if len(items) > 50:
            raise ValidationError("Можно указать до 50 получателей.")
        return items


class ComposeForm(forms.Form):
    to = AddressList(label="Кому", widget=forms.TextInput(attrs={"inputmode": "email"}))
    cc = AddressList(label="Копия", required=False)
    subject = forms.CharField(label="Тема", max_length=250)
    body = forms.CharField(label="Сообщение", max_length=150000, widget=forms.Textarea(attrs={"rows": 6}))
    token = forms.CharField(widget=forms.HiddenInput)


class MailSignatureForm(forms.Form):
    text = forms.CharField(label="Моя подпись", required=False, max_length=4000, widget=forms.Textarea(attrs={"rows": 5}))


class NewMailLeadForm(forms.Form):
    title = forms.CharField(label="Название лида", max_length=255)
    need = forms.CharField(label="Что нужно клиенту", max_length=2000)
    next_title = forms.CharField(label="Следующий шаг", max_length=255)
    due = forms.DateTimeField(
        label="Когда выполнить", widget=forms.DateTimeInput(attrs={"type": "datetime-local"})
    )
