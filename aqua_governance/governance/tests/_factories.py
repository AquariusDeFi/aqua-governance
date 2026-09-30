import json
from typing import Optional
from unittest.mock import Mock, patch

from django.conf import settings
from django.db import connection

from django_quill.quill import Quill
from stellar_sdk import Account, Asset, HashMemo, Keypair, TransactionBuilder

from aqua_governance.governance.asset_tokens import (
    derive_asset_contract_address,
    upsert_asset_token_from_proposal,
)
from aqua_governance.governance.models import AssetToken, Proposal


OWNER_KEYPAIR = Keypair.from_raw_ed25519_seed(bytes([0]) * 32)
ATTACKER_KEYPAIR = Keypair.from_raw_ed25519_seed(bytes([1]) * 32)

DEFAULT_PROPOSED_BY = OWNER_KEYPAIR.public_key
SECONDARY_ACCOUNT = ATTACKER_KEYPAIR.public_key
TERTIARY_ACCOUNT = Keypair.from_raw_ed25519_seed(bytes([2]) * 32).public_key
QUATERNARY_ACCOUNT = Keypair.from_raw_ed25519_seed(bytes([3]) * 32).public_key
DEFAULT_CODE = 'AQUA'
DEFAULT_ISSUER = 'GBNZILSTVQZ4R7IKQDGHYGY2QXL5QOFJYQMXPKWRRM5PAV7Y4M67AQUA'


# ``ProposalViewSet.queryset`` carries ``.exclude(id=65)`` for one poisoned production row.
EXCLUDED_PROPOSAL_ID = 65


def skip_excluded_proposal_id():
    """Advance the Proposal id sequence past the id the API refuses to serve.

    The id sequence is global and monotonic across a whole test run, so exactly one test per
    run builds a proposal that every ``/api/proposal/`` route answers 404 for - and which
    test that is moves every time a case is added anywhere in the suite.  Stepping the
    sequence past the excluded id makes it unreachable.

    Re-checked on every factory row rather than latched by a one-shot flag: the check is a
    single ``SELECT last_value``, and latching it would silently stop protecting the run
    after anything reset the sequence, such as ``TransactionTestCase.reset_sequences``.
    """
    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_get_serial_sequence(%s, %s)', [Proposal._meta.db_table, 'id'])
        sequence_name = cursor.fetchone()[0]
        # The name comes from PostgreSQL itself, so interpolating it is safe; a sequence
        # cannot be read through a placeholder.
        cursor.execute('SELECT last_value, is_called FROM {0}'.format(sequence_name))  # NOQA S608
        last_value, is_called = cursor.fetchone()
        if (last_value if is_called else last_value - 1) < EXCLUDED_PROPOSAL_ID:
            cursor.execute('SELECT setval(%s, %s, true)', [sequence_name, EXCLUDED_PROPOSAL_ID])


def distinct_hash(index):
    """A 64-hex transaction hash unique to ``index``, so no two claims collide by accident."""
    return '{0:064x}'.format(index + 1)


def build_aqua_burn_envelope(*, source, amount, memo_hash_hex, op_source=None, destination=None,
                             asset=None, extra_ops=(), sequence=1):
    """Build, fully offline, the AQUA burn envelope a client posts before signing.

    ``op_source`` is the only way to make the operation source differ from the transaction
    source, which is what tells a payer check done at the operation level apart from one
    done at the transaction level.
    """
    asset = asset if asset is not None else Asset(settings.AQUA_ASSET_CODE, settings.AQUA_ASSET_ISSUER)
    destination = destination if destination is not None else settings.AQUA_ASSET_ISSUER

    builder = TransactionBuilder(
        source_account=Account(source, sequence),
        network_passphrase=settings.NETWORK_PASSPHRASE,
        base_fee=100,
    )
    builder.set_timeout(300)
    builder.add_memo(HashMemo(memo_hash_hex))
    for operation_source, operation_amount in extra_ops:
        builder.append_payment_op(
            destination=destination,
            asset=asset,
            amount=str(operation_amount),
            source=operation_source,
        )
    builder.append_payment_op(
        destination=destination,
        asset=asset,
        amount=str(amount),
        source=op_source,
    )

    envelope = builder.build()
    return envelope.to_xdr(), envelope.hash_hex()


def _quill_text(html='<p>x</p>'):
    return Quill(json.dumps({'delta': {'ops': []}, 'html': html}))


def asset_narratives():
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
    skip_excluded_proposal_id()
    with patch_ice_circulating_supply():
        return Proposal.objects.create(**defaults)


def make_general_proposal(**overrides) -> Proposal:
    """A GENERAL proposal, spelled so the reader does not have to know that
    ``make_asset_proposal_raw(proposal_type=GENERAL)`` early-returns past every asset field.
    """
    overrides.setdefault('title', 'Test general proposal')
    return _create_proposal(proposal_type=Proposal.PROPOSAL_TYPE_GENERAL, **overrides)


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
        **asset_narratives(),
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
