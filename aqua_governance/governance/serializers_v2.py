from django.conf import settings
from django.db import transaction
from rest_framework import serializers
from rest_framework.exceptions import ValidationError

from aqua_governance.governance.asset_payload import validate_asset_payload
from aqua_governance.governance.asset_tokens import (
    find_active_asset_proposal_conflict,
    serialize_asset_proposal_conflict,
    upsert_asset_token_from_proposal,
)
from aqua_governance.governance.db_locks import acquire_proposal_transition_lock
from aqua_governance.governance.exceptions import AssetProposalConflictError, ASSET_PROPOSAL_CONFLICT_DETAIL
from aqua_governance.governance.models import AssetToken, Proposal, HistoryProposal, ProposalQueueSlot
from aqua_governance.governance.proposal_queue import validate_weekly_queue_slot
from aqua_governance.governance.proposal_queue_slots import is_queue_slot_available
from aqua_governance.governance.serializer_fields import QuillField
from aqua_governance.governance.serializers import HistoryProposalSerializer, LogVoteSerializer
from aqua_governance.utils.payments import check_transaction_xdr

# ---------------------------------------------------------------------------
# Asset field constants (shared by validation helpers)
# ---------------------------------------------------------------------------
ASSET_REQUIRED_TEXT_FIELDS = (
    "asset_issuer_information",
    "asset_token_description",
    "asset_holder_distribution",
    "asset_liquidity",
    "asset_trading_volume",
    "asset_audit_info",
    "asset_stellar_flags",
    "asset_related_projects",
    "asset_community_references",
    "asset_aquarius_traction",
    "asset_issuer_commitments",
)
ASSET_IDENTIFIER_FIELDS = (
    "asset_code",
    "asset_issuer",
    "asset_contract_address",
)
ASSET_FIELDS = ASSET_IDENTIFIER_FIELDS + ASSET_REQUIRED_TEXT_FIELDS


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def _value_is_blank(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def validate_no_matching_pending_create(attrs):
    """Reject creation if a pending proposal with the same owner/title/type already exists."""
    pending = Proposal.objects.filter(
        proposed_by=attrs.get("proposed_by"),
        title=attrs.get("title"),
        proposal_type=attrs.get("proposal_type", Proposal.PROPOSAL_TYPE_GENERAL),
        draft=True,
        hide=False,
        action=Proposal.TO_CREATE,
        payment_status=Proposal.FINE,
    ).first()
    if pending:
        raise ValidationError(
            {
                "proposal_id": pending.id,
                "non_field_errors": "Please wait a few minutes while the pending proposal payment is checked.",
            }
        )


def validate_asset_payload_fields(attrs):
    """Validate asset-proposal-specific payload fields (reused by multiple serializers)."""
    asset_code = attrs.get("asset_code")
    asset_issuer = attrs.get("asset_issuer")
    contract_address = attrs.get("asset_contract_address")
    has_classic_asset = not _value_is_blank(asset_code) and not _value_is_blank(asset_issuer)
    has_contract_asset = not _value_is_blank(contract_address)
    if not has_classic_asset and not has_contract_asset:
        raise ValidationError(
            {
                "asset_code": "Provide asset_code + asset_issuer, or asset_contract_address.",
                "asset_contract_address": "Provide asset_code + asset_issuer, or asset_contract_address.",
            }
        )

    errors = {}
    for field_name in ASSET_REQUIRED_TEXT_FIELDS:
        if _value_is_blank(attrs.get(field_name)):
            errors[field_name] = "This field is required for asset proposal."
    if errors:
        raise ValidationError(errors)

    try:
        validate_asset_payload(
            asset_code=asset_code,
            asset_issuer=asset_issuer,
            asset_contract_address=contract_address,
            require_onchain_verification=True,
        )
    except ValueError as exc:
        raise ValidationError(_map_asset_validation_error(str(exc))) from exc


def _asset_proposal_conflict_validation_error(conflict) -> ValidationError:
    return ValidationError(
        {
            'proposal_id': conflict.proposal.id,
            'non_field_errors': ASSET_PROPOSAL_CONFLICT_DETAIL,
        }
    )


def _raise_asset_proposal_conflict(conflict) -> None:
    raise AssetProposalConflictError(
        canonical_asset_contract_address=conflict.canonical_asset_contract_address,
        conflict=serialize_asset_proposal_conflict(conflict),
    )


def _map_asset_validation_error(message: str):
    if "Provide both asset_code and asset_issuer together." in message:
        return {
            "asset_code": message,
            "asset_issuer": message,
        }
    if "Provide asset_code + asset_issuer, or asset_contract_address." in message:
        return {
            "asset_code": message,
            "asset_issuer": message,
            "asset_contract_address": message,
        }
    if "asset_issuer" in message:
        return {"asset_issuer": message}
    if "asset_contract_address" in message or "Soroban RPC" in message:
        return {"asset_contract_address": message}
    if "Horizon" in message or "contract_id" in message:
        return {
            "asset_code": message,
            "asset_issuer": message,
        }
    return {"proposal_type": message}


# ---------------------------------------------------------------------------
# List / Detail serializers (flat, keep Proposal.asset_* fields)
# ---------------------------------------------------------------------------
class ProposalListSerializer(serializers.ModelSerializer):
    text = QuillField()
    logvote_set = LogVoteSerializer(many=True)
    payment_verification_status = serializers.SerializerMethodField()

    class Meta:
        model = Proposal
        fields = [
            "id",
            "proposed_by",
            "title",
            "text",
            "start_at",
            "end_at",
            "vote_for_result",
            "vote_against_result",
            "is_simple_proposal",
            "aqua_circulating_supply",
            "proposal_status",
            "payment_status",
            "payment_verification_status",
            "discord_channel_url",
            "discord_channel_name",
            "discord_username",
            "last_updated_at",
            "created_at",
            "logvote_set",
            "percent_for_quorum",
            "ice_circulating_supply",
            "vote_for_issuer",
            "vote_against_issuer",
            "abstain_issuer",
            "vote_abstain_result",
            "proposal_type",
            "onchain_action_type",
            "onchain_action_args",
            "onchain_execution_status",
            "onchain_execution_tx_hash",
            "onchain_execution_started_at",
            "onchain_execution_submitted_at",
            "onchain_execution_poll_count",
            "asset_code",
            "asset_issuer",
            "asset_contract_address",
            "asset_issuer_information",
            "asset_token_description",
            "asset_holder_distribution",
            "asset_liquidity",
            "asset_trading_volume",
            "asset_audit_info",
            "asset_stellar_flags",
            "asset_related_projects",
            "asset_community_references",
            "asset_aquarius_traction",
            "asset_issuer_commitments",
        ]

    def get_payment_verification_status(self, obj):
        return obj.payment_verification_status


class ProposalDetailSerializer(serializers.ModelSerializer):
    text = QuillField()
    history_proposal = HistoryProposalSerializer(read_only=True, many=True)
    payment_verification_status = serializers.SerializerMethodField()

    class Meta:
        model = Proposal
        fields = [
            "id",
            "version",
            "proposed_by",
            "title",
            "text",
            "start_at",
            "end_at",
            "is_simple_proposal",
            "proposal_status",
            "payment_status",
            "payment_verification_status",
            "last_updated_at",
            "vote_for_issuer",
            "vote_against_issuer",
            "vote_for_result",
            "vote_against_result",
            "aqua_circulating_supply",
            "discord_channel_url",
            "discord_channel_name",
            "discord_username",
            "history_proposal",
            "created_at",
            "percent_for_quorum",
            "ice_circulating_supply",
            "abstain_issuer",
            "vote_abstain_result",
            "proposal_type",
            "onchain_action_type",
            "onchain_action_args",
            "onchain_execution_status",
            "onchain_execution_tx_hash",
            "onchain_execution_started_at",
            "onchain_execution_submitted_at",
            "onchain_execution_poll_count",
            "asset_code",
            "asset_issuer",
            "asset_contract_address",
            "asset_issuer_information",
            "asset_token_description",
            "asset_holder_distribution",
            "asset_liquidity",
            "asset_trading_volume",
            "asset_audit_info",
            "asset_stellar_flags",
            "asset_related_projects",
            "asset_community_references",
            "asset_aquarius_traction",
            "asset_issuer_commitments",
        ]

    def get_payment_verification_status(self, obj):
        return obj.payment_verification_status


# ---------------------------------------------------------------------------
# Create serializer — GENERAL only
# ---------------------------------------------------------------------------
class ProposalCreateSerializer(serializers.ModelSerializer):
    text = QuillField()
    discord_username = serializers.CharField(required=True, allow_null=True)
    payment_verification_status = serializers.SerializerMethodField()

    class Meta:
        model = Proposal
        fields = [
            "id",
            "proposed_by",
            "title",
            "text",
            "start_at",
            "end_at",
            "transaction_hash",
            "discord_channel_name",
            "discord_username",
            "envelope_xdr",
            "discord_channel_url",
            "proposal_type",
            "onchain_action_type",
            "onchain_action_args",
            "onchain_execution_status",
            "onchain_execution_tx_hash",
            "onchain_execution_started_at",
            "onchain_execution_submitted_at",
            "onchain_execution_poll_count",
            "proposal_status",
            "payment_status",
            "payment_verification_status",
            "draft",
            "last_updated_at",
            "created_at",
        ]
        read_only_fields = [
            "proposal_status",
            "payment_status",
            "payment_verification_status",
            "draft",
            "start_at",
            "end_at",
            "last_updated_at",
            "created_at",
            "discord_channel_name",
            "discord_channel_url",
            "onchain_action_type",
            "onchain_action_args",
            "onchain_execution_status",
            "onchain_execution_tx_hash",
            "onchain_execution_started_at",
            "onchain_execution_submitted_at",
            "onchain_execution_poll_count",
        ]
        extra_kwargs = {
            "envelope_xdr": {"required": True},
            "transaction_hash": {"required": True},
        }

    def get_payment_verification_status(self, obj):
        return obj.payment_verification_status

    def validate(self, attrs):
        proposal_type = attrs.get("proposal_type", Proposal.PROPOSAL_TYPE_GENERAL)


        if proposal_type != Proposal.PROPOSAL_TYPE_GENERAL:
            raise ValidationError(
                {"proposal_type": "Only GENERAL proposals are allowed via this endpoint."}
            )

        self._validate_general_payload(attrs)
        validate_no_matching_pending_create(attrs)
        return attrs

    def _validate_general_payload(self, attrs):
        """Reject any asset-related field that is present and non-blank."""
        errors = {}
        for field_name in ASSET_FIELDS:
            if not _value_is_blank(self.initial_data.get(field_name)):
                errors[field_name] = "General proposal does not support asset fields."
        if errors:
            raise ValidationError(errors)

    def create(self, validated_data):
        validated_data["draft"] = True
        validated_data["action"] = Proposal.TO_CREATE
        validated_data["proposal_type"] = Proposal.PROPOSAL_TYPE_GENERAL
        validated_data["onchain_execution_status"] = Proposal.ONCHAIN_EXECUTION_NOT_REQUIRED

        status = check_transaction_xdr(validated_data, settings.PROPOSAL_CREATE_OR_UPDATE_COST)
        if status not in (Proposal.FINE, Proposal.HORIZON_ERROR):

            validated_data["hide"] = True
        validated_data["payment_status"] = status

        return super().create(validated_data)


# ---------------------------------------------------------------------------
# Create serializer — ASSET proposals only  (ADD_ASSET / REMOVE_ASSET)
# ---------------------------------------------------------------------------
class AssetProposalCreateSerializer(serializers.ModelSerializer):
    payment_verification_status = serializers.SerializerMethodField()
    text = QuillField()
    discord_username = serializers.CharField(required=True, allow_null=True)

    def get_payment_verification_status(self, obj):
        return obj.payment_verification_status

    class Meta:
        model = Proposal
        fields = [
            "id",
            "proposed_by",
            "title",
            "text",
            "start_at",
            "end_at",
            "transaction_hash",
            "discord_channel_name",
            "discord_username",
            "envelope_xdr",
            "discord_channel_url",
            "proposal_type",
            "asset_code",
            "asset_issuer",
            "asset_contract_address",
            "asset_issuer_information",
            "asset_token_description",
            "asset_holder_distribution",
            "asset_liquidity",
            "asset_trading_volume",
            "asset_audit_info",
            "asset_stellar_flags",
            "asset_related_projects",
            "asset_community_references",
            "asset_aquarius_traction",
            "asset_issuer_commitments",
            "onchain_action_type",
            "onchain_action_args",
            "onchain_execution_status",
            "onchain_execution_tx_hash",
            "onchain_execution_started_at",
            "onchain_execution_submitted_at",
            "onchain_execution_poll_count",
            "proposal_status",
            "payment_status",
            "payment_verification_status",
            "draft",
            "last_updated_at",
            "created_at",
        ]
        read_only_fields = [
            "proposal_status",
            "payment_status",
            "payment_verification_status",
            "draft",
            "start_at",
            "end_at",
            "last_updated_at",
            "created_at",
            "discord_channel_name",
            "discord_channel_url",
            "onchain_action_type",
            "onchain_action_args",
            "onchain_execution_status",
            "onchain_execution_tx_hash",
            "onchain_execution_started_at",
            "onchain_execution_submitted_at",
            "onchain_execution_poll_count",
        ]
        extra_kwargs = {
            "envelope_xdr": {"required": True},
            "transaction_hash": {"required": True},
            "proposal_type": {"required": True},
        }

    def validate_proposal_type(self, value):
        if value not in (Proposal.PROPOSAL_TYPE_ADD_ASSET, Proposal.PROPOSAL_TYPE_REMOVE_ASSET):
            raise ValidationError("Asset proposal type must be ADD_ASSET or REMOVE_ASSET.")
        return value

    def validate(self, attrs):
        if attrs.get("proposal_type") not in Proposal.ASSET_PROPOSAL_TYPES:
            raise ValidationError({"proposal_type": "Asset proposal type must be ADD_ASSET or REMOVE_ASSET."})
        validate_asset_payload_fields(attrs)
        conflict = find_active_asset_proposal_conflict(
            proposal_type=attrs["proposal_type"],
            asset_code=attrs.get("asset_code"),
            asset_issuer=attrs.get("asset_issuer"),
            asset_contract_address=attrs.get("asset_contract_address"),
        )
        if conflict is not None:
            raise _asset_proposal_conflict_validation_error(conflict)
        validate_no_matching_pending_create(attrs)
        return attrs

    def create(self, validated_data):
        validated_data["draft"] = True
        validated_data["action"] = Proposal.TO_CREATE
        validated_data["onchain_execution_status"] = Proposal.ONCHAIN_EXECUTION_PENDING

        status = check_transaction_xdr(validated_data, settings.PROPOSAL_CREATE_OR_UPDATE_COST)
        if status not in (Proposal.FINE, Proposal.HORIZON_ERROR):
            validated_data["hide"] = True
        validated_data["payment_status"] = status

        with transaction.atomic():
            proposal = super().create(validated_data)
            upsert_asset_token_from_proposal(proposal, save=True)

        return proposal


# ---------------------------------------------------------------------------
# Update / Submit serializers
# ---------------------------------------------------------------------------
class ProposalUpdateSerializer(serializers.ModelSerializer):  # think about joining with create serializer
    text = QuillField(required=False)
    new_text = QuillField()
    discord_username = serializers.CharField(required=False, allow_null=True)
    payment_verification_status = serializers.SerializerMethodField()

    class Meta:
        model = Proposal
        fields = [
            "id",
            "version",
            "proposed_by",
            "title",
            "text",
            "start_at",
            "end_at",
            "transaction_hash",
            "discord_channel_url",
            "discord_channel_name",
            "discord_username",
            "envelope_xdr",
            "proposal_status",
            "payment_status",
            "payment_verification_status",
            "last_updated_at",
            "created_at",
            "proposal_type",
            "onchain_action_type",
            "onchain_action_args",
            "onchain_execution_status",
            "onchain_execution_tx_hash",
            "onchain_execution_started_at",
            "onchain_execution_submitted_at",
            "onchain_execution_poll_count",
            "new_envelope_xdr",
            "new_transaction_hash",
            "new_title",
            "new_text",
        ]
        read_only_fields = [
            "id",
            "proposed_by",
            "start_at",
            "end_at",
            "version",
            "title",
            "text",
            "discord_channel_url",
            "discord_channel_name",
            "discord_username",
            "proposal_status",
            "payment_status",
            "payment_verification_status",
            "last_updated_at",
            "created_at",
            "proposal_type",
            "onchain_action_type",
            "onchain_action_args",
            "onchain_execution_status",
            "onchain_execution_tx_hash",
            "onchain_execution_started_at",
            "onchain_execution_submitted_at",
            "onchain_execution_poll_count",
        ]
        extra_kwargs = {
            "new_title": {"required": True},
            "new_text": {"required": True},
            "new_envelope_xdr": {"required": True},
            "new_transaction_hash": {"required": True},
        }

    def get_payment_verification_status(self, obj):
        return obj.payment_verification_status

    def update(self, instance, validated_data):
        validated_data["action"] = Proposal.TO_UPDATE
        data_to_check = {
            "text": validated_data["new_text"],
            "envelope_xdr": validated_data["new_envelope_xdr"],
        }

        status = check_transaction_xdr(data_to_check, settings.PROPOSAL_CREATE_OR_UPDATE_COST)
        validated_data["payment_status"] = status
        return super().update(instance, validated_data)


class SubmitSerializer(serializers.ModelSerializer):
    payment_verification_status = serializers.SerializerMethodField()
    text = QuillField(required=False)

    start_at = serializers.DateTimeField(source='new_start_at')
    end_at = serializers.DateTimeField(source='new_end_at')

    class Meta:
        model = Proposal
        fields = [
            "id",
            "proposed_by",
            "title",
            "text",
            "start_at",
            "end_at",
            "discord_channel_url",
            "discord_channel_name",
            "discord_username",
            "envelope_xdr",
            "proposal_status",
            "payment_status",
            "payment_verification_status",
            "last_updated_at",
            "created_at",
            "proposal_type",
            "asset_code",
            "asset_issuer",
            "asset_contract_address",
            "asset_issuer_information",
            "asset_token_description",
            "asset_holder_distribution",
            "asset_liquidity",
            "asset_trading_volume",
            "asset_audit_info",
            "asset_stellar_flags",
            "asset_related_projects",
            "asset_community_references",
            "asset_aquarius_traction",
            "asset_issuer_commitments",
            "onchain_action_type",
            "onchain_action_args",
            "onchain_execution_status",
            "onchain_execution_tx_hash",
            "onchain_execution_started_at",
            "onchain_execution_submitted_at",
            "onchain_execution_poll_count",
            "new_envelope_xdr",
            "new_transaction_hash",
        ]
        read_only_fields = [
            "id",
            "proposed_by",
            "title",
            "text",
            "discord_channel_url",
            "discord_channel_name",
            "discord_username",
            "proposal_status",
            "payment_status",
            "payment_verification_status",
            "last_updated_at",
            "created_at",
            "proposal_type",
            "asset_code",
            "asset_issuer",
            "asset_contract_address",
            "asset_issuer_information",
            "asset_token_description",
            "asset_holder_distribution",
            "asset_liquidity",
            "asset_trading_volume",
            "asset_audit_info",
            "asset_stellar_flags",
            "asset_related_projects",
            "asset_community_references",
            "asset_aquarius_traction",
            "asset_issuer_commitments",
            "onchain_action_type",
            "onchain_action_args",
            "onchain_execution_status",
            "onchain_execution_tx_hash",
            "onchain_execution_started_at",
            "onchain_execution_submitted_at",
            "onchain_execution_poll_count",
        ]
        extra_kwargs = {
            "start_at": {"required": True},
            "end_at": {"required": True},
            "new_envelope_xdr": {"required": True},
            "new_transaction_hash": {"required": True},
        }

    def get_payment_verification_status(self, obj):
        return obj.payment_verification_status

    def validate(self, attrs):
        new_start_at = attrs["new_start_at"]
        new_end_at = attrs["new_end_at"]
        validate_weekly_queue_slot(new_start_at, new_end_at)

        if self.instance.is_asset_proposal:
            conflict = find_active_asset_proposal_conflict(proposal=self.instance)
            if conflict is not None:
                _raise_asset_proposal_conflict(conflict)

        if not self._is_queue_slot_available(new_start_at, new_end_at, self.instance.id):
            raise self._queue_slot_conflict_error()
        return attrs

    @staticmethod
    def _is_queue_slot_available(new_start_at, new_end_at, current_proposal_id: int) -> bool:
        return is_queue_slot_available(
            start_at=new_start_at,
            end_at=new_end_at,
            exclude_proposal_id=current_proposal_id,
        )

    @staticmethod
    def _queue_slot_conflict_error() -> ValidationError:
        return ValidationError(
            {
                "start_at": "The selected queue slot is already occupied by another proposal.",
                "end_at": "The selected queue slot is already occupied by another proposal.",
            }
        )

    def update(self, instance, validated_data):
        validated_data["action"] = Proposal.TO_SUBMIT
        data_to_check = {"text": instance.text, "envelope_xdr": validated_data["new_envelope_xdr"]}
        status = check_transaction_xdr(data_to_check, settings.PROPOSAL_SUBMIT_COST)
        validated_data["payment_status"] = status

        with transaction.atomic():
            acquire_proposal_transition_lock()
            locked_instance = Proposal.objects.select_for_update().get(id=instance.id)
            if locked_instance.is_asset_proposal:
                conflict = find_active_asset_proposal_conflict(proposal=locked_instance)
                if conflict is not None:
                    _raise_asset_proposal_conflict(conflict)
            if not self._is_queue_slot_available(
                validated_data["new_start_at"],
                validated_data["new_end_at"],
                locked_instance.id,
            ):
                raise self._queue_slot_conflict_error()
            return super().update(locked_instance, validated_data)


# ---------------------------------------------------------------------------
# Asset-token serializers
# ---------------------------------------------------------------------------
class AssetTokenProposalSerializer(serializers.ModelSerializer):
    payment_verification_status = serializers.SerializerMethodField()

    class Meta:
        model = Proposal
        fields = [
            "id",
            "proposal_type",
            "proposal_status",
            "payment_verification_status",
            "title",
            "start_at",
            "end_at",
            "new_start_at",
            "new_end_at",
            "vote_for_result",
            "vote_against_result",
            "vote_abstain_result",
            "onchain_execution_status",
            "onchain_execution_tx_hash",
            "created_at",
            "last_updated_at",
        ]

    def get_payment_verification_status(self, obj):
        return obj.payment_verification_status


# ---------------------------------------------------------------------------
# Proposal Queue serializers
# ---------------------------------------------------------------------------
class ProposalQueueSlotSerializer(serializers.ModelSerializer):
    proposal = serializers.IntegerField(source='proposal_id', read_only=True)
    proposal_title = serializers.CharField(source='proposal.title', read_only=True)
    proposal_status = serializers.CharField(source='proposal.proposal_status', read_only=True)

    class Meta:
        model = ProposalQueueSlot
        fields = [
            "proposal",
            "proposal_title",
            "proposal_status",
            "start_at",
            "end_at",
            "occupied_at",
        ]


class AssetTokenSerializer(serializers.ModelSerializer):
    asset_code = serializers.CharField(source="classic_code", allow_null=True, read_only=True)
    asset_issuer = serializers.CharField(source="classic_issuer", allow_null=True, read_only=True)
    asset_contract_address = serializers.CharField(source="contract_address", read_only=True)
    proposals = AssetTokenProposalSerializer(
        many=True, read_only=True, source="visible_proposals"
    )

    class Meta:
        model = AssetToken
        fields = [
            "asset_code",
            "asset_issuer",
            "asset_contract_address",
            "whitelisted",
            "proposals",
        ]
