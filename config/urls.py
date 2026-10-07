"""Root URL configuration for the fuel_route_optimizer project."""

from django.urls import include, path

from routing import views as routing_views

urlpatterns = [
    path("api/v1/", include("routing.urls")),
    path("map/", routing_views.map_page, name="map"),
]
