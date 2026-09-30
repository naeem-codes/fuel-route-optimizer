from django.urls import path

from .views import health

app_name = 'routes'

urlpatterns = [
    path('health/', health, name='health'),
]
