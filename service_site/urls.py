"""
URL configuration for service_site project.
"""
from django.contrib import admin
from django.urls import path, include
from pool_service.views import CustomLoginView
from django.contrib.auth import views as auth_views
from django.views.generic import TemplateView

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', include('pool_service.urls')),
    path('accounts/login/', CustomLoginView.as_view(), name='login'),
    path('accounts/logout/', auth_views.LogoutView.as_view(next_page='/'), name='logout'),
    path('consent/', TemplateView.as_view(template_name='registration/consent.html'), name='consent'),
    path('ckeditor5/', include('django_ckeditor_5.urls')),
]
