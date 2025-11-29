from django.urls import path
from .views import home, pool_list, pool_detail, water_reading_create, readings_all
from . import views

urlpatterns = [
    path('', home, name='home'),
    path('index/', views.index, name='index'),
    path('pools/', pool_list, name='pool_list'),
    path('pools/<int:pool_id>/', pool_detail, name='pool_detail'),
    path('pools/<int:pool_id>/new-reading/', water_reading_create, name='water_reading_create'),
    path("readings/all", readings_all, name="readings_all"),
]
