from datetime import datetime, timedelta, timezone as datetime_timezone
from decimal import Decimal
from unittest.mock import Mock, patch

from django.conf import settings
from django.test import TestCase
from rest_framework.test import APIClient

from aqua_governance.governance.models import HistoryProposal, LogVote, Proposal, ProposalQueueSlot
from aqua_governance.governance.proposal_queue import get_queue_week_start
from aqua_governance.governance.tasks import task_sync_proposal_statuses_by_time, task_update_proposal_results
from aqua_governance.governance.tests._factories import (
    DEFAULT_PROPOSED_BY,
    SECONDARY_ACCOUNT,
    TERTIARY_ACCOUNT,
    patch_ice_circulating_supply,
)


FIXED_NOW = datetime(2024, 1, 10, 12, 0, tzinfo=datetime_timezone.utc)


def _iso_z(value):
    return value.isoformat().replace('+00:00', 'Z')


def _expected_vote_key(*, proposal_id, vote_choice, account_issuer, asset_code, time_list):
    return f"{proposal_id}|{vote_choice}|{account_issuer}|{asset_code}|{sorted(time_list)}"


class FakeHorizonRequestBuilder:
    def __init__(self, records_by_key, *, key_name):
        self._records_by_key = records_by_key
        self._key_name = key_name
        self._key = None
        self._limit = None
        self._cursor = None

    def for_claimant(self, claimant):
        self._key = claimant
        return self

    def for_claimable_balance(self, balance_id):
        self._key = balance_id
        return self

    def order(self, desc=False):
        return self

    def limit(self, limit):
        self._limit = limit
        return self

    def cursor(self, cursor):
        self._cursor = cursor
        return self

    def call(self):
        records = list(self._records_by_key.get(self._key, []))
        if self._cursor is not None:
            records = [record for record in records if record['paging_token'] > self._cursor]
        if self._limit is not None:
            records = records[:self._limit]
        return {'_embedded': {'records': records}}


class FakeHorizonServer:
    def __init__(self, *, claimable_balances_by_claimant, operations_by_balance_id):
        self._claimable_balances_by_claimant = claimable_balances_by_claimant
        self._operations_by_balance_id = operations_by_balance_id

    def claimable_balances(self):
        return FakeHorizonRequestBuilder(self._claimable_balances_by_claimant, key_name='claimant')

    def operations(self):
        return FakeHorizonRequestBuilder(self._operations_by_balance_id, key_name='claimable_balance')


class FullVotingLifecycleTests(TestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.fixed_now = FIXED_NOW

        self.ice_supply_patcher = patch_ice_circulating_supply(amount=10000)
        self.ice_supply_patcher.start()
        self.addCleanup(self.ice_supply_patcher.stop)

        self.finalization_ice_response = Mock()
        self.finalization_ice_response.status_code = 200
        self.finalization_ice_response.json.return_value = {'ice_supply_amount': '10000'}
        self.finalization_ice_patcher = patch(
            'aqua_governance.governance.task_logic.proposal_finalization.requests.get',
            return_value=self.finalization_ice_response,
        )
        self.finalization_ice_patcher.start()
        self.addCleanup(self.finalization_ice_patcher.stop)

    def _queue_slot(self, *, weeks_ahead=1, base_now=None):
        start_at = get_queue_week_start(base_now or self.fixed_now) + timedelta(weeks=weeks_ahead)
        end_at = start_at + timedelta(days=7, seconds=-1)
        return start_at, end_at

    def _claimable_balance(self, *, balance_id, amount, asset, vote_destination, voter_account, unlock_at, sponsor=None):
        return {
            'id': balance_id,
            'paging_token': balance_id,
            'asset': asset,
            'amount': amount,
            'sponsor': sponsor or voter_account,
            'last_modified_time': _iso_z(unlock_at - timedelta(hours=1)),
            'claimants': [
                {
                    'destination': vote_destination,
                    'predicate': {},
                },
                {
                    'destination': voter_account,
                    'predicate': {
                        'not': {
                            'abs_before': _iso_z(unlock_at),
                            'abs_before_epoch': str(round(unlock_at.timestamp())),
                        },
                    },
                },
            ],
            '_links': {
                'transactions': {
                    'href': 'https://horizon.example/transactions/{?cursor,limit,order}',
                },
            },
        }

    def _create_claimable_balance_operation(self, *, amount, created_at):
        return {
            'paging_token': f'op-{created_at.timestamp()}',
            'type': 'create_claimable_balance',
            'amount': amount,
            'created_at': _iso_z(created_at),
        }

    @patch('aqua_governance.governance.views.ProposalViewSet._check_owner_permissions')
    @patch('aqua_governance.governance.proposal_transactions.check_proposal_status', return_value=Proposal.FINE)
    @patch('aqua_governance.governance.serializers_v2.check_transaction_xdr', return_value=Proposal.FINE)
    def test_general_proposal_full_queue_vote_and_finalize_with_fake_horizon_votes_and_mocked_payment_checks(
        self,
        _mock_check_xdr,
        _mock_check_status,
        _mock_owner_permissions,
    ):
        """Payment/XDR/ownership checks are mocked here; dedicated tests cover real validation paths."""
        create_response = self.client.post(
            '/api/proposal/',
            {
                'proposed_by': DEFAULT_PROPOSED_BY,
                'title': 'General lifecycle proposal',
                'text': '<p>Deterministic full lifecycle</p>',
                'transaction_hash': 'a' * 64,
                'envelope_xdr': 'create-xdr',
                'discord_username': 'tester',
                'proposal_type': Proposal.PROPOSAL_TYPE_GENERAL,
            },
            format='json',
        )

        self.assertEqual(create_response.status_code, 201)
        proposal = Proposal.objects.get(id=create_response.data['id'])
        self.assertTrue(proposal.draft)
        self.assertEqual(proposal.action, Proposal.TO_CREATE)
        self.assertEqual(proposal.proposal_status, Proposal.DISCUSSION)
        self.assertEqual(proposal.payment_status, Proposal.FINE)
        self.assertEqual(proposal.onchain_execution_status, Proposal.ONCHAIN_EXECUTION_NOT_REQUIRED)
        self.assertIsNone(proposal.start_at)
        self.assertIsNone(proposal.end_at)
        self.assertFalse(ProposalQueueSlot.objects.filter(proposal=proposal).exists())

        creation_payment_response = self.client.post(f'/api/proposal/{proposal.id}/check_payment/', {}, format='json')

        self.assertEqual(creation_payment_response.status_code, 200)
        proposal.refresh_from_db()
        self.assertFalse(proposal.draft)
        self.assertEqual(proposal.action, Proposal.NONE)
        self.assertEqual(proposal.proposal_status, Proposal.DISCUSSION)
        self.assertEqual(proposal.payment_status, Proposal.FINE)

        Proposal.objects.filter(id=proposal.id).update(
            last_updated_at=self.fixed_now - settings.DISCUSSION_TIME - timedelta(minutes=1),
        )

        slot_start, slot_end = self._queue_slot(weeks_ahead=1)
        with patch('aqua_governance.governance.proposal_queue.timezone.now', return_value=self.fixed_now), patch(
            'aqua_governance.governance.views.timezone.now', return_value=self.fixed_now
        ):
            submit_response = self.client.post(
                f'/api/proposal/{proposal.id}/submit/',
                {
                    'start_at': _iso_z(slot_start),
                    'end_at': _iso_z(slot_end),
                    'new_transaction_hash': 'b' * 64,
                    'new_envelope_xdr': 'submit-xdr',
                },
                format='json',
            )

        self.assertEqual(submit_response.status_code, 200)
        proposal.refresh_from_db()
        self.assertEqual(proposal.action, Proposal.TO_SUBMIT)
        self.assertEqual(proposal.proposal_status, Proposal.DISCUSSION)
        self.assertEqual(proposal.new_start_at, slot_start)
        self.assertEqual(proposal.new_end_at, slot_end)
        self.assertEqual(proposal.new_transaction_hash, 'b' * 64)
        self.assertEqual(proposal.new_envelope_xdr, 'submit-xdr')
        self.assertIsNone(proposal.start_at)
        self.assertIsNone(proposal.end_at)
        self.assertFalse(ProposalQueueSlot.objects.filter(proposal=proposal).exists())
        self.assertFalse(HistoryProposal.objects.filter(proposal=proposal, hide=True).exists())

        with patch('aqua_governance.governance.proposal_transactions.timezone.now', return_value=self.fixed_now):
            submit_payment_response = self.client.post(f'/api/proposal/{proposal.id}/check_payment/', {}, format='json')

        self.assertEqual(submit_payment_response.status_code, 200)
        proposal.refresh_from_db()
        self.assertEqual(proposal.proposal_status, Proposal.QUEUED)
        self.assertEqual(proposal.action, Proposal.NONE)
        self.assertEqual(proposal.payment_status, Proposal.FINE)
        self.assertEqual(proposal.start_at, slot_start)
        self.assertEqual(proposal.end_at, slot_end)
        self.assertIsNone(proposal.new_start_at)
        self.assertIsNone(proposal.new_end_at)
        self.assertIsNone(proposal.new_transaction_hash)
        self.assertIsNone(proposal.new_envelope_xdr)
        self.assertTrue(ProposalQueueSlot.objects.filter(proposal=proposal, start_at=slot_start, end_at=slot_end).exists())
        self.assertEqual(HistoryProposal.objects.filter(proposal=proposal, hide=True).count(), 1)

        with patch(
            'aqua_governance.governance.tasks.timezone.now',
            return_value=slot_start + timedelta(minutes=1),
        ):
            task_sync_proposal_statuses_by_time()

        proposal.refresh_from_db()
        self.assertEqual(proposal.proposal_status, Proposal.VOTING)
        self.assertTrue(ProposalQueueSlot.objects.filter(proposal=proposal, start_at=slot_start, end_at=slot_end).exists())

        unlock_at = proposal.end_at + timedelta(hours=1)
        for_balance_id = '3' * 72
        for_balance_id_secondary = '1' * 72
        against_balance_id = '2' * 72
        for_created_at = proposal.start_at + timedelta(hours=2)
        for_created_at_secondary = proposal.start_at + timedelta(hours=4)
        against_created_at = proposal.start_at + timedelta(hours=3)
        service_sponsor = DEFAULT_PROPOSED_BY
        self.assertNotEqual(service_sponsor, SECONDARY_ACCOUNT)
        self.assertNotEqual(service_sponsor, TERTIARY_ACCOUNT)
        expected_for_key = _expected_vote_key(
            proposal_id=proposal.id,
            vote_choice=LogVote.VOTE_FOR,
            account_issuer=SECONDARY_ACCOUNT,
            asset_code=settings.GOVERNANCE_ICE_ASSET_CODE,
            time_list=[_iso_z(unlock_at)],
        )
        expected_against_key = _expected_vote_key(
            proposal_id=proposal.id,
            vote_choice=LogVote.VOTE_AGAINST,
            account_issuer=TERTIARY_ACCOUNT,
            asset_code=settings.GDICE_ASSET_CODE,
            time_list=[_iso_z(unlock_at)],
        )

        fake_server = FakeHorizonServer(
            claimable_balances_by_claimant={
                proposal.vote_for_issuer: [
                    self._claimable_balance(
                        balance_id=for_balance_id_secondary,
                        amount='200.0000000',
                        asset=f'{settings.GOVERNANCE_ICE_ASSET_CODE}:{settings.GOVERNANCE_ICE_ASSET_ISSUER}',
                        vote_destination=proposal.vote_for_issuer,
                        voter_account=SECONDARY_ACCOUNT,
                        unlock_at=unlock_at,
                        sponsor=service_sponsor,
                    ),
                    self._claimable_balance(
                        balance_id=for_balance_id,
                        amount='1000.0000000',
                        asset=f'{settings.GOVERNANCE_ICE_ASSET_CODE}:{settings.GOVERNANCE_ICE_ASSET_ISSUER}',
                        vote_destination=proposal.vote_for_issuer,
                        voter_account=SECONDARY_ACCOUNT,
                        unlock_at=unlock_at,
                        sponsor=service_sponsor,
                    ),
                ],
                proposal.vote_against_issuer: [
                    self._claimable_balance(
                        balance_id=against_balance_id,
                        amount='250.0000000',
                        asset=f'{settings.GDICE_ASSET_CODE}:{settings.GDICE_ASSET_ISSUER}',
                        vote_destination=proposal.vote_against_issuer,
                        voter_account=TERTIARY_ACCOUNT,
                        unlock_at=unlock_at,
                        sponsor=service_sponsor,
                    ),
                ],
                proposal.abstain_issuer: [],
            },
            operations_by_balance_id={
                for_balance_id: [
                    self._create_claimable_balance_operation(
                        amount='1000.0000000',
                        created_at=for_created_at,
                    ),
                ],
                for_balance_id_secondary: [
                    self._create_claimable_balance_operation(
                        amount='200.0000000',
                        created_at=for_created_at_secondary,
                    ),
                ],
                against_balance_id: [
                    self._create_claimable_balance_operation(
                        amount='250.0000000',
                        created_at=against_created_at,
                    ),
                ],
            },
        )

        with patch('aqua_governance.governance.tasks.Server', return_value=fake_server):
            task_update_proposal_results(proposal.id, freezing_amount=False)

        proposal.refresh_from_db()
        self.assertEqual(proposal.vote_for_result, Decimal('1200'))
        self.assertEqual(proposal.vote_against_result, Decimal('250'))
        self.assertEqual(proposal.vote_abstain_result, Decimal('0'))

        votes = {vote.claimable_balance_id: vote for vote in LogVote.objects.filter(proposal=proposal, hide=False)}
        self.assertEqual(set(votes.keys()), {for_balance_id, for_balance_id_secondary, against_balance_id})

        for_vote = votes[for_balance_id]
        self.assertEqual(for_vote.vote_choice, LogVote.VOTE_FOR)
        self.assertEqual(for_vote.asset_code, settings.GOVERNANCE_ICE_ASSET_CODE)
        self.assertEqual(for_vote.account_issuer, SECONDARY_ACCOUNT)
        self.assertEqual(for_vote.key, expected_for_key)
        self.assertEqual(for_vote.group_index, 0)
        self.assertEqual(for_vote.amount, Decimal('1000'))
        self.assertEqual(for_vote.original_amount, Decimal('1000'))
        self.assertEqual(for_vote.transaction_link, 'https://horizon.example/transactions/')
        self.assertEqual(for_vote.created_at, for_created_at)
        self.assertIsNone(for_vote.voted_amount)
        self.assertFalse(for_vote.claimed)

        for_vote_secondary = votes[for_balance_id_secondary]
        self.assertEqual(for_vote_secondary.vote_choice, LogVote.VOTE_FOR)
        self.assertEqual(for_vote_secondary.asset_code, settings.GOVERNANCE_ICE_ASSET_CODE)
        self.assertEqual(for_vote_secondary.account_issuer, SECONDARY_ACCOUNT)
        self.assertEqual(for_vote_secondary.key, expected_for_key)
        self.assertEqual(for_vote_secondary.group_index, 1)
        self.assertEqual(for_vote_secondary.amount, Decimal('200'))
        self.assertEqual(for_vote_secondary.original_amount, Decimal('200'))
        self.assertEqual(for_vote_secondary.transaction_link, 'https://horizon.example/transactions/')
        self.assertEqual(for_vote_secondary.created_at, for_created_at_secondary)
        self.assertIsNone(for_vote_secondary.voted_amount)
        self.assertFalse(for_vote_secondary.claimed)

        against_vote = votes[against_balance_id]
        self.assertEqual(against_vote.vote_choice, LogVote.VOTE_AGAINST)
        self.assertEqual(against_vote.asset_code, settings.GDICE_ASSET_CODE)
        self.assertEqual(against_vote.account_issuer, TERTIARY_ACCOUNT)
        self.assertEqual(against_vote.key, expected_against_key)
        self.assertEqual(against_vote.group_index, 0)
        self.assertEqual(against_vote.amount, Decimal('250'))
        self.assertEqual(against_vote.original_amount, Decimal('250'))
        self.assertEqual(against_vote.transaction_link, 'https://horizon.example/transactions/')
        self.assertEqual(against_vote.created_at, against_created_at)
        self.assertIsNone(against_vote.voted_amount)
        self.assertFalse(against_vote.claimed)

        def _run_results_synchronously(proposal_id, freezing_amount=False):
            return task_update_proposal_results(proposal_id, freezing_amount)

        with patch('aqua_governance.governance.tasks.Server', return_value=fake_server), patch(
            'aqua_governance.governance.tasks.task_update_proposal_results.delay',
            side_effect=_run_results_synchronously,
        ), patch(
            'aqua_governance.governance.tasks.timezone.now',
            return_value=slot_end + timedelta(seconds=1),
        ):
            task_sync_proposal_statuses_by_time()

        proposal.refresh_from_db()
        self.assertEqual(proposal.proposal_status, Proposal.VOTED)
        self.assertFalse(ProposalQueueSlot.objects.filter(proposal=proposal).exists())
        self.assertEqual(proposal.vote_for_result, Decimal('1200'))
        self.assertEqual(proposal.vote_against_result, Decimal('250'))
        self.assertEqual(proposal.vote_abstain_result, Decimal('0'))
        self.assertEqual(proposal.onchain_execution_status, Proposal.ONCHAIN_EXECUTION_NOT_REQUIRED)

        for_vote.refresh_from_db()
        for_vote_secondary.refresh_from_db()
        against_vote.refresh_from_db()
        self.assertEqual(for_vote.voted_amount, Decimal('1000'))
        self.assertEqual(for_vote_secondary.voted_amount, Decimal('200'))
        self.assertEqual(against_vote.voted_amount, Decimal('250'))
        self.assertEqual(for_vote.amount, Decimal('1000'))
        self.assertEqual(for_vote_secondary.amount, Decimal('200'))
        self.assertEqual(against_vote.amount, Decimal('250'))
