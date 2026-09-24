from datetime import timedelta
from decimal import Decimal
from itertools import count
from unittest.mock import Mock, patch

from django.conf import settings
from django.test import TestCase
from django.utils import timezone

from aqua_governance.governance.models import AssetToken, LogVote, Proposal
from aqua_governance.governance.parser import generate_vote_key
from aqua_governance.governance.task_logic.unlock_rules import get_expected_unlock_timestamp
from aqua_governance.governance.task_logic.vote_indexing import IncompleteVoteSnapshot
from aqua_governance.governance.tasks import (
    CLOSED_PROPOSAL_CLAIM_SYNC_DELAY,
    task_retry_failed_onchain_executions,
    task_sync_closed_proposal_claims,
)
from aqua_governance.governance.tests._factories import (
    DEFAULT_PROPOSED_BY,
    SECONDARY_ACCOUNT,
    _create_proposal,
    make_asset_proposal,
)


TASKS = 'aqua_governance.governance.tasks'
INDEXING = 'aqua_governance.governance.task_logic.vote_indexing'
FINALIZATION = 'aqua_governance.governance.task_logic.proposal_finalization'

DEFAULT_ENDED_AGO = timedelta(hours=2)

_balance_ids = count()
_asset_codes = count()


def _close(proposal, *, ended_ago=DEFAULT_ENDED_AGO, status=Proposal.VOTED, **fields):
    Proposal.objects.filter(pk=proposal.pk).update(
        proposal_status=status, end_at=timezone.now() - ended_ago, **fields,
    )
    proposal.refresh_from_db()
    return proposal


def _closed_general(**kwargs):
    return _close(_create_proposal(proposal_type=Proposal.PROPOSAL_TYPE_GENERAL), **kwargs)


def _closed_asset(execution_status, **kwargs):
    proposal = make_asset_proposal(asset_code=f'TK{next(_asset_codes)}')
    return _close(proposal, onchain_execution_status=execution_status, **kwargs)


def _vote(proposal, **overrides):
    fields = {
        'proposal': proposal,
        'claimable_balance_id': f'balance-{next(_balance_ids)}',
        'created_at': proposal.end_at - timedelta(days=1),
        'amount': Decimal('1000'),
        'original_amount': Decimal('1000'),
        'voted_amount': Decimal('1000'),
        'key': 'key',
        'account_issuer': DEFAULT_PROPOSED_BY,
        'asset_code': settings.GDICE_ASSET_CODE,
        'vote_choice': LogVote.VOTE_FOR,
    }
    fields.update(overrides)
    return LogVote.objects.create(**fields)


def _raw_vote(proposal, balance_id, *, asset_code=None, amount='900', service=True):
    if asset_code is None or asset_code == settings.GDICE_ASSET_CODE:
        asset_code, issuer = settings.GDICE_ASSET_CODE, settings.GDICE_ASSET_ISSUER
    else:
        issuer = settings.GOVERNANCE_ICE_ASSET_ISSUER
    return {
        'id': balance_id,
        'asset': f'{asset_code}:{issuer}',
        'amount': amount,
        'sponsor': SECONDARY_ACCOUNT if service else DEFAULT_PROPOSED_BY,
        'claimants': [{
            'destination': DEFAULT_PROPOSED_BY,
            'predicate': {'not': {'abs_before': str(get_expected_unlock_timestamp(proposal))}},
        }],
        '_links': {'transactions': {'href': 'https://example.com/transactions'}},
    }


def _horizon(created_at):
    server = Mock()
    server.operations.return_value.for_claimable_balance.return_value.order.return_value.limit.return_value.call.return_value = {  # noqa: E501
        '_embedded': {'records': [{
            'type': 'create_claimable_balance', 'created_at': created_at.isoformat(), 'amount': '1000',
        }]},
    }
    return server


def _synced_proposal_ids():
    with patch(f'{TASKS}.Server'), patch(f'{TASKS}.update_proposal_votes_snapshot') as snapshot:
        task_sync_closed_proposal_claims()
    return [call.kwargs['proposal'].pk for call in snapshot.call_args_list]


class ClosedProposalClaimSyncSelectionTests(TestCase):
    def test_selects_closed_proposals_with_unclaimed_visible_votes(self):
        general = _closed_general()
        _vote(general)
        skipped = _closed_asset(Proposal.ONCHAIN_EXECUTION_SKIPPED)
        _vote(skipped)
        succeeded = _closed_asset(Proposal.ONCHAIN_EXECUTION_SUCCESS)
        _vote(succeeded)

        self.assertEqual(_synced_proposal_ids(), [general.pk, skipped.pk, succeeded.pk])

    def test_skips_proposals_without_unclaimed_visible_votes(self):
        _vote(_closed_general(), claimed=True)
        _vote(_closed_general(), hide=True)
        only_claimed_or_hidden = _closed_general()
        _vote(only_claimed_or_hidden, claimed=True)
        _vote(only_claimed_or_hidden, hide=True)
        _closed_general()

        self.assertEqual(_synced_proposal_ids(), [])

    def test_skips_voting_hidden_and_recently_closed_proposals(self):
        _vote(_closed_general(status=Proposal.VOTING))
        _vote(_closed_general(hide=True))
        _vote(_closed_general(ended_ago=CLOSED_PROPOSAL_CLAIM_SYNC_DELAY - timedelta(minutes=1)))
        due = _closed_general(ended_ago=CLOSED_PROPOSAL_CLAIM_SYNC_DELAY + timedelta(minutes=1))
        _vote(due)

        self.assertEqual(_synced_proposal_ids(), [due.pk])

    def test_skips_asset_proposals_whose_results_can_still_be_recomputed(self):
        for status in (
            Proposal.ONCHAIN_EXECUTION_PENDING,
            Proposal.ONCHAIN_EXECUTION_FAILED,
            Proposal.ONCHAIN_EXECUTION_IN_PROGRESS,
            Proposal.ONCHAIN_EXECUTION_SUBMITTED,
            Proposal.ONCHAIN_EXECUTION_REQUIRES_REVIEW,
        ):
            _vote(_closed_asset(status))

        self.assertEqual(_synced_proposal_ids(), [])


class ClosedProposalClaimSyncBehaviourTests(TestCase):
    def test_updates_votes_without_freezing_or_finalizing(self):
        proposal = _closed_general()
        _vote(proposal)
        server = Mock()

        with patch(f'{TASKS}.Server', return_value=server), patch(
            f'{TASKS}.update_proposal_votes_snapshot',
        ) as snapshot, patch(f'{TASKS}.update_proposal_final_results') as finalize, patch(
            f'{TASKS}._hold_incomplete_vote_snapshot',
        ) as hold:
            task_sync_closed_proposal_claims()

        snapshot.assert_called_once_with(proposal=proposal, horizon_server=server, freezing_amount=False)
        finalize.assert_not_called()
        hold.assert_not_called()

    def test_incomplete_snapshot_leaves_finalized_asset_proposals_untouched(self):
        for status in (Proposal.ONCHAIN_EXECUTION_SKIPPED, Proposal.ONCHAIN_EXECUTION_SUCCESS):
            with self.subTest(status=status):
                proposal = _closed_asset(status)
                _vote(proposal, claimable_balance_id=f'frozen-{status}')
                proposal_before = Proposal.objects.filter(pk=proposal.pk).values().get()
                token_before = AssetToken.objects.filter(pk=proposal.asset_token_id).values().get()
                votes_before = list(proposal.logvote_set.values())
                groups = {'unknown-key': [(LogVote.VOTE_FOR, _raw_vote(proposal, f'unknown-{status}'))]}

                with patch(f'{TASKS}.Server'), patch(
                    f'{INDEXING}._build_raw_vote_groups', return_value=groups,
                ), patch(f'{INDEXING}._load_original_metadata', return_value=None), patch(
                    f'{TASKS}.update_proposal_final_results',
                ) as finalize:
                    task_sync_closed_proposal_claims()

                finalize.assert_not_called()
                self.assertEqual(Proposal.objects.filter(pk=proposal.pk).values().get(), proposal_before)
                self.assertEqual(AssetToken.objects.filter(pk=proposal.asset_token_id).values().get(), token_before)
                self.assertEqual(list(proposal.logvote_set.values()), votes_before)
                LogVote.objects.filter(proposal=proposal).update(claimed=True)

    def test_failure_on_one_proposal_does_not_stop_the_others(self):
        proposals = [_closed_general() for _ in range(3)]
        for proposal in proposals:
            _vote(proposal)
        failures = {
            proposals[0].pk: ConnectionError('horizon unavailable'),
            proposals[1].pk: IncompleteVoteSnapshot('metadata unavailable'),
        }

        def snapshot_side_effect(proposal, horizon_server, freezing_amount):
            if proposal.pk in failures:
                raise failures[proposal.pk]

        with patch(f'{TASKS}.Server'), patch(
            f'{TASKS}.update_proposal_votes_snapshot', side_effect=snapshot_side_effect,
        ) as snapshot, patch(f'{TASKS}._hold_incomplete_vote_snapshot') as hold:
            task_sync_closed_proposal_claims()

        self.assertEqual(
            [call.kwargs['proposal'].pk for call in snapshot.call_args_list],
            [proposal.pk for proposal in proposals],
        )
        hold.assert_not_called()


class ClosedProposalClaimSyncIndexingTests(TestCase):
    def test_melting_replacement_and_claimed_balance_are_synced_without_changing_results(self):
        proposal = _closed_general(
            ended_ago=timedelta(days=2),
            vote_for_result=Decimal('1500'),
            vote_against_result=Decimal('0'),
            vote_abstain_result=Decimal('0'),
        )
        replacement = _raw_vote(proposal, 'melted-replacement')
        melted = _vote(
            proposal, claimable_balance_id='melted-original',
            key=generate_vote_key(replacement, proposal, LogVote.VOTE_FOR),
        )
        claimed_raw = _raw_vote(proposal, 'claimed', asset_code=settings.GOVERNANCE_ICE_ASSET_CODE)
        claimed = _vote(
            proposal, claimable_balance_id='claimed', asset_code=settings.GOVERNANCE_ICE_ASSET_CODE,
            amount=Decimal('500'), original_amount=Decimal('500'), voted_amount=Decimal('500'),
            key=generate_vote_key(claimed_raw, proposal, LogVote.VOTE_FOR),
        )
        proposal_before = Proposal.objects.filter(pk=proposal.pk).values().get()

        with patch(f'{TASKS}.Server', return_value=_horizon(melted.created_at)), patch(
            f'{INDEXING}.load_all_records', side_effect=[[replacement], [], []],
        ), patch(f'{INDEXING}.find_origin_claimable_balance_id', return_value='origin'):
            task_sync_closed_proposal_claims()

        melted.refresh_from_db()
        claimed.refresh_from_db()
        self.assertEqual(melted.claimable_balance_id, 'melted-replacement')
        self.assertEqual(melted.amount, Decimal('900'))
        self.assertEqual(melted.voted_amount, Decimal('1000'))
        self.assertFalse(melted.claimed)
        self.assertTrue(claimed.claimed)
        self.assertEqual(claimed.voted_amount, Decimal('500'))
        self.assertEqual(Proposal.objects.filter(pk=proposal.pk).values().get(), proposal_before)

    def test_recomputable_asset_proposal_is_not_synced_so_its_results_stay_frozen(self):
        proposal = _closed_asset(
            Proposal.ONCHAIN_EXECUTION_FAILED,
            ended_ago=timedelta(days=2),
            vote_for_result=Decimal('1000'),
        )
        replacement = _raw_vote(proposal, 'melted-replacement')
        unfrozen = _vote(
            proposal, claimable_balance_id='unfrozen', voted_amount=None,
            key=generate_vote_key(replacement, proposal, LogVote.VOTE_FOR),
        )

        with patch(f'{TASKS}.Server', return_value=_horizon(unfrozen.created_at)), patch(
            f'{INDEXING}.load_all_records', side_effect=[[replacement], [], []],
        ) as horizon_records, patch(f'{INDEXING}.find_origin_claimable_balance_id', return_value='origin'):
            task_sync_closed_proposal_claims()
        with patch(f'{FINALIZATION}._update_ice_circulating_supply', return_value=True), patch(
            f'{TASKS}.task_execute_onchain_action_send.delay',
        ):
            task_retry_failed_onchain_executions()

        horizon_records.assert_not_called()
        unfrozen.refresh_from_db()
        proposal.refresh_from_db()
        self.assertEqual(unfrozen.claimable_balance_id, 'unfrozen')
        self.assertEqual(unfrozen.amount, Decimal('1000'))
        self.assertEqual(proposal.vote_for_result, Decimal('1000'))
