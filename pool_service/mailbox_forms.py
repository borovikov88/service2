import re

from django import forms
from django.core.exceptions import ValidationError
from django.core.validators import validate_email

from .services.mailbox_mime import display_address, smtp_address


class MailboxSetupForm(forms.Form):
    address = forms.EmailField(label="Адрес почтового ящика", max_length=254)
    password = forms.CharField(
        label="Пароль приложения VK WorkSpace",
        strip=False,
        max_length=256,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )

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


class NewMailLeadForm(forms.Form):
    title = forms.CharField(label="Название лида", max_length=255)
    need = forms.CharField(label="Что нужно клиенту", max_length=2000)
    next_title = forms.CharField(label="Следующий шаг", max_length=255)
    due = forms.DateTimeField(
        label="Когда выполнить", widget=forms.DateTimeInput(attrs={"type": "datetime-local"})
    )
