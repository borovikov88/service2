from django import forms
from .models import WaterReading


class WaterReadingForm(forms.ModelForm):
    # Дата скрыта, заполняется на клиенте локальным временем
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
        labels = {
            "date": "Дата/время",
            "temperature": "Температура",
            "ph": "pH",
            "cl_free": "Свободный хлор",
            "cl_total": "Связанный хлор",
            "ph_dosing_station": "pH, станция дозирования",
            "cl_free_dosing_station": "Свободный хлор, станция дозирования",
            "cl_total_dosing_station": "Общий хлор, станция дозирования",
            "redox_dosing_station": "Redox, станция дозирования",
            "comment": "Комментарий",
            "required_materials": "Требуемые материалы",
            "performed_works": "Выполненные работы",
        }
        widgets = {
            "temperature": forms.NumberInput(
                attrs={"class": "form-control form-control-lg rounded-3", "placeholder": "Введите значение"}
            ),
            "ph": forms.NumberInput(
                attrs={"class": "form-control form-control-lg rounded-3", "placeholder": "Введите значение"}
            ),
            "cl_free": forms.NumberInput(
                attrs={"class": "form-control form-control-lg rounded-3", "placeholder": "Введите значение"}
            ),
            "cl_total": forms.NumberInput(
                attrs={"class": "form-control form-control-lg rounded-3", "placeholder": "Введите значение"}
            ),
            "ph_dosing_station": forms.NumberInput(
                attrs={"class": "form-control form-control-lg rounded-3", "placeholder": "Введите значение"}
            ),
            "cl_free_dosing_station": forms.NumberInput(
                attrs={"class": "form-control form-control-lg rounded-3", "placeholder": "Введите значение"}
            ),
            "cl_total_dosing_station": forms.NumberInput(
                attrs={"class": "form-control form-control-lg rounded-3", "placeholder": "Введите значение"}
            ),
            "redox_dosing_station": forms.NumberInput(
                attrs={"class": "form-control form-control-lg rounded-3", "placeholder": "Введите значение"}
            ),
            "comment": forms.Textarea(
                attrs={"class": "form-control rounded-3", "rows": 3, "placeholder": "Дополнительные заметки..."}
            ),
            "required_materials": forms.Textarea(attrs={"class": "form-control rounded-3", "rows": 2}),
            "performed_works": forms.Textarea(attrs={"class": "form-control rounded-3", "rows": 2}),
        }
