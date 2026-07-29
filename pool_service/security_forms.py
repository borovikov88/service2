import re

from django import forms


PIN_RE = re.compile(r"^\d{4,8}$")


class SecurityPinForm(forms.Form):
    current_password = forms.CharField(
        label="Текущий пароль",
        widget=forms.PasswordInput(attrs={"class": "form-control", "autocomplete": "current-password"}),
    )
    pin = forms.CharField(
        label="PIN-код",
        help_text="От 4 до 8 цифр.",
        widget=forms.PasswordInput(attrs={"class": "form-control", "autocomplete": "new-password", "inputmode": "numeric"}),
    )
    pin_confirm = forms.CharField(
        label="Повторите PIN",
        widget=forms.PasswordInput(attrs={"class": "form-control", "autocomplete": "new-password", "inputmode": "numeric"}),
    )

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user

    def clean_current_password(self):
        password = self.cleaned_data["current_password"]
        if not self.user.check_password(password):
            raise forms.ValidationError("Текущий пароль указан неверно.")
        return password

    def clean_pin(self):
        pin = self.cleaned_data["pin"]
        if not PIN_RE.match(pin):
            raise forms.ValidationError("PIN должен содержать от 4 до 8 цифр.")
        return pin

    def clean(self):
        cleaned = super().clean()
        pin = cleaned.get("pin")
        pin_confirm = cleaned.get("pin_confirm")
        if pin and pin_confirm and pin != pin_confirm:
            self.add_error("pin_confirm", "PIN-коды не совпадают.")
        return cleaned


class SecurityPinDisableForm(forms.Form):
    current_password = forms.CharField(
        label="Текущий пароль",
        widget=forms.PasswordInput(attrs={"class": "form-control", "autocomplete": "current-password"}),
    )

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user

    def clean_current_password(self):
        password = self.cleaned_data["current_password"]
        if not self.user.check_password(password):
            raise forms.ValidationError("Текущий пароль указан неверно.")
        return password


class SecurityUnlockForm(forms.Form):
    pin = forms.CharField(
        label="PIN-код",
        widget=forms.PasswordInput(attrs={"class": "form-control form-control-lg text-center", "autocomplete": "current-password", "inputmode": "numeric", "autofocus": "autofocus"}),
    )

    def clean_pin(self):
        pin = self.cleaned_data["pin"]
        if not PIN_RE.match(pin):
            raise forms.ValidationError("PIN должен содержать только цифры.")
        return pin
