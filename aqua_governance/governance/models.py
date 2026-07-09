import requests
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from aqua_governance.governance.onchain_actions import derive_proposal_onchain_action_args
from aqua_governance.governance import payment_statuses
from aqua_governance.governance import proposal_constants
from django_quill.fields import QuillField
from stellar_sdk import Keypair


class AssetToken(models.Model):
    CONTRACT_SYNC_PENDING = 'PENDING'
    CONTRACT_SYNC_SYNCED = 'SYNCED'
    CONTRACT_SYNC_FAILED = 'FAILED'
    CONTRACT_SYNC_REQUIRES_REVIEW = 'REQUIRES_REVIEW'
    CONTRACT_SYNC_STATUS_CHOICES = (
        (CONTRACT_SYNC_SYNCED, 'Contract is up to date with DB state'),
        (CONTRACT_SYNC_PENDING, 'Waiting for contract update'),
        (CONTRACT_SYNC_FAILED, 'Contract update failed'),
        (CONTRACT_SYNC_REQUIRES_REVIEW, 'Contract update requires manual review'),
    )

    contract_address = models.CharField(max_length=128, primary_key=True)
    classic_code = models.CharField(max_length=64, null=True, blank=True)
    classic_issuer = models.CharField(max_length=56, null=True, blank=True)
    whitelisted = models.BooleanField(default=False)
    whitelisted_since = models.DateTimeField(null=True, blank=True)
    unwhitelisted_since = models.DateTimeField(null=True, blank=True)
    last_execution_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    # Contract synchronisation state: tracks whether the on-chain asset-registry
    # contract reflects the DB whitelisted flag.
    # Default SYNCED ensures backfilled tokens from 0028 (which already passed
    # on-chain execution) are treated as consistent with the contract.
    contract_sync_status = models.CharField(
        choices=CONTRACT_SYNC_STATUS_CHOICES,
        max_length=16,
        default=CONTRACT_SYNC_SYNCED,
        db_index=True,
    )
    contract_sync_tx_hash = models.CharField(max_length=128, null=True, blank=True)
    contract_sync_updated_at = models.DateTimeField(null=True, blank=True)
    contract_sync_error = models.TextField(null=True, blank=True)

    def __str__(self):
        return self.contract_address

    class Meta:
        indexes = [
            models.Index(fields=['last_execution_at'], name='gov_assettoken_last_exec_at'),
        ]


class AssetProposalInfo(models.Model):
    # Mandatory only for asset proposal types.
    asset_code = models.CharField(max_length=64, null=True, blank=True)
    asset_issuer = models.CharField(max_length=56, null=True, blank=True)
    asset_contract_address = models.CharField(max_length=128, null=True, blank=True)
    asset_issuer_information = models.TextField(null=True, blank=True)
    asset_token_description = models.TextField(null=True, blank=True)
    asset_holder_distribution = models.TextField(null=True, blank=True)
    asset_liquidity = models.TextField(null=True, blank=True)
    asset_trading_volume = models.TextField(null=True, blank=True)
    asset_audit_info = models.TextField(null=True, blank=True)
    asset_stellar_flags = models.TextField(null=True, blank=True)
    asset_related_projects = models.TextField(null=True, blank=True)
    asset_community_references = models.TextField(null=True, blank=True)
    asset_aquarius_traction = models.TextField(null=True, blank=True)
    asset_issuer_commitments = models.TextField(null=True, blank=True)
    asset_token = models.ForeignKey(
        AssetToken,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name='proposals',
    )

    class Meta:
        abstract = True


class Proposal(AssetProposalInfo):
    HORIZON_ERROR = payment_statuses.HORIZON_ERROR
    BAD_MEMO = payment_statuses.BAD_MEMO
    INVALID_PAYMENT = payment_statuses.INVALID_PAYMENT
    FINE = payment_statuses.FINE
    FAILED_TRANSACTION = payment_statuses.FAILED_TRANSACTION

    PROPOSAL_STATUS_CHOICES = (
        (HORIZON_ERROR, 'Bad horizon response'),
        (BAD_MEMO, 'Bad transaction memo'),
        (INVALID_PAYMENT, 'Invalid payment'),
        (FINE, 'Fine'),
        (FAILED_TRANSACTION, 'Transaction unsuccessful'),
    )  # TODO: remove it

    DISCUSSION = proposal_constants.PROPOSAL_STATUS_DISCUSSION
    QUEUED = proposal_constants.PROPOSAL_STATUS_QUEUED
    VOTING = proposal_constants.PROPOSAL_STATUS_VOTING
    VOTED = proposal_constants.PROPOSAL_STATUS_VOTED
    EXPIRED = proposal_constants.PROPOSAL_STATUS_EXPIRED

    NEW_PROPOSAL_STATUS_CHOICES = proposal_constants.PROPOSAL_STATUS_CHOICES

    PAYMENT_STATUS_CHOICES = (
        (HORIZON_ERROR, 'Bad horizon response'),
        (BAD_MEMO, 'Bad transaction memo'),
        (INVALID_PAYMENT, 'Invalid payment'),
        (FAILED_TRANSACTION, 'Transaction unsuccessful'),
        (FINE, 'Fine'),
    )

    NONE = proposal_constants.PROPOSAL_ACTION_NONE
    TO_UPDATE = proposal_constants.PROPOSAL_ACTION_TO_UPDATE
    TO_SUBMIT = proposal_constants.PROPOSAL_ACTION_TO_SUBMIT
    TO_CREATE = proposal_constants.PROPOSAL_ACTION_TO_CREATE

    PROPOSAL_ACTION_CHOICES = proposal_constants.PROPOSAL_ACTION_CHOICES

    PROPOSAL_TYPE_GENERAL = 'GENERAL'
    PROPOSAL_TYPE_ADD_ASSET = 'ADD_ASSET'
    PROPOSAL_TYPE_REMOVE_ASSET = 'REMOVE_ASSET'
    PROPOSAL_TYPE_CHOICES = (
        (PROPOSAL_TYPE_GENERAL, 'General proposal'),
        (PROPOSAL_TYPE_ADD_ASSET, 'Add asset proposal'),
        (PROPOSAL_TYPE_REMOVE_ASSET, 'Remove asset proposal'),
    )
    ASSET_PROPOSAL_TYPES = (
        PROPOSAL_TYPE_ADD_ASSET,
        PROPOSAL_TYPE_REMOVE_ASSET,
    )
    EXECUTION_SOURCE_FIELDS = (
        'proposal_type',
        'asset_code',
        'asset_issuer',
        'asset_contract_address',
    )

    ONCHAIN_ACTION_NONE = 'NONE'
    ONCHAIN_ACTION_ADD_ASSET = PROPOSAL_TYPE_ADD_ASSET
    ONCHAIN_ACTION_REMOVE_ASSET = PROPOSAL_TYPE_REMOVE_ASSET
    ONCHAIN_ACTION_CHOICES = (
        (ONCHAIN_ACTION_NONE, 'No onchain action'),
        (ONCHAIN_ACTION_ADD_ASSET, 'Add asset'),
        (ONCHAIN_ACTION_REMOVE_ASSET, 'Remove asset'),
    )

    ONCHAIN_EXECUTION_NOT_REQUIRED = 'NOT_REQUIRED'
    ONCHAIN_EXECUTION_PENDING = 'PENDING'
    ONCHAIN_EXECUTION_IN_PROGRESS = 'IN_PROGRESS'
    ONCHAIN_EXECUTION_SUBMITTED = 'SUBMITTED'
    ONCHAIN_EXECUTION_SUCCESS = 'SUCCESS'
    ONCHAIN_EXECUTION_FAILED = 'FAILED'
    ONCHAIN_EXECUTION_REQUIRES_REVIEW = 'REQUIRES_REVIEW'
    ONCHAIN_EXECUTION_SKIPPED = 'SKIPPED'
    ONCHAIN_EXECUTION_STATUS_CHOICES = (
        (ONCHAIN_EXECUTION_NOT_REQUIRED, 'No execution required'),
        (ONCHAIN_EXECUTION_PENDING, 'Pending execution'),
        (ONCHAIN_EXECUTION_IN_PROGRESS, 'Execution in progress'),
        (ONCHAIN_EXECUTION_SUBMITTED, 'Transaction submitted'),
        (ONCHAIN_EXECUTION_SUCCESS, 'Execution succeeded'),
        (ONCHAIN_EXECUTION_FAILED, 'Execution failed'),
        (ONCHAIN_EXECUTION_REQUIRES_REVIEW, 'Execution requires review'),
        (ONCHAIN_EXECUTION_SKIPPED, 'Execution skipped'),
    )

    proposed_by = models.CharField(max_length=56)
    title = models.CharField(max_length=256)
    text = QuillField()
    version = models.PositiveSmallIntegerField(default=1)

    vote_for_issuer = models.CharField(max_length=56)
    vote_against_issuer = models.CharField(max_length=56)
    abstain_issuer = models.CharField(max_length=56, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    last_updated_at = models.DateTimeField(auto_now_add=True)
    start_at = models.DateTimeField(null=True, blank=True)
    end_at = models.DateTimeField(null=True, blank=True)

    hide = models.BooleanField(default=False)
    is_simple_proposal = models.BooleanField(default=True)  # for future custom voting options
    draft = models.BooleanField(default=False)

    status = models.CharField(choices=PROPOSAL_STATUS_CHOICES, max_length=64, default=FINE)  # TODO: remove
    proposal_status = models.CharField(choices=NEW_PROPOSAL_STATUS_CHOICES, max_length=64, default=DISCUSSION)
    payment_status = models.CharField(choices=PAYMENT_STATUS_CHOICES, max_length=64, default=FINE)

    vote_for_result = models.DecimalField(decimal_places=7, max_digits=20, default=0, blank=True, null=True)
    vote_against_result = models.DecimalField(decimal_places=7, max_digits=20, default=0, blank=True, null=True)
    vote_abstain_result = models.DecimalField(decimal_places=7, max_digits=20, default=0, blank=True, null=True)

    transaction_hash = models.CharField(max_length=64, unique=True, null=True)
    envelope_xdr = models.TextField(null=True, blank=True)

    aqua_circulating_supply = models.DecimalField(decimal_places=7, max_digits=20, default=0, blank=True)
    ice_circulating_supply = models.DecimalField(decimal_places=7, max_digits=20, default=0, blank=True)
    percent_for_quorum = models.PositiveSmallIntegerField(blank=True, default=20)

    discord_channel_url = models.URLField(blank=True, null=True, default=settings.DEFAULT_DISCORD_URL)
    discord_channel_name = models.CharField(max_length=64, blank=True, null=True)
    discord_username = models.CharField(max_length=64, blank=True, null=True)

    new_title = models.CharField(max_length=256, null=True)
    new_text = QuillField(null=True)
    new_transaction_hash = models.CharField(max_length=64, unique=True, null=True)
    new_envelope_xdr = models.TextField(null=True, blank=True)
    new_start_at = models.DateTimeField(null=True, blank=True)
    new_end_at = models.DateTimeField(null=True, blank=True)

    proposal_type = models.CharField(
        choices=PROPOSAL_TYPE_CHOICES,
        max_length=32,
        default=PROPOSAL_TYPE_GENERAL,
        db_index=True,
    )
    action = models.CharField(choices=PROPOSAL_ACTION_CHOICES, max_length=64, default=NONE)

    onchain_execution_status = models.CharField(
        choices=ONCHAIN_EXECUTION_STATUS_CHOICES,
        max_length=32,
        default=ONCHAIN_EXECUTION_NOT_REQUIRED,
    )
    onchain_execution_tx_hash = models.CharField(max_length=128, null=True, blank=True)
    onchain_execution_started_at = models.DateTimeField(null=True, blank=True)
    onchain_execution_submitted_at = models.DateTimeField(null=True, blank=True)
    onchain_execution_poll_count = models.PositiveIntegerField(default=0)

    def __str__(self):
        return str(self.id)

    @classmethod
    def is_asset_proposal_type(cls, proposal_type: str) -> bool:
        return proposal_type in cls.ASSET_PROPOSAL_TYPES

    @property
    def is_asset_proposal(self) -> bool:
        return self.is_asset_proposal_type(self.proposal_type)

    @property
    def payment_verification_status(self) -> str:
        if self.draft and self.action != self.NONE and self.payment_status in {self.FINE, self.HORIZON_ERROR}:
            return 'PENDING'
        return self.payment_status

    @classmethod
    def has_active_voting_proposal_conflict(cls, current_proposal_id=None) -> bool:
        queryset = cls.objects.filter(
            hide=False,
            draft=False,
            proposal_status=cls.VOTING,
        )
        if current_proposal_id is not None:
            queryset = queryset.exclude(id=current_proposal_id)
        return queryset.exists()

    @property
    def onchain_action_type(self) -> str:
        if self.proposal_type == self.PROPOSAL_TYPE_ADD_ASSET:
            return self.ONCHAIN_ACTION_ADD_ASSET
        if self.proposal_type == self.PROPOSAL_TYPE_REMOVE_ASSET:
            return self.ONCHAIN_ACTION_REMOVE_ASSET
        return self.ONCHAIN_ACTION_NONE

    @property
    def onchain_action_args(self) -> list[str]:
        if not self.is_asset_proposal:
            return []
        if self.asset_token_id:
            return [self.asset_token_id]

        return derive_proposal_onchain_action_args(
            asset_code=self.asset_code,
            asset_issuer=self.asset_issuer,
            asset_contract_address=self.asset_contract_address,
        )

    def clean(self):
        super().clean()
        if self.is_asset_proposal and self.asset_token_id:
            from aqua_governance.governance.asset_tokens import validate_asset_token_consistency
            validate_asset_token_consistency(self)

    def save(self, force_insert=False, force_update=False, using=None, update_fields=None):
        if not self.vote_against_issuer:
            keypair = Keypair.random()
            self.vote_against_issuer = keypair.public_key
        if not self.vote_for_issuer:
            keypair = Keypair.random()
            self.vote_for_issuer = keypair.public_key
        if not self.abstain_issuer:
            keypair = Keypair.random()
            self.abstain_issuer = keypair.public_key

        self._validate_execution_source_fields_immutable(update_fields=update_fields)

        if self.onchain_action_type == self.ONCHAIN_ACTION_NONE:
            if (
                self.onchain_execution_status != self.ONCHAIN_EXECUTION_NOT_REQUIRED
                or self.onchain_execution_tx_hash
                or self.onchain_execution_started_at
                or self.onchain_execution_submitted_at
                or self.onchain_execution_poll_count
            ):
                self.onchain_execution_status = self.ONCHAIN_EXECUTION_NOT_REQUIRED
                self.onchain_execution_tx_hash = None
                self.onchain_execution_started_at = None
                self.onchain_execution_submitted_at = None
                self.onchain_execution_poll_count = 0
        elif (
            self.onchain_execution_status == self.ONCHAIN_EXECUTION_NOT_REQUIRED
            and not self.onchain_execution_tx_hash
        ):
            self.onchain_execution_status = self.ONCHAIN_EXECUTION_PENDING
            self.onchain_execution_started_at = None
            self.onchain_execution_submitted_at = None
            self.onchain_execution_poll_count = 0

        if not self.pk:
            # AQUA voting is deprecated: keep denominator based on ICE only for new proposals.
            self.aqua_circulating_supply = 0
            response = requests.get(settings.ICE_CIRCULATING_URL)
            if response.status_code == 200:
                self.ice_circulating_supply = float(response.json()['ice_supply_amount'])

        super(Proposal, self).save(force_insert, force_update, using, update_fields)

    def _validate_execution_source_fields_immutable(self, update_fields=None):
        if not self.pk:
            return

        fields_to_check = set(self.EXECUTION_SOURCE_FIELDS)
        if update_fields is not None:
            fields_to_check &= set(update_fields)
            if not fields_to_check:
                return

        persisted = type(self).objects.only(*fields_to_check).get(pk=self.pk)
        changed_fields = [
            field_name
            for field_name in fields_to_check
            if getattr(self, field_name) != getattr(persisted, field_name)
        ]
        if changed_fields:
            raise ValidationError({
                field_name: 'Execution source fields are immutable after proposal creation.'
                for field_name in changed_fields
            })

    class Meta:
        indexes = [
            models.Index(fields=['asset_token', 'hide', 'draft'], name='gov_proposal_at_hidedraft'),
        ]
        permissions = [
            ('manage_asset_proposals', 'Can manage asset proposals in admin'),
        ]


class ProposalQueueSlot(models.Model):
    proposal = models.OneToOneField(
        Proposal,
        primary_key=True,
        on_delete=models.CASCADE,
        related_name='queue_slot',
    )
    start_at = models.DateTimeField(unique=True)
    end_at = models.DateTimeField()
    occupied_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['start_at', 'proposal_id']

    def __str__(self):
        return f'{self.proposal_id}: {self.start_at.isoformat()} -> {self.end_at.isoformat()}'


class LogVote(models.Model):
    VOTE_FOR = 'vote_for'
    VOTE_AGAINST = 'vote_against'
    VOTE_ABSTAIN = 'vote_abstain'
    VOTE_TYPES = (
        (VOTE_FOR, 'Vote For'),
        (VOTE_AGAINST, 'Vote Against'),
        (VOTE_ABSTAIN, 'Vote Abstain'),
    )
    ASSET_TYPES = (
        (settings.AQUA_ASSET_CODE, settings.AQUA_ASSET_CODE),
        (settings.GOVERNANCE_ICE_ASSET_CODE, settings.GOVERNANCE_ICE_ASSET_CODE),
        (settings.GDICE_ASSET_CODE, settings.GDICE_ASSET_CODE),
    )

    claimable_balance_id = models.CharField(max_length=72, null=True)
    transaction_link = models.URLField(null=True)
    account_issuer = models.CharField(max_length=56, null=True)
    proposal = models.ForeignKey(Proposal, on_delete=models.CASCADE, null=True)
    vote_choice = models.CharField(max_length=15, choices=VOTE_TYPES, default=None, null=True)
    asset_code = models.CharField(max_length=15, choices=ASSET_TYPES, default=settings.AQUA_ASSET_CODE)
    created_at = models.DateTimeField(default=None, null=True)
    key = models.CharField(
        max_length=170,
        null=True,
        help_text=(
            ""
        )
    )
    group_index = models.IntegerField(
        default=0,
        help_text=(
            ""
        )
    )
    amount = models.DecimalField(
        decimal_places=7,
        max_digits=20,
        blank=True,
        null=True,
        help_text=(
            ""
        )
    )
    original_amount = models.DecimalField(
        decimal_places=7,
        max_digits=20,
        blank=True,
        null=True,
        help_text=(
            ""
        )
    )
    voted_amount = models.DecimalField(
        decimal_places=7,
        max_digits=20,
        blank=True,
        null=True,
        help_text=(
            ""
        )
    )
    claimed = models.BooleanField(
        default=False,
        help_text=(
            ""
        )
    )
    hide = models.BooleanField(
        default=False,
        help_text=(
            "System managed soft exclusion for this vote. Use cases: votes parsed after proposal end; "
            "duplicates reingested with the same claimable_balance_id; votes invalidated after rule changes or failed "
            "reverification; spam or abuse detection; temporary suppression during reprocessing. When True, the vote "
            "remains stored but is excluded from public endpoints and all counts or quorum. Automatically set by "
            "parser or background tasks; not edited manually. Part of the composite uniqueness with claimable_balance_id "
            "to allow a hidden shadow row without conflicts."
        ),
    )

    class Meta:
        unique_together = [['hide', 'claimable_balance_id']]

    def __str__(self):
        return str(self.id)


class HistoryProposal(models.Model):
    version = models.PositiveSmallIntegerField()
    hide = models.BooleanField(default=False)

    title = models.CharField(max_length=256)
    text = QuillField()

    created_at = models.DateTimeField()

    transaction_hash = models.CharField(max_length=64, unique=True, null=True)
    envelope_xdr = models.TextField(null=True, blank=True)
    proposal = models.ForeignKey(Proposal, related_name='history_proposal', on_delete=models.CASCADE, null=True)

    def __str__(self):
        return 'History proposal ' + str(self.id)
