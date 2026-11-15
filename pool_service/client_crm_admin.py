"""The technical admin participates in the same client edit concurrency contract."""
from django import forms
from django.db import transaction
from django.http import HttpResponse

from .client_crm_models import ClientCRMProfile
from .client_import_review import digest, editor_revision
from .models import Client, Organization


def revision(client):
    profile = ClientCRMProfile.objects.filter(client=client).first() or ClientCRMProfile(client=client)
    return digest([editor_revision(client, profile), client.organization_id, client.user_id])


class ClientAdminForm(forms.ModelForm):
    crm_revision = forms.CharField(widget=forms.HiddenInput, required=False)

    class Meta:
        model = Client
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.initial["crm_revision"] = revision(self.instance)

    def clean(self):
        data = super().clean()
        if self.instance.pk:
            current = Client.objects.get(pk=self.instance.pk)
            if data.get("crm_revision") != revision(current):
                raise forms.ValidationError("Карточка изменилась или форма устарела. Обновите страницу.")
        return data


class ClientConcurrencyAdminMixin:
    form = ClientAdminForm

    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        if request.method != "POST" or not object_id:
            return super().changeform_view(request, object_id, form_url, extra_context)
        try:
            pk = int(object_id)
        except (TypeError, ValueError):
            return super().changeform_view(request, object_id, form_url, extra_context)
        org_id = Client.objects.filter(pk=pk).values_list("organization_id", flat=True).first()
        with transaction.atomic():
            if org_id:
                Organization.objects.select_for_update().get(pk=org_id)
            current = Client.objects.select_for_update().filter(pk=pk).first()
            if current and current.organization_id != org_id:
                return HttpResponse("Организация клиента изменилась. Обновите страницу.", status=409)
            return super().changeform_view(request, object_id, form_url, extra_context)
