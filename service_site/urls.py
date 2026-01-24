"""
URL configuration for service_site project.
"""
from django.contrib import admin
from django.urls import path, include
from pool_service.views import CustomLoginView, robots_txt, sitemap_xml
from django.contrib.auth import views as auth_views
from django.views.generic import TemplateView
from django.urls import reverse_lazy
from django.conf import settings
from django.templatetags.static import static
from django.conf.urls.static import static as static_serve

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', include('pool_service.urls')),
    path('robots.txt', robots_txt, name='robots_txt'),
    path('sitemap.xml', sitemap_xml, name='sitemap_xml'),
    path('accounts/login/', CustomLoginView.as_view(), name='login'),
    path('accounts/logout/', auth_views.LogoutView.as_view(next_page='/'), name='logout'),
    path(
        'accounts/password-reset/',
        auth_views.PasswordResetView.as_view(
            template_name='registration/password_reset_form.html',
            email_template_name='registration/password_reset_email.txt',
            subject_template_name='registration/password_reset_subject.txt',
            success_url=reverse_lazy('password_reset_done'),
            html_email_template_name='registration/password_reset_email.html',
            extra_email_context={
                'site_url': settings.SITE_URL,
                'logo_url': f"{settings.SITE_URL.rstrip('/')}{static('assets/images/favicon.png')}",
                'brand_url': f"{settings.SITE_URL.rstrip('/')}{static('assets/images/rovikpool.png')}",
            },
            extra_context={'hide_header': True},
        ),
        name='password_reset',
    ),
    path(
        'accounts/password-reset/done/',
        auth_views.PasswordResetDoneView.as_view(
            template_name='registration/password_reset_done.html',
            extra_context={'hide_header': True},
        ),
        name='password_reset_done',
    ),
    path(
        'accounts/reset/<uidb64>/<token>/',
        auth_views.PasswordResetConfirmView.as_view(
            template_name='registration/password_reset_confirm.html',
            success_url=reverse_lazy('password_reset_complete'),
            extra_context={'hide_header': True},
        ),
        name='password_reset_confirm',
    ),
    path(
        'accounts/reset/done/',
        auth_views.PasswordResetCompleteView.as_view(
            template_name='registration/password_reset_complete.html',
            extra_context={'hide_header': True},
        ),
        name='password_reset_complete',
    ),
    path('consent/', TemplateView.as_view(template_name='registration/consent.html'), name='consent'),
    path('sw.js', TemplateView.as_view(template_name='sw.js', content_type='application/javascript'), name='service_worker'),
    path(
        'manifest.webmanifest',
        TemplateView.as_view(template_name='manifest.webmanifest', content_type='application/manifest+json'),
        name='manifest',
    ),
    path('ckeditor5/', include('django_ckeditor_5.urls')),
]

if settings.DEBUG:
    urlpatterns += static_serve(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
