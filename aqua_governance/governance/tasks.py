import logging
from datetime import timedelta
from typing import Optional

from django.conf import settings
from django.db import transaction
from django.db.models import Exists, F, OuterRef
from django.utils import timezone

from stellar_sdk import Server
from stellar_sdk.soroban_rpc import GetTransactionStatus

from aqua_governance.governance import proposal_transactions
from aqua_governance.governance.db_locks import acquire_proposal_transition_lock
from aqua_governance.governance.models import AssetToken, LogVote, Proposal, ProposalQueueSlot
from aqua_governance.governance.onchain_hooks import execute_onchain_action
from aqua_governance.governance.onchain_hooks.soroban import get_soroban_transaction
from aqua_governance.governance.proposal_queue_slots import sync_proposal_queue_slot
from aqua_governance.governance.task_logic.proposal_finalization import update_proposal_final_results
from aqua_governance.governance.task_logic.vote_indexing import IncompleteVoteSnapshot, update_proposal_votes_snapshot
from aqua_governance.taskapp import app as celery_app


logger = logging.getLogger(__name__)

# Keeps the claim sync clear of the closing freeze and its retries.
CLOSED_PROPOSAL_CLAIM_SYNC_DELAY = timedelta(hours=1)
# Execution statuses whose stored results are never recomputed. PENDING/FAILED results are recomputed by
# task_retry_failed_onchain_executions from `amount` when `voted_amount` is None, IN_PROGRESS/SUBMITTED can
# still become FAILED, and REQUIRES_REVIEW is operator-owned.
CLOSED_PROPOSAL_CLAIM_SYNC_EXECUTION_STATUSES = (
    Proposal.ONCHAIN_EXECUTION_NOT_REQUIRED,
    Proposal.ONCHAIN_EXECUTION_SKIPPED,
    Proposal.ONCHAIN_EXECUTION_SUCCESS,
)


def _start_due_scheduled_proposals(now) -> int:
    started_count = 0
    with transaction.atomic():
        acquire_proposal_transition_lock()
        proposals = list(
            Proposal.objects.filter(
                hide=False,
                draft=False,
                action=Proposal.NONE,
                proposal_status=Proposal.QUEUED,
                start_at__lte=now,
                end_at__gt=now,
                queue_slot__start_at=F('start_at'),
                queue_slot__end_at=F('end_at'),
            ).order_by('start_at', 'id'),
        )
        for proposal in proposals:
            locked_proposal = Proposal.objects.select_for_update().get(id=proposal.id)
            if locked_proposal.proposal_status != Proposal.QUEUED:
                continue
            if locked_proposal.hide or locked_proposal.draft:
                continue
            if locked_proposal.action != Proposal.NONE:
                continue
            if locked_proposal.start_at is None or locked_proposal.start_at > now:
                continue
            if locked_proposal.end_at is None or locked_proposal.end_at <= now:
                continue
            if not ProposalQueueSlot.objects.filter(
                proposal_id=locked_proposal.id,
                start_at=locked_proposal.start_at,
                end_at=locked_proposal.end_at,
            ).exists():
                continue
            if Proposal.has_active_voting_proposal_conflict(current_proposal_id=locked_proposal.id):
                continue
            locked_proposal.proposal_status = Proposal.VOTING
            locked_proposal.save(update_fields=['proposal_status'])
            started_count += 1
            # The global voting invariant allows only one proposal to be active at a time.
            break
    return started_count


def _finish_due_voting_proposals(now) -> None:
    proposals = Proposal.objects.filter(
        hide=False,
        draft=False,
        proposal_status=Proposal.VOTING,
        end_at__lte=now,
    )
    for proposal in proposals:
        proposal.proposal_status = Proposal.VOTED
        proposal.save(update_fields=['proposal_status'])
        sync_proposal_queue_slot(proposal)
        task_update_proposal_results.delay(proposal.id, True)


def _expire_stale_slotless_discussion_proposals(now) -> int:
    expired_period = now - settings.EXPIRED_TIME
    return Proposal.objects.filter(
        hide=False,
        draft=False,
        action=Proposal.NONE,
        proposal_status=Proposal.DISCUSSION,
        last_updated_at__lte=expired_period,
        queue_slot__isnull=True,
    ).update(proposal_status=Proposal.EXPIRED, action=Proposal.NONE)


def _mark_stale_in_progress_onchain_executions_for_review() -> None:
    cutoff = timezone.now() - timedelta(seconds=settings.ONCHAIN_EXECUTION_LEASE_SECONDS)
    stale_ids = list(
        Proposal.objects.filter(
            proposal_status=Proposal.VOTED,
            onchain_execution_status=Proposal.ONCHAIN_EXECUTION_IN_PROGRESS,
            onchain_execution_tx_hash__isnull=True,
            onchain_execution_started_at__isnull=False,
            onchain_execution_started_at__lt=cutoff,
        ).values_list('id', flat=True),
    )
    if not stale_ids:
        return

    Proposal.objects.filter(id__in=stale_ids).update(
        onchain_execution_status=Proposal.ONCHAIN_EXECUTION_REQUIRES_REVIEW,
    )
    logger.error(
        'Marked stale IN_PROGRESS onchain executions for review: proposals=%s',
        stale_ids,
    )


@celery_app.task(ignore_result=True)
def task_sync_proposal_statuses_by_time():
    now = timezone.now()
    _finish_due_voting_proposals(now)
    _expire_stale_slotless_discussion_proposals(now)
    _start_due_scheduled_proposals(now)


@celery_app.task(ignore_result=True)
def task_update_active_proposals():
    """
    Update active proposals.
    """
    now = timezone.now()
    active_proposals = Proposal.objects.filter(proposal_status=Proposal.VOTING, start_at__lte=now, end_at__gte=now)

    for proposal in active_proposals:
        task_update_proposal_results.delay(proposal.id)


@celery_app.task(ignore_result=True)
def task_check_expired_proposals():
    """
    Check expired proposals.
    """
    _expire_stale_slotless_discussion_proposals(timezone.now())


@celery_app.task(ignore_result=True)
def task_check_pending_proposal_payments():
    proposals = Proposal.objects.filter(
        hide=False,
    ).exclude(action=Proposal.NONE)
    for proposal in proposals:
        proposal_transactions.check_transaction(proposal)


@celery_app.task(bind=True, ignore_result=True)
def task_update_proposal_results(self, proposal_id: int, freezing_amount: bool = False):
    if task_update_votes(proposal_id, freezing_amount) is False:
        if freezing_amount and Proposal.objects.filter(
            pk=proposal_id,
            proposal_type=Proposal.PROPOSAL_TYPE_GENERAL,
            proposal_status=Proposal.VOTED,
        ).exists():
            raise self.retry(exc=IncompleteVoteSnapshot(
                f'Proposal {proposal_id} final vote snapshot has incomplete original metadata.',
            ))
        return
    update_proposal_final_results(proposal_id)


def _hold_incomplete_vote_snapshot(proposal_id: int) -> None:
    with transaction.atomic():
        proposal = Proposal.objects.select_for_update().filter(
            pk=proposal_id, proposal_status=Proposal.VOTED,
            proposal_type__in=Proposal.ASSET_PROPOSAL_TYPES,
            onchain_execution_status__in=[
                Proposal.ONCHAIN_EXECUTION_PENDING, Proposal.ONCHAIN_EXECUTION_FAILED,
                Proposal.ONCHAIN_EXECUTION_SKIPPED,
            ],
            onchain_execution_tx_hash__isnull=True,
            onchain_execution_started_at__isnull=True,
            onchain_execution_submitted_at__isnull=True,
            onchain_execution_poll_count=0,
        ).first()
        if proposal is None:
            return
        Proposal.objects.filter(pk=proposal.pk).update(
            onchain_execution_status=Proposal.ONCHAIN_EXECUTION_REQUIRES_REVIEW,
        )
        if proposal.asset_token_id:
            AssetToken.objects.filter(pk=proposal.asset_token_id, contract_sync_tx_hash__isnull=True).update(
                contract_sync_status=AssetToken.CONTRACT_SYNC_REQUIRES_REVIEW,
                contract_sync_error=f'Proposal {proposal.pk} has incomplete original vote metadata; review required.',
                contract_sync_updated_at=timezone.now(),
            )


@celery_app.task(ignore_result=True)
def task_update_votes(proposal_id: Optional[int] = None, freezing_amount: bool = False):
    """
    Update votes for proposal.
    """
    if proposal_id is None:
        # Accept historical bulk-update tasks already queued before their schedule was removed.
        return True

    proposals = Proposal.objects.filter(id=proposal_id)
    horizon_server = Server(settings.HORIZON_URL)
    complete = True

    for proposal in proposals:
        try:
            update_proposal_votes_snapshot(
                proposal=proposal,
                horizon_server=horizon_server,
                freezing_amount=freezing_amount,
            )
        except IncompleteVoteSnapshot:
            complete = False
            logger.exception('Skip finalization of proposal %s: incomplete original vote metadata.', proposal.pk)
            _hold_incomplete_vote_snapshot(proposal.pk)
    return complete


def _closed_proposals_with_unclaimed_votes(now):
    unclaimed_votes = LogVote.objects.filter(proposal=OuterRef('pk'), hide=False, claimed=False)
    return Proposal.objects.filter(
        Exists(unclaimed_votes),
        proposal_status=Proposal.VOTED,
        hide=False,
        end_at__lte=now - CLOSED_PROPOSAL_CLAIM_SYNC_DELAY,
        onchain_execution_status__in=CLOSED_PROPOSAL_CLAIM_SYNC_EXECUTION_STATUSES,
    ).order_by('id')


@celery_app.task(ignore_result=True)
def task_sync_closed_proposal_claims():
    """
    Follow melting replacements and claims of closed proposals' votes without touching frozen results.
    """
    horizon_server = Server(settings.HORIZON_URL)
    for proposal in _closed_proposals_with_unclaimed_votes(timezone.now()):
        try:
            update_proposal_votes_snapshot(
                proposal=proposal,
                horizon_server=horizon_server,
                freezing_amount=False,
            )
        except IncompleteVoteSnapshot:
            logger.warning('Skip claim sync of closed proposal %s: incomplete original vote metadata.', proposal.pk)
        except Exception:  # noqa: B902
            logger.exception('Claim sync of closed proposal %s failed.', proposal.pk)


@celery_app.task(ignore_result=True)
def task_execute_onchain_action_send(proposal_id: int):
    claimed = Proposal.objects.filter(
        id=proposal_id,
        proposal_status=Proposal.VOTED,
        onchain_execution_status=Proposal.ONCHAIN_EXECUTION_PENDING,
        onchain_execution_tx_hash__isnull=True,
    ).update(
        onchain_execution_status=Proposal.ONCHAIN_EXECUTION_IN_PROGRESS,
        onchain_execution_started_at=timezone.now(),
        onchain_execution_submitted_at=None,
        onchain_execution_poll_count=0,
    )
    if not claimed:
        return

    proposal = Proposal.objects.get(id=proposal_id)

    try:
        tx_hash = execute_onchain_action(proposal)
        if not tx_hash:
            raise ValueError('Onchain hook returned empty transaction hash')
    except Exception:
        logger.exception(
            'Onchain send failed for proposal %s (action=%s).',
            proposal.id,
            proposal.onchain_action_type,
        )
        Proposal.objects.filter(id=proposal_id).update(
            onchain_execution_status=Proposal.ONCHAIN_EXECUTION_FAILED,
            onchain_execution_tx_hash=None,
            onchain_execution_submitted_at=None,
            onchain_execution_poll_count=0,
        )
        # Mark AssetToken contract sync as FAILED but do NOT revert whitelisted.
        if proposal.is_asset_proposal and proposal.asset_token_id:
            AssetToken.objects.filter(pk=proposal.asset_token_id).update(
                contract_sync_status=AssetToken.CONTRACT_SYNC_FAILED,
                contract_sync_error='Onchain send failed',
                contract_sync_updated_at=timezone.now(),
            )
        return

    Proposal.objects.filter(id=proposal_id).update(
        onchain_execution_status=Proposal.ONCHAIN_EXECUTION_SUBMITTED,
        onchain_execution_tx_hash=tx_hash,
        onchain_execution_submitted_at=timezone.now(),
        onchain_execution_poll_count=0,
    )

    # Record the submitted tx hash on AssetToken so the admin/UI can track it.
    if proposal.is_asset_proposal and proposal.asset_token_id:
        AssetToken.objects.filter(pk=proposal.asset_token_id).update(
            contract_sync_tx_hash=tx_hash,
            contract_sync_updated_at=timezone.now(),
        )


def _sync_asset_token_on_success(proposal_id: int) -> None:
    """
    Called after Soroban confirms SUCCESS for an on-chain execution.

    Since the DB whitelisted flag was already updated during finalization
    (via ``apply_asset_proposal_result_to_token``), this function only needs
    to mark the on-chain sync status as SYNCED and record the confirmation
    timestamp.
    """
    with transaction.atomic():
        proposal = Proposal.objects.select_for_update().get(id=proposal_id)
        if proposal.onchain_execution_status != Proposal.ONCHAIN_EXECUTION_SUBMITTED:
            return

        if proposal.is_asset_proposal and proposal.asset_token_id:
            AssetToken.objects.filter(pk=proposal.asset_token_id).update(
                contract_sync_status=AssetToken.CONTRACT_SYNC_SYNCED,
                contract_sync_updated_at=timezone.now(),
                contract_sync_error=None,
            )

        proposal.onchain_execution_status = Proposal.ONCHAIN_EXECUTION_SUCCESS
        proposal.save(update_fields=['onchain_execution_status'])


@celery_app.task(ignore_result=True)
def task_poll_submitted_onchain_executions():
    proposals = Proposal.objects.filter(
        proposal_status=Proposal.VOTED,
        onchain_execution_status=Proposal.ONCHAIN_EXECUTION_SUBMITTED,
    ).exclude(onchain_execution_tx_hash__isnull=True)

    for proposal in proposals:
        try:
            result = get_soroban_transaction(proposal.onchain_execution_tx_hash)
        except Exception:
            logger.exception(
                'Failed to fetch Soroban transaction status for proposal %s tx=%s.',
                proposal.id,
                proposal.onchain_execution_tx_hash,
            )
            next_poll_count = proposal.onchain_execution_poll_count + 1
            update_kwargs = {'onchain_execution_poll_count': next_poll_count}
            if next_poll_count >= settings.ONCHAIN_TX_MAX_POLLS:
                update_kwargs['onchain_execution_status'] = Proposal.ONCHAIN_EXECUTION_REQUIRES_REVIEW
                logger.error(
                    'Soroban transaction polling failed for proposal %s tx=%s after %s attempts; '
                    'manual review required.',
                    proposal.id,
                    proposal.onchain_execution_tx_hash,
                    next_poll_count,
                )
            Proposal.objects.filter(id=proposal.id).update(**update_kwargs)
            if next_poll_count >= settings.ONCHAIN_TX_MAX_POLLS and proposal.is_asset_proposal and proposal.asset_token_id:
                AssetToken.objects.filter(pk=proposal.asset_token_id).update(
                    contract_sync_status=AssetToken.CONTRACT_SYNC_REQUIRES_REVIEW,
                    contract_sync_error=(
                        f'Polling exhausted after {next_poll_count} attempts: '
                        f'tx={proposal.onchain_execution_tx_hash}'
                    ),
                    contract_sync_updated_at=timezone.now(),
                )
            continue

        if result.status == GetTransactionStatus.SUCCESS:
            try:
                _sync_asset_token_on_success(proposal.id)
            except Exception:
                logger.exception(
                    'Failed to sync AssetToken after Soroban success for proposal %s tx=%s.',
                    proposal.id,
                    proposal.onchain_execution_tx_hash,
                )
                next_poll_count = proposal.onchain_execution_poll_count + 1
                update_kwargs = {'onchain_execution_poll_count': next_poll_count}
                if next_poll_count >= settings.ONCHAIN_TX_MAX_POLLS:
                    update_kwargs['onchain_execution_status'] = Proposal.ONCHAIN_EXECUTION_REQUIRES_REVIEW
                Proposal.objects.filter(id=proposal.id).update(**update_kwargs)
                if next_poll_count >= settings.ONCHAIN_TX_MAX_POLLS and proposal.is_asset_proposal and proposal.asset_token_id:
                    AssetToken.objects.filter(pk=proposal.asset_token_id).update(
                        contract_sync_status=AssetToken.CONTRACT_SYNC_REQUIRES_REVIEW,
                        contract_sync_error=(
                            'Sync failed after Soroban SUCCESS: '
                            f'tx={proposal.onchain_execution_tx_hash}'
                        ),
                        contract_sync_updated_at=timezone.now(),
                    )
            continue

        if result.status == GetTransactionStatus.FAILED:
            logger.error(
                'Soroban transaction failed for proposal %s tx=%s result_xdr=%s',
                proposal.id,
                proposal.onchain_execution_tx_hash,
                getattr(result, 'result_xdr', None),
            )
            next_poll_count = proposal.onchain_execution_poll_count + 1
            Proposal.objects.filter(id=proposal.id).update(
                onchain_execution_status=Proposal.ONCHAIN_EXECUTION_FAILED,
                onchain_execution_poll_count=next_poll_count,
            )
            # Mark AssetToken contract sync as FAILED but do NOT revert whitelisted.
            if proposal.is_asset_proposal and proposal.asset_token_id:
                AssetToken.objects.filter(pk=proposal.asset_token_id).update(
                    contract_sync_status=AssetToken.CONTRACT_SYNC_FAILED,
                    contract_sync_error=(
                        f'Soroban transaction FAILED: tx={proposal.onchain_execution_tx_hash}'
                    ),
                    contract_sync_updated_at=timezone.now(),
                )
            continue

        next_poll_count = proposal.onchain_execution_poll_count + 1
        update_kwargs = {'onchain_execution_poll_count': next_poll_count}
        if next_poll_count >= settings.ONCHAIN_TX_MAX_POLLS:
            update_kwargs['onchain_execution_status'] = Proposal.ONCHAIN_EXECUTION_REQUIRES_REVIEW
            logger.error(
                'Soroban transaction remained NOT_FOUND for proposal %s tx=%s after %s polls; '
                'manual review required.',
                proposal.id,
                proposal.onchain_execution_tx_hash,
                next_poll_count,
            )
        Proposal.objects.filter(id=proposal.id).update(**update_kwargs)
        if next_poll_count >= settings.ONCHAIN_TX_MAX_POLLS and proposal.is_asset_proposal and proposal.asset_token_id:
            AssetToken.objects.filter(pk=proposal.asset_token_id).update(
                contract_sync_status=AssetToken.CONTRACT_SYNC_REQUIRES_REVIEW,
                contract_sync_error=(
                    f'Polling NOT_FOUND exhausted after {next_poll_count} attempts: '
                    f'tx={proposal.onchain_execution_tx_hash}'
                ),
                contract_sync_updated_at=timezone.now(),
            )


@celery_app.task(ignore_result=True)
def task_retry_failed_onchain_executions():
    _mark_stale_in_progress_onchain_executions_for_review()

    proposals = Proposal.objects.filter(
        proposal_status=Proposal.VOTED,
        proposal_type__in=Proposal.ASSET_PROPOSAL_TYPES,
        onchain_execution_status__in=[
            Proposal.ONCHAIN_EXECUTION_FAILED,
            Proposal.ONCHAIN_EXECUTION_PENDING,
        ],
    ).order_by('-id')

    for proposal in proposals:
        # Recompute final results from already-frozen data and re-attempt
        # onchain execution.  Do NOT call task_update_proposal_results()
        # (which would re-index/re-freeze claimable balances) because the
        # proposal is already VOTED and its voted_amount snapshots must
        # not be overwritten by current (possibly melted/claimed) balances.
        update_proposal_final_results(proposal.id)
