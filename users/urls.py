from django.contrib.auth.views import LoginView, LogoutView, PasswordChangeView
from django.urls import path, reverse_lazy

from . import views

app_name = "users"

urlpatterns = [
    path("register/", views.register, name="register"),
    path(
        "password/",
        PasswordChangeView.as_view(
            template_name="users/password_change.html",
            success_url=reverse_lazy("dashboard"),
        ),
        name="password_change",
    ),
    path(
        "login/",
        LoginView.as_view(template_name="users/login.html"),
        name="login",
    ),
    path(
        "logout/",
        LogoutView.as_view(next_page="users:login"),
        name="logout",
    ),
]
