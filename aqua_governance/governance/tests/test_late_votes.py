from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock, patch

from django.conf import settings
from django.test import TestCase
from django.utils import timezone

from rest_framework.test import APIClient

from requests.exceptions import ConnectionError

from aqua_governance.governance.models import AssetToken, LogVote, Proposal
from aqua_governance.governance.task_logic.proposal_finalization import (
    _sum_votes_for_proposal,
    update_proposal_final_results,
)
from aqua_governance.governance.task_logic.unlock_rules import get_expected_unlock_timestamp
from aqua_governance.governance.task_logic.vote_indexing import (
    IncompleteVoteSnapshot,
    _make_new_vote,
    reconcile_vote_group,
    update_proposal_votes_snapshot,
)
from aqua_governance.governance.tasks import task_retry_failed_onchain_executions, task_update_proposal_results
from aqua_governance.governance.tests._factories import (
    DEFAULT_PROPOSED_BY,
    SECONDARY_ACCOUNT,
    _create_proposal,
    make_asset_proposal,
)


INDEXING = 'aqua_governance.governance.task_logic.vote_indexing'


class LateVoteTests(TestCase):
    def setUp(self):
        self.end = timezone.now() - timedelta(days=1)
        self.proposal = _create_proposal(proposal_type=Proposal.PROPOSAL_TYPE_GENERAL)
        Proposal.objects.filter(pk=self.proposal.pk).update(end_at=self.end, proposal_status=Proposal.VOTED)
        self.proposal.refresh_from_db()

    def raw_vote(self, balance_id='new', service=False):
        return {
            'id': balance_id,
            'asset': f'{settings.GDICE_ASSET_CODE}:{settings.GDICE_ASSET_ISSUER}',
            'amount': '90',
            'sponsor': SECONDARY_ACCOUNT if service else DEFAULT_PROPOSED_BY,
            'claimants': [{
                'destination': DEFAULT_PROPOSED_BY,
                'predicate': {'not': {'abs_before': (self.end + timedelta(days=1)).isoformat()}},
            }],
            'last_modified_time': (self.end + timedelta(hours=1)).isoformat(),
            '_links': {'transactions': {'href': 'https://example.com/transactions'}},
        }

    def server(self, created_at, duplicate=False):
        record = {'type': 'create_claimable_balance', 'created_at': created_at, 'amount': '120'}
        server = Mock()
        server.operations.return_value.for_claimable_balance.return_value.order.return_value.limit.return_value.call.return_value = {  # noqa: E501
            '_embedded': {'records': [record, record] if duplicate else [record]},
        }
        return server

    def new_vote(self, created_at, service=False, origin='original', duplicate=False):
        with patch(f'{INDEXING}.find_origin_claimable_balance_id', return_value=origin):
            return _make_new_vote(
                'key', 0, self.raw_vote(service=service), self.proposal, LogVote.VOTE_FOR, True,
                horizon_server=self.server(created_at, duplicate=duplicate), restore_from_origin=service,
            )

    def stored_vote(self, balance_id='old', **kwargs):
        fields = {
            'proposal': self.proposal, 'claimable_balance_id': balance_id, 'created_at': self.end,
            'amount': Decimal('90'), 'original_amount': Decimal('120'), 'voted_amount': Decimal('100'),
            'key': 'key', 'account_issuer': DEFAULT_PROPOSED_BY,
            'asset_code': settings.GDICE_ASSET_CODE, 'vote_choice': LogVote.VOTE_FOR,
        }
        fields.update(kwargs)
        return LogVote.objects.create(**fields)

    def test_creation_at_deadline_is_accepted_but_after_is_not(self):
        self.assertIsNotNone(self.new_vote(self.end.isoformat()))
        self.assertIsNone(self.new_vote((self.end + timedelta(seconds=1)).isoformat()))

    def test_missing_ambiguous_and_naive_original_date_are_not_accepted(self):
        for date, duplicate in [(None, False), (self.end.isoformat(), True), ('2026-01-01', False)]:
            with self.subTest(date=date, duplicate=duplicate):
                self.assertIsNone(self.new_vote(date, duplicate=duplicate))

    def test_service_creation_uses_origin_deadline_and_original_amount(self):
        on_time = self.new_vote(self.end.isoformat(), service=True)
        self.assertIsNotNone(on_time)
        self.assertEqual(Decimal(on_time.original_amount), Decimal('120'))
        self.assertIsNone(self.new_vote((self.end + timedelta(seconds=1)).isoformat(), service=True))
        self.assertIsNone(self.new_vote(self.end.isoformat(), service=True, origin=None))

    def test_late_service_replacement_cannot_inherit_existing_eligible_date(self):
        old = self.stored_vote()
        with patch(f'{INDEXING}.find_origin_claimable_balance_id', return_value='origin'):
            new, updated, _ = reconcile_vote_group(
                'key', [(LogVote.VOTE_FOR, self.raw_vote(service=True))], [old],
                self.proposal.logvote_set.all(), self.proposal, False,
                horizon_server=self.server((self.end + timedelta(seconds=1)).isoformat()),
            )
        self.assertEqual((new, updated), ([], []))

    def test_unknown_service_metadata_preserves_old_row_for_retry(self):
        old = self.stored_vote()
        with patch(f'{INDEXING}.find_origin_claimable_balance_id', return_value=None):
            new, updated, processed = reconcile_vote_group(
                'key', [(LogVote.VOTE_FOR, self.raw_vote(service=True))], [old],
                self.proposal.logvote_set.all(), self.proposal, False,
                horizon_server=self.server(self.end.isoformat()),
            )
        self.assertEqual((new, updated), ([], []))
        self.assertIn(old.pk, processed)

    def test_valid_service_replacement_preserves_frozen_weight_and_original_date(self):
        old = self.stored_vote(created_at=self.end - timedelta(days=1))
        with patch(f'{INDEXING}.find_origin_claimable_balance_id', return_value='original'):
            new, updated, processed = reconcile_vote_group(
                'key', [(LogVote.VOTE_FOR, self.raw_vote(service=True))], [old],
                self.proposal.logvote_set.all(), self.proposal, False,
                horizon_server=self.server(old.created_at.isoformat()),
            )
        self.assertEqual(new, [])
        self.assertEqual(updated[0].claimable_balance_id, 'new')
        self.assertEqual(updated[0].voted_amount, old.voted_amount)
        self.assertEqual(updated[0].created_at, str(old.created_at))
        self.assertEqual(processed, {old.pk})

    def test_repeated_indexing_does_not_restore_hidden_late_balance(self):
        late = self.stored_vote('new', hide=True, created_at=self.end + timedelta(seconds=1))
        raw = self.raw_vote()
        raw['claimants'][0]['predicate']['not']['abs_before'] = str(get_expected_unlock_timestamp(self.proposal))
        server = self.server(late.created_at.isoformat())
        for _ in range(2):
            with patch(f'{INDEXING}.load_all_records', side_effect=[[raw], [], []]):
                update_proposal_votes_snapshot(self.proposal, server)
        self.assertEqual(list(self.proposal.logvote_set.values_list('pk', 'hide')), [(late.pk, True)])

    def test_no_deadline_preserves_legacy_metadata_fallback(self):
        self.proposal.end_at = None
        self.assertIsNotNone(self.new_vote(None))

    def test_unsupported_sponsored_assets_do_not_block_finalization(self):
        for asset in [
            f'SPAM:{settings.GDICE_ASSET_ISSUER}',
            f'{settings.GDICE_ASSET_CODE}:{DEFAULT_PROPOSED_BY}',
        ]:
            with self.subTest(asset=asset):
                proposal = make_asset_proposal()
                Proposal.objects.filter(pk=proposal.pk).update(end_at=self.end, proposal_status=Proposal.VOTED)
                proposal.refresh_from_db()
                raw = self.raw_vote(service=True)
                raw['asset'] = asset
                raw['claimants'][0]['predicate']['not']['abs_before'] = str(get_expected_unlock_timestamp(proposal))
                with patch(f'{INDEXING}.load_all_records', side_effect=[[raw], [], []]), patch(
                    f'{INDEXING}.find_origin_claimable_balance_id', return_value=None,
                ) as origin, patch('aqua_governance.governance.tasks.Server'), patch(
                    'aqua_governance.governance.tasks.update_proposal_final_results',
                ) as finalize:
                    task_update_proposal_results(proposal.pk, True)
                origin.assert_not_called()
                finalize.assert_called_once_with(proposal.pk)
                self.assertFalse(proposal.logvote_set.exists())
                proposal.refresh_from_db()
                self.assertEqual(proposal.onchain_execution_status, Proposal.ONCHAIN_EXECUTION_PENDING)

    def test_supported_sponsored_assets_with_unknown_origin_still_hold_finalization(self):
        for asset in [
            f'{settings.AQUA_ASSET_CODE}:{settings.AQUA_ASSET_ISSUER}',
            f'{settings.GOVERNANCE_ICE_ASSET_CODE}:{settings.GOVERNANCE_ICE_ASSET_ISSUER}',
            f'{settings.GDICE_ASSET_CODE}:{settings.GDICE_ASSET_ISSUER}',
        ]:
            with self.subTest(asset=asset):
                proposal = make_asset_proposal()
                Proposal.objects.filter(pk=proposal.pk).update(end_at=self.end, proposal_status=Proposal.VOTED)
                proposal.refresh_from_db()
                raw = self.raw_vote(service=True)
                raw['asset'] = asset
                raw['claimants'][0]['predicate']['not']['abs_before'] = str(get_expected_unlock_timestamp(proposal))
                with patch(f'{INDEXING}.load_all_records', side_effect=[[raw], [], []]), patch(
                    f'{INDEXING}.find_origin_claimable_balance_id', return_value=None,
                ) as origin, patch('aqua_governance.governance.tasks.Server'), patch(
                    'aqua_governance.governance.tasks.update_proposal_final_results',
                ) as finalize:
                    task_update_proposal_results(proposal.pk, True)
                origin.assert_called_once()
                finalize.assert_not_called()
                proposal.refresh_from_db()
                self.assertEqual(proposal.onchain_execution_status, Proposal.ONCHAIN_EXECUTION_REQUIRES_REVIEW)

    def test_unknown_provenance_rolls_back_the_entire_freeze(self):
        old = self.stored_vote()
        before = list(self.proposal.logvote_set.values())
        groups = {
            'key': [(LogVote.VOTE_FOR, self.raw_vote('old'))],
            'mixed': [(LogVote.VOTE_FOR, self.raw_vote('valid-new')),
                      (LogVote.VOTE_FOR, self.raw_vote('unknown', service=True))],
        }
        with patch(f'{INDEXING}._build_raw_vote_groups', return_value=groups), patch(
            f'{INDEXING}._load_original_metadata', side_effect=[(self.end, '120'), None],
        ), self.assertRaisesRegex(RuntimeError, 'metadata'):
            update_proposal_votes_snapshot(self.proposal, Mock(), freezing_amount=True)
        self.assertEqual(list(self.proposal.logvote_set.values()), before)
        old.refresh_from_db()
        self.assertEqual(old.voted_amount, Decimal('100'))

    def test_unknown_self_or_service_blocks_finalization_and_retries(self):
        for service, freezing in [(False, True), (True, True), (False, False), (True, False)]:
            with self.subTest(service=service, freezing=freezing):
                proposal = make_asset_proposal()
                Proposal.objects.filter(pk=proposal.pk).update(
                    end_at=self.end, proposal_status=Proposal.VOTED,
                )
                groups = {'key': [(LogVote.VOTE_FOR, self.raw_vote(service=service))]}
                with patch(f'{INDEXING}._build_raw_vote_groups', return_value=groups), patch(
                    f'{INDEXING}._load_original_metadata', return_value=None,
                ), patch('aqua_governance.governance.tasks.Server'), patch(
                    'aqua_governance.governance.tasks.update_proposal_final_results',
                ) as finalize, patch(
                    'aqua_governance.governance.tasks.task_execute_onchain_action_send.delay',
                ) as enqueue:
                    task_update_proposal_results(proposal.pk, freezing)
                    task_retry_failed_onchain_executions()
                finalize.assert_not_called()
                enqueue.assert_not_called()
                proposal.refresh_from_db()
                self.assertEqual(proposal.onchain_execution_status, Proposal.ONCHAIN_EXECUTION_REQUIRES_REVIEW)
                self.assertEqual(proposal.asset_token.contract_sync_status, AssetToken.CONTRACT_SYNC_REQUIRES_REVIEW)

    def test_incomplete_snapshot_preserves_submitted_success_and_execution_markers(self):
        for status, fields in [
            (Proposal.ONCHAIN_EXECUTION_SUBMITTED, {'onchain_execution_tx_hash': 'submitted-tx'}),
            (Proposal.ONCHAIN_EXECUTION_SUCCESS, {}),
            (Proposal.ONCHAIN_EXECUTION_PENDING, {'onchain_execution_tx_hash': 'unexpected-tx'}),
            (Proposal.ONCHAIN_EXECUTION_IN_PROGRESS, {'onchain_execution_started_at': self.end}),
        ]:
            with self.subTest(status=status):
                proposal = make_asset_proposal()
                Proposal.objects.filter(pk=proposal.pk).update(
                    end_at=self.end, proposal_status=Proposal.VOTED, onchain_execution_status=status, **fields,
                )
                before = Proposal.objects.filter(pk=proposal.pk).values().get()
                token_before = AssetToken.objects.filter(pk=proposal.asset_token_id).values().get()
                groups = {'key': [(LogVote.VOTE_FOR, self.raw_vote())]}
                with patch(f'{INDEXING}._build_raw_vote_groups', return_value=groups), patch(
                    f'{INDEXING}._load_original_metadata', return_value=None,
                ), patch('aqua_governance.governance.tasks.Server'), patch(
                    'aqua_governance.governance.tasks.update_proposal_final_results',
                ) as finalize:
                    task_update_proposal_results(proposal.pk, True)
                finalize.assert_not_called()
                self.assertEqual(Proposal.objects.filter(pk=proposal.pk).values().get(), before)
                self.assertEqual(AssetToken.objects.filter(pk=proposal.asset_token_id).values().get(), token_before)

    def test_metadata_transport_failure_holds_execution_without_finalizing(self):
        proposal = make_asset_proposal()
        Proposal.objects.filter(pk=proposal.pk).update(end_at=self.end, proposal_status=Proposal.VOTED)
        server = self.server(self.end.isoformat())
        server.operations.return_value.for_claimable_balance.return_value.order.return_value.limit.return_value.call.side_effect = ConnectionError('unavailable')  # noqa: E501
        groups = {'key': [(LogVote.VOTE_FOR, self.raw_vote())]}
        with patch(f'{INDEXING}._build_raw_vote_groups', return_value=groups), patch(
            'aqua_governance.governance.tasks.Server', return_value=server,
        ), patch('aqua_governance.governance.tasks.update_proposal_final_results') as finalize:
            task_update_proposal_results(proposal.pk, True)
        finalize.assert_not_called()
        proposal.refresh_from_db()
        self.assertEqual(proposal.onchain_execution_status, Proposal.ONCHAIN_EXECUTION_REQUIRES_REVIEW)

    def test_general_final_snapshot_retries_and_freezes_complete_results(self):
        old = self.stored_vote(key='old-key', voted_amount=None)
        groups = {
            'old-key': [(LogVote.VOTE_FOR, self.raw_vote('old'))],
            'new-key': [(LogVote.VOTE_AGAINST, self.raw_vote('new'))],
        }
        with patch(f'{INDEXING}._build_raw_vote_groups', return_value=groups), patch(
            f'{INDEXING}._load_original_metadata', side_effect=[None, (self.end, '120')],
        ), patch('aqua_governance.governance.tasks.Server'), patch(
            'aqua_governance.governance.tasks.update_proposal_votes_snapshot',
            wraps=update_proposal_votes_snapshot,
        ) as snapshot, patch(
            'aqua_governance.governance.tasks.update_proposal_final_results',
            wraps=update_proposal_final_results,
        ) as finalize, patch(
            'aqua_governance.governance.task_logic.proposal_finalization._update_ice_circulating_supply',
            return_value=True,
        ), patch('aqua_governance.governance.tasks.task_execute_onchain_action_send.delay') as enqueue:
            result = task_update_proposal_results.apply(
                kwargs={'proposal_id': self.proposal.pk, 'freezing_amount': True}, throw=False,
            )
        self.assertTrue(result.successful())
        self.assertEqual(snapshot.call_count, 2)
        self.assertEqual(
            [(call.kwargs['proposal'].pk, call.kwargs['freezing_amount']) for call in snapshot.call_args_list],
            [(self.proposal.pk, True), (self.proposal.pk, True)],
        )
        finalize.assert_called_once_with(self.proposal.pk)
        enqueue.assert_not_called()
        self.proposal.refresh_from_db()
        old.refresh_from_db()
        new = self.proposal.logvote_set.get(claimable_balance_id='new')
        self.assertEqual(old.voted_amount, Decimal('90'))
        self.assertEqual(new.voted_amount, Decimal('90'))
        self.assertEqual(self.proposal.vote_for_result, Decimal('90'))
        self.assertEqual(self.proposal.vote_against_result, Decimal('90'))

    def test_general_final_snapshot_retry_exhaustion_fails_without_finalizing(self):
        with patch('aqua_governance.governance.tasks.Server'), patch(
            'aqua_governance.governance.tasks.update_proposal_votes_snapshot',
            side_effect=IncompleteVoteSnapshot('metadata unavailable'),
        ) as snapshot, patch('aqua_governance.governance.tasks.update_proposal_final_results') as finalize:
            result = task_update_proposal_results.apply(args=(self.proposal.pk, True), throw=False)
        self.assertTrue(result.failed())
        self.assertIsInstance(result.result, IncompleteVoteSnapshot)
        self.assertEqual(snapshot.call_count, task_update_proposal_results.max_retries + 1)
        finalize.assert_not_called()

    def test_incomplete_asset_final_snapshot_is_held_without_retry(self):
        proposal = make_asset_proposal()
        Proposal.objects.filter(pk=proposal.pk).update(end_at=self.end, proposal_status=Proposal.VOTED)
        with patch('aqua_governance.governance.tasks.Server'), patch(
            'aqua_governance.governance.tasks.update_proposal_votes_snapshot',
            side_effect=IncompleteVoteSnapshot('metadata unavailable'),
        ) as snapshot, patch('aqua_governance.governance.tasks.update_proposal_final_results') as finalize:
            result = task_update_proposal_results.apply(args=(proposal.pk, True), throw=False)
        self.assertTrue(result.successful())
        snapshot.assert_called_once()
        finalize.assert_not_called()
        proposal.refresh_from_db()
        self.assertEqual(proposal.onchain_execution_status, Proposal.ONCHAIN_EXECUTION_REQUIRES_REVIEW)

    def test_general_incomplete_nonfinal_snapshot_does_not_retry(self):
        for status, freezing in [(Proposal.VOTING, False), (Proposal.VOTED, False), (Proposal.VOTING, True)]:
            with self.subTest(status=status, freezing=freezing):
                Proposal.objects.filter(pk=self.proposal.pk).update(proposal_status=status)
                with patch('aqua_governance.governance.tasks.Server'), patch(
                    'aqua_governance.governance.tasks.update_proposal_votes_snapshot',
                    side_effect=IncompleteVoteSnapshot('metadata unavailable'),
                ) as snapshot, patch('aqua_governance.governance.tasks.update_proposal_final_results') as finalize:
                    result = task_update_proposal_results.apply(args=(self.proposal.pk, freezing), throw=False)
                self.assertTrue(result.successful())
                snapshot.assert_called_once()
                finalize.assert_not_called()

    def test_frozen_and_live_tallies_exclude_late_and_unknown_votes_for_all_choices(self):
        for index, choice in enumerate([LogVote.VOTE_FOR, LogVote.VOTE_AGAINST, LogVote.VOTE_ABSTAIN]):
            self.stored_vote(f'valid-{index}', vote_choice=choice, claimed=True)
            self.stored_vote(f'late-{index}', vote_choice=choice, created_at=self.end + timedelta(seconds=1))
            self.stored_vote(f'unknown-{index}', vote_choice=choice, created_at=None)
            self.assertEqual(_sum_votes_for_proposal(self.proposal, choice), Decimal('100'))
        self.proposal.proposal_status = Proposal.VOTING
        self.assertEqual(_sum_votes_for_proposal(self.proposal, LogVote.VOTE_FOR), Decimal('0'))

    def test_public_owner_and_active_filters_require_the_same_eligible_vote(self):
        self.stored_vote('late', created_at=self.end + timedelta(seconds=1))
        self.stored_vote('other', account_issuer=SECONDARY_ACCOUNT)
        self.stored_vote('claimed', claimed=True)
        client = APIClient()
        response = client.get('/api/proposal/', {'vote_owner_public_key': DEFAULT_PROPOSED_BY, 'active': 'true'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['results'], [])
        response = client.get('/api/proposal/', {'vote_owner_public_key': DEFAULT_PROPOSED_BY})
        votes = response.json()['results'][0]['logvote_set']
        self.assertEqual([vote['claimable_balance_id'] for vote in votes], ['claimed'])
        response = client.get('/api/votes-for-proposal/', {'proposal_id': self.proposal.pk})
        self.assertEqual({vote['claimable_balance_id'] for vote in response.json()['results']}, {'other', 'claimed'})
