"""URL routes for the routing application (Sections 1 and 7)."""

from django.urls import path

from . import views

urlpatterns = [
    path("health/", views.health, name="health"),
    path("routes/", views.routes, name="routes"),
]
