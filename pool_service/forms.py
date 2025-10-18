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
            'chlorine', 
            'cltotal', 
            'redox', 
            'comment', 
            'required_materials', 
            'performed_works', 

        ]
        labels = {
            'date': 'Дата',
            'temperature': 'Температура',
            'ph': 'pH',
            'chlorine': 'Свободный хлор',
            'cltotal': 'Общий хлор',
            'redox': 'Redox',
            'comment': 'Комментарий',
            'required_materials': 'Необходимые материалы',
            'performed_works': 'Произведённые работы',

        }