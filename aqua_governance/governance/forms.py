from django import forms
from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone

from aqua_governance.governance.asset_tokens import find_active_asset_proposal_conflict
from aqua_governance.governance.db_locks import acquire_proposal_transition_lock
from aqua_governance.governance.exceptions import ASSET_PROPOSAL_CONFLICT_DETAIL
from aqua_governance.governance.models import Proposal
from aqua_governance.governance.asset_payload import validate_asset_payload
from aqua_governance.governance.proposal_queue import validate_weekly_queue_slot
from aqua_governance.governance.proposal_queue_slots import is_queue_slot_available
from aqua_governance.governance.serializers_v2 import ASSET_FIELDS, ASSET_REQUIRED_TEXT_FIELDS
from aqua_governance.utils.payments import check_transaction_xdr
from aqua_governance.utils.widgets import CustomQuillWidget


ADMIN_OPTIONAL_FIELDS = (
    'discord_username',
    'asset_holder_distribution',
    'asset_liquidity',
    'asset_trading_volume',
)


def _value_is_blank(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


class ProposalAdminForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['text'].widget = CustomQuillWidget()
        if 'new_text' in self.fields:
            self.fields['new_text'].widget = CustomQuillWidget()

        for field_name in ('transaction_hash', 'new_transaction_hash'):
            if field_name in self.fields:
                self.fields[field_name].widget = forms.TextInput()

        for field_name in ('envelope_xdr', 'new_envelope_xdr'):
            if field_name in self.fields:
                self.fields[field_name].widget = forms.Textarea(attrs={'rows': 6})

        for field_name in ASSET_REQUIRED_TEXT_FIELDS:
            if field_name in self.fields:
                self.fields[field_name].widget = forms.Textarea(attrs={'rows': 3})

        for field_name in ('transaction_hash', 'envelope_xdr'):
            if field_name in self.fields:
                self.fields[field_name].required = False
        if 'proposal_status' in self.fields:
            self.fields['proposal_status'].required = False

        for field_name in ADMIN_OPTIONAL_FIELDS:
            if field_name in self.fields:
                self.fields[field_name].required = False

    class Meta:
        model = Proposal
        fields = forms.ALL_FIELDS

    def _is_asset_manager(self) -> bool:
        request_user = getattr(self, 'request_user', None)
        return bool(
            request_user
            and request_user.is_authenticated
            and not request_user.is_superuser
            and request_user.has_perm('governance.manage_asset_proposals')
        )

    def clean(self):
        cleaned_data = super().clean()
        proposal_type = cleaned_data.get('proposal_type') or self.instance.proposal_type or Proposal.PROPOSAL_TYPE_GENERAL
        is_asset_proposal = Proposal.is_asset_proposal_type(proposal_type)

        if self._is_asset_manager() and not is_asset_proposal:
            raise ValidationError({'proposal_type': 'Managers can manage only asset proposals.'})

        if proposal_type == Proposal.PROPOSAL_TYPE_GENERAL:
            self._validate_general_payload(cleaned_data)
        elif is_asset_proposal:
            self._validate_asset_payload(cleaned_data)
        else:
            raise ValidationError({'proposal_type': 'Unsupported proposal_type value.'})

        interval_lock_acquired = False
        if 'proposal_status' in cleaned_data:
            target_status = cleaned_data['proposal_status']
        else:
            target_status = self.instance.proposal_status
        target_status = target_status or Proposal.DISCUSSION

        if 'start_at' in cleaned_data:
            start_at = cleaned_data['start_at']
        else:
            start_at = self.instance.start_at
        if 'end_at' in cleaned_data:
            end_at = cleaned_data['end_at']
        else:
            end_at = self.instance.end_at

        if self.instance._state.adding and not is_asset_proposal:
            start_at = None
            end_at = None

        if (
            not self.instance._state.adding
            and target_status == Proposal.DISCUSSION
            and self.instance.proposal_status != Proposal.DISCUSSION
            and (start_at is not None or end_at is not None)
        ):
            # Leaving a queued/voting-style state for DISCUSSION must also drop
            # any reserved window so the proposal cannot retain slot-like
            # timing metadata without an attached queue slot.
            cleaned_data['start_at'] = None
            cleaned_data['end_at'] = None
            start_at = None
            end_at = None

        if (
            self.instance._state.adding
            and target_status == Proposal.DISCUSSION
            and (start_at is not None or end_at is not None)
        ):
            raise ValidationError({
                'start_at': 'Discussion proposals must not set a voting window before submit.',
                'end_at': 'Discussion proposals must not set a voting window before submit.',
            })

        # Detect whether start_at or end_at changed vs the persisted instance.
        if self.instance._state.adding:
            start_at_changed = start_at is not None
            end_at_changed = end_at is not None
        else:
            start_at_changed = 'start_at' in cleaned_data and cleaned_data['start_at'] != self.instance.start_at
            end_at_changed = 'end_at' in cleaned_data and cleaned_data['end_at'] != self.instance.end_at

        times_changed = start_at_changed or end_at_changed
        queue_relevant_status = target_status in (Proposal.QUEUED, Proposal.VOTING)
        entering_queue_relevant_status = queue_relevant_status and (
            self.instance._state.adding or target_status != self.instance.proposal_status
        )
        weekly_slot_validation_required = queue_relevant_status and (
            times_changed or entering_queue_relevant_status
        )

        if times_changed or entering_queue_relevant_status:
            acquire_proposal_transition_lock()
            interval_lock_acquired = True
            if target_status == Proposal.DISCUSSION and times_changed and (start_at is not None or end_at is not None):
                raise ValidationError({
                    'start_at': 'Discussion proposals must not set a voting window before submit.',
                    'end_at': 'Discussion proposals must not set a voting window before submit.',
                })

            if queue_relevant_status and (not start_at or not end_at):
                raise ValidationError({
                    'start_at': 'start_at is required for a queued or voting proposal.',
                    'end_at': 'end_at is required for a queued or voting proposal.',
                })

            if start_at and end_at:
                now = timezone.now()
                if weekly_slot_validation_required:
                    validate_weekly_queue_slot(start_at, end_at, now=now)
                if queue_relevant_status:
                    if target_status == Proposal.QUEUED and start_at <= now:
                        raise ValidationError({
                            'start_at': 'Queued proposals must use a future queue slot.',
                        })
                    if target_status == Proposal.VOTING:
                        if end_at <= now:
                            raise ValidationError({'end_at': 'end_at must be in the future.'})
                        if start_at > now:
                            raise ValidationError({
                                'start_at': 'Voting proposals must use the current queue slot.',
                            })

                current_proposal_id = None if self.instance._state.adding else self.instance.id
                if not is_queue_slot_available(
                    start_at=start_at,
                    end_at=end_at,
                    exclude_proposal_id=current_proposal_id,
                ):
                    raise ValidationError({
                        'start_at': 'Proposal voting interval overlaps with another queued or active proposal.',
                        'end_at': 'Proposal voting interval overlaps with another queued or active proposal.',
                    })

        if is_asset_proposal and queue_relevant_status:
            conflict = find_active_asset_proposal_conflict(
                proposal_type=proposal_type,
                asset_code=self._cleaned_or_instance_value(cleaned_data, 'asset_code'),
                asset_issuer=self._cleaned_or_instance_value(cleaned_data, 'asset_issuer'),
                asset_contract_address=self._cleaned_or_instance_value(cleaned_data, 'asset_contract_address'),
                exclude_proposal_id=None if self.instance._state.adding else self.instance.id,
            )
            if conflict is not None:
                raise ValidationError(
                    f'{ASSET_PROPOSAL_CONFLICT_DETAIL} Conflicting proposal ID: {conflict.proposal.id}.'
                )

        if self.instance._state.adding:
            if is_asset_proposal:
                if not interval_lock_acquired:
                    acquire_proposal_transition_lock()
                # Temporary admin-only path: asset proposals are created without payment/XDR.
                self.instance.draft = False
                self.instance.action = Proposal.NONE
                self.instance.payment_status = Proposal.FINE
                self.instance.hide = False
            else:
                self._validate_general_payment_fields(cleaned_data)
                self.instance.draft = True
                self.instance.action = Proposal.TO_CREATE
                # General proposals must go through the submit flow to set start_at/end_at;
                # otherwise the time-based sync task would promote them to VOTING without
                # a paid submit step.
                cleaned_data['start_at'] = None
                cleaned_data['end_at'] = None

        if not is_asset_proposal and cleaned_data.get('envelope_xdr'):
            payment_status = check_transaction_xdr(cleaned_data, settings.PROPOSAL_CREATE_OR_UPDATE_COST)
            self.instance.payment_status = payment_status
            if self.instance._state.adding and payment_status != Proposal.FINE:
                self.instance.hide = True

        return cleaned_data

    @staticmethod
    def _validate_general_payment_fields(cleaned_data):
        errors = {}
        for field_name in ('transaction_hash', 'envelope_xdr'):
            if _value_is_blank(cleaned_data.get(field_name)):
                errors[field_name] = 'This field is required for general proposal.'
        if errors:
            raise ValidationError(errors)

    def _validate_general_payload(self, cleaned_data):
        errors = {}
        for field_name in ASSET_FIELDS:
            if not _value_is_blank(cleaned_data.get(field_name)):
                errors[field_name] = 'General proposal does not support asset fields.'
        if errors:
            raise ValidationError(errors)

    def _validate_asset_payload(self, cleaned_data):
        errors = {}
        for field_name in ASSET_REQUIRED_TEXT_FIELDS:
            if field_name in ADMIN_OPTIONAL_FIELDS:
                continue
            if _value_is_blank(cleaned_data.get(field_name)):
                errors[field_name] = 'This field is required for asset proposal.'
        if errors:
            raise ValidationError(errors)

        try:
            validate_asset_payload(
                asset_code=self._cleaned_or_instance_value(cleaned_data, 'asset_code'),
                asset_issuer=self._cleaned_or_instance_value(cleaned_data, 'asset_issuer'),
                asset_contract_address=self._cleaned_or_instance_value(cleaned_data, 'asset_contract_address'),
                require_onchain_verification=False,
            )
        except ValueError as exc:
            raise ValidationError(self._map_asset_validation_error(str(exc))) from exc

    def _cleaned_or_instance_value(self, cleaned_data, field_name):
        if field_name in cleaned_data:
            return cleaned_data.get(field_name)
        return getattr(self.instance, field_name)

    @staticmethod
    def _map_asset_validation_error(message: str):
        if 'Provide both asset_code and asset_issuer together.' in message:
            return {
                'asset_code': message,
                'asset_issuer': message,
            }
        if 'Provide asset_code + asset_issuer, or asset_contract_address.' in message:
            return {
                'asset_code': message,
                'asset_issuer': message,
                'asset_contract_address': message,
            }
        if 'asset_issuer' in message:
            return {'asset_issuer': message}
        if 'asset_contract_address' in message or 'Soroban RPC' in message:
            return {'asset_contract_address': message}
        if 'Horizon' in message or 'contract_id' in message:
            return {
                'asset_code': message,
                'asset_issuer': message,
            }
        return {'proposal_type': message}
