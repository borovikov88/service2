from django.urls import path
from .views import home, pool_list, pool_detail, water_reading_create, water_reading_edit, readings_all, profile_view, users_view, register, pool_create, pool_edit, client_create, yandex_suggest, confirm_email, password_change_inline
from . import views

urlpatterns = [
    path('', home, name='home'),
    path('index/', views.index, name='index'),
    path('pools/', pool_list, name='pool_list'),
    path('pools/<uuid:pool_uuid>/', pool_detail, name='pool_detail'),
    path('pools/create/', pool_create, name='pool_create'),
    path("pools/<uuid:pool_uuid>/edit/", pool_edit, name="pool_edit"),
    path("api/yandex/suggest/", yandex_suggest, name="yandex_suggest"),
    path('pools/<uuid:pool_uuid>/new-reading/', water_reading_create, name='water_reading_create'),
    path("readings/<uuid:reading_uuid>/edit/", water_reading_edit, name="water_reading_edit"),
    path("readings/all", readings_all, name="readings_all"),
    path("profile/", profile_view, name="profile"),
    path("users/", users_view, name="users"),
    path("register/", register, name="register"),
    path("accounts/confirm-email/<uidb64>/<token>/", confirm_email, name="confirm_email"),
    path("accounts/password-change/", password_change_inline, name="password_change_inline"),
    path("clients/create/", client_create, name="client_create"),
]
