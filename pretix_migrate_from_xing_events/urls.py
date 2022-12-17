from django.urls import path

from . import views

urlpatterns = [
    path(
        "control/organizer/<str:organizer>/migrate_from_xing/",
        views.IndexView.as_view(),
        name="index",
    ),
    path(
        "control/organizer/<str:organizer>/migrate_from_xing/selection/",
        views.SelectionView.as_view(),
        name="selection",
    ),
    path(
        "control/organizer/<str:organizer>/migrate_from_xing/status/<str:taskid>/",
        views.StatusView.as_view(),
        name="status",
    ),
]
