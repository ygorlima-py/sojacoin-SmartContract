"""Company and bearer-link routes for issuance batch lifecycle actions."""

from django.urls import path

from . import views


app_name = "issuance_batches"

urlpatterns = [
    path("<int:company_id>/<int:receipt_id>/prepare/", views.PrepareBatchView.as_view(), name="prepare"),
    path(
        "<int:company_id>/<int:batch_id>/attempt/<int:attempt_id>/<str:action>/",
        views.BatchAttemptActionView.as_view(),
        name="attempt-action",
    ),
    path("<int:company_id>/<int:batch_id>/approval-link/", views.ApprovalLinkView.as_view(), name="approval-link"),
    path(
        "<int:company_id>/<int:batch_id>/approval-link/<int:link_id>/cancel/",
        views.CancelApprovalLinkView.as_view(),
        name="approval-link-cancel",
    ),
    path("<int:company_id>/<int:batch_id>/cancel/prepare/", views.PrepareBatchCancellationView.as_view(), name="cancel-prepare"),
    path("approval/<str:token>/", views.ParticipantApprovalView.as_view(), name="participant-approval"),
    path("approval/<str:token>/prepare/", views.PrepareParticipantApprovalView.as_view(), name="participant-prepare"),
    path("approval/<str:token>/cancel/prepare/", views.PrepareParticipantCancellationView.as_view(), name="participant-cancel-prepare"),
    path("approval/<str:token>/attempt/<int:attempt_id>/<str:action>/", views.ParticipantAttemptActionView.as_view(), name="participant-attempt-action"),
    path(
        "approval/<str:token>/documents/<int:receipt_id>/<int:document_id>/",
        views.ParticipantReceiptDocumentView.as_view(),
        name="participant-document",
    ),
]
