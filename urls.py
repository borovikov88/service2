from django.contrib import admin
from django.urls import path, include
from django.contrib.auth import views as auth_views

urlpatterns = [
    path('admin/', admin.site.urls),
    # Подключаем маршруты из pool_service. В файле pool_service/urls.py должен быть определён маршрут для главной страницы (home).
    path('', include('pool_service.urls')),
    # Маршруты для авторизации:
    path('accounts/login/', auth_views.LoginView.as_view(template_name='registration/login.html'), name='login'),
    path('accounts/logout/', auth_views.LogoutView.as_view(next_page='/'), name='logout'),
]
