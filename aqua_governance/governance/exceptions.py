from rest_framework import status
from rest_framework.exceptions import APIException


class ClaimableBalanceParsingError(Exception):
    pass


class GenerateGrouKeyException(Exception):
    pass


ASSET_PROPOSAL_CONFLICT_CODE = 'asset_proposal_conflict'
ASSET_PROPOSAL_CONFLICT_DETAIL = (
    'Another active or queued asset proposal already exists for this asset.'
)


def build_asset_proposal_conflict_detail(*, canonical_asset_contract_address, conflict):
    return {
        'detail': ASSET_PROPOSAL_CONFLICT_DETAIL,
        'code': ASSET_PROPOSAL_CONFLICT_CODE,
        'asset_contract_address': canonical_asset_contract_address,
        'conflict': conflict,
    }


class AssetProposalConflictError(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = ASSET_PROPOSAL_CONFLICT_DETAIL
    default_code = ASSET_PROPOSAL_CONFLICT_CODE

    def __init__(self, *, canonical_asset_contract_address, conflict):
        super().__init__(
            build_asset_proposal_conflict_detail(
                canonical_asset_contract_address=canonical_asset_contract_address,
                conflict=conflict,
            )
        )


TRANSACTION_ALREADY_CONSUMED_DETAIL = (
    'This payment transaction has already been used for a proposal transition.'
)


class TransactionAlreadyConsumedError(Exception):
    """A payment hash was already spent on a transition.

    Deliberately a plain ``Exception``: the claim runs in the Celery beat sweep as well as
    in the request path, where an ``APIException`` would surface as an unhandled task
    failure.  It must not subclass ``IntegrityError`` either, because the submit path
    catches that for slot-booking recovery.
    """

    def __init__(self, *, transaction_hash, existing=None):
        self.transaction_hash = transaction_hash
        self.existing = existing
        super().__init__(TRANSACTION_ALREADY_CONSUMED_DETAIL)
