import base64
import hashlib
import json

from django.conf import settings

from rest_framework import serializers
from rest_framework.exceptions import ValidationError

from django_quill.quill import Quill
from stellar_sdk import HashMemo, Server, TransactionEnvelope

from aqua_governance.governance.models import LogVote, Proposal, HistoryProposal
from aqua_governance.governance.serializer_fields import QuillField
from aqua_governance.utils.payments import (
    check_payment,
    check_xdr_payment,
    check_proposal_status,
    is_dev_payment_bypass_enabled,
)


class LogVoteSerializer(serializers.ModelSerializer):

    class Meta:
        model = LogVote
        fields = [
            'account_issuer', 'vote_choice', 'amount', 'voted_amount', 'claimed',
            'transaction_link', 'created_at', 'asset_code', 'claimable_balance_id',
            ]


class HistoryProposalSerializer(serializers.ModelSerializer):
    text = QuillField(required=False)

    class Meta:
        model = HistoryProposal
        fields = ['version', 'title', 'text', 'created_at']


class ProposalListSerializer(serializers.ModelSerializer):
    text = QuillField()
    payment_verification_status = serializers.SerializerMethodField()

    class Meta:
        model = Proposal
        fields = [
            'id', 'proposed_by', 'title', 'text', 'start_at', 'end_at', 'vote_for_result', 'vote_against_result',
            'is_simple_proposal', 'aqua_circulating_supply', 'vote_abstain_result',
            'proposal_type', 'proposal_status', 'payment_status', 'payment_verification_status', 'draft', 'action',
            'onchain_action_type', 'onchain_action_args', 'onchain_execution_status', 'onchain_execution_tx_hash',
            'onchain_execution_started_at', 'onchain_execution_submitted_at', 'onchain_execution_poll_count',
        ]

    def get_payment_verification_status(self, obj):
        return obj.payment_verification_status


class ProposalDetailSerializer(serializers.ModelSerializer):
    text = QuillField()
    payment_verification_status = serializers.SerializerMethodField()

    class Meta:
        model = Proposal
        fields = [
            'id', 'proposed_by', 'title', 'text', 'start_at', 'end_at', 'is_simple_proposal',
            'vote_for_issuer', 'vote_against_issuer', 'vote_for_result', 'vote_against_result',
            'aqua_circulating_supply', 'discord_channel_url', 'discord_channel_name', 'discord_username',
            'abstain_issuer', 'vote_abstain_result',
            'proposal_type', 'proposal_status', 'payment_status', 'payment_verification_status', 'draft', 'action',
            'onchain_action_type', 'onchain_action_args', 'onchain_execution_status', 'onchain_execution_tx_hash',
            'onchain_execution_started_at', 'onchain_execution_submitted_at', 'onchain_execution_poll_count',
            'asset_code', 'asset_issuer', 'asset_contract_address', 'asset_issuer_information',
            'asset_token_description', 'asset_holder_distribution', 'asset_liquidity', 'asset_trading_volume',
            'asset_audit_info', 'asset_stellar_flags', 'asset_related_projects', 'asset_community_references',
            'asset_aquarius_traction', 'asset_issuer_commitments',
        ]

    def get_payment_verification_status(self, obj):
        return obj.payment_verification_status


class ProposalCreateSerializer(serializers.ModelSerializer):
    text = QuillField()
    discord_username = serializers.CharField(required=False, allow_null=True)

    class Meta:
        model = Proposal
        fields = [
            'proposed_by', 'title', 'text', 'start_at', 'end_at', 'transaction_hash',
            'discord_channel_name', 'discord_username', 'status',
        ]
        read_only_fields = ['status', ]
        extra_kwargs = {
            'transaction_hash': {'required': True},
            # 'discord_channel_name': {'required': True},
        }

    def validate(self, data):
        data = super(ProposalCreateSerializer, self).validate(data)
        if is_dev_payment_bypass_enabled():
            data['hide'] = False
            data['status'] = Proposal.FINE
            return data

        data['hide'] = True

        tx_hash = data.get('transaction_hash', None)
        horizon_server = Server(settings.HORIZON_URL)
        try:
            transaction_info = horizon_server.transactions().transaction(tx_hash).call()
        except Exception:
            data['status'] = Proposal.HORIZON_ERROR
            return data

        if not transaction_info.get('successful', None):
            data['status'] = Proposal.FAILED_TRANSACTION

        if not check_payment(tx_hash):
            data['status'] = Proposal.INVALID_PAYMENT

        memo = transaction_info.get('memo', None)
        if not memo:
            data['status'] = Proposal.BAD_MEMO

        text_hash = hashlib.sha256(data['text'].html.encode('utf-8')).hexdigest()

        if not base64.b64encode(HashMemo(text_hash).memo_hash).decode() == memo:
            data['status'] = Proposal.BAD_MEMO

        return data
