# core/urls.py
from django.urls import path
from .views import health_check, optimize_energy

urlpatterns = [
    path('health/', health_check, name='health_check'),
    path('optimize-energy', optimize_energy, name='optimize-energy'),
]