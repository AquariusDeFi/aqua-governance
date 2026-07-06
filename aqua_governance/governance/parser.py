from decimal import Decimal
from typing import Optional

from django.conf import settings
from stellar_sdk import Asset

from aqua_governance.governance.exceptions import GenerateGrouKeyException
from aqua_governance.governance.models import LogVote, Proposal
from aqua_governance.utils.stellar.asset import parse_asset_string

AQUA_ASSET = Asset(settings.AQUA_ASSET_CODE, settings.AQUA_ASSET_ISSUER)
ICE_ASSET = Asset(settings.GOVERNANCE_ICE_ASSET_CODE, settings.GOVERNANCE_ICE_ASSET_ISSUER)
GDICE_ASSET = Asset(settings.GDICE_ASSET_CODE, settings.GDICE_ASSET_ISSUER)


def parse_vote(vote_key: str, vote_group_index: int, claimable_balance: dict, proposal: Proposal, vote_choice: str,
               created_at: str, original_amount: str, vote_id: Optional[int], freezing_amount: bool = False,
               original_voted_amount: Optional[Decimal] = None) -> Optional[LogVote]:
    balance_id = claimable_balance['id']
    asset = parse_asset_string(claimable_balance['asset'])
    asset_code = claimable_balance['asset'].split(':')[0]
    amount = claimable_balance['amount']
    transaction_link = claimable_balance['_links']['transactions']['href'].replace('{?cursor,limit,order}', '')

    if asset not in [AQUA_ASSET, ICE_ASSET, GDICE_ASSET]:
        return None

    time_list, account_issuer = _make_time_list_and_account_issuer_for_vote(claimable_balance, proposal)
    if not time_list:
        return None

    if freezing_amount:
        voted_amount = amount
    else:
        voted_amount = original_voted_amount

    return LogVote(
        id=vote_id,
        key=vote_key,
        group_index=vote_group_index,
        claimable_balance_id=balance_id,
        proposal=proposal,
        vote_choice=vote_choice,
        amount=amount,
        original_amount=original_amount,
        voted_amount=voted_amount,
        account_issuer=account_issuer,
        created_at=created_at,
        transaction_link=transaction_link,
        asset_code=asset_code,
        hide=False,
        claimed=False
    )


def generate_vote_key(claimable_balance: dict, proposal: Proposal, vote_choice: str) -> str:
    asset = parse_asset_string(claimable_balance['asset'])

    time_list, account_issuer = _make_time_list_and_account_issuer_for_vote(claimable_balance, proposal)
    proposal_id = proposal.id

    if not time_list:
        raise GenerateGrouKeyException("Invalid claimable_balance: time_list is empty")

    return generate_vote_key_by_raw_data(proposal_id, vote_choice, account_issuer, asset.code, time_list)


def generate_vote_key_by_raw_data(proposal_id: int, vote_choice: str, account_issuer: str, asset: str,
                                  time_list: list[str]) -> str:
    return f"{proposal_id}|{vote_choice}|{account_issuer}|{asset}|{sorted(time_list)}"


def _make_time_list_and_account_issuer_for_vote(claimable_balance: dict, proposal: Proposal) -> tuple[list[str], str]:
    account_issuer = claimable_balance['sponsor']
    claimants: list = claimable_balance['claimants']
    time_list: list[str] = []

    for claimant in claimants:
        abs_before: Optional[str] = claimant.get('predicate', {}).get('not', {}).get('abs_before', None)
        destination = claimant.get('destination', None)
        if abs_before is not None and destination is not None:
            account_issuer = destination
            time_list.append(abs_before)

    return time_list, account_issuer
