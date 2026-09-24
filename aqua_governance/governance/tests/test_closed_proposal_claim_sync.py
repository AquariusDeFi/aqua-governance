from datetime import timedelta
from decimal import Decimal
from io import StringIO
from itertools import count
from unittest.mock import Mock, patch

from django.conf import settings
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from celery.exceptions import SoftTimeLimitExceeded

from aqua_governance.governance.claimable_trace import find_origin_claimable_balance_id
from aqua_governance.governance.models import AssetToken, LogVote, Proposal
from aqua_governance.governance.parser import generate_vote_key
from aqua_governance.governance.task_logic.unlock_rules import get_expected_unlock_timestamp
from aqua_governance.governance.task_logic.vote_indexing import IncompleteVoteSnapshot, update_proposal_votes_snapshot
from aqua_governance.governance.tasks import (
    CLOSED_PROPOSAL_CLAIM_SYNC_DELAY,
    task_retry_failed_onchain_executions,
    task_sync_closed_proposal_claims,
    task_update_proposal_results,
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


class _ChainHorizon:
    """
    Horizon stand-in for melting chains: each balance in `parents` was created by a service-sponsored
    clawback→create of its parent; a balance whose parent is None is the voter's own create. Balances
    listed in `transactions` share that transaction, like the melter's batched transactions.
    """

    def __init__(self, parents, created_at, on_operations_call=None, transactions=None):
        self.parents = parents
        self.created_at = created_at
        self.on_operations_call = on_operations_call
        self.transactions = transactions or {}
        self.operations_calls = 0
        self.transaction_calls = 0

    def transaction_hash(self, balance_id):
        return self.transactions.get(balance_id, f'tx-{balance_id}')

    @classmethod
    def linear(cls, length, created_at, **kwargs):
        parents = {_chain_id(0): None}
        parents.update({_chain_id(step): _chain_id(step - 1) for step in range(1, length + 1)})
        return cls(parents, created_at, **kwargs)

    def claimable_balances(self):
        return _BalancesRequest()

    def operations(self):
        return _OperationsRequest(self)

    def balance_records(self, balance_id):
        parent = self.parents[balance_id]
        return [{
            'id': f'create-{balance_id}',
            'type': 'create_claimable_balance',
            'transaction_hash': self.transaction_hash(balance_id),
            'created_at': self.created_at.isoformat(),
            'amount': '1000',
            'sponsor': DEFAULT_PROPOSED_BY if parent is None else SECONDARY_ACCOUNT,
            'claimants': [{'destination': DEFAULT_PROPOSED_BY}],
        }]

    def transaction_records(self, transaction_hash):
        self.transaction_calls += 1
        records = []
        for balance_id, parent in self.parents.items():
            if self.transaction_hash(balance_id) != transaction_hash:
                continue
            if parent is not None:
                records.append({
                    'id': f'clawback-{balance_id}', 'type': 'clawback_claimable_balance', 'balance_id': parent,
                })
            records.append({'id': f'create-{balance_id}', 'type': 'create_claimable_balance'})
        return records


class _BalancesRequest:
    claimant = None

    def for_claimant(self, claimant):
        self.claimant = claimant
        return self

    def order(self, *args, **kwargs):
        return self


class _OperationsRequest:
    def __init__(self, horizon):
        self.horizon = horizon
        self.load = None

    def for_claimable_balance(self, balance_id):
        self.load = lambda: self.horizon.balance_records(balance_id)
        return self

    def for_transaction(self, transaction_hash):
        self.load = lambda: self.horizon.transaction_records(transaction_hash)
        return self

    def order(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def call(self):
        self.horizon.operations_calls += 1
        if self.horizon.on_operations_call is not None:
            self.horizon.on_operations_call()
        return {'_embedded': {'records': self.load()}}


def _chain_id(step):
    return f'chain-{step}'


def _proposal_with_live_and_gone_votes():
    proposal = _closed_general(ended_ago=timedelta(days=2))
    live_raw = _raw_vote(proposal, f'live-{proposal.pk}', service=False)
    live = _vote(
        proposal, claimable_balance_id=live_raw['id'],
        key=generate_vote_key(live_raw, proposal, LogVote.VOTE_FOR),
    )
    gone = _vote(proposal, claimable_balance_id=f'gone-{proposal.pk}', key='gone-key')
    return proposal, live, gone, live_raw


def _load_balances_by_claimant(balances, requested):
    def load(request_builder):
        requested.append(request_builder.claimant)
        return balances.get(request_builder.claimant, [])
    return load


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

        self.assertEqual(_synced_proposal_ids(), [succeeded.pk, skipped.pk, general.pk])

    def test_newest_proposals_are_synced_first(self):
        older = _closed_general(ended_ago=timedelta(days=30))
        _vote(older)
        newer = _closed_general()
        _vote(newer)

        self.assertEqual(_synced_proposal_ids(), [newer.pk, older.pk])

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

        snapshot.assert_called_once_with(
            proposal=proposal, horizon_server=server, freezing_amount=False, strict=False,
        )
        finalize.assert_not_called()
        hold.assert_not_called()

    def test_unresolved_group_on_finalized_asset_proposal_is_left_untouched_and_not_held(self):
        for status in (Proposal.ONCHAIN_EXECUTION_SKIPPED, Proposal.ONCHAIN_EXECUTION_SUCCESS):
            with self.subTest(status=status):
                proposal = _closed_asset(status)
                _vote(proposal, claimable_balance_id=f'frozen-{status}', key='unknown-key')
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


class ClosedProposalClaimSyncRunTests(TestCase):
    def test_soft_time_limit_stops_the_run_and_rolls_back_the_current_proposal(self):
        older, _, older_gone, older_raw = _proposal_with_live_and_gone_votes()
        newer, newer_live, newer_gone, newer_raw = _proposal_with_live_and_gone_votes()
        balances = {older.vote_for_issuer: [older_raw], newer.vote_for_issuer: [newer_raw]}
        requested = []

        with patch(f'{TASKS}.Server', return_value=_ChainHorizon({}, timezone.now())), patch(
            f'{INDEXING}.load_all_records', side_effect=_load_balances_by_claimant(balances, requested),
        ), patch.object(
            LogVote.objects, 'bulk_update', side_effect=SoftTimeLimitExceeded(),
        ), self.assertRaises(SoftTimeLimitExceeded):
            task_sync_closed_proposal_claims()

        self.assertIn(newer.vote_for_issuer, requested)
        self.assertNotIn(older.vote_for_issuer, requested)
        newer_gone.refresh_from_db()
        older_gone.refresh_from_db()
        self.assertFalse(newer_gone.claimed)
        self.assertFalse(older_gone.claimed)

    def _assert_synced(self, live, gone):
        live.refresh_from_db()
        gone.refresh_from_db()
        self.assertEqual(live.amount, Decimal('900'))
        self.assertTrue(gone.claimed)

    def _assert_untouched(self, live, gone):
        live.refresh_from_db()
        gone.refresh_from_db()
        self.assertEqual(live.amount, Decimal('1000'))
        self.assertFalse(gone.claimed)

    def test_horizon_failure_midway_through_a_proposal_does_not_stop_the_others(self):
        synced = [_proposal_with_live_and_gone_votes() for _ in range(3)]
        failing = synced[1][0]
        balances = {proposal.vote_for_issuer: [raw] for proposal, _, _, raw in synced}
        load = _load_balances_by_claimant(balances, [])

        def load_or_fail(request_builder):
            if request_builder.claimant == failing.vote_against_issuer:
                raise ConnectionError('horizon unavailable')
            return load(request_builder)

        with patch(f'{TASKS}.Server', return_value=_ChainHorizon({}, timezone.now())), patch(
            f'{INDEXING}.load_all_records', side_effect=load_or_fail,
        ):
            task_sync_closed_proposal_claims()

        self._assert_synced(*synced[0][1:3])
        self._assert_untouched(*synced[1][1:3])
        self._assert_synced(*synced[2][1:3])

    def test_failure_after_partial_writes_rolls_back_only_that_proposal(self):
        synced = [_proposal_with_live_and_gone_votes() for _ in range(3)]
        failing = synced[1][0]
        balances = {proposal.vote_for_issuer: [raw] for proposal, _, _, raw in synced}
        bulk_update = LogVote.objects.bulk_update

        def bulk_update_or_fail(objs, fields, **kwargs):
            if any(vote.proposal_id == failing.pk for vote in objs):
                raise RuntimeError('database unavailable')
            return bulk_update(objs, fields, **kwargs)

        with patch(f'{TASKS}.Server', return_value=_ChainHorizon({}, timezone.now())), patch(
            f'{INDEXING}.load_all_records', side_effect=_load_balances_by_claimant(balances, []),
        ), patch.object(LogVote.objects, 'bulk_update', side_effect=bulk_update_or_fail):
            task_sync_closed_proposal_claims()

        self._assert_synced(*synced[0][1:3])
        self._assert_untouched(*synced[1][1:3])
        self._assert_synced(*synced[2][1:3])


class ClosedProposalClaimSyncIndexingTests(TestCase):
    def test_melting_replacement_and_claimed_balance_are_synced_keeping_frozen_weight(self):
        proposal = _closed_general(ended_ago=timedelta(days=2))
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

    def test_concurrent_freeze_is_not_overwritten_by_the_sync(self):
        proposal = _closed_general(ended_ago=timedelta(days=2))
        replacement = _raw_vote(proposal, _chain_id(2))
        vote = _vote(
            proposal, claimable_balance_id=_chain_id(1), voted_amount=None,
            key=generate_vote_key(replacement, proposal, LogVote.VOTE_FOR),
        )

        def freeze_concurrently():
            LogVote.objects.filter(pk=vote.pk).update(voted_amount=Decimal('777'))

        horizon = _ChainHorizon.linear(2, vote.created_at, on_operations_call=freeze_concurrently)
        with patch(f'{TASKS}.Server', return_value=horizon), patch(
            f'{INDEXING}.load_all_records', side_effect=[[replacement], [], []],
        ):
            task_sync_closed_proposal_claims()

        vote.refresh_from_db()
        self.assertEqual(vote.claimable_balance_id, _chain_id(2))
        self.assertEqual(vote.voted_amount, Decimal('777'))


class ClosedProposalClaimSyncLineageTests(TestCase):
    ORIGIN_DEPTH = 130

    def _sync_chain(self, stored_step, horizon=None, **vote_fields):
        proposal = _closed_general(ended_ago=timedelta(days=2))
        replacement = _raw_vote(proposal, _chain_id(self.ORIGIN_DEPTH))
        vote = _vote(
            proposal, claimable_balance_id=_chain_id(stored_step),
            key=generate_vote_key(replacement, proposal, LogVote.VOTE_FOR), **vote_fields,
        )
        horizon = horizon or _ChainHorizon.linear(self.ORIGIN_DEPTH, proposal.end_at - timedelta(days=3))
        with patch(f'{TASKS}.Server', return_value=horizon), patch(
            f'{INDEXING}.load_all_records', side_effect=[[replacement], [], []],
        ):
            task_sync_closed_proposal_claims()
        vote.refresh_from_db()
        return proposal, vote, horizon

    def test_replacement_is_matched_through_the_stored_id_beyond_the_origin_trace_depth(self):
        proposal, vote, _ = self._sync_chain(self.ORIGIN_DEPTH - 1)

        self.assertEqual(vote.claimable_balance_id, _chain_id(self.ORIGIN_DEPTH))
        self.assertEqual(vote.amount, Decimal('900'))
        self.assertEqual(proposal.logvote_set.count(), 1)

    def test_horizon_calls_are_proportional_to_steps_since_the_stored_id(self):
        for steps in (1, 3, 7):
            with self.subTest(steps=steps):
                _, vote, horizon = self._sync_chain(self.ORIGIN_DEPTH - steps)

                self.assertEqual(vote.claimable_balance_id, _chain_id(self.ORIGIN_DEPTH))
                self.assertEqual(horizon.operations_calls, 2 * steps)
                vote.delete()

    def test_late_stored_row_reached_through_the_chain_stays_excluded_despite_an_on_time_origin(self):
        proposal = _closed_general(ended_ago=timedelta(days=2))
        replacement = _raw_vote(proposal, _chain_id(5))
        late = _vote(
            proposal, claimable_balance_id=_chain_id(4), created_at=proposal.end_at + timedelta(hours=1),
            key=generate_vote_key(replacement, proposal, LogVote.VOTE_FOR),
        )
        horizon = _ChainHorizon.linear(5, proposal.end_at - timedelta(days=3))
        with patch(f'{TASKS}.Server', return_value=horizon), patch(
            f'{INDEXING}.load_all_records', side_effect=[[replacement], [], []],
        ):
            task_sync_closed_proposal_claims()

        late.refresh_from_db()
        self.assertEqual(late.claimable_balance_id, _chain_id(4))
        self.assertFalse(late.claimed)
        self.assertEqual(proposal.logvote_set.count(), 1)

    def test_replacements_reaching_the_same_stored_row_fall_back_to_origin_matching(self):
        proposal = _closed_general(ended_ago=timedelta(days=2))
        first = _raw_vote(proposal, 'split-a', amount='900')
        second = _raw_vote(proposal, 'split-b', amount='800')
        stored = _vote(
            proposal, claimable_balance_id=_chain_id(3),
            key=generate_vote_key(first, proposal, LogVote.VOTE_FOR),
        )
        horizon = _ChainHorizon.linear(3, proposal.end_at - timedelta(days=3))
        horizon.parents.update({'split-a': _chain_id(3), 'split-b': _chain_id(3)})

        with patch(f'{TASKS}.Server', return_value=horizon), patch(
            f'{INDEXING}.load_all_records', side_effect=[[first, second], [], []],
        ), patch(
            f'{INDEXING}.find_origin_claimable_balance_id', wraps=find_origin_claimable_balance_id,
        ) as origin_trace:
            task_sync_closed_proposal_claims()

        traced = {call.args[1] for call in origin_trace.call_args_list}
        self.assertLessEqual({'split-a', 'split-b'}, traced)
        # Origin matching is ambiguous (two replacements, one vote), so the stored vote is paired with the
        # largest replacement and the other one is added with its origin's creation metadata.
        stored.refresh_from_db()
        self.assertEqual(
            (stored.claimable_balance_id, stored.amount, stored.voted_amount),
            ('split-a', Decimal('900'), Decimal('1000')),
        )
        added = proposal.logvote_set.exclude(pk=stored.pk).get()
        self.assertEqual(
            (added.claimable_balance_id, added.amount, added.original_amount, added.voted_amount, added.created_at),
            ('split-b', Decimal('800'), Decimal('1000'), None, horizon.created_at),
        )


def _soft_time_limit_on_first_horizon_call():
    calls = []

    def hook():
        calls.append(None)
        if len(calls) == 1:
            raise SoftTimeLimitExceeded()
    return hook


class SoftTimeLimitTests(TestCase):
    CASES = ('lineage walk', 'origin trace')

    def _melted_vote(self, proposal, case):
        """
        A service-sponsored replacement whose first Horizon operations call happens inside the lineage
        walk (a stored ancestor exists) or inside the full origin trace (nothing stored in its group).
        """
        replacement = _raw_vote(proposal, _chain_id(2))
        key = generate_vote_key(replacement, proposal, LogVote.VOTE_FOR)
        if case == 'lineage walk':
            vote = _vote(proposal, claimable_balance_id=_chain_id(1), voted_amount=None, key=key)
        else:
            vote = _vote(proposal, claimable_balance_id='unrelated', key='unrelated-key')
        return replacement, vote

    def _horizon_patches(self, proposal, replacement, hook):
        horizon = _ChainHorizon.linear(2, proposal.end_at - timedelta(days=3), on_operations_call=hook)
        balances = {proposal.vote_for_issuer: [replacement]}
        return (
            patch(f'{TASKS}.Server', return_value=horizon),
            patch(f'{INDEXING}.load_all_records', side_effect=_load_balances_by_claimant(balances, [])),
        )

    def test_claim_sync_run_stops_on_soft_time_limit(self):
        for case in self.CASES:
            with self.subTest(case=case):
                older = _closed_general(ended_ago=timedelta(days=3))
                older_gone = _vote(older, claimable_balance_id=f'gone-{older.pk}', key='gone-key')
                newer = _closed_general(ended_ago=timedelta(days=2))
                replacement, _ = self._melted_vote(newer, case)
                server_patch, records_patch = self._horizon_patches(
                    newer, replacement, _soft_time_limit_on_first_horizon_call(),
                )

                with server_patch, records_patch, self.assertRaises(SoftTimeLimitExceeded):
                    task_sync_closed_proposal_claims()

                older_gone.refresh_from_db()
                self.assertFalse(older_gone.claimed)
                LogVote.objects.filter(proposal__in=[older, newer]).delete()

    def test_asset_freeze_interrupted_by_soft_time_limit_is_held_for_review(self):
        for case in self.CASES:
            with self.subTest(case=case):
                proposal = _close(make_asset_proposal(asset_code=f'TK{next(_asset_codes)}'))
                replacement, _ = self._melted_vote(proposal, case)
                server_patch, records_patch = self._horizon_patches(
                    proposal, replacement, _soft_time_limit_on_first_horizon_call(),
                )

                with server_patch, records_patch, patch(f'{TASKS}.update_proposal_final_results') as finalize:
                    task_update_proposal_results(proposal.pk, True)

                finalize.assert_not_called()
                proposal.refresh_from_db()
                self.assertEqual(proposal.onchain_execution_status, Proposal.ONCHAIN_EXECUTION_REQUIRES_REVIEW)
                self.assertEqual(proposal.asset_token.contract_sync_status, AssetToken.CONTRACT_SYNC_REQUIRES_REVIEW)
                LogVote.objects.filter(proposal=proposal).delete()

    def test_general_freeze_interrupted_by_soft_time_limit_is_retried(self):
        for case in self.CASES:
            with self.subTest(case=case):
                proposal = _closed_general(ended_ago=timedelta(days=2))
                replacement, _ = self._melted_vote(proposal, case)
                server_patch, records_patch = self._horizon_patches(
                    proposal, replacement, _soft_time_limit_on_first_horizon_call(),
                )

                with server_patch, records_patch, patch(
                    f'{TASKS}.update_proposal_votes_snapshot', wraps=update_proposal_votes_snapshot,
                ) as snapshot, patch(f'{TASKS}.update_proposal_final_results') as finalize:
                    result = task_update_proposal_results.apply(args=(proposal.pk, True), throw=False)

                self.assertTrue(result.successful())
                self.assertEqual(snapshot.call_count, 2)
                finalize.assert_called_once_with(proposal.pk)
                LogVote.objects.filter(proposal=proposal).delete()


class UnresolvableGroupTests(TestCase):
    DEPTH = 125

    def setUp(self):
        self.proposal, self.live, self.gone, live_raw = _proposal_with_live_and_gone_votes()
        self.deep_raw = _raw_vote(self.proposal, _chain_id(self.DEPTH), asset_code=settings.GOVERNANCE_ICE_ASSET_CODE)
        self.unresolved = _vote(
            self.proposal, claimable_balance_id='unrelated-stored', asset_code=settings.GOVERNANCE_ICE_ASSET_CODE,
            key=generate_vote_key(self.deep_raw, self.proposal, LogVote.VOTE_FOR),
        )
        self.horizon = _ChainHorizon.linear(self.DEPTH, self.proposal.end_at - timedelta(days=3))
        balances = {self.proposal.vote_for_issuer: [live_raw, self.deep_raw]}
        self.records_patch = patch(
            f'{INDEXING}.load_all_records', side_effect=_load_balances_by_claimant(balances, []),
        )

    def test_unresolvable_group_is_left_untouched_while_the_rest_syncs(self):
        with patch(f'{TASKS}.Server', return_value=self.horizon), self.records_patch:
            task_sync_closed_proposal_claims()

        self.live.refresh_from_db()
        self.gone.refresh_from_db()
        self.unresolved.refresh_from_db()
        self.assertEqual(self.live.amount, Decimal('900'))
        self.assertTrue(self.gone.claimed)
        self.assertEqual(self.unresolved.claimable_balance_id, 'unrelated-stored')
        self.assertFalse(self.unresolved.claimed)
        self.assertEqual(self.proposal.logvote_set.count(), 3)

    def test_non_strict_snapshot_reports_unresolved_groups(self):
        with self.records_patch:
            unresolved_groups = update_proposal_votes_snapshot(
                proposal=self.proposal, horizon_server=self.horizon, strict=False,
            )

        self.assertEqual(unresolved_groups, 1)

    def test_freeze_stays_strict(self):
        before = list(self.proposal.logvote_set.order_by('pk').values())
        with self.records_patch, self.assertRaises(IncompleteVoteSnapshot):
            update_proposal_votes_snapshot(proposal=self.proposal, horizon_server=self.horizon, freezing_amount=True)

        self.assertEqual(list(self.proposal.logvote_set.order_by('pk').values()), before)


class HiddenLineageTests(TestCase):
    DEPTH = 125

    def test_replacement_of_a_hidden_late_vote_is_excluded_without_an_origin_trace(self):
        proposal, live, gone, live_raw = _proposal_with_live_and_gone_votes()
        replacement = _raw_vote(proposal, _chain_id(self.DEPTH), asset_code=settings.GOVERNANCE_ICE_ASSET_CODE)
        hidden = _vote(
            proposal, claimable_balance_id=_chain_id(self.DEPTH - 1), hide=True,
            asset_code=settings.GOVERNANCE_ICE_ASSET_CODE, created_at=proposal.end_at + timedelta(hours=1),
            key=generate_vote_key(replacement, proposal, LogVote.VOTE_FOR),
        )
        hidden_before = LogVote.objects.filter(pk=hidden.pk).values().get()
        horizon = _ChainHorizon.linear(self.DEPTH, proposal.end_at + timedelta(hours=1))
        balances = {proposal.vote_for_issuer: [live_raw, replacement]}

        with patch(f'{TASKS}.Server', return_value=horizon), patch(
            f'{INDEXING}.load_all_records', side_effect=_load_balances_by_claimant(balances, []),
        ), patch(
            f'{INDEXING}.find_origin_claimable_balance_id', wraps=find_origin_claimable_balance_id,
        ) as origin_trace:
            task_sync_closed_proposal_claims()

        origin_trace.assert_not_called()
        self.assertEqual(horizon.operations_calls, 2)
        self.assertEqual(LogVote.objects.filter(pk=hidden.pk).values().get(), hidden_before)
        self.assertFalse(proposal.logvote_set.filter(claimable_balance_id=replacement['id']).exists())
        live.refresh_from_db()
        gone.refresh_from_db()
        self.assertEqual(live.amount, Decimal('900'))
        self.assertTrue(gone.claimed)


class HorizonCallBudgetTests(TestCase):
    def test_origin_found_by_the_lineage_walk_is_not_traced_again(self):
        proposal = _closed_general(ended_ago=timedelta(days=2))
        stored_replacement = _raw_vote(proposal, 'a-2', amount='900')
        unstored_replacement = _raw_vote(proposal, 'b-3', amount='800')
        stored = _vote(
            proposal, claimable_balance_id='a-1',
            key=generate_vote_key(stored_replacement, proposal, LogVote.VOTE_FOR),
        )
        parents = {'a-1': None, 'a-2': 'a-1', 'b-0': None, 'b-1': 'b-0', 'b-2': 'b-1', 'b-3': 'b-2'}
        horizon = _ChainHorizon(parents, proposal.end_at - timedelta(days=3))

        with patch(f'{TASKS}.Server', return_value=horizon), patch(
            f'{INDEXING}.load_all_records', side_effect=[[stored_replacement, unstored_replacement], [], []],
        ):
            task_sync_closed_proposal_claims()

        stored.refresh_from_db()
        self.assertEqual(stored.claimable_balance_id, 'a-2')
        added = proposal.logvote_set.get(claimable_balance_id='b-3')
        self.assertEqual(added.created_at, horizon.created_at)
        # a-2: 1 step (2 calls); b-3: 3 steps back to its own create b-0 (4 balance + 3 transaction calls);
        # b-3 metadata: 1 call for b-0's create operation, with the origin reused from the walk.
        self.assertEqual(horizon.operations_calls, 10)

    def test_batched_melt_transaction_is_loaded_once_per_snapshot(self):
        proposal = _closed_general(ended_ago=timedelta(days=2))
        ice_replacement = _raw_vote(proposal, 'ice-2', asset_code=settings.GOVERNANCE_ICE_ASSET_CODE)
        gdice_replacement = _raw_vote(proposal, 'gdice-2')
        ice = _vote(
            proposal, claimable_balance_id='ice-1', asset_code=settings.GOVERNANCE_ICE_ASSET_CODE,
            key=generate_vote_key(ice_replacement, proposal, LogVote.VOTE_FOR),
        )
        gdice = _vote(
            proposal, claimable_balance_id='gdice-1',
            key=generate_vote_key(gdice_replacement, proposal, LogVote.VOTE_FOR),
        )
        horizon = _ChainHorizon(
            {'ice-1': None, 'ice-2': 'ice-1', 'gdice-1': None, 'gdice-2': 'gdice-1'},
            proposal.end_at - timedelta(days=3),
            transactions={'ice-2': 'melt-batch', 'gdice-2': 'melt-batch'},
        )

        with patch(f'{TASKS}.Server', return_value=horizon), patch(
            f'{INDEXING}.load_all_records', side_effect=[[ice_replacement, gdice_replacement], [], []],
        ):
            task_sync_closed_proposal_claims()

        ice.refresh_from_db()
        gdice.refresh_from_db()
        self.assertEqual((ice.claimable_balance_id, gdice.claimable_balance_id), ('ice-2', 'gdice-2'))
        self.assertEqual(horizon.transaction_calls, 1)
        self.assertEqual(horizon.operations_calls, 3)


class SyncClosedProposalClaimsCommandTests(TestCase):
    DEPTH = 125

    def setUp(self):
        self.failing, self.failing_live, _, failing_raw = _proposal_with_live_and_gone_votes()
        self.unresolved, _, _, unresolved_raw = _proposal_with_live_and_gone_votes()
        deep_raw = _raw_vote(self.unresolved, _chain_id(self.DEPTH), asset_code=settings.GOVERNANCE_ICE_ASSET_CODE)
        _vote(
            self.unresolved, claimable_balance_id='unrelated-stored', asset_code=settings.GOVERNANCE_ICE_ASSET_CODE,
            key=generate_vote_key(deep_raw, self.unresolved, LogVote.VOTE_FOR),
        )
        self.synced, self.synced_live, self.synced_gone, synced_raw = _proposal_with_live_and_gone_votes()
        self.voting = _closed_general(status=Proposal.VOTING)
        _vote(self.voting)
        balances = {
            self.failing.vote_for_issuer: [failing_raw],
            self.unresolved.vote_for_issuer: [unresolved_raw, deep_raw],
            self.synced.vote_for_issuer: [synced_raw],
        }
        load = _load_balances_by_claimant(balances, [])

        def load_or_fail(request_builder):
            if request_builder.claimant == self.failing.vote_for_issuer:
                raise ConnectionError('horizon unavailable')
            return load(request_builder)

        self.horizon = _ChainHorizon.linear(self.DEPTH, self.synced.end_at - timedelta(days=3))
        self.load_or_fail = load_or_fail

    def _call(self, *args):
        stdout = StringIO()
        with patch(
            'aqua_governance.governance.management.commands.sync_closed_proposal_claims.Server',
            return_value=self.horizon,
        ), patch(f'{INDEXING}.load_all_records', side_effect=self.load_or_fail):
            call_command('sync_closed_proposal_claims', *args, stdout=stdout)
        return stdout.getvalue().splitlines()

    def test_reports_every_selected_proposal_newest_first(self):
        self.assertEqual(self._call(), [
            f'proposal {self.synced.pk}: synced (unresolved groups: 0)',
            f'proposal {self.unresolved.pk}: synced (unresolved groups: 1)',
            f"proposal {self.failing.pk}: error: ConnectionError('horizon unavailable')",
        ])
        self.synced_live.refresh_from_db()
        self.synced_gone.refresh_from_db()
        self.failing_live.refresh_from_db()
        self.assertEqual(self.synced_live.amount, Decimal('900'))
        self.assertTrue(self.synced_gone.claimed)
        self.assertEqual(self.failing_live.amount, Decimal('1000'))

    def test_requested_proposals_go_through_the_task_selection(self):
        lines = self._call('--proposal-id', str(self.unresolved.pk), '--proposal-id', str(self.voting.pk))

        self.assertEqual(lines, [
            f'proposal {self.unresolved.pk}: synced (unresolved groups: 1)',
            f'proposal {self.voting.pk}: not selected',
        ])
        self.synced_live.refresh_from_db()
        self.assertEqual(self.synced_live.amount, Decimal('1000'))
