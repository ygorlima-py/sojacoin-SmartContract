"""Company-scoped routes for the operational dashboard."""

from django.urls import path

from .views import CompanyHomeView


app_name = "dashboard"

urlpatterns = [
    path("<int:company_id>/", CompanyHomeView.as_view(), name="home"),
]
