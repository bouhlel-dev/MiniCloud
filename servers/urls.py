from django.urls import path

from . import views


app_name = "servers"

urlpatterns = [
    path("", views.ServerListView.as_view(), name="list"),
    path("servers/create/", views.ServerCreateView.as_view(), name="create"),
    path("servers/<int:pk>/", views.ServerDetailView.as_view(), name="detail"),
    path("servers/<int:pk>/metrics/", views.ServerMetricsView.as_view(), name="metrics"),
    path("servers/<int:pk>/edit/", views.ServerUpdateView.as_view(), name="edit"),
    path("servers/<int:pk>/start/", views.ServerStartView.as_view(), name="start"),
    path("servers/<int:pk>/stop/", views.ServerStopView.as_view(), name="stop"),
    path("servers/<int:pk>/restart/", views.ServerRestartView.as_view(), name="restart"),
    path("servers/<int:pk>/delete/", views.ServerDeleteView.as_view(), name="delete"),
    path("server-templates/", views.ServerTemplateListView.as_view(), name="template-list"),
    path("server-templates/create/", views.ServerTemplateCreateView.as_view(), name="template-create"),
    path("server-templates/<int:pk>/edit/", views.ServerTemplateUpdateView.as_view(), name="template-edit"),
    path("server-templates/<int:pk>/delete/", views.ServerTemplateDeleteView.as_view(), name="template-delete"),
]
