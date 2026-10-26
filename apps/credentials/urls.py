from django.urls import path

from . import views

app_name = "platform_keys"

urlpatterns = [
    path("", views.platform_keys, name="index"),
]
