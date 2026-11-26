"""URL routes for company participant management and public term acceptance."""

from django.urls import path

from . import views


app_name = "participants"

urlpatterns = [
    path(
        "<int:company_id>/terms/",
        views.ParticipantTermsDocumentsView.as_view(),
        name="terms_manage",
    ),
    path(
        "<int:company_id>/terms/<int:document_id>/pdf/",
        views.ParticipantTermsDocumentFileView.as_view(),
        name="terms_pdf_company",
    ),
    path("<int:company_id>/", views.ParticipantListView.as_view(), name="list"),
    path("<int:company_id>/new/", views.ParticipantCreateView.as_view(), name="create"),
    path(
        "<int:company_id>/<int:participant_id>/",
        views.ParticipantDetailView.as_view(),
        name="detail",
    ),
    path(
        "<int:company_id>/<int:participant_id>/proposal/prepare/",
        views.ParticipantProposalPrepareView.as_view(),
        name="proposal_prepare",
    ),
    path(
        "<int:company_id>/<int:participant_id>/attempts/<int:attempt_id>/started/",
        views.ParticipantAttemptStartedView.as_view(),
        name="attempt_started",
    ),
    path(
        "<int:company_id>/<int:participant_id>/attempts/<int:attempt_id>/rejected/",
        views.ParticipantAttemptRejectedView.as_view(),
        name="attempt_rejected",
    ),
    path(
        "<int:company_id>/<int:participant_id>/attempts/<int:attempt_id>/hash/",
        views.ParticipantAttemptHashView.as_view(),
        name="attempt_hash",
    ),
    path(
        "<int:company_id>/<int:participant_id>/attempts/<int:attempt_id>/confirm/",
        views.ParticipantAttemptConfirmView.as_view(),
        name="attempt_confirm",
    ),
    path(
        "<int:company_id>/<int:participant_id>/acceptance-link/",
        views.ParticipantAcceptanceLinkCreateView.as_view(),
        name="acceptance_link_create",
    ),
    path(
        "<int:company_id>/<int:participant_id>/acceptance-links/<int:link_id>/cancel/",
        views.ParticipantAcceptanceLinkCancelView.as_view(),
        name="acceptance_link_cancel",
    ),
    path("accept/<str:token>/", views.ParticipantAcceptanceView.as_view(), name="accept"),
    path(
        "accept/<str:token>/terms.pdf",
        views.ParticipantAcceptanceTermsPdfView.as_view(),
        name="acceptance_terms_pdf",
    ),
    path(
        "accept/<str:token>/challenge/",
        views.ParticipantAcceptanceChallengeView.as_view(),
        name="acceptance_challenge",
    ),
    path(
        "accept/<str:token>/prepare/",
        views.ParticipantAcceptancePrepareView.as_view(),
        name="acceptance_prepare",
    ),
    path(
        "accept/<str:token>/attempts/<int:attempt_id>/started/",
        views.ParticipantAcceptanceStartedView.as_view(),
        name="acceptance_started",
    ),
    path(
        "accept/<str:token>/attempts/<int:attempt_id>/rejected/",
        views.ParticipantAcceptanceRejectedView.as_view(),
        name="acceptance_rejected",
    ),
    path(
        "accept/<str:token>/attempts/<int:attempt_id>/hash/",
        views.ParticipantAcceptanceHashView.as_view(),
        name="acceptance_hash",
    ),
    path(
        "accept/<str:token>/attempts/<int:attempt_id>/confirm/",
        views.ParticipantAcceptanceConfirmView.as_view(),
        name="acceptance_confirm",
    ),
]
