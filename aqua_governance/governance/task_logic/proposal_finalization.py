import logging
from decimal import Decimal
from typing import Any

import requests
from django.conf import settings
from django.db import transaction

from aqua_governance.governance.asset_tokens import apply_asset_proposal_result_to_token
from aqua_governance.governance.models import LogVote, Proposal


logger = logging.getLogger()


def _sum_votes_for_proposal(proposal: Proposal, vote_choice: str) -> Decimal:
    """Sum LogVote amounts for *proposal* and *vote_choice*, respecting status.

    For ``Proposal.VOTED`` proposals the frozen ``voted_amount`` is used and
    votes that have already been *claimed* are still counted (the snapshot was
    taken at the moment voting ended, before claims).  For every other status
    the current ``amount`` is used and *claimed* rows are excluded.

    For VOTED proposals, ``voted_amount`` is preferred.  When ``voted_amount``
    is ``None`` (which happens for rows that were first indexed after the
    voting window closed, e.g. during a periodic reindex without freezing),
    the current ``amount`` is used as a fallback.  This covers legacy /
    late-discovered rows that missed the freeze.  Non-freezing updates to rows
    that already have a snapshot preserve the existing ``voted_amount``.
    """
    supported_vote_assets = [settings.GOVERNANCE_ICE_ASSET_CODE, settings.GDICE_ASSET_CODE]

    if proposal.proposal_status == Proposal.VOTED:
        rows = proposal.logvote_set.filter(
            vote_choice=vote_choice,
            hide=False,
            asset_code__in=supported_vote_assets,
        ).values_list('voted_amount', 'amount')
        total = sum(
            (voted_amount if voted_amount is not None else amount)
            for voted_amount, amount in rows
        )
        return _as_decimal(total)

    amounts = proposal.logvote_set.filter(
        vote_choice=vote_choice,
        hide=False,
        claimed=False,
        asset_code__in=supported_vote_assets,
    ).values_list('amount', flat=True)
    return _as_decimal(sum(amounts))


def update_proposal_final_results(proposal_id: int) -> None:
    proposal = Proposal.objects.get(id=proposal_id)
    vote_for_result = _sum_votes_for_proposal(proposal, LogVote.VOTE_FOR)
    vote_against_result = _sum_votes_for_proposal(proposal, LogVote.VOTE_AGAINST)
    vote_abstain_result = _sum_votes_for_proposal(proposal, LogVote.VOTE_ABSTAIN)
    proposal.vote_for_result = vote_for_result
    proposal.vote_against_result = vote_against_result
    proposal.vote_abstain_result = vote_abstain_result

    has_fresh_ice_supply = _update_ice_circulating_supply(proposal)

    proposal.save(
        update_fields=[
            'vote_for_result',
            'vote_against_result',
            'vote_abstain_result',
            'ice_circulating_supply',
        ],
    )
    _execute_onchain_action_if_needed(proposal, has_fresh_ice_supply=has_fresh_ice_supply)


def _update_ice_circulating_supply(proposal: Proposal) -> bool:
    try:
        response = requests.get(settings.ICE_CIRCULATING_URL, timeout=10)
    except requests.RequestException:
        logger.exception(
            'Failed to fetch ICE circulating supply for proposal %s.',
            proposal.id,
        )
        return False

    if response.status_code != 200:
        logger.error(
            'ICE supply fetch returned non-200 status for proposal %s: %s',
            proposal.id,
            response.status_code,
        )
        return False

    try:
        proposal.ice_circulating_supply = float(response.json()['ice_supply_amount'])
    except (KeyError, TypeError, ValueError):
        logger.exception(
            'Failed to parse ICE circulating supply payload for proposal %s.',
            proposal.id,
        )
        return False

    return True


def _as_decimal(value: Any) -> Decimal:
    if value is None:
        return Decimal('0')
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _has_proposal_quorum(proposal: Proposal) -> bool:
    total_votes = (
        _as_decimal(proposal.vote_for_result)
        + _as_decimal(proposal.vote_against_result)
        + _as_decimal(proposal.vote_abstain_result)
    )
    required_votes = (
        _as_decimal(proposal.ice_circulating_supply)
        * _as_decimal(proposal.percent_for_quorum)
        / Decimal('100')
    )
    return total_votes >= required_votes


def _is_proposal_approved(proposal: Proposal) -> bool:
    return _as_decimal(proposal.vote_for_result) > _as_decimal(proposal.vote_against_result)


def _execute_onchain_action_if_needed(proposal: Proposal, has_fresh_ice_supply: bool) -> None:
    should_enqueue_send_task = False

    with transaction.atomic():
        proposal = Proposal.objects.select_for_update().get(id=proposal.id)

        if proposal.onchain_action_type == Proposal.ONCHAIN_ACTION_NONE:
            if (
                proposal.onchain_execution_status != Proposal.ONCHAIN_EXECUTION_NOT_REQUIRED
                or proposal.onchain_execution_tx_hash
                or proposal.onchain_execution_started_at
                or proposal.onchain_execution_submitted_at
                or proposal.onchain_execution_poll_count
            ):
                proposal.onchain_execution_status = Proposal.ONCHAIN_EXECUTION_NOT_REQUIRED
                proposal.onchain_execution_tx_hash = None
                proposal.onchain_execution_started_at = None
                proposal.onchain_execution_submitted_at = None
                proposal.onchain_execution_poll_count = 0
                proposal.save(
                    update_fields=[
                        'onchain_execution_status',
                        'onchain_execution_tx_hash',
                        'onchain_execution_started_at',
                        'onchain_execution_submitted_at',
                        'onchain_execution_poll_count',
                    ],
                )
            return

        if proposal.proposal_status != Proposal.VOTED:
            return

        if proposal.onchain_execution_status == Proposal.ONCHAIN_EXECUTION_SUCCESS:
            return

        if proposal.onchain_execution_status in (
            Proposal.ONCHAIN_EXECUTION_IN_PROGRESS,
            Proposal.ONCHAIN_EXECUTION_SUBMITTED,
            Proposal.ONCHAIN_EXECUTION_REQUIRES_REVIEW,
        ):
            return

        if not has_fresh_ice_supply:
            logger.error(
                'Skip onchain action for proposal %s due to stale or missing ICE supply.',
                proposal.id,
            )
            proposal.onchain_execution_status = Proposal.ONCHAIN_EXECUTION_FAILED
            proposal.onchain_execution_tx_hash = None
            proposal.onchain_execution_started_at = None
            proposal.onchain_execution_submitted_at = None
            proposal.onchain_execution_poll_count = 0
            proposal.save(
                update_fields=[
                    'onchain_execution_status',
                    'onchain_execution_tx_hash',
                    'onchain_execution_started_at',
                    'onchain_execution_submitted_at',
                    'onchain_execution_poll_count',
                ],
            )
            # Do NOT change AssetToken.whitelisted on ICE supply failure.
            return

        is_approved = _is_proposal_approved(proposal)
        has_quorum = _has_proposal_quorum(proposal)
        if not is_approved or not has_quorum:
            proposal.onchain_execution_status = Proposal.ONCHAIN_EXECUTION_SKIPPED
            proposal.onchain_execution_tx_hash = None
            proposal.onchain_execution_started_at = None
            proposal.onchain_execution_submitted_at = None
            proposal.onchain_execution_poll_count = 0
            logger.info(
                'Skip onchain action for proposal %s due to result check. approved=%s quorum=%s',
                proposal.id,
                is_approved,
                has_quorum,
            )
            proposal.save(
                update_fields=[
                    'onchain_execution_status',
                    'onchain_execution_tx_hash',
                    'onchain_execution_started_at',
                    'onchain_execution_submitted_at',
                    'onchain_execution_poll_count',
                ],
            )
            # Do NOT change AssetToken.whitelisted when not approved / no quorum.
            return

        # ── Approved + quorum + fresh ICE supply ──
        # Apply the asset proposal result to AssetToken immediately in the DB
        # (before any Soroban transaction). The API will see the new whitelisted
        # state as soon as this transaction commits. The contract sync continues
        # asynchronously via the enqueued send task.
        if proposal.is_asset_proposal and proposal.asset_token_id:
            try:
                apply_asset_proposal_result_to_token(proposal)
            except Exception:
                logger.exception(
                    'Failed to apply asset proposal result to AssetToken for proposal %s.',
                    proposal.id,
                )
                proposal.onchain_execution_status = Proposal.ONCHAIN_EXECUTION_FAILED
                proposal.onchain_execution_tx_hash = None
                proposal.onchain_execution_started_at = None
                proposal.onchain_execution_submitted_at = None
                proposal.onchain_execution_poll_count = 0
                proposal.save(
                    update_fields=[
                        'onchain_execution_status',
                        'onchain_execution_tx_hash',
                        'onchain_execution_started_at',
                        'onchain_execution_submitted_at',
                        'onchain_execution_poll_count',
                    ],
                )
                return

        proposal.onchain_execution_status = Proposal.ONCHAIN_EXECUTION_PENDING
        proposal.onchain_execution_tx_hash = None
        proposal.onchain_execution_started_at = None
        proposal.onchain_execution_submitted_at = None
        proposal.onchain_execution_poll_count = 0
        proposal.save(
            update_fields=[
                'onchain_execution_status',
                'onchain_execution_tx_hash',
                'onchain_execution_started_at',
                'onchain_execution_submitted_at',
                'onchain_execution_poll_count',
            ],
        )
        should_enqueue_send_task = True

    if should_enqueue_send_task:
        _enqueue_onchain_send_task(proposal.id)


def _enqueue_onchain_send_task(proposal_id: int) -> None:
    from aqua_governance.governance.tasks import task_execute_onchain_action_send

    task_execute_onchain_action_send.delay(proposal_id)
