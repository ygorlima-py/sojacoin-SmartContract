from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("login/", views.AccountLoginView.as_view(), name="login"),
    path("logout/", views.AccountLogoutView.as_view(), name="logout"),
    path("profile/", views.ProfileView.as_view(), name="profile"),
    path("password/change/", views.AccountPasswordChangeView.as_view(), name="password_change"),
    path("password/change/done/", views.AccountPasswordChangeDoneView.as_view(), name="password_change_done"),
    path("password-reset/", views.AccountPasswordResetView.as_view(), name="password_reset"),
    path("password-reset/done/", views.AccountPasswordResetDoneView.as_view(), name="password_reset_done"),
    path("reset/<uidb64>/<token>/", views.AccountPasswordResetConfirmView.as_view(), name="password_reset_confirm"),
    path("reset/complete/", views.AccountPasswordResetCompleteView.as_view(), name="password_reset_complete"),
]
