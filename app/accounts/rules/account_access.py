"""Account activation rules used by authentication and private views."""

from typing import Any

from django.contrib.auth.models import AbstractBaseUser
from django.contrib.auth import logout
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect


def is_active_account(user: AbstractBaseUser | None) -> bool:
    """Return whether both the Django user and its profile are active."""

    if not user or not user.is_authenticated or not user.is_active:
        return False

    profile = getattr(user, "profile", None)
    return profile is not None and profile.active


class ActiveAccountRequiredMixin(LoginRequiredMixin):
    """Allow a class-based view only for an active user and profile."""

    def dispatch(
        self,
        request: HttpRequest,
        *args: Any,
        **kwargs: Any,
    ) -> HttpResponse:
        """End an invalid session before the protected view is executed."""

        if request.user.is_authenticated:
            if not is_active_account(request.user):
                logout(request)
                return redirect("accounts:login")
        elif request.session.get("_auth_user_id"):
            # Django may expose a deactivated session as AnonymousUser.
            logout(request)

        return super().dispatch(request, *args, **kwargs)
