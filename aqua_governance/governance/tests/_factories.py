import json
from typing import Optional
from unittest.mock import Mock, patch

from django_quill.quill import Quill
from stellar_sdk import Keypair

from aqua_governance.governance.asset_tokens import (
    derive_asset_contract_address,
    upsert_asset_token_from_proposal,
)
from aqua_governance.governance.models import AssetToken, Proposal


DEFAULT_PROPOSED_BY = Keypair.from_raw_ed25519_seed(bytes([0]) * 32).public_key
SECONDARY_ACCOUNT = Keypair.from_raw_ed25519_seed(bytes([1]) * 32).public_key
TERTIARY_ACCOUNT = Keypair.from_raw_ed25519_seed(bytes([2]) * 32).public_key
QUATERNARY_ACCOUNT = Keypair.from_raw_ed25519_seed(bytes([3]) * 32).public_key
DEFAULT_CODE = 'AQUA'
DEFAULT_ISSUER = 'GBNZILSTVQZ4R7IKQDGHYGY2QXL5QOFJYQMXPKWRRM5PAV7Y4M67AQUA'


def _quill_text(html='<p>x</p>'):
    return Quill(json.dumps({'delta': {'ops': []}, 'html': html}))


def _default_narratives():
    return {
        'asset_issuer_information': 'info',
        'asset_token_description': 'desc',
        'asset_holder_distribution': 'dist',
        'asset_liquidity': 'liq',
        'asset_trading_volume': 'vol',
        'asset_audit_info': 'audit',
        'asset_stellar_flags': 'flags',
        'asset_related_projects': 'projects',
        'asset_community_references': 'refs',
        'asset_aquarius_traction': 'traction',
        'asset_issuer_commitments': 'commitments',
    }


def patch_ice_circulating_supply(amount=0):
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.json.return_value = {'ice_supply_amount': amount}
    return patch('aqua_governance.governance.models.requests.get', return_value=mock_response)


def _create_proposal(**overrides):
    defaults = {
        'proposed_by': DEFAULT_PROPOSED_BY,
        'title': 'Test asset proposal',
        'text': _quill_text(),
        'proposal_type': Proposal.PROPOSAL_TYPE_ADD_ASSET,
        'draft': False,
        'action': Proposal.NONE,
        'proposal_status': Proposal.DISCUSSION,
    }
    defaults.update(overrides)
    with patch_ice_circulating_supply():
        return Proposal.objects.create(**defaults)


def _asset_fields(
    *,
    asset_code: Optional[str],
    asset_issuer: Optional[str],
    asset_contract_address: Optional[str],
    narratives: Optional[dict],
):
    fields = {
        'asset_code': asset_code,
        'asset_issuer': asset_issuer,
        'asset_contract_address': asset_contract_address,
        **_default_narratives(),
    }
    if narratives:
        fields.update(narratives)
    return fields


def make_asset_proposal(
    *,
    proposal_type: str = Proposal.PROPOSAL_TYPE_ADD_ASSET,
    asset_code: Optional[str] = DEFAULT_CODE,
    asset_issuer: Optional[str] = DEFAULT_ISSUER,
    asset_contract_address: Optional[str] = None,
    narratives: Optional[dict] = None,
    **proposal_kwargs,
) -> Proposal:
    proposal = _create_proposal(
        proposal_type=proposal_type,
        **_asset_fields(
            asset_code=asset_code,
            asset_issuer=asset_issuer,
            asset_contract_address=asset_contract_address,
            narratives=narratives,
        ),
        **proposal_kwargs,
    )

    if proposal.is_asset_proposal:
        upsert_asset_token_from_proposal(proposal, save=True)
        proposal.refresh_from_db()

    return proposal


def make_asset_proposal_raw(
    *,
    proposal_type: str = Proposal.PROPOSAL_TYPE_ADD_ASSET,
    asset_code: Optional[str] = DEFAULT_CODE,
    asset_issuer: Optional[str] = DEFAULT_ISSUER,
    asset_contract_address: Optional[str] = None,
    narratives: Optional[dict] = None,
    skip_payload: bool = False,
    **proposal_kwargs,
) -> Proposal:
    if not Proposal.is_asset_proposal_type(proposal_type):
        return _create_proposal(proposal_type=proposal_type, **proposal_kwargs)

    contract_address = asset_contract_address
    if not contract_address:
        contract_address = derive_asset_contract_address(
            asset_code=asset_code,
            asset_issuer=asset_issuer,
            asset_contract_address=asset_contract_address,
        )
    token = None
    if not skip_payload:
        token, _ = AssetToken.objects.get_or_create(
            contract_address=contract_address,
            defaults={
                'classic_code': asset_code,
                'classic_issuer': asset_issuer,
            },
        )

    return _create_proposal(
        proposal_type=proposal_type,
        asset_token=token,
        **_asset_fields(
            asset_code=asset_code,
            asset_issuer=asset_issuer,
            asset_contract_address=contract_address,
            narratives=narratives,
        ),
        **proposal_kwargs,
    )
