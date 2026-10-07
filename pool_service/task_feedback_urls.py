from django.urls import path
from pool_service.task_feedback_views import task_feedback

urlpatterns = [
    path("tasks/<int:task_id>/feedback/", task_feedback, name="task_feedback"),
]
