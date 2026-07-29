from django import forms

from .models import Pool


class PoolServiceDetailsForm(forms.ModelForm):
    class Meta:
        model = Pool
        fields = ["service_monthly_price", "service_frequency", "service_details_comment"]
        labels = {
            "service_monthly_price": "Стоимость обслуживания в месяц",
            "service_frequency": "Частота визитов",
            "service_details_comment": "Комментарий",
        }
        widgets = {
            "service_monthly_price": forms.NumberInput(
                attrs={
                    "class": "form-control rounded-3",
                    "step": "0.01",
                    "min": "0",
                    "placeholder": "Например, 25000",
                }
            ),
            "service_frequency": forms.Select(attrs={"class": "form-select rounded-3"}),
            "service_details_comment": forms.Textarea(
                attrs={
                    "class": "form-control rounded-3",
                    "rows": 5,
                    "placeholder": "Условия обслуживания, особенности доступа, договорённости с клиентом",
                }
            ),
        }
