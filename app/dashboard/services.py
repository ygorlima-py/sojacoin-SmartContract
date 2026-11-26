"""Build a permission-scoped, read-only snapshot for the company home page."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from django.db.models import Count, Q, QuerySet
from django.urls import reverse
from django.utils.formats import get_format, number_format
from django.utils import timezone
from django.utils.translation import gettext

from companies.models import Company, CompanyMembership, MembershipRole, Unit, UnitAccess
from issuance_batches.models import IssuanceBatch
from participants.models import Participant
from receipts.models import Receipt


Tone = Literal["neutral", "success", "warning", "danger"]


@dataclass(frozen=True)
class HomeMetric:
    """A compact number and destination shown in the home summary."""

    label: str
    value: int
    icon: str
    url: str | None


@dataclass(frozen=True)
class HomeTask:
    """A pending count, optionally linked to an authorized workflow page."""

    label: str
    count: int
    icon: str
    tone: Tone
    url: str | None
    help_text: str = ""


@dataclass(frozen=True)
class HomeQuickLink:
    """A navigation link the current company member can open."""

    label: str
    icon: str
    url: str


@dataclass(frozen=True)
class HomeIssuance:
    """A confirmed batch row with its exact, display-ready SJC amount."""

    chain_batch_id: int | None
    participant_name: str
    unit_name: str
    net_weight_kg: int
    issued_amount_sjc: str | None


@dataclass(frozen=True)
class HomeSnapshot:
    """All company-scoped values required by the home template."""

    metrics: tuple[HomeMetric, ...]
    tasks: tuple[HomeTask, ...]
    recent_receipts: tuple[Receipt, ...]
    recent_issuances: tuple[HomeIssuance, ...]
    quick_links: tuple[HomeQuickLink, ...]
    can_browse_receipts: bool
    can_view_issuance_summary: bool
    can_receive: bool
    can_issue: bool
    can_manage_team: bool


def _company_url(route_name: str, company_id: int, query: str = "") -> str:
    """Build a local company URL, optionally preserving one dashboard filter."""

    path = reverse(route_name, kwargs={"company_id": company_id})
    return f"{path}?{query}" if query else path


def _receipt_status_presentation(receipt: Receipt) -> tuple[str, Tone]:
    """Return a short translated status for the recent-receipt table."""

    presentation: dict[str, tuple[str, Tone]] = {
        Receipt.Status.READY_FOR_BLOCKCHAIN: (gettext("Aguardando blockchain"), "warning"),
        Receipt.Status.BLOCKCHAIN_IN_PROGRESS: (gettext("Em andamento"), "warning"),
        Receipt.Status.BLOCKCHAIN_CONFIRMED: (gettext("Confirmado"), "success"),
        Receipt.Status.NEEDS_REVIEW: (gettext("Precisa de conferência"), "danger"),
    }
    return presentation.get(receipt.status, (gettext("Em andamento"), "neutral"))


def _format_sjc_amount(amount: int, decimals: int) -> str:
    """Format raw SJC units exactly, using the currently selected locale."""

    if amount < 0 or not 0 <= decimals <= 77:
        raise ValueError("Token amount or decimal precision is outside the supported range.")

    whole, remainder = divmod(amount, 10**decimals)
    formatted_whole = number_format(whole, force_grouping=True)
    if remainder == 0 or decimals == 0:
        return formatted_whole

    fraction = str(remainder).zfill(decimals).rstrip("0")
    if not fraction:
        return formatted_whole
    return f"{formatted_whole}{get_format('DECIMAL_SEPARATOR')}{fraction}"


def _accessible_units(
    company: Company,
    membership: CompanyMembership,
) -> tuple[set[int], set[int]]:
    """Return unit IDs the member may browse through receiving or issuance.

    Receipt browsing follows the existing receipt pages: an active receiving
    role plus ``can_receive``, or an active issuance role plus ``can_issue``.
    Company administrators do not gain operational visibility from their role
    alone.
    """

    if not company.active:
        return set(), set()

    active_roles = set(
        membership.roles.filter(active=True).values_list("role", flat=True)
    )
    grants = UnitAccess.objects.filter(
        membership=membership,
        active=True,
        unit__company_id=company.pk,
        unit__active=True,
    )

    receiving_unit_ids: set[int] = set()
    issuance_unit_ids: set[int] = set()
    if MembershipRole.Role.RECEIVING in active_roles:
        receiving_unit_ids = set(
            grants.filter(can_receive=True).values_list("unit_id", flat=True)
        )
    if MembershipRole.Role.ISSUANCE in active_roles:
        issuance_unit_ids = set(
            grants.filter(can_issue=True).values_list("unit_id", flat=True)
        )
    return receiving_unit_ids, issuance_unit_ids


def _metric_cards(
    *,
    company: Company,
    receipt_counts: dict[str, int],
    batch_counts: dict[str, int],
    can_browse_receipts: bool,
    can_issue: bool,
    can_view_company_issuance: bool,
    is_company_administrator: bool,
) -> tuple[HomeMetric, ...]:
    """Select the few summary numbers relevant to the member's available work."""

    company_id = company.pk
    metrics: list[HomeMetric] = []
    if can_browse_receipts:
        metrics.extend(
            (
                HomeMetric(
                    label=gettext("Recebimentos confirmados"),
                    value=receipt_counts["confirmed"],
                    icon="fa-solid fa-circle-check",
                    url=_company_url("receipts:list", company_id),
                ),
                HomeMetric(
                    label=gettext("Recebimentos em andamento"),
                    value=receipt_counts["pending"],
                    icon="fa-solid fa-truck-ramp-box",
                    url=_company_url("receipts:list", company_id),
                ),
            )
        )
    if can_view_company_issuance:
        batch_list_url = (
            _company_url("issuance_batches:list", company_id) if can_issue else None
        )
        awaiting_batch_url = (
            _company_url(
                "issuance_batches:list",
                company_id,
                "status=awaiting_approval",
            )
            if can_issue
            else None
        )
        metrics.extend(
            (
                HomeMetric(
                    label=gettext("Lotes aguardando participante"),
                    value=batch_counts["awaiting_participant"],
                    icon="fa-solid fa-hourglass-half",
                    url=awaiting_batch_url,
                ),
                HomeMetric(
                    label=gettext("Emissões confirmadas"),
                    value=batch_counts["completed"],
                    icon="fa-solid fa-boxes-stacked",
                    url=batch_list_url,
                ),
            )
        )

    general_metrics = (
        HomeMetric(
            label=gettext("Participantes cadastrados"),
            value=Participant.objects.filter(company=company).count(),
            icon="fa-solid fa-users",
            url=_company_url("participants:list", company_id),
        ),
        HomeMetric(
            label=gettext("Unidades ativas"),
            value=Unit.objects.filter(company=company, active=True).count(),
            icon="fa-solid fa-warehouse",
            url=_company_url("companies:unit_list", company_id),
        ),
    )
    if is_company_administrator:
        metrics.extend(general_metrics[: max(0, 4 - len(metrics))])
    elif not metrics:
        metrics.extend(general_metrics)
    return tuple(metrics)


def _pending_tasks(
    *,
    company: Company,
    receipt_counts: dict[str, int],
    batch_counts: dict[str, int],
    can_browse_receipts: bool,
    can_issue: bool,
    is_company_administrator: bool,
) -> tuple[HomeTask, ...]:
    """Summarize pending work in the member's scope or company-wide for admins.

    Administrators receive read-only company-wide counts. They get no workflow
    link here, because those pages remain protected by operational permissions.
    """

    tasks: list[HomeTask] = []
    company_id = company.pk

    if can_browse_receipts or is_company_administrator:
        receipt_url = (
            _company_url("receipts:list", company_id)
            if can_browse_receipts and not is_company_administrator
            else None
        )
        if receipt_counts["needs_review"]:
            tasks.append(
                HomeTask(
                    label=gettext("Recebimentos para conferência"),
                    count=receipt_counts["needs_review"],
                    icon="fa-solid fa-triangle-exclamation",
                    tone="danger",
                    url=receipt_url,
                )
            )
        if receipt_counts["pending"]:
            tasks.append(
                HomeTask(
                    label=gettext("Recebimentos aguardando blockchain"),
                    count=receipt_counts["pending"],
                    icon="fa-solid fa-truck-ramp-box",
                    tone="warning",
                    url=receipt_url,
                )
            )

    if can_issue or is_company_administrator:
        issuance_url = (
            _company_url("issuance_batches:list", company_id)
            if can_issue and not is_company_administrator
            else None
        )
        if batch_counts["needs_review"]:
            tasks.append(
                HomeTask(
                    label=gettext("Lotes para conferência"),
                    count=batch_counts["needs_review"],
                    icon="fa-solid fa-triangle-exclamation",
                    tone="danger",
                    url=(
                        _company_url(
                            "issuance_batches:list",
                            company_id,
                            "status=needs_review",
                        )
                        if issuance_url
                        else None
                    ),
                )
            )
        if batch_counts["expired"]:
            tasks.append(
                HomeTask(
                    label=gettext("Lotes com prazo expirado"),
                    count=batch_counts["expired"],
                    icon="fa-solid fa-clock",
                    tone="warning",
                    url=(
                        _company_url(
                            "issuance_batches:list", company_id, "status=expired"
                        )
                        if issuance_url
                        else None
                    ),
                    help_text=gettext(
                        "A carga continua bloqueada até o cancelamento ser confirmado."
                    ),
                )
            )
        if batch_counts["transactions_pending"]:
            tasks.append(
                HomeTask(
                    label=gettext("Lotes aguardando envio ou confirmação"),
                    count=batch_counts["transactions_pending"],
                    icon="fa-solid fa-arrows-rotate",
                    tone="warning",
                    url=issuance_url,
                )
            )
        if batch_counts["awaiting_participant"]:
            tasks.append(
                HomeTask(
                    label=gettext("Lotes aguardando participante"),
                    count=batch_counts["awaiting_participant"],
                    icon="fa-solid fa-hourglass-half",
                    tone="warning",
                    url=(
                        _company_url(
                            "issuance_batches:list",
                            company_id,
                            "status=awaiting_approval",
                        )
                        if issuance_url
                        else None
                    ),
                )
            )
        if batch_counts["legacy_approved"]:
            tasks.append(
                HomeTask(
                    label=gettext("Lotes aprovados aguardando conclusão"),
                    count=batch_counts["legacy_approved"],
                    icon="fa-solid fa-boxes-stacked",
                    tone="warning",
                    url=(
                        _company_url(
                            "issuance_batches:list", company_id, "status=approved"
                        )
                        if issuance_url
                        else None
                    ),
                )
            )
    return tuple(tasks)


def build_home_snapshot(
    company: Company,
    membership: CompanyMembership,
) -> HomeSnapshot:
    """Gather scoped totals, pending work, quick links and recent receipts.

    All queries are read-only and use the company from the authorized URL.
    Receipt records and operational issuance links are limited to active units
    covered by the member's existing role and unit grants. Company administrators
    also receive read-only company-wide pending counts and recent confirmed batches.
    """

    receiving_unit_ids, issuance_unit_ids = _accessible_units(company, membership)
    readable_unit_ids = receiving_unit_ids | issuance_unit_ids
    can_browse_receipts = bool(readable_unit_ids)
    can_issue = bool(issuance_unit_ids)
    can_receive = bool(receiving_unit_ids)
    active_roles = set(
        membership.roles.filter(active=True).values_list("role", flat=True)
    )
    is_company_administrator = (
        MembershipRole.Role.ADMINISTRATOR in active_roles
    )
    can_view_company_issuance = can_issue or is_company_administrator

    receipt_counts = {"confirmed": 0, "pending": 0, "needs_review": 0}
    recent_receipts: tuple[Receipt, ...] = ()
    if can_browse_receipts or is_company_administrator:
        receipt_queryset: QuerySet[Receipt] = Receipt.objects.filter(company=company)
        if not is_company_administrator:
            receipt_queryset = receipt_queryset.filter(unit_id__in=readable_unit_ids)
        receipt_summary = receipt_queryset.aggregate(
            confirmed=Count(
                "pk", filter=Q(status=Receipt.Status.BLOCKCHAIN_CONFIRMED)
            ),
            pending=Count(
                "pk",
                filter=Q(
                    status__in=(
                        Receipt.Status.READY_FOR_BLOCKCHAIN,
                        Receipt.Status.BLOCKCHAIN_IN_PROGRESS,
                    )
                ),
            ),
            needs_review=Count("pk", filter=Q(status=Receipt.Status.NEEDS_REVIEW)),
        )
        receipt_counts = {key: int(value or 0) for key, value in receipt_summary.items()}
        if can_browse_receipts:
            recent_queryset = Receipt.objects.filter(
                company=company,
                unit_id__in=readable_unit_ids,
            )
            recent_rows = list(
                recent_queryset.select_related(
                    "participant", "unit", "weighing_ticket"
                ).order_by("-created_at", "-pk")[:5]
            )
            for receipt in recent_rows:
                receipt.dashboard_status_label, receipt.dashboard_status_tone = (
                    _receipt_status_presentation(receipt)
                )
            recent_receipts = tuple(recent_rows)

    batch_counts = {
        "awaiting_participant": 0,
        "completed": 0,
        "expired": 0,
        "needs_review": 0,
        "transactions_pending": 0,
        "legacy_approved": 0,
    }
    recent_issuances: tuple[HomeIssuance, ...] = ()
    if can_view_company_issuance:
        current_timestamp = int(timezone.now().timestamp())
        batch_queryset = IssuanceBatch.objects.filter(company=company)
        if not is_company_administrator:
            batch_queryset = batch_queryset.filter(
                unit_id__in=issuance_unit_ids,
                unit__active=True,
            )
        batch_summary = batch_queryset.aggregate(
            awaiting_participant=Count(
                "pk",
                filter=Q(
                    status=IssuanceBatch.Status.AWAITING_APPROVAL,
                    approval_deadline__gt=current_timestamp,
                ),
            ),
            expired=Count(
                "pk",
                filter=Q(
                    status=IssuanceBatch.Status.AWAITING_APPROVAL,
                    approval_deadline__lte=current_timestamp,
                ),
            ),
            completed=Count("pk", filter=Q(status=IssuanceBatch.Status.COMPLETED)),
            needs_review=Count("pk", filter=Q(status=IssuanceBatch.Status.NEEDS_REVIEW)),
            transactions_pending=Count(
                "pk",
                filter=Q(
                    status__in=(
                        IssuanceBatch.Status.CREATION_PREPARED,
                        IssuanceBatch.Status.CREATION_SUBMITTED,
                        IssuanceBatch.Status.APPROVAL_SUBMITTED,
                        IssuanceBatch.Status.CANCEL_SUBMITTED,
                    )
                ),
            ),
            legacy_approved=Count("pk", filter=Q(status=IssuanceBatch.Status.APPROVED)),
        )
        batch_counts = {key: int(value or 0) for key, value in batch_summary.items()}
        completed_batches = batch_queryset.filter(
            status=IssuanceBatch.Status.COMPLETED
        ).select_related("participant", "unit").order_by("-updated_at", "-pk")[:5]
        recent_issuances = tuple(
            HomeIssuance(
                chain_batch_id=batch.chain_batch_id,
                participant_name=batch.participant.name,
                unit_name=batch.unit.name,
                net_weight_kg=batch.total_net_weight_kg,
                issued_amount_sjc=(
                    _format_sjc_amount(
                        batch.issued_amount_sjc,
                        batch.token_decimals,
                    )
                    if batch.issued_amount_sjc is not None
                    and batch.token_decimals is not None
                    else None
                ),
            )
            for batch in completed_batches
        )

    metrics = _metric_cards(
        company=company,
        receipt_counts=receipt_counts,
        batch_counts=batch_counts,
        can_browse_receipts=can_browse_receipts,
        can_issue=can_issue,
        can_view_company_issuance=can_view_company_issuance,
        is_company_administrator=is_company_administrator,
    )
    tasks = _pending_tasks(
        company=company,
        receipt_counts=receipt_counts,
        batch_counts=batch_counts,
        can_browse_receipts=can_browse_receipts,
        can_issue=can_issue,
        is_company_administrator=is_company_administrator,
    )

    quick_links = [
        HomeQuickLink(
            label=gettext("Participantes"),
            icon="fa-solid fa-users",
            url=_company_url("participants:list", company.pk),
        ),
        HomeQuickLink(
            label=gettext("Unidades"),
            icon="fa-solid fa-warehouse",
            url=_company_url("companies:unit_list", company.pk),
        ),
    ]
    if can_receive:
        quick_links.insert(
            0,
            HomeQuickLink(
                label=gettext("Novo recebimento"),
                icon="fa-solid fa-truck-ramp-box",
                url=_company_url("receipts:create", company.pk),
            ),
        )
    if can_issue:
        quick_links.insert(
            1 if can_receive else 0,
            HomeQuickLink(
                label=gettext("Emissões"),
                icon="fa-solid fa-boxes-stacked",
                url=_company_url("issuance_batches:list", company.pk),
            ),
        )

    return HomeSnapshot(
        metrics=metrics,
        tasks=tasks,
        recent_receipts=recent_receipts,
        recent_issuances=recent_issuances,
        quick_links=tuple(quick_links),
        can_browse_receipts=can_browse_receipts,
        can_view_issuance_summary=can_view_company_issuance,
        can_receive=can_receive,
        can_issue=can_issue,
        can_manage_team=(
            company.active
            and MembershipRole.Role.ADMINISTRATOR in active_roles
        ),
    )
