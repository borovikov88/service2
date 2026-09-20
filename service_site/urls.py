"""
URL configuration for service_site project.
"""
from django.contrib import admin
from django.urls import path, include
from pool_service.views import CustomLoginView, robots_txt, sitemap_xml
from service_site.health_views import health_live, health_ready
from django.contrib.auth import views as auth_views
from django.views.generic import TemplateView
from django.urls import reverse_lazy
from django.conf import settings
from django.templatetags.static import static
from django.conf.urls.static import static as static_serve
from pool_service.mcp_views import mcp_test
from pool_service.finance_mcp_views import (
    finance_mcp,
    finance_protected_resource_metadata,
)
from pool_service.onec_diagnostic_mcp_chatgpt import onec_diagnostic_mcp
from pool_service.mcp_oauth_shared import (
    diagnostic_protected_resource_metadata,
    shared_authorization_server_metadata,
    shared_oauth_authorize,
    shared_oauth_token,
)

urlpatterns = [
    path('health/live/', health_live, name='health_live'),
    path('health/ready/', health_ready, name='health_ready'),
    # Keep transport URLs byte-for-byte identical to their OAuth resource
    # identifiers. Finance and Diagnostic keep isolated resources/scopes while
    # sharing the proven Finance authorization-server issuer and endpoints.
    path('mcp/finance', finance_mcp, name='finance_mcp'),
    path(
        '.well-known/oauth-protected-resource/mcp/finance',
        finance_protected_resource_metadata,
        name='finance_mcp_protected_resource_metadata',
    ),
    path(
        '.well-known/oauth-authorization-server',
        shared_authorization_server_metadata,
        name='finance_mcp_authorization_server_metadata',
    ),
    path('oauth/finance/authorize', shared_oauth_authorize, name='finance_mcp_authorize'),
    path('oauth/finance/token', shared_oauth_token, name='finance_mcp_token'),
    path('mcp/1c', onec_diagnostic_mcp, name='onec_diagnostic_mcp'),
    path(
        '.well-known/oauth-protected-resource/mcp/1c',
        diagnostic_protected_resource_metadata,
        name='onec_diagnostic_mcp_protected_resource_metadata',
    ),
    # Backward-compatible route aliases. They now advertise and execute the
    # same shared root issuer so OAuth metadata and authorization-response iss
    # cannot disagree for saved Diagnostic clients that rediscover metadata.
    path(
        '.well-known/oauth-authorization-server/onec-diagnostic',
        shared_authorization_server_metadata,
        name='onec_diagnostic_mcp_authorization_server_metadata',
    ),
    path('oauth/1c/authorize', shared_oauth_authorize, name='onec_diagnostic_mcp_authorize'),
    path('oauth/1c/token', shared_oauth_token, name='onec_diagnostic_mcp_token'),
    path('mcp/test/', mcp_test, name='mcp_test'),
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
        'manifest.webmanifest', TemplateView.as_view(template_name='manifest.webmanifest', content_type='application/manifest+json'), name='manifest',
    ),
    path('ckeditor5/', include('django_ckeditor_5.urls')),
]

if settings.DEBUG or getattr(settings, "SERVE_MEDIA", False):
    urlpatterns += static_serve(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
