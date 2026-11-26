"""URL routes for company-scoped receipt entry and document access."""

from django.urls import path

from . import views


app_name = "receipts"

urlpatterns = [
    path("<int:company_id>/", views.ReceiptListView.as_view(), name="list"),
    path("<int:company_id>/new/", views.ReceiptCreateView.as_view(), name="create"),
    path(
        "<int:company_id>/<int:receipt_id>/blockchain/attempt/",
        views.ReceiptBlockchainActionView.as_view(),
        name="blockchain-attempt-base",
    ),
    path(
        "<int:company_id>/<int:receipt_id>/blockchain/attempt/<int:attempt_id>/<str:action>/",
        views.ReceiptBlockchainActionView.as_view(),
        name="blockchain-attempt-action",
    ),
    path(
        "<int:company_id>/<int:receipt_id>/blockchain/prepare/",
        views.ReceiptBlockchainActionView.as_view(),
        name="blockchain-prepare",
    ),
    path(
        "<int:company_id>/<int:receipt_id>/documents/<int:document_id>/",
        views.ReceiptDocumentFileView.as_view(),
        name="document",
    ),
    path(
        "<int:company_id>/<int:receipt_id>/",
        views.ReceiptDetailView.as_view(),
        name="detail",
    ),
]
