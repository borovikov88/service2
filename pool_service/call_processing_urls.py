from django.urls import path
from pool_service.call_processing_views import call_processing_settings

urlpatterns = [
    path("communications/calls/auto-processing/", call_processing_settings, name="call_processing_settings"),
]
