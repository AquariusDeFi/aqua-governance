from datetime import timedelta
from typing import cast
from unittest.mock import patch

from django.conf import settings
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from aqua_governance.governance import proposal_transactions
from aqua_governance.governance.asset_tokens import derive_asset_contract_address
from aqua_governance.governance.models import Proposal, ProposalQueueSlot
from aqua_governance.governance.proposal_queue import get_queue_week_start
from aqua_governance.governance.serializers_v2 import SubmitSerializer
from aqua_governance.governance.tests._factories import (
    DEFAULT_CODE,
    DEFAULT_ISSUER,
    DEFAULT_PROPOSED_BY,
    make_asset_proposal_raw,
    patch_ice_circulating_supply,
)


class AssetProposalConflictTests(TestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.ice_supply_patcher = patch_ice_circulating_supply()
        self.ice_supply_patcher.start()
        self.addCleanup(self.ice_supply_patcher.stop)
        self.contract_address = derive_asset_contract_address(
            asset_code=DEFAULT_CODE,
            asset_issuer=DEFAULT_ISSUER,
            asset_contract_address=None,
        )

    def _asset_payload(self, **overrides):
        payload = {
            'proposed_by': DEFAULT_PROPOSED_BY,
            'title': 'Asset proposal',
            'text': '<p>x</p>',
            'transaction_hash': overrides.pop('transaction_hash', 'a' * 64),
            'envelope_xdr': overrides.pop('envelope_xdr', 'xdr'),
            'discord_username': 'user',
            'proposal_type': Proposal.PROPOSAL_TYPE_ADD_ASSET,
            'asset_code': DEFAULT_CODE,
            'asset_issuer': DEFAULT_ISSUER,
            'asset_contract_address': None,
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
        payload.update(overrides)
        return payload

    def _create_asset_proposal(self, **overrides):
        return make_asset_proposal_raw(
            transaction_hash=overrides.pop('transaction_hash', 'b' * 64),
            proposal_type=overrides.pop('proposal_type', Proposal.PROPOSAL_TYPE_ADD_ASSET),
            asset_code=overrides.pop('asset_code', DEFAULT_CODE),
            asset_issuer=overrides.pop('asset_issuer', DEFAULT_ISSUER),
            asset_contract_address=overrides.pop('asset_contract_address', None),
            draft=overrides.pop('draft', False),
            action=overrides.pop('action', Proposal.NONE),
            proposal_status=overrides.pop('proposal_status', Proposal.DISCUSSION),
            payment_status=overrides.pop('payment_status', Proposal.FINE),
            hide=overrides.pop('hide', False),
            **overrides,
        )

    def _queue_window(self, *, weeks_ahead=1):
        start_at = get_queue_week_start(timezone.now()) + timedelta(weeks=weeks_ahead)
        end_at = start_at + timedelta(days=7, seconds=-1)
        return start_at, end_at

    @patch('aqua_governance.governance.serializers_v2.check_transaction_xdr', return_value=Proposal.FINE)
    def test_queued_or_voting_asset_proposals_block_same_canonical_asset_across_types_and_identifiers(self, _mock_check_xdr):
        scenarios = [
            {
                'label': 'add blocks add classic',
                'blocker': {
                    'proposal_type': Proposal.PROPOSAL_TYPE_ADD_ASSET,
                    'asset_code': DEFAULT_CODE,
                    'asset_issuer': DEFAULT_ISSUER,
                    'asset_contract_address': None,
                },
                'candidate': {
                    'proposal_type': Proposal.PROPOSAL_TYPE_ADD_ASSET,
                    'asset_code': DEFAULT_CODE,
                    'asset_issuer': DEFAULT_ISSUER,
                    'asset_contract_address': None,
                    'transaction_hash': '1' * 64,
                },
            },
            {
                'label': 'classic add blocks contract remove',
                'blocker': {
                    'proposal_type': Proposal.PROPOSAL_TYPE_ADD_ASSET,
                    'asset_code': DEFAULT_CODE,
                    'asset_issuer': DEFAULT_ISSUER,
                    'asset_contract_address': None,
                },
                'candidate': {
                    'proposal_type': Proposal.PROPOSAL_TYPE_REMOVE_ASSET,
                    'asset_code': None,
                    'asset_issuer': None,
                    'asset_contract_address': self.contract_address,
                    'transaction_hash': '2' * 64,
                },
            },
            {
                'label': 'contract add blocks classic add',
                'blocker': {
                    'proposal_type': Proposal.PROPOSAL_TYPE_ADD_ASSET,
                    'asset_code': None,
                    'asset_issuer': None,
                    'asset_contract_address': self.contract_address,
                },
                'candidate': {
                    'proposal_type': Proposal.PROPOSAL_TYPE_ADD_ASSET,
                    'asset_code': DEFAULT_CODE,
                    'asset_issuer': DEFAULT_ISSUER,
                    'asset_contract_address': None,
                    'transaction_hash': '3' * 64,
                },
            },
        ]

        for blocker_status in (Proposal.QUEUED, Proposal.VOTING):
            for scenario in scenarios:
                with self.subTest(status=blocker_status, label=scenario['label']):
                    ProposalQueueSlot.objects.all().delete()
                    Proposal.objects.all().delete()
                    blocker = self._create_asset_proposal(
                        proposal_status=blocker_status,
                        **scenario['blocker'],
                    )

                    response = self.client.post(
                        '/api/asset-proposal/',
                        self._asset_payload(**scenario['candidate']),
                        format='json',
                    )

                    self.assertEqual(response.status_code, 400)
                    self.assertIn('non_field_errors', response.data)
                    self.assertIn('proposal_id', response.data)
                    self.assertEqual(int(response.data['proposal_id'][0]), blocker.id)

    @patch('aqua_governance.governance.serializers_v2.check_transaction_xdr', return_value=Proposal.FINE)
    def test_discussion_same_asset_proposal_does_not_block_create(self, _mock_check_xdr):
        self._create_asset_proposal(transaction_hash='d' * 64, proposal_status=Proposal.DISCUSSION)

        response = self.client.post(
            '/api/asset-proposal/',
            self._asset_payload(transaction_hash='e' * 64),
            format='json',
        )

        self.assertEqual(response.status_code, 201)

    @patch('aqua_governance.governance.serializers_v2.check_transaction_xdr', return_value=Proposal.FINE)
    def test_hidden_expired_and_voted_asset_proposals_do_not_block_create(self, _mock_check_xdr):
        scenarios = [
            {'label': 'hidden', 'overrides': {'hide': True}},
            {'label': 'expired', 'overrides': {'proposal_status': Proposal.EXPIRED}},
            {'label': 'voted', 'overrides': {'proposal_status': Proposal.VOTED}},
        ]

        for index, scenario in enumerate(scenarios, start=1):
            with self.subTest(label=scenario['label']):
                ProposalQueueSlot.objects.all().delete()
                Proposal.objects.all().delete()
                self._create_asset_proposal(transaction_hash=str(index) * 64, **scenario['overrides'])

                response = self.client.post(
                    '/api/asset-proposal/',
                    self._asset_payload(transaction_hash=str(index + 3) * 64),
                    format='json',
                )

                self.assertEqual(response.status_code, 201)

    @patch('aqua_governance.governance.serializers_v2.check_transaction_xdr', return_value=Proposal.FINE)
    def test_pending_draft_create_does_not_block_create(self, _mock_check_xdr):
        self._create_asset_proposal(
            draft=True,
            action=Proposal.TO_CREATE,
            transaction_hash='4' * 64,
        )

        response = self.client.post(
            '/api/asset-proposal/',
            self._asset_payload(transaction_hash='5' * 64),
            format='json',
        )

        self.assertEqual(response.status_code, 201)

    @patch('aqua_governance.governance.serializers_v2.check_transaction_xdr', return_value=Proposal.FINE)
    def test_to_submit_and_to_update_asset_proposals_do_not_block_create_while_still_in_discussion(self, _mock_check_xdr):
        for index, action in enumerate((Proposal.TO_SUBMIT, Proposal.TO_UPDATE), start=1):
            with self.subTest(action=action):
                ProposalQueueSlot.objects.all().delete()
                Proposal.objects.all().delete()
                self._create_asset_proposal(
                    action=action,
                    payment_status=Proposal.HORIZON_ERROR,
                    transaction_hash=str(index) * 64,
                )

                response = self.client.post(
                    '/api/asset-proposal/',
                    self._asset_payload(transaction_hash=str(index + 5) * 64),
                    format='json',
                )

                self.assertEqual(response.status_code, 201)

    @patch('aqua_governance.governance.proposal_transactions.check_proposal_status', return_value=Proposal.FINE)
    def test_confirmed_create_payment_keeps_asset_proposal_pending_while_conflict_exists(self, _mock_check_status):
        blocker = self._create_asset_proposal(
            transaction_hash='6' * 64,
            proposal_status=Proposal.QUEUED,
        )
        proposal = self._create_asset_proposal(
            transaction_hash='7' * 64,
            draft=True,
            action=Proposal.TO_CREATE,
            payment_status=Proposal.HORIZON_ERROR,
        )

        result = proposal_transactions.check_transaction(proposal)

        proposal.refresh_from_db()
        self.assertIsNotNone(result)
        result = cast(dict, result)
        self.assertEqual(result['outcome'], 'asset_proposal_conflict')
        self.assertEqual(result['asset_contract_address'], self.contract_address)
        self.assertEqual(result['conflict']['proposal'], str(blocker.id))
        self.assertTrue(proposal.draft)
        self.assertEqual(proposal.action, Proposal.TO_CREATE)
        self.assertEqual(proposal.payment_status, Proposal.FINE)
        self.assertFalse(proposal.hide)

    @patch('aqua_governance.governance.serializers_v2.check_transaction_xdr', return_value=Proposal.FINE)
    def test_discussion_same_asset_proposal_does_not_block_submit(self, _mock_check_xdr):
        self._create_asset_proposal(transaction_hash='8' * 64, proposal_status=Proposal.DISCUSSION)
        proposal = self._create_asset_proposal(transaction_hash='9' * 64)
        Proposal.objects.filter(id=proposal.id).update(
            last_updated_at=timezone.now() - settings.DISCUSSION_TIME - timedelta(seconds=1),
        )
        start_at, end_at = self._queue_window(weeks_ahead=1)

        submit_serializer = SubmitSerializer(
            proposal,
            data={
                'start_at': start_at,
                'end_at': end_at,
                'new_envelope_xdr': 'submit-xdr',
                'new_transaction_hash': 'f' * 64,
            },
        )

        self.assertTrue(submit_serializer.is_valid(), submit_serializer.errors)
        submit_serializer.save()
        proposal.refresh_from_db()
        self.assertEqual(proposal.action, Proposal.TO_SUBMIT)
        self.assertEqual(proposal.new_start_at, start_at)
        self.assertEqual(proposal.new_end_at, end_at)

    def test_submit_endpoint_returns_409_for_active_same_asset_conflict_and_does_not_stage_submit(self):
        blocker = self._create_asset_proposal(
            transaction_hash='a' * 64,
            proposal_status=Proposal.QUEUED,
        )
        proposal = self._create_asset_proposal(transaction_hash='b' * 64)
        Proposal.objects.filter(id=proposal.id).update(
            last_updated_at=timezone.now() - settings.DISCUSSION_TIME - timedelta(seconds=1),
        )
        start_at, end_at = self._queue_window(weeks_ahead=1)

        response = self.client.post(
            f'/api/proposal/{proposal.id}/submit/',
            {
                'start_at': start_at.isoformat().replace('+00:00', 'Z'),
                'end_at': end_at.isoformat().replace('+00:00', 'Z'),
                'new_envelope_xdr': 'submit-xdr',
                'new_transaction_hash': 'c' * 64,
            },
            format='json',
        )

        self.assertEqual(response.status_code, 409)
        body = response.json()
        self.assertEqual(body['code'], 'asset_proposal_conflict')
        self.assertEqual(body['asset_contract_address'], self.contract_address)
        self.assertEqual(body['conflict'], {
            'proposal': str(blocker.id),
            'proposal_status': blocker.proposal_status,
            'proposal_type': blocker.proposal_type,
        })
        proposal.refresh_from_db()
        self.assertEqual(proposal.action, Proposal.NONE)
        self.assertIsNone(proposal.new_start_at)
        self.assertIsNone(proposal.new_end_at)
        self.assertFalse(ProposalQueueSlot.objects.filter(proposal=proposal).exists())

    @patch('aqua_governance.governance.proposal_transactions.check_proposal_status', return_value=Proposal.FINE)
    def test_check_payment_returns_409_for_active_same_asset_conflict_and_does_not_book_slot(self, _mock_check_status):
        blocker = self._create_asset_proposal(
            transaction_hash='d' * 64,
            proposal_status=Proposal.VOTING,
        )
        start_at, end_at = self._queue_window(weeks_ahead=1)
        proposal = self._create_asset_proposal(
            transaction_hash='1' * 64,
            action=Proposal.TO_SUBMIT,
            payment_status=Proposal.HORIZON_ERROR,
            new_start_at=start_at,
            new_end_at=end_at,
            new_envelope_xdr='submit-xdr',
            new_transaction_hash='2' * 64,
        )

        response = self.client.post(f'/api/proposal/{proposal.id}/check_payment/', {}, format='json')

        self.assertEqual(response.status_code, 409)
        body = response.json()
        self.assertEqual(body['code'], 'asset_proposal_conflict')
        self.assertEqual(body['asset_contract_address'], self.contract_address)
        self.assertEqual(body['conflict'], {
            'proposal': str(blocker.id),
            'proposal_status': blocker.proposal_status,
            'proposal_type': blocker.proposal_type,
        })
        proposal.refresh_from_db()
        self.assertEqual(proposal.action, Proposal.TO_SUBMIT)
        self.assertEqual(proposal.payment_status, Proposal.FINE)
        self.assertEqual(proposal.proposal_status, Proposal.DISCUSSION)
        self.assertIsNone(proposal.start_at)
        self.assertIsNone(proposal.end_at)
        self.assertFalse(ProposalQueueSlot.objects.filter(proposal=proposal).exists())
