from django.urls import path
from pool_service.task_feedback_views import task_feedback
from pool_service.task_waiting_guards import guarded_task_edit, guarded_task_move

urlpatterns = [
    path("tasks/<int:task_id>/feedback/", task_feedback, name="task_feedback"),
    # Preserve the existing named URLs in pool_service.urls. This earlier
    # include adds write guards to those exact paths, not new public endpoints.
    path("tasks/move/", guarded_task_move),
    path("tasks/<int:task_id>/", guarded_task_edit),
]
