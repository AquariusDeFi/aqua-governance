from datetime import datetime, timezone as dt_timezone

from django.conf import settings
from django.db.models import Exists, F, OuterRef, Prefetch, Q
from django.http import Http404
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied
from rest_framework.generics import get_object_or_404
from rest_framework.response import Response
from rest_framework.decorators import action
from rest_framework import status

from rest_framework.filters import OrderingFilter
from rest_framework.mixins import CreateModelMixin, ListModelMixin, RetrieveModelMixin, UpdateModelMixin
from rest_framework.permissions import AllowAny
from rest_framework.viewsets import GenericViewSet
from stellar_sdk import TransactionEnvelope

from aqua_governance.governance import proposal_transactions
from aqua_governance.governance.exceptions import build_asset_proposal_conflict_detail
from aqua_governance.governance.filters import (
    ProposalStatusFilterBackend,
    ProposalOwnerFilterBackend,
    ProposalTypeFilterBackend,
    LogVoteOwnerFilterBackend,
    LogVoteProposalIdFilterBackend,
    ProposalVoteOwnerFilterBackend,
    build_logvote_prefetch,
    is_active_vote_query,
)
from aqua_governance.governance.models import AssetToken, LogVote, Proposal, HistoryProposal, ProposalQueueSlot
from aqua_governance.governance.proposal_queue import get_max_booking_datetime
from aqua_governance.governance.proposal_queue_slots import true_queue_slot_occupancy_q
from aqua_governance.governance.pagination import CustomPageNumberPagination
from aqua_governance.governance.serializers import (
    LogVoteSerializer,
    ProposalCreateSerializer,
    ProposalDetailSerializer,
    ProposalListSerializer,
)
from aqua_governance.governance import serializers_v2
from aqua_governance.governance.serializers_v2 import AssetTokenSerializer


class AssetTokenView(ListModelMixin, GenericViewSet):
    permission_classes = (AllowAny,)
    pagination_class = CustomPageNumberPagination
    serializer_class = AssetTokenSerializer

    def get_queryset(self):
        linked_proposals = Proposal.objects.filter(
            asset_token_id=OuterRef('pk'),
            draft=False,
        )
        return (
            AssetToken.objects.filter(Exists(linked_proposals))
            .prefetch_related(
                Prefetch(
                    "proposals",
                    queryset=Proposal.objects.filter(hide=False, draft=False)
                    .order_by("-end_at", "-created_at"),
                    to_attr="visible_proposals",
                )
            )
            .order_by(F("last_execution_at").desc(nulls_last=True), "-created_at")
        )


class ProposalsView(ListModelMixin, RetrieveModelMixin, CreateModelMixin, GenericViewSet):
    queryset = Proposal.objects.filter(
        hide=False,
        draft=False,
        created_at__lte=datetime(2022, 4, 15, tzinfo=dt_timezone.utc),
    )
    permission_classes = (AllowAny,)
    serializer_class = ProposalListSerializer
    pagination_class = CustomPageNumberPagination
    filter_backends = (
        OrderingFilter,
        ProposalTypeFilterBackend,
    )
    ordering = ["created_at"]

    def get_serializer_class(self):
        if self.action == "retrieve":
            return ProposalDetailSerializer
        elif self.action == "create":
            return ProposalCreateSerializer
        return super().get_serializer_class()

    def get_queryset(self):
        queryset = super().get_queryset()
        active_only = is_active_vote_query(self.request)
        if self.action == "list" and active_only:
            queryset = queryset.filter(logvote__hide=False, logvote__claimed=False).distinct()
        return queryset.prefetch_related(build_logvote_prefetch(self.request))


class LogVoteView(ListModelMixin, GenericViewSet):
    queryset = LogVote.objects.filter(hide=False)
    permission_classes = (AllowAny,)
    serializer_class = LogVoteSerializer
    pagination_class = CustomPageNumberPagination
    filter_backends = (
        OrderingFilter,
        LogVoteOwnerFilterBackend,
        LogVoteProposalIdFilterBackend,
    )
    ordering = ["-created_at"]
    ordering_fields = ["created_at", "amount", "vote_choice", "account_issuer"]


class ProposalViewSet(
    ListModelMixin,
    UpdateModelMixin,
    CreateModelMixin,
    RetrieveModelMixin,
    GenericViewSet,
):
    queryset = Proposal.objects.filter(hide=False).exclude(id=65)
    permission_classes = (AllowAny,)
    serializer_class = serializers_v2.ProposalDetailSerializer
    pagination_class = CustomPageNumberPagination
    filter_backends = (
        OrderingFilter,
        ProposalStatusFilterBackend,
        ProposalOwnerFilterBackend,
        ProposalTypeFilterBackend,
        ProposalVoteOwnerFilterBackend,
    )
    ordering = ["created_at"]

    def get_queryset(self):
        queryset = super(ProposalViewSet, self).get_queryset().prefetch_related(
            Prefetch("history_proposal", HistoryProposal.objects.filter(hide=False))
        )
        if self.action != "retrieve" and self.action != "list":
            queryset = queryset.exclude(proposal_status=Proposal.EXPIRED)

        if self.action == "submit_proposal":
            queryset = queryset.filter(proposal_status=Proposal.DISCUSSION).filter(
                Q(action=Proposal.TO_SUBMIT)
                | Q(last_updated_at__lte=timezone.now() - settings.DISCUSSION_TIME),
            )

        if self.action == "update" or self.action == "partial_update":
            queryset = queryset.filter(proposal_status=Proposal.DISCUSSION)
        if self.action == "check_proposal_payment":
            return queryset.exclude(action=Proposal.NONE)
        queryset = queryset.filter(draft=False)
        has_vote_owner_filter = bool(self.request.query_params.get('vote_owner_public_key'))
        if self.action == "list" and is_active_vote_query(self.request) and not has_vote_owner_filter:
            queryset = queryset.filter(logvote__hide=False, logvote__claimed=False).distinct()

        if has_vote_owner_filter:
            return queryset

        return queryset

    def get_serializer_class(self):
        if self.action == "list":
            return serializers_v2.ProposalListSerializer
        if self.action == "update" or self.action == "partial_update":
            return serializers_v2.ProposalUpdateSerializer
        if self.action == "create":
            return serializers_v2.ProposalCreateSerializer
        if self.action == "retrieve":
            return serializers_v2.ProposalDetailSerializer

        return super().get_serializer_class()

    def _check_owner_permissions(self, proposal, data):
        envelope_xdr = data.get("new_envelope_xdr", None)
        try:
            transaction_envelope = TransactionEnvelope.from_xdr(envelope_xdr, settings.NETWORK_PASSPHRASE)
        except Exception:
            raise PermissionDenied(detail="Horizon connection error ")

        if transaction_envelope.transaction.source.account_id != proposal.proposed_by:
            raise PermissionDenied(detail="You are not the proposal owner")

    def perform_update(self, serializer):
        instance = self.get_object()
        self._check_owner_permissions(instance, serializer.validated_data)
        serializer.save()

    def partial_update(self, request, *args, **kwargs):
        # disable partial update
        return self.update(request, *args, **kwargs)

    @action(detail=True, methods=["post"], url_path="submit", url_name="submit-proposal")
    def submit_proposal(self, request, pk=None):
        proposal = self.get_object()
        serializer = serializers_v2.SubmitSerializer(proposal, data=request.data)
        serializer.is_valid(raise_exception=True)
        self._check_owner_permissions(proposal, request.data)
        serializer.save()
        return Response(data=serializer.data)

    @action(detail=True, methods=["post"], url_path="check_payment", url_name="check-payment")
    def check_proposal_payment(self, request, pk=None):
        proposal = self.get_object()
        result = proposal_transactions.check_transaction(proposal)
        if result and result.get('outcome') == 'asset_proposal_conflict':
            return Response(
                data=build_asset_proposal_conflict_detail(
                    canonical_asset_contract_address=result.get('asset_contract_address'),
                    conflict=result.get('conflict'),
                ),
                status=status.HTTP_409_CONFLICT,
            )
        if result and result.get('outcome') == 'slot_conflict':
            return Response(
                data={
                    'detail': 'The selected queue slot is already occupied by another proposal.',
                    'code': 'proposal_queue_slot_conflict',
                    'selected_slot': {
                        'start_at': proposal.new_start_at,
                        'end_at': proposal.new_end_at,
                    },
                    'conflict': result.get('conflict'),
                },
                status=status.HTTP_409_CONFLICT,
            )
        return Response(data=self.get_serializer(instance=proposal).data)


class AssetProposalViewSet(CreateModelMixin, GenericViewSet):
    permission_classes = (AllowAny,)
    serializer_class = serializers_v2.AssetProposalCreateSerializer
    queryset = Proposal.objects.filter(hide=False).exclude(id=65)


class TestProposalViewSet(ProposalViewSet):
    queryset = Proposal.objects.filter(hide=False)


class ProposalQueueViewSet(ListModelMixin, GenericViewSet):
    permission_classes = (AllowAny,)
    pagination_class = CustomPageNumberPagination
    serializer_class = serializers_v2.ProposalQueueSlotSerializer
    ordering = ["start_at"]

    def get_queryset(self):
        now = timezone.now()
        return (
            ProposalQueueSlot.objects
            .filter(
                true_queue_slot_occupancy_q(),
                end_at__gte=now,
            )
            .select_related("proposal")
            .order_by("start_at", "proposal_id")
        )

    def list(self, request, *args, **kwargs):
        response = super().list(request, *args, **kwargs)
        response.data["max_booking_datetime"] = get_max_booking_datetime()
        response.data["booking_horizon_weeks"] = settings.PROPOSAL_QUEUE_BOOKING_HORIZON_WEEKS
        return response
