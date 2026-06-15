from dataclasses import dataclass
import logging

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from stellar_sdk import Asset

from aqua_governance.governance.models import AssetToken, Proposal


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AssetProposalConflict:
    proposal: Proposal
    canonical_asset_contract_address: str


BLOCKING_ASSET_PROPOSAL_STATUSES = (
    Proposal.QUEUED,
    Proposal.VOTING,
)


def normalize_asset_value(value):
    """Strip whitespace and return ``None`` for blank/``None`` values."""
    if value is None:
        return None
    stripped = str(value).strip()
    return stripped if stripped else None


def derive_asset_contract_address(*, asset_code, asset_issuer, asset_contract_address):
    """
    Derive the Soroban contract address from a classic asset pair (code +
    issuer) via ``stellar_sdk.Asset``.

    * If both classic and an explicit contract address are provided, validate
      they match.
    * If only the classic pair is provided, derive and return the contract
      address.
    * If only the contract address is provided, return it as-is.
    * If neither is provided, raise ``ValueError``.
    """
    normalized_code = normalize_asset_value(asset_code)
    normalized_issuer = normalize_asset_value(asset_issuer)
    normalized_contract = normalize_asset_value(asset_contract_address)

    has_classic = bool(normalized_code) and bool(normalized_issuer)
    has_contract = bool(normalized_contract)

    if bool(normalized_code) != bool(normalized_issuer):
        raise ValueError("Provide both asset_code and asset_issuer together.")

    derived = None
    if has_classic:
        derived = Asset(normalized_code, normalized_issuer).contract_id(
            settings.NETWORK_PASSPHRASE
        )

    if has_contract:
        if derived and derived != normalized_contract:
            raise ValueError(
                "asset_contract_address does not match asset_code + asset_issuer."
            )
        return normalized_contract

    if derived:
        return derived

    raise ValueError("Provide asset_code + asset_issuer, or asset_contract_address.")


def get_canonical_asset_contract_address(
    *,
    proposal=None,
    proposal_type=None,
    asset_code=None,
    asset_issuer=None,
    asset_contract_address=None,
    asset_token_id=None,
):
    if proposal is not None:
        proposal_type = proposal.proposal_type
        asset_code = proposal.asset_code
        asset_issuer = proposal.asset_issuer
        asset_contract_address = proposal.asset_contract_address
        asset_token_id = proposal.asset_token_id

    if proposal_type not in Proposal.ASSET_PROPOSAL_TYPES:
        return None

    normalized_asset_token_id = normalize_asset_value(asset_token_id)
    if normalized_asset_token_id:
        return normalized_asset_token_id

    return derive_asset_contract_address(
        asset_code=asset_code,
        asset_issuer=asset_issuer,
        asset_contract_address=asset_contract_address,
    )


def find_active_asset_proposal_conflict(
    *,
    proposal=None,
    proposal_type=None,
    asset_code=None,
    asset_issuer=None,
    asset_contract_address=None,
    asset_token_id=None,
    exclude_proposal_id=None,
):
    canonical_asset_contract_address = get_canonical_asset_contract_address(
        proposal=proposal,
        proposal_type=proposal_type,
        asset_code=asset_code,
        asset_issuer=asset_issuer,
        asset_contract_address=asset_contract_address,
        asset_token_id=asset_token_id,
    )
    if canonical_asset_contract_address is None:
        return None

    if proposal is not None and exclude_proposal_id is None:
        exclude_proposal_id = proposal.id

    queryset = Proposal.objects.filter(
        proposal_type__in=Proposal.ASSET_PROPOSAL_TYPES,
        hide=False,
        draft=False,
        proposal_status__in=BLOCKING_ASSET_PROPOSAL_STATUSES,
    ).filter(
        Q(asset_token_id=canonical_asset_contract_address)
        | Q(asset_contract_address=canonical_asset_contract_address)
    )
    if exclude_proposal_id is not None:
        queryset = queryset.exclude(id=exclude_proposal_id)

    conflicting_proposal = queryset.order_by('created_at', 'id').first()
    if conflicting_proposal is None:
        return None

    return AssetProposalConflict(
        proposal=conflicting_proposal,
        canonical_asset_contract_address=canonical_asset_contract_address,
    )


def serialize_asset_proposal_conflict(conflict: AssetProposalConflict):
    return {
        'proposal': str(conflict.proposal.id),
        'proposal_status': conflict.proposal.proposal_status,
        'proposal_type': conflict.proposal.proposal_type,
    }


def validate_asset_token_consistency(proposal):
    """
    Validate that ``proposal.asset_token`` FK is consistent with the
    ``proposal.asset_*`` identifier fields.

    Derives the expected contract address from ``asset_code``, ``asset_issuer``,
    and ``asset_contract_address``, then compares it against
    ``proposal.asset_token.contract_address``.

    Raises ``ValidationError`` on mismatch.
    """
    if not proposal.is_asset_proposal or not proposal.asset_token_id:
        return

    asset_code = normalize_asset_value(proposal.asset_code)
    asset_issuer = normalize_asset_value(proposal.asset_issuer)
    existing_contract_address = normalize_asset_value(proposal.asset_contract_address)

    derived_contract_address = derive_asset_contract_address(
        asset_code=asset_code,
        asset_issuer=asset_issuer,
        asset_contract_address=existing_contract_address,
    )

    if proposal.asset_token_id != derived_contract_address:
        raise ValidationError({
            'asset_token': (
                f'Asset token FK ({proposal.asset_token_id}) does not match '
                f'derived contract address ({derived_contract_address}).'
            ),
        })


def apply_asset_proposal_result_to_token(proposal):
    """
    Atomically apply the result of a successful asset proposal to the
    :class:`AssetToken` in the database.

    This is called **before** the Soroban contract transaction is sent, making
    the DB the immediate source-of-truth for ``whitelisted``.  The API will
    reflect the new whitelist state as soon as this transaction commits.

    * ADD_ASSET → ``whitelisted=True``, ``contract_sync_status=PENDING``
    * REMOVE_ASSET → ``whitelisted=False``, ``contract_sync_status=PENDING``

    Returns the :class:`AssetToken` or ``None`` if the proposal is not an asset
    proposal or has no token linked.
    """
    if not proposal.is_asset_proposal or not proposal.asset_token:
        return None

    with transaction.atomic():
        token = AssetToken.objects.select_for_update().get(pk=proposal.asset_token_id)
        execution_at = timezone.now()

        if proposal.proposal_type == Proposal.PROPOSAL_TYPE_ADD_ASSET:
            if not token.whitelisted:
                token.whitelisted_since = execution_at
            token.whitelisted = True
        elif proposal.proposal_type == Proposal.PROPOSAL_TYPE_REMOVE_ASSET:
            if token.whitelisted:
                token.unwhitelisted_since = execution_at
            token.whitelisted = False
        else:
            return token

        token.last_execution_at = execution_at
        token.contract_sync_status = AssetToken.CONTRACT_SYNC_PENDING
        token.contract_sync_tx_hash = None
        token.contract_sync_updated_at = None
        token.contract_sync_error = None
        token.save(update_fields=[
            'whitelisted',
            'whitelisted_since',
            'unwhitelisted_since',
            'last_execution_at',
            'contract_sync_status',
            'contract_sync_tx_hash',
            'contract_sync_updated_at',
            'contract_sync_error',
        ])

    return token


def upsert_asset_token_from_proposal(proposal, save=True):
    """
    Create or update the :class:`AssetToken` for an asset proposal.

    * Derives the contract address (validating consistency).
    * Fills ``proposal.asset_contract_address`` if it is still blank.
    * Links ``proposal.asset_token`` to the token.
    * Validates FK consistency if token already linked.
    * Persists changed fields on the proposal when ``save=True``.

    Returns the :class:`AssetToken`, or ``None`` for non-asset proposals.
    """
    if not proposal.is_asset_proposal:
        return None

    asset_code = normalize_asset_value(proposal.asset_code)
    asset_issuer = normalize_asset_value(proposal.asset_issuer)
    existing_contract_address = normalize_asset_value(proposal.asset_contract_address)

    contract_address = derive_asset_contract_address(
        asset_code=asset_code,
        asset_issuer=asset_issuer,
        asset_contract_address=existing_contract_address,
    )

    if not existing_contract_address:
        proposal.asset_contract_address = contract_address

    # Create or update AssetToken
    token, created = AssetToken.objects.get_or_create(
        contract_address=contract_address,
        defaults={
            "classic_code": asset_code,
            "classic_issuer": asset_issuer,
        },
    )

    if not created:
        update_fields = []
        if asset_code and not token.classic_code:
            token.classic_code = asset_code
            update_fields.append("classic_code")
        if asset_issuer and not token.classic_issuer:
            token.classic_issuer = asset_issuer
            update_fields.append("classic_issuer")
        if update_fields:
            token.save(update_fields=update_fields)

    # Link proposal to token
    proposal.asset_token = token

    # Validate FK consistency — ensures proposal.asset_* matches token.address
    if not created or proposal.asset_token_id:
        validate_asset_token_consistency(proposal)

    if save:
        proposal_updates = {}
        if not existing_contract_address:
            proposal_updates["asset_contract_address"] = contract_address
        proposal_updates["asset_token_id"] = token.pk
        type(proposal).objects.filter(pk=proposal.pk).update(**proposal_updates)

    return token
