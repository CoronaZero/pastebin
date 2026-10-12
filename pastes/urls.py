"""pastes 应用 URL 路由。"""
from django.urls import path

from . import views

app_name = "pastes"

urlpatterns = [
    path("", views.index, name="index"),
    path("created/<uuid:paste_id>/", views.created, name="created"),
    path("p/<uuid:paste_id>/", views.view, name="view"),
    path("robots.txt", views.robots, name="robots"),
]
