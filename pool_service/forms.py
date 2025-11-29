from django import forms
from .models import WaterReading

class WaterReadingForm(forms.ModelForm):
    # Можно сделать поле даты скрытым, чтобы пользователь его не редактировал вручную
    date = forms.DateTimeField(widget=forms.HiddenInput())

    class Meta:
        model = WaterReading
        fields = [
            'date',
            'temperature', 
            'ph', 
            'cl_free', 
            'cl_total',
            'ph_dosing_station', 
            'cl_free_dosing_station', 
            'cl_total_dosing_station',
            'redox_dosing_station', 
            'comment', 
            'required_materials', 
            'performed_works', 

        ]
        labels = {
            'date': 'Дата',
            'temperature': 'Температура',
            'ph': 'pH',
            'cl_free': 'Свободный хлор',
            'cl_total': 'Общий хлор',
            'ph_dosing_station': 'pH, станция дозирования',
            'cl_free_dosing_station': 'Свободный хлор, станция дозирования',
            'cl_total_dosing_station': 'Общий хлор, станция дозирования',
            'redox_dosing_station': 'Redox, станция дозирования',
            'comment': 'Комментарий',
            'required_materials': 'Необходимые материалы',
            'performed_works': 'Произведённые работы',

        }