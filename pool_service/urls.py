from django.urls import path
from .views import home, pool_list, pool_detail, water_reading_create, readings_all, profile_view, users_view, register, pool_create, client_create
from . import views

urlpatterns = [
    path('', home, name='home'),
    path('index/', views.index, name='index'),
    path('pools/', pool_list, name='pool_list'),
    path('pools/<int:pool_id>/', pool_detail, name='pool_detail'),
    path('pools/create/', pool_create, name='pool_create'),
    path('pools/<int:pool_id>/new-reading/', water_reading_create, name='water_reading_create'),
    path("readings/all", readings_all, name="readings_all"),
    path("profile/", profile_view, name="profile"),
    path("users/", users_view, name="users"),
    path("register/", register, name="register"),
    path("clients/create/", client_create, name="client_create"),
]
