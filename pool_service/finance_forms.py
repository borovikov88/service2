import uuid
from pathlib import Path

from django import forms
from PIL import Image, UnidentifiedImageError

from pool_service.models import (
    AccountableTransaction,
    Client,
    Expense,
    ExpenseCategory,
)
from pool_service.services.finance import (
    finance_staff,
    find_client_by_name,
    period_is_closed,
    user_display_name,
)


ALLOWED_RECEIPT_CONTENT_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "application/pdf",
}
ALLOWED_RECEIPT_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".pdf"}
MAX_RECEIPT_SIZE = 10 * 1024 * 1024
MAX_RECEIPTS = 5


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    def clean(self, data, initial=None):
        files = data if isinstance(data, (list, tuple)) else ([data] if data else [])
        return [super().clean(item, initial) for item in files]


def validate_receipts(files):
    if len(files) > MAX_RECEIPTS:
        raise forms.ValidationError(f"Можно прикрепить не больше {MAX_RECEIPTS} файлов.")
    for uploaded_file in files:
        suffix = Path(uploaded_file.name).suffix.lower()
        content_type = (getattr(uploaded_file, "content_type", "") or "").lower()
        if suffix not in ALLOWED_RECEIPT_EXTENSIONS or content_type not in ALLOWED_RECEIPT_CONTENT_TYPES:
            raise forms.ValidationError("Разрешены изображения JPG, PNG, WEBP и документы PDF.")
        if uploaded_file.size > MAX_RECEIPT_SIZE:
            raise forms.ValidationError("Размер каждого файла не должен превышать 10 МБ.")
        try:
            uploaded_file.seek(0)
            if content_type.startswith("image/"):
                image = Image.open(uploaded_file)
                if image.width * image.height > 40_000_000:
                    raise forms.ValidationError("Изображение чека имеет слишком большое разрешение.")
                image.verify()
            elif uploaded_file.read(5) != b"%PDF-":
                raise forms.ValidationError("Выбранный PDF-файл повреждён или имеет неверный формат.")
        except (Image.DecompressionBombError, UnidentifiedImageError, OSError, ValueError):
            raise forms.ValidationError("Один из файлов не является корректным изображением.")
        finally:
            uploaded_file.seek(0)
    return files


class AccountableTransactionForm(forms.ModelForm):
    class Meta:
        model = AccountableTransaction
        fields = ["employee", "transaction_type", "amount", "occurred_on", "note"]
        labels = {
            "employee": "Сотрудник",
            "transaction_type": "Операция",
            "amount": "Сумма, ₽",
            "occurred_on": "Дата",
            "note": "Комментарий",
        }
        widgets = {
            "employee": forms.Select(attrs={"class": "form-select"}),
            "transaction_type": forms.Select(attrs={"class": "form-select"}),
            "amount": forms.NumberInput(attrs={"class": "form-control", "min": "0.01", "step": "0.01"}),
            "occurred_on": forms.DateInput(attrs={"class": "form-control", "type": "date"}, format="%Y-%m-%d"),
            "note": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        }

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, **kwargs)
        self.organization = organization
        self.fields["employee"].queryset = finance_staff(organization)
        self.fields["employee"].label_from_instance = user_display_name
        self.fields["transaction_type"].choices = [
            (AccountableTransaction.TYPE_ISSUE, "Выдать под отчёт"),
            (AccountableTransaction.TYPE_RETURN, "Принять возврат"),
        ]
        self.fields["occurred_on"].input_formats = ["%Y-%m-%d"]

    def clean_occurred_on(self):
        occurred_on = self.cleaned_data["occurred_on"]
        if period_is_closed(self.organization, occurred_on):
            raise forms.ValidationError("Этот месяц закрыт. Новые операции запрещены.")
        return occurred_on


class ClientPaymentForm(forms.ModelForm):
    destination_query = forms.CharField(
        required=True,
        label="Клиент",
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "list": "finance-client-options",
                "autocomplete": "off",
                "placeholder": "Начните вводить имя или название",
                "data-client-query": "1",
            }
        ),
    )
    client_id = forms.IntegerField(required=False, widget=forms.HiddenInput(attrs={"data-client-id": "1"}))

    class Meta:
        model = AccountableTransaction
        fields = ["employee", "amount", "occurred_on", "note"]
        labels = {
            "employee": "Кто получил деньги",
            "amount": "Сумма, ₽",
            "occurred_on": "Дата прихода",
            "note": "Комментарий",
        }
        widgets = {
            "employee": forms.Select(attrs={"class": "form-select"}),
            "amount": forms.NumberInput(attrs={"class": "form-control", "min": "0.01", "step": "0.01"}),
            "occurred_on": forms.DateInput(attrs={"class": "form-control", "type": "date"}, format="%Y-%m-%d"),
            "note": forms.Textarea(
                attrs={
                    "class": "form-control",
                    "rows": 3,
                    "placeholder": "Например: оплата диагностики",
                }
            ),
        }

    def __init__(self, *args, organization, user, can_manage, **kwargs):
        super().__init__(*args, **kwargs)
        self.organization = organization
        self.current_user = user
        self.can_manage = can_manage
        self.resolved_client = None
        self.new_client_name = ""
        self.fields["employee"].queryset = finance_staff(organization)
        self.fields["employee"].label_from_instance = user_display_name
        self.fields["occurred_on"].input_formats = ["%Y-%m-%d"]
        if self.instance.pk and self.instance.client_id:
            self.fields["client_id"].initial = self.instance.client_id
            self.fields["destination_query"].initial = self.instance.client.name
        if not can_manage:
            self.fields["employee"].widget = forms.HiddenInput()
            self.fields["employee"].initial = user

    def clean_employee(self):
        if self.can_manage:
            return self.cleaned_data["employee"]
        return self.current_user

    def clean_occurred_on(self):
        occurred_on = self.cleaned_data["occurred_on"]
        if period_is_closed(self.organization, occurred_on):
            raise forms.ValidationError("Этот месяц закрыт. Новые операции запрещены.")
        return occurred_on

    def clean(self):
        cleaned = super().clean()
        client_id = cleaned.get("client_id")
        destination_query = (cleaned.get("destination_query") or "").strip()
        if client_id:
            self.resolved_client = Client.objects.filter(
                id=client_id,
                organization=self.organization,
            ).first()
            if not self.resolved_client:
                self.add_error("destination_query", "Клиент не найден.")
        elif destination_query:
            self.resolved_client = find_client_by_name(self.organization, destination_query)
            if not self.resolved_client:
                self.new_client_name = destination_query[:255]
        else:
            self.add_error("destination_query", "Выберите или укажите нового клиента.")
        return cleaned


class ExpenseForm(forms.ModelForm):
    request_id = forms.UUIDField(widget=forms.HiddenInput)
    receipt_missing_confirmed = forms.BooleanField(required=False, widget=forms.HiddenInput)
    destination_query = forms.CharField(
        required=False,
        label="Клиент",
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "list": "finance-client-options",
                "autocomplete": "off",
                "placeholder": "Начните вводить имя или название",
                "data-client-query": "1",
            }
        ),
    )
    client_id = forms.IntegerField(required=False, widget=forms.HiddenInput(attrs={"data-client-id": "1"}))
    receipts = MultipleFileField(
        required=False,
        label="Фото чека",
        widget=MultipleFileInput(
            attrs={
                "class": "form-control",
                "accept": "image/jpeg,image/png,image/webp,application/pdf",
                "data-finance-receipts": "1",
                "data-photo-picker": "1",
            }
        ),
    )

    class Meta:
        model = Expense
        fields = [
            "source",
            "employee",
            "category",
            "amount",
            "spent_on",
            "destination_type",
            "vendor",
            "description",
            "receipt_missing_confirmed",
        ]
        labels = {
            "source": "Источник денег",
            "employee": "Кто оплатил",
            "category": "Категория",
            "amount": "Сумма, ₽",
            "spent_on": "Дата расхода",
            "destination_type": "Отнести расход на",
            "vendor": "Магазин или поставщик",
            "description": "Что приобретено или оплачено",
        }
        widgets = {
            "source": forms.Select(attrs={"class": "form-select"}),
            "employee": forms.Select(attrs={"class": "form-select"}),
            "category": forms.Select(attrs={"class": "form-select"}),
            "amount": forms.NumberInput(attrs={"class": "form-control", "min": "0.01", "step": "0.01"}),
            "spent_on": forms.DateInput(attrs={"class": "form-control", "type": "date"}, format="%Y-%m-%d"),
            "destination_type": forms.Select(attrs={"class": "form-select", "data-destination-type": "1"}),
            "vendor": forms.TextInput(attrs={"class": "form-control", "placeholder": "Например, Леруа Мерлен"}),
            "description": forms.Textarea(attrs={"class": "form-control", "rows": 4}),
        }

    def __init__(self, *args, organization, user, can_manage, **kwargs):
        super().__init__(*args, **kwargs)
        self.organization = organization
        self.current_user = user
        self.can_manage = can_manage
        self.resolved_client = None
        self.new_client_name = ""
        self.fields["employee"].queryset = finance_staff(organization)
        self.fields["employee"].label_from_instance = user_display_name
        self.fields["category"].queryset = ExpenseCategory.objects.filter(
            organization=organization,
            is_active=True,
        )
        self.fields["spent_on"].input_formats = ["%Y-%m-%d"]
        if self.instance.pk:
            self.fields["request_id"].initial = self.instance.uuid
            if self.instance.client_id:
                self.fields["client_id"].initial = self.instance.client_id
                self.fields["destination_query"].initial = self.instance.client.name
        else:
            self.fields["request_id"].initial = uuid.uuid4()
            self.fields["employee"].initial = user
            self.fields["source"].initial = Expense.SOURCE_ACCOUNTABLE
            self.fields["destination_type"].initial = Expense.DESTINATION_OFFICE
        if not can_manage:
            self.fields["employee"].widget = forms.HiddenInput()
            self.fields["source"].widget = forms.HiddenInput()

    def clean_receipts(self):
        files = self.cleaned_data.get("receipts") or []
        skip_receipt = self.data.get("receipt_missing_confirmed") in {"1", "true", "True", "on"}
        if not files and not self.instance.pk and not skip_receipt:
            raise forms.ValidationError("Добавьте фотографию/PDF чека или нажмите «Пропустить».")
        return validate_receipts(files)

    def clean_spent_on(self):
        spent_on = self.cleaned_data["spent_on"]
        if period_is_closed(self.organization, spent_on):
            raise forms.ValidationError("Этот месяц закрыт. Расход нельзя добавить или изменить.")
        return spent_on

    def clean(self):
        cleaned = super().clean()
        if not self.can_manage:
            cleaned["employee"] = self.current_user
            cleaned["source"] = Expense.SOURCE_ACCOUNTABLE
        destination_type = cleaned.get("destination_type")
        destination_query = (cleaned.get("destination_query") or "").strip()
        client_id = cleaned.get("client_id")
        if destination_type == Expense.DESTINATION_CLIENT:
            if client_id:
                self.resolved_client = Client.objects.filter(
                    id=client_id,
                    organization=self.organization,
                ).first()
                if not self.resolved_client:
                    self.add_error("destination_query", "Клиент не найден.")
            elif destination_query:
                self.resolved_client = find_client_by_name(self.organization, destination_query)
                if not self.resolved_client:
                    self.new_client_name = destination_query[:255]
            else:
                self.add_error("destination_query", "Выберите или укажите нового клиента.")
        else:
            self.resolved_client = None
            self.new_client_name = ""
        return cleaned

    def _post_clean(self):
        self.instance.organization = self.organization
        self.instance.client = self.resolved_client
        self.instance.pool = None
        self.instance.destination_name = self.new_client_name or (
            self.resolved_client.name if self.resolved_client else "Офисные расходы"
        )
        self.instance._allow_unresolved_client = self.cleaned_data.get("destination_type") == Expense.DESTINATION_CLIENT
        try:
            super()._post_clean()
        finally:
            del self.instance._allow_unresolved_client


class ExpenseReviewForm(forms.Form):
    decision = forms.ChoiceField(
        choices=[
            (Expense.STATUS_APPROVED, "Подтвердить"),
            (Expense.STATUS_REJECTED, "Отклонить"),
        ],
        widget=forms.RadioSelect,
        label="Решение",
    )
    review_comment = forms.CharField(
        required=False,
        label="Комментарий",
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 3}),
    )

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("decision") == Expense.STATUS_REJECTED and not (cleaned.get("review_comment") or "").strip():
            self.add_error("review_comment", "Укажите причину отклонения.")
        return cleaned
