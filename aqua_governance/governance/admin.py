from django import forms
from django.contrib import admin
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Count

from aqua_governance.governance.asset_tokens import (
    derive_asset_contract_address,
    normalize_asset_value,
    upsert_asset_token_from_proposal,
)
from aqua_governance.governance.forms import ProposalAdminForm
from aqua_governance.governance.models import AssetToken, LogVote, Proposal, ProposalQueueSlot
from aqua_governance.governance.proposal_queue_slots import sync_proposal_queue_slot


@admin.register(Proposal)
class ProposalAdmin(admin.ModelAdmin):
    list_display = [
        'id',
        'proposed_by', 'hide', 'draft', 'action', 'proposal_status', 'payment_status',
        'title', 'proposal_type', 'start_at', 'end_at', 'created_at', 'last_updated_at',
        'onchain_action_type', 'onchain_execution_status',
        '_list_display_quorum',
    ]
    readonly_fields = [
        'vote_for_issuer', 'vote_against_issuer', 'abstain_issuer', 'version',
        'created_at', 'last_updated_at',
        'draft', 'status', 'action',
        'proposal_status', 'payment_status',
        'vote_for_result', 'vote_against_result', 'vote_abstain_result',
        'aqua_circulating_supply', 'ice_circulating_supply', 'percent_for_quorum',
        'onchain_action_type', 'onchain_action_args',
        'onchain_execution_status', 'onchain_execution_tx_hash',
        'onchain_execution_started_at', 'onchain_execution_submitted_at', 'onchain_execution_poll_count',
        'new_title', 'new_text', 'new_transaction_hash', 'new_envelope_xdr', 'new_start_at', 'new_end_at',
    ]
    search_fields = ['proposed_by', 'title', 'transaction_hash', 'new_transaction_hash']
    fields = [
        'proposed_by', 'title', 'text', 'proposal_type', 'is_simple_proposal', 'hide', 'draft', 'status', 'action',
        'proposal_status', 'payment_status', 'version', 'created_at', 'last_updated_at',
        'transaction_hash', 'envelope_xdr', 'start_at', 'end_at',
        'new_title', 'new_text', 'new_transaction_hash', 'new_envelope_xdr', 'new_start_at', 'new_end_at',
        'vote_for_issuer', 'vote_against_issuer', 'abstain_issuer',
        'vote_for_result', 'vote_against_result', 'vote_abstain_result',
        'aqua_circulating_supply', 'ice_circulating_supply', 'percent_for_quorum',
        'discord_channel_url', 'discord_channel_name', 'discord_username',
        'asset_code', 'asset_issuer', 'asset_contract_address', 'asset_issuer_information',
        'asset_token_description', 'asset_holder_distribution', 'asset_liquidity', 'asset_trading_volume',
        'asset_audit_info', 'asset_stellar_flags', 'asset_related_projects', 'asset_community_references',
        'asset_aquarius_traction', 'asset_issuer_commitments',
        'onchain_action_type', 'onchain_action_args', 'onchain_execution_status', 'onchain_execution_tx_hash',
        'onchain_execution_started_at', 'onchain_execution_submitted_at', 'onchain_execution_poll_count',
    ]
    list_filter = ('proposal_type', 'proposal_status', 'payment_status', 'draft', 'hide', 'action', 'start_at', 'end_at')
    form = ProposalAdminForm

    class Media:
        css = {
            'all': ('admin/django_quill.css',),
        }

    def changeform_view(self, request, object_id=None, form_url='', extra_context=None):
        if request.method == 'POST':
            with transaction.atomic():
                return super().changeform_view(request, object_id, form_url, extra_context)
        return super().changeform_view(request, object_id, form_url, extra_context)

    def get_form(self, request, obj=None, change=False, **kwargs):
        form_class = super().get_form(request, obj, change=change, **kwargs)

        class RequestBoundProposalAdminForm(form_class):
            def __init__(self, *args, **form_kwargs):
                self.request_user = request.user
                super().__init__(*args, **form_kwargs)

        return RequestBoundProposalAdminForm

    def _is_asset_manager(self, request) -> bool:
        return bool(
            request.user.is_authenticated
            and not request.user.is_superuser
            and request.user.has_perm('governance.manage_asset_proposals')
        )

    def _is_asset_object(self, obj) -> bool:
        return bool(obj and Proposal.is_asset_proposal_type(obj.proposal_type))

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        if self._is_asset_manager(request):
            return queryset.filter(proposal_type__in=Proposal.ASSET_PROPOSAL_TYPES)
        return queryset

    def formfield_for_choice_field(self, db_field, request, **kwargs):
        if db_field.name == 'proposal_type' and self._is_asset_manager(request):
            kwargs['choices'] = [
                choice for choice in db_field.choices
                if choice[0] in Proposal.ASSET_PROPOSAL_TYPES
            ]
        return super().formfield_for_choice_field(db_field, request, **kwargs)

    def get_readonly_fields(self, request, obj=None):
        readonly_fields = list(self.readonly_fields)
        if self._is_asset_manager(request) and (obj is None or self._is_asset_object(obj)):
            readonly_fields.remove('proposal_status')
        if obj:
            readonly_fields += [
                'proposed_by',
                'proposal_type',
                'transaction_hash', 'envelope_xdr',
                'asset_code', 'asset_issuer', 'asset_contract_address',
            ]
        if not request.user.is_superuser:
            readonly_fields.append('hide')
        return readonly_fields

    def has_add_permission(self, request):
        return request.user.is_superuser or request.user.has_perm('governance.manage_asset_proposals')

    def has_change_permission(self, request, obj=None):
        if request.user.is_superuser:
            return True
        if not request.user.has_perm('governance.manage_asset_proposals'):
            return False
        if obj is None:
            return True
        return self._is_asset_object(obj)

    def has_view_permission(self, request, obj=None):
        if request.user.is_superuser:
            return True
        if not request.user.has_perm('governance.manage_asset_proposals'):
            return False
        if obj is None:
            return True
        return self._is_asset_object(obj)

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser

    def get_actions(self, request):
        actions = super().get_actions(request)
        if not request.user.is_superuser:
            actions.pop('delete_selected', None)
        return actions

    def save_model(self, request, obj, form, change):
        if not request.user.is_superuser and not obj.is_asset_proposal:
            raise PermissionDenied('Managers can manage only asset proposals.')
        super().save_model(request, obj, form, change)
        sync_proposal_queue_slot(
            obj,
            create_missing=self._should_create_missing_queue_slot(form=form, change=change),
        )
        if obj.is_asset_proposal:
            upsert_asset_token_from_proposal(obj, save=True)

    @staticmethod
    def _should_create_missing_queue_slot(*, form, change: bool) -> bool:
        if not change:
            return True

        if form is None:
            return False

        # Only create a missing slot after the admin form revalidated scheduling
        # fields for this save. This keeps legacy slotless queued/voting rows
        # editable without silently manufacturing a new queue occupant.
        return bool({'proposal_status', 'start_at', 'end_at'} & set(form.changed_data))

    def _list_display_quorum(self, obj):
        if not obj.ice_circulating_supply or float(obj.ice_circulating_supply) <= 0:
            return 'Not enough votes'
        if obj.vote_for_result + obj.vote_against_result + obj.vote_abstain_result >= (
            float(obj.ice_circulating_supply)) * obj.percent_for_quorum / 100:
            return 'Enough votes'
        return 'Not enough votes'

    _list_display_quorum.short_description = 'quorum'


@admin.register(LogVote)
class LogVoteAdmin(admin.ModelAdmin):
    list_display = [
        'id',
        'asset_code',
        'vote_choice',
        'amount',
        'original_amount',
        'voted_amount',
        'group_index',
        'claimed',
        'hide',
        'created_at',
        'proposal',
        'account_issuer',
        'claimable_balance_id',
    ]
    readonly_fields = [
        'id',
        'asset_code',
        'claimable_balance_id',
        'transaction_link',
        'account_issuer',
        'proposal',
        'vote_choice',
        'created_at',
        'key',
        'group_index',
        'amount',
        'original_amount',
        'voted_amount',
        'claimed',
        'hide',
    ]
    search_fields = [
        '=id',
        'claimable_balance_id',
        'account_issuer',
        'key',
        '=proposal__id',
        'proposal__vote_for_issuer',
        'proposal__vote_against_issuer',
        'proposal__abstain_issuer',
    ]
    fields = readonly_fields
    list_filter = ('vote_choice', 'asset_code', 'claimed', 'hide')
    ordering = ('-created_at',)
    list_select_related = ('proposal',)

    def has_change_permission(self, request, obj=None):
        return False

    def has_add_permission(self, request):
        return False


@admin.register(AssetToken)
class AssetTokenAdmin(admin.ModelAdmin):
    list_display = [
        'contract_address',
        'classic_code',
        'classic_issuer',
        'whitelisted',
        'whitelisted_since',
        'unwhitelisted_since',
        'last_execution_at',
        'contract_sync_status',
        'contract_sync_tx_hash',
        'contract_sync_updated_at',
        '_proposal_count',
        'created_at',
        'updated_at',
    ]
    search_fields = [
        '=contract_address',
        'classic_code',
        'classic_issuer',
        'contract_sync_tx_hash',
    ]
    list_filter = [
        'whitelisted',
        'contract_sync_status',
        ('whitelisted_since', admin.DateFieldListFilter),
        ('unwhitelisted_since', admin.DateFieldListFilter),
        ('last_execution_at', admin.DateFieldListFilter),
        ('contract_sync_updated_at', admin.DateFieldListFilter),
        ('created_at', admin.DateFieldListFilter),
    ]
    ordering = ['-last_execution_at', '-created_at']
    readonly_fields = [
        'contract_address',
        'classic_code',
        'classic_issuer',
        'whitelisted',
        'whitelisted_since',
        'unwhitelisted_since',
        'last_execution_at',
        'contract_sync_status',
        'contract_sync_tx_hash',
        'contract_sync_updated_at',
        'contract_sync_error',
        'created_at',
        'updated_at',
    ]

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(proposal_count=Count('proposals'))

    def has_module_permission(self, request):
        return bool(
            request.user.is_superuser
            or request.user.has_perm('governance.manage_asset_proposals')
        )

    def has_add_permission(self, request):
        if request.user.is_superuser:
            return True
        return bool(
            request.user.is_authenticated
            and request.user.has_perm('governance.manage_asset_proposals')
        )

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_view_permission(self, request, obj=None):
        if request.user.is_superuser:
            return True
        return bool(
            request.user.is_authenticated
            and request.user.has_perm('governance.manage_asset_proposals')
        )

    def get_fields(self, request, obj=None):
        if obj is None:
            return ['contract_address', 'classic_code', 'classic_issuer']
        return super().get_fields(request, obj=obj)

    def get_readonly_fields(self, request, obj=None):
        if obj is None:
            return [f for f in self.readonly_fields if f not in ('contract_address', 'classic_code', 'classic_issuer')]
        return self.readonly_fields

    def get_form(self, request, obj=None, change=False, **kwargs):
        form = super().get_form(request, obj=obj, change=change, **kwargs)

        if obj is None:
            # Redeclare contract_address as optional on add so it can be
            # auto-derived from classic_code + classic_issuer in clean().
            class AssetTokenAddForm(form):
                contract_address = forms.CharField(
                    required=False,
                    max_length=128,
                )

                def clean(self):
                    cleaned_data = super().clean()
                    code = normalize_asset_value(cleaned_data.get('classic_code'))
                    issuer = normalize_asset_value(cleaned_data.get('classic_issuer'))
                    contract = normalize_asset_value(cleaned_data.get('contract_address'))
                    try:
                        derived = derive_asset_contract_address(
                            asset_code=code,
                            asset_issuer=issuer,
                            asset_contract_address=contract,
                        )
                        cleaned_data['contract_address'] = derived
                    except ValueError as e:
                        message = str(e)
                        if 'Provide both asset_code and asset_issuer together' in message:
                            raise ValidationError({
                                'classic_code': message,
                                'classic_issuer': message,
                            })
                        if 'does not match' in message:
                            raise ValidationError({'contract_address': message})
                        raise ValidationError(message)
                    return cleaned_data

            return AssetTokenAddForm

        return form

    def _proposal_count(self, obj):
        return getattr(obj, 'proposal_count', obj.proposals.count())

    _proposal_count.short_description = 'Proposals'


@admin.register(ProposalQueueSlot)
class ProposalQueueSlotAdmin(admin.ModelAdmin):
    list_display = [
        'proposal_id',
        'proposal_title',
        'proposal_status',
        'start_at',
        'end_at',
        'occupied_at',
    ]
    readonly_fields = [
        'proposal',
        'proposal_title',
        'proposal_status',
        'start_at',
        'end_at',
        'occupied_at',
    ]
    fields = readonly_fields
    search_fields = [
        '=proposal__id',
        'proposal__title',
    ]
    list_filter = [
        ('start_at', admin.DateFieldListFilter),
        ('end_at', admin.DateFieldListFilter),
        ('occupied_at', admin.DateFieldListFilter),
    ]
    ordering = ['start_at', 'proposal_id']
    list_select_related = ('proposal',)

    @admin.display(description='proposal title')
    def proposal_title(self, obj):
        return obj.proposal.title

    @admin.display(description='proposal status')
    def proposal_status(self, obj):
        return obj.proposal.proposal_status

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
