from django.urls import path

from . import views

app_name = "companies"

urlpatterns = [
    path("", views.CompanyListView.as_view(), name="list"),
    path(
        "<int:company_id>/edit/",
        views.CompanyUpdateView.as_view(),
        name="edit",
    ),
    path(
        "<int:company_id>/members/",
        views.MemberListView.as_view(),
        name="member_list",
    ),
    path(
        "<int:company_id>/members/new/",
        views.MemberCreateView.as_view(),
        name="member_new",
    ),
    path(
        "<int:company_id>/members/<int:membership_id>/roles/new/",
        views.MemberRoleCreateView.as_view(),
        name="member_role_new",
    ),
    path(
        "<int:company_id>/members/<int:membership_id>/roles/<int:role_id>/revoke/",
        views.MemberRoleRevokeView.as_view(),
        name="member_role_revoke",
    ),
    path(
        "<int:company_id>/members/<int:membership_id>/units/<int:unit_id>/access/",
        views.MemberUnitAccessUpdateView.as_view(),
        name="member_unit_access",
    ),
    path(
        "<int:company_id>/members/<int:membership_id>/",
        views.MemberDetailView.as_view(),
        name="member_detail",
    ),
    path(
        "<int:company_id>/history/",
        views.AccessHistoryListView.as_view(),
        name="history",
    ),
    path(
        "<int:company_id>/units/",
        views.UnitListView.as_view(),
        name="unit_list",
    ),
    path(
        "<int:company_id>/units/new/",
        views.UnitCreateView.as_view(),
        name="unit_new",
    ),
    path(
        "<int:company_id>/units/<int:unit_id>/edit/",
        views.UnitUpdateView.as_view(),
        name="unit_edit",
    ),
    path(
        "<int:company_id>/units/<int:unit_id>/",
        views.UnitDetailView.as_view(),
        name="unit_detail",
    ),
    path("<int:company_id>/", views.CompanyDetailView.as_view(), name="detail"),
]
