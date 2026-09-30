from django.urls import path

from .views import health, plan_route_view

app_name = 'routes'

urlpatterns = [
    path('health/', health, name='health'),
    path('routes/', plan_route_view, name='route-plan'),
]
