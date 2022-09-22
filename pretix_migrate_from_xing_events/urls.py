from django.conf.urls import url

from . import views

urlpatterns = [
    url(
        r"^control/organizer/(?P<organizer>[^/]+)/migrate_from_xing/$",
        views.IndexView.as_view(),
        name="index",
    ),
    url(
        r"^control/organizer/(?P<organizer>[^/]+)/migrate_from_xing/selection/$",
        views.SelectionView.as_view(),
        name="selection",
    ),
    url(
        r"^control/organizer/(?P<organizer>[^/]+)/migrate_from_xing/status/(?P<taskid>[^/]+)/$",
        views.StatusView.as_view(),
        name="status",
    ),
]
