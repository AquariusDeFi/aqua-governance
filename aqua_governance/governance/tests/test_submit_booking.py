from datetime import datetime, timedelta
from datetime import timezone as datetime_timezone
from unittest.mock import patch

from django.db import IntegrityError
from django.test import TestCase
from django.utils import timezone

from rest_framework.test import APIClient

from aqua_governance.governance import proposal_transactions
from aqua_governance.governance.models import Proposal, ProposalQueueSlot
from aqua_governance.governance.proposal_queue import get_queue_week_start
from aqua_governance.governance.proposal_queue_slots import QueueSlotConflict
from aqua_governance.governance.serializers_v2 import SubmitSerializer
from aqua_governance.governance.tests._factories import make_asset_proposal_raw, patch_ice_circulating_supply


UTC = datetime_timezone.utc


class SubmitBookingFlowTests(TestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.ice_supply_patcher = patch_ice_circulating_supply()
        self.ice_supply_patcher.start()
        self.addCleanup(self.ice_supply_patcher.stop)

    def _proposal(self, **overrides):
        defaults = {
            'proposal_type': Proposal.PROPOSAL_TYPE_GENERAL,
            'proposal_status': Proposal.DISCUSSION,
            'draft': False,
            'action': Proposal.NONE,
        }
        defaults.update(overrides)
        return make_asset_proposal_raw(**defaults)

    def _week_slot(self, *, weeks_ahead=1):
        start_at = get_queue_week_start(timezone.now()) + timedelta(weeks=weeks_ahead)
        end_at = start_at + timedelta(days=7, seconds=-1)
        return start_at, end_at

    @patch('aqua_governance.governance.serializers_v2.check_transaction_xdr', return_value=Proposal.FINE)
    def test_submit_serializer_stages_public_start_and_end_without_booking_slot(self, _mock_check_xdr):
        proposal = self._proposal()
        start_at, end_at = self._week_slot(weeks_ahead=1)

        serializer = SubmitSerializer(
            proposal,
            data={
                'start_at': start_at,
                'end_at': end_at,
                'new_envelope_xdr': 'submit-xdr',
                'new_transaction_hash': 'a' * 64,
            },
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()

        proposal.refresh_from_db()
        self.assertEqual(proposal.action, Proposal.TO_SUBMIT)
        self.assertEqual(proposal.new_start_at, start_at)
        self.assertEqual(proposal.new_end_at, end_at)
        self.assertIsNone(proposal.start_at)
        self.assertIsNone(proposal.end_at)
        self.assertFalse(ProposalQueueSlot.objects.filter(proposal=proposal).exists())

    @patch('aqua_governance.governance.serializers_v2.check_transaction_xdr', return_value=Proposal.FINE)
    def test_submit_serializer_rejects_occupied_slot(self, _mock_check_xdr):
        start_at, end_at = self._week_slot(weeks_ahead=1)
        ProposalQueueSlot.objects.create(
            proposal=self._proposal(
                title='Occupied slot',
                proposal_status=Proposal.QUEUED,
                start_at=start_at,
                end_at=end_at,
            ),
            start_at=start_at,
            end_at=end_at,
        )
        proposal = self._proposal(title='Target proposal', transaction_hash='b' * 64)

        serializer = SubmitSerializer(
            proposal,
            data={
                'start_at': start_at,
                'end_at': end_at,
                'new_envelope_xdr': 'submit-xdr',
                'new_transaction_hash': 'c' * 64,
            },
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn('start_at', serializer.errors)
        self.assertIn('end_at', serializer.errors)

    @patch('aqua_governance.governance.serializers_v2.check_transaction_xdr', return_value=Proposal.FINE)
    def test_submit_serializer_rejects_current_week(self, _mock_check_xdr):
        proposal = self._proposal()
        start_at, end_at = self._week_slot(weeks_ahead=0)

        serializer = SubmitSerializer(
            proposal,
            data={
                'start_at': start_at,
                'end_at': end_at,
                'new_envelope_xdr': 'submit-xdr',
                'new_transaction_hash': 'c' * 64,
            },
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn('end_at', serializer.errors)

    @patch('aqua_governance.governance.proposal_transactions.timezone.now')
    @patch('aqua_governance.governance.proposal_transactions.check_proposal_status', return_value=Proposal.FINE)
    def test_confirmed_submit_accepts_slot_that_became_current_on_monday(
        self,
        _mock_check_status,
        mock_now,
    ):
        start_at = datetime(2026, 7, 13, 0, 0, 0, tzinfo=UTC)
        end_at = datetime(2026, 7, 19, 23, 59, 59, tzinfo=UTC)
        mock_now.return_value = datetime(2026, 7, 13, 0, 0, 0, tzinfo=UTC)
        proposal = self._proposal(
            action=Proposal.TO_SUBMIT,
            payment_status=Proposal.HORIZON_ERROR,
            new_start_at=start_at,
            new_end_at=end_at,
            new_envelope_xdr='submit-xdr',
            new_transaction_hash='d' * 64,
        )

        result = proposal_transactions.check_transaction(proposal)

        proposal.refresh_from_db()
        self.assertEqual(result['outcome'], 'booked')
        self.assertEqual(proposal.proposal_status, Proposal.VOTING)
        self.assertEqual(proposal.action, Proposal.NONE)
        self.assertEqual(proposal.payment_status, Proposal.FINE)
        self.assertEqual(proposal.start_at, start_at)
        self.assertEqual(proposal.end_at, end_at)
        self.assertIsNone(proposal.new_start_at)
        self.assertIsNone(proposal.new_end_at)
        self.assertTrue(ProposalQueueSlot.objects.filter(
            proposal=proposal,
            start_at=start_at,
            end_at=end_at,
        ).exists())

    @patch('aqua_governance.governance.proposal_transactions.check_proposal_status', return_value=Proposal.FINE)
    def test_confirmed_submit_books_future_slot_and_sets_queued(self, _mock_check_status):
        start_at, end_at = self._week_slot(weeks_ahead=1)
        proposal = self._proposal(
            action=Proposal.TO_SUBMIT,
            payment_status=Proposal.HORIZON_ERROR,
            new_start_at=start_at,
            new_end_at=end_at,
            new_envelope_xdr='submit-xdr',
            new_transaction_hash='d' * 64,
        )

        result = proposal_transactions.check_transaction(proposal)

        proposal.refresh_from_db()
        self.assertEqual(result['outcome'], 'booked')
        self.assertEqual(proposal.proposal_status, Proposal.QUEUED)
        self.assertEqual(proposal.action, Proposal.NONE)
        self.assertEqual(proposal.payment_status, Proposal.FINE)
        self.assertEqual(proposal.start_at, start_at)
        self.assertEqual(proposal.end_at, end_at)
        self.assertIsNone(proposal.new_start_at)
        self.assertIsNone(proposal.new_end_at)
        self.assertTrue(ProposalQueueSlot.objects.filter(proposal=proposal, start_at=start_at, end_at=end_at).exists())

    @patch('aqua_governance.governance.proposal_transactions.check_proposal_status', return_value=Proposal.FINE)
    def test_check_payment_reclaims_stale_non_occupying_slot_and_books(self, _mock_check_status):
        start_at, end_at = self._week_slot(weeks_ahead=1)
        scenarios = [
            ('hidden', {'hide': True, 'proposal_status': Proposal.QUEUED}),
            ('draft', {'draft': True, 'proposal_status': Proposal.QUEUED}),
            ('pending', {'action': Proposal.TO_SUBMIT, 'proposal_status': Proposal.DISCUSSION}),
        ]

        for index, (label, overrides) in enumerate(scenarios, start=1):
            with self.subTest(label=label):
                ProposalQueueSlot.objects.all().delete()
                Proposal.objects.all().delete()

                stale = self._proposal(
                    title=f'Stale {label} proposal',
                    start_at=start_at,
                    end_at=end_at,
                    transaction_hash=str(index) * 64,
                    **overrides,
                )
                ProposalQueueSlot.objects.create(
                    proposal=stale,
                    start_at=start_at,
                    end_at=end_at,
                )
                proposal = self._proposal(
                    title=f'Pending submit {label}',
                    transaction_hash=str(index + 3) * 64,
                    action=Proposal.TO_SUBMIT,
                    payment_status=Proposal.HORIZON_ERROR,
                    new_start_at=start_at,
                    new_end_at=end_at,
                    new_envelope_xdr='submit-xdr',
                    new_transaction_hash=str(index + 6) * 64,
                )

                response = self.client.post(f'/api/proposal/{proposal.id}/check_payment/', {}, format='json')

                self.assertEqual(response.status_code, 200)
                proposal.refresh_from_db()
                self.assertEqual(proposal.proposal_status, Proposal.QUEUED)
                self.assertEqual(proposal.action, Proposal.NONE)
                self.assertTrue(
                    ProposalQueueSlot.objects.filter(
                        proposal=proposal,
                        start_at=start_at,
                        end_at=end_at,
                    ).exists()
                )
                self.assertFalse(ProposalQueueSlot.objects.filter(proposal=stale).exists())

    @patch('aqua_governance.governance.proposal_transactions.check_proposal_status', return_value=Proposal.FINE)
    def test_check_payment_reclaims_mismatched_queued_or_voting_ghost_slot_and_books(self, _mock_check_status):
        booked_start, booked_end = self._week_slot(weeks_ahead=1)
        ghost_start, ghost_end = self._week_slot(weeks_ahead=2)

        for index, status in enumerate((Proposal.QUEUED, Proposal.VOTING), start=1):
            with self.subTest(status=status):
                ProposalQueueSlot.objects.all().delete()
                Proposal.objects.all().delete()

                ghost = self._proposal(
                    title=f'Ghost {status.lower()} proposal',
                    proposal_status=status,
                    start_at=ghost_start,
                    end_at=ghost_end,
                    transaction_hash=str(index) * 64,
                )
                ProposalQueueSlot.objects.create(
                    proposal=ghost,
                    start_at=booked_start,
                    end_at=booked_end,
                )
                proposal = self._proposal(
                    title=f'Pending submit {status.lower()}',
                    transaction_hash=str(index + 2) * 64,
                    action=Proposal.TO_SUBMIT,
                    payment_status=Proposal.HORIZON_ERROR,
                    new_start_at=booked_start,
                    new_end_at=booked_end,
                    new_envelope_xdr='submit-xdr',
                    new_transaction_hash=str(index + 4) * 64,
                )

                response = self.client.post(f'/api/proposal/{proposal.id}/check_payment/', {}, format='json')

                self.assertEqual(response.status_code, 200)
                proposal.refresh_from_db()
                self.assertEqual(proposal.proposal_status, Proposal.QUEUED)
                self.assertEqual(proposal.action, Proposal.NONE)
                self.assertTrue(
                    ProposalQueueSlot.objects.filter(
                        proposal=proposal,
                        start_at=booked_start,
                        end_at=booked_end,
                    ).exists()
                )
                self.assertFalse(ProposalQueueSlot.objects.filter(proposal=ghost).exists())

    @patch('aqua_governance.governance.proposal_transactions.check_proposal_status', return_value=Proposal.FINE)
    def test_confirmed_submit_starts_voting_when_slot_is_already_active(self, _mock_check_status):
        start_at, end_at = self._week_slot(weeks_ahead=0)
        proposal = self._proposal(
            action=Proposal.TO_SUBMIT,
            payment_status=Proposal.HORIZON_ERROR,
            new_start_at=start_at,
            new_end_at=end_at,
            new_envelope_xdr='submit-xdr',
            new_transaction_hash='e' * 64,
        )

        result = proposal_transactions.check_transaction(proposal)

        proposal.refresh_from_db()
        self.assertEqual(result['outcome'], 'booked')
        self.assertEqual(proposal.proposal_status, Proposal.VOTING)
        self.assertTrue(ProposalQueueSlot.objects.filter(proposal=proposal, start_at=start_at, end_at=end_at).exists())

    @patch('aqua_governance.governance.proposal_transactions.logger')
    @patch(
        'aqua_governance.governance.proposal_transactions.check_proposal_status',
        return_value=Proposal.HORIZON_ERROR,
    )
    def test_horizon_error_keeps_to_submit_without_booking_slot(self, _mock_check_status, mock_logger):
        start_at, end_at = self._week_slot(weeks_ahead=1)
        proposal = self._proposal(
            action=Proposal.TO_SUBMIT,
            new_start_at=start_at,
            new_end_at=end_at,
            new_envelope_xdr='submit-xdr',
            new_transaction_hash='f' * 64,
        )

        result = proposal_transactions.check_transaction(proposal)

        proposal.refresh_from_db()
        self.assertEqual(result['outcome'], 'payment_not_confirmed')
        self.assertEqual(proposal.action, Proposal.TO_SUBMIT)
        self.assertEqual(proposal.payment_status, Proposal.HORIZON_ERROR)
        self.assertFalse(ProposalQueueSlot.objects.filter(proposal=proposal).exists())
        mock_logger.info.assert_called_once()
        _call_args = mock_logger.info.call_args
        self.assertIn('proposal_id', _call_args[1]['extra'])
        self.assertIn('transaction_hash', _call_args[1]['extra'])

    @patch('aqua_governance.governance.proposal_transactions._alert_operator')
    @patch('aqua_governance.governance.proposal_transactions.check_proposal_status', return_value=Proposal.FINE)
    def test_paid_slot_conflict_keeps_submit_state_and_does_not_book_slot(self, _mock_check_status, mock_alert):
        start_at, end_at = self._week_slot(weeks_ahead=1)
        stale_time = timezone.now() - timedelta(days=8)
        blocking_proposal = self._proposal(
            title='Blocking proposal',
            proposal_status=Proposal.QUEUED,
            start_at=start_at,
            end_at=end_at,
        )
        ProposalQueueSlot.objects.create(
            proposal=blocking_proposal,
            start_at=start_at,
            end_at=end_at,
        )
        proposal = self._proposal(
            title='Target proposal',
            transaction_hash='1' * 64,
            action=Proposal.TO_SUBMIT,
            payment_status=Proposal.HORIZON_ERROR,
            new_start_at=start_at,
            new_end_at=end_at,
            new_envelope_xdr='submit-xdr',
            new_transaction_hash='2' * 64,
        )
        Proposal.objects.filter(id=proposal.id).update(last_updated_at=stale_time)

        result = proposal_transactions.check_transaction(proposal)

        proposal.refresh_from_db()
        self.assertEqual(result['outcome'], 'slot_conflict')
        self.assertEqual(result['conflict']['proposal'], blocking_proposal.id)
        self.assertEqual(proposal.action, Proposal.TO_SUBMIT)
        self.assertEqual(proposal.payment_status, Proposal.FINE)
        self.assertEqual(proposal.proposal_status, Proposal.DISCUSSION)
        self.assertIsNone(proposal.start_at)
        self.assertIsNone(proposal.end_at)
        self.assertEqual(proposal.last_updated_at, stale_time)
        self.assertEqual(proposal.new_start_at, start_at)
        self.assertEqual(proposal.new_end_at, end_at)
        self.assertFalse(ProposalQueueSlot.objects.filter(proposal=proposal).exists())
        mock_alert.assert_called_once()
        _call_args = mock_alert.call_args
        self.assertIn('proposal_id', _call_args[1]['extra'])
        self.assertIn('conflicting_proposal_id', _call_args[1]['extra'])

    @patch('aqua_governance.governance.proposal_transactions.sync_proposal_queue_slot', side_effect=IntegrityError)
    @patch('aqua_governance.governance.proposal_transactions.find_queue_slot_conflict')
    @patch('aqua_governance.governance.proposal_transactions._alert_operator')
    @patch('aqua_governance.governance.proposal_transactions.check_proposal_status', return_value=Proposal.FINE)
    def test_submit_booking_integrity_error_rolls_back_confirmed_state_before_conflict_response(
        self,
        _mock_check_status,
        mock_alert,
        mock_find_conflict,
        _mock_sync_slot,
    ):
        start_at, end_at = self._week_slot(weeks_ahead=1)
        stale_time = timezone.now() - timedelta(days=8)
        blocking_proposal = self._proposal(
            title='Blocking proposal',
            proposal_status=Proposal.QUEUED,
            start_at=start_at,
            end_at=end_at,
        )
        blocking_slot = ProposalQueueSlot.objects.create(
            proposal=blocking_proposal,
            start_at=start_at,
            end_at=end_at,
        )
        proposal = self._proposal(
            title='Target proposal',
            transaction_hash='1' * 64,
            envelope_xdr='current-xdr',
            action=Proposal.TO_SUBMIT,
            payment_status=Proposal.HORIZON_ERROR,
            new_start_at=start_at,
            new_end_at=end_at,
            new_envelope_xdr='submit-xdr',
            new_transaction_hash='2' * 64,
        )
        Proposal.objects.filter(id=proposal.id).update(last_updated_at=stale_time)
        mock_find_conflict.side_effect = [
            None,
            QueueSlotConflict(proposal=blocking_proposal, slot=blocking_slot),
        ]

        result = proposal_transactions.check_transaction(proposal)

        proposal.refresh_from_db()
        self.assertEqual(result['outcome'], 'slot_conflict')
        self.assertEqual(result['conflict']['proposal'], blocking_proposal.id)
        self.assertEqual(proposal.action, Proposal.TO_SUBMIT)
        self.assertEqual(proposal.payment_status, Proposal.FINE)
        self.assertEqual(proposal.proposal_status, Proposal.DISCUSSION)
        self.assertEqual(proposal.transaction_hash, '1' * 64)
        self.assertEqual(proposal.envelope_xdr, 'current-xdr')
        self.assertIsNone(proposal.start_at)
        self.assertIsNone(proposal.end_at)
        self.assertEqual(proposal.last_updated_at, stale_time)
        self.assertEqual(proposal.new_start_at, start_at)
        self.assertEqual(proposal.new_end_at, end_at)
        self.assertEqual(proposal.new_transaction_hash, '2' * 64)
        self.assertEqual(proposal.new_envelope_xdr, 'submit-xdr')
        self.assertFalse(ProposalQueueSlot.objects.filter(proposal=proposal).exists())
        self.assertEqual(proposal.history_proposal.count(), 0)
        mock_alert.assert_called_once()

    @patch('aqua_governance.governance.views.ProposalViewSet._check_owner_permissions')
    @patch('aqua_governance.governance.serializers_v2.check_transaction_xdr', return_value=Proposal.FINE)
    @patch('aqua_governance.governance.proposal_transactions.check_proposal_status', return_value=Proposal.FINE)
    def test_submit_endpoint_allows_immediate_retry_for_to_submit_conflict(self, _mock_check_status, _mock_check_xdr, _mock_owner):
        first_start_at, first_end_at = self._week_slot(weeks_ahead=1)
        second_start_at, second_end_at = self._week_slot(weeks_ahead=2)
        stale_time = timezone.now() - timedelta(days=8)
        proposal = self._proposal(title='Pending proposal', transaction_hash='a' * 64)
        Proposal.objects.filter(id=proposal.id).update(last_updated_at=stale_time)

        first_submit = self.client.post(
            f'/api/proposal/{proposal.id}/submit/',
            {
                'start_at': first_start_at.isoformat().replace('+00:00', 'Z'),
                'end_at': first_end_at.isoformat().replace('+00:00', 'Z'),
                'new_envelope_xdr': 'submit-xdr-1',
                'new_transaction_hash': 'b' * 64,
            },
            format='json',
        )
        self.assertEqual(first_submit.status_code, 200)

        blocking_proposal = self._proposal(
            title='Blocking proposal',
            proposal_status=Proposal.QUEUED,
            start_at=first_start_at,
            end_at=first_end_at,
            transaction_hash='d' * 64,
        )
        ProposalQueueSlot.objects.create(
            proposal=blocking_proposal,
            start_at=first_start_at,
            end_at=first_end_at,
        )

        conflict_response = self.client.post(f'/api/proposal/{proposal.id}/check_payment/', {}, format='json')
        self.assertEqual(conflict_response.status_code, 409)

        proposal.refresh_from_db()
        self.assertEqual(proposal.action, Proposal.TO_SUBMIT)
        self.assertEqual(proposal.last_updated_at, stale_time)

        Proposal.objects.filter(id=proposal.id).update(last_updated_at=timezone.now())

        retry_submit = self.client.post(
            f'/api/proposal/{proposal.id}/submit/',
            {
                'start_at': second_start_at.isoformat().replace('+00:00', 'Z'),
                'end_at': second_end_at.isoformat().replace('+00:00', 'Z'),
                'new_envelope_xdr': 'submit-xdr-2',
                'new_transaction_hash': 'c' * 64,
            },
            format='json',
        )

        self.assertEqual(retry_submit.status_code, 200)
        proposal.refresh_from_db()
        self.assertEqual(proposal.action, Proposal.TO_SUBMIT)
        self.assertEqual(proposal.new_start_at, second_start_at)
        self.assertEqual(proposal.new_end_at, second_end_at)
        self.assertEqual(proposal.new_transaction_hash, 'c' * 64)

    @patch('aqua_governance.governance.proposal_transactions.check_proposal_status', side_effect=[Proposal.HORIZON_ERROR, Proposal.FINE])
    def test_retry_later_books_slot_when_confirmation_eventually_succeeds(self, _mock_check_status):
        start_at, end_at = self._week_slot(weeks_ahead=1)
        proposal = self._proposal(
            action=Proposal.TO_SUBMIT,
            new_start_at=start_at,
            new_end_at=end_at,
            new_envelope_xdr='submit-xdr',
            new_transaction_hash='3' * 64,
        )

        first_result = proposal_transactions.check_transaction(proposal)
        second_result = proposal_transactions.check_transaction(proposal)

        proposal.refresh_from_db()
        self.assertEqual(first_result['outcome'], 'payment_not_confirmed')
        self.assertEqual(second_result['outcome'], 'booked')
        self.assertEqual(proposal.proposal_status, Proposal.QUEUED)
        self.assertTrue(ProposalQueueSlot.objects.filter(proposal=proposal, start_at=start_at, end_at=end_at).exists())

    @patch('aqua_governance.governance.proposal_transactions.check_proposal_status', return_value=Proposal.FINE)
    def test_check_payment_endpoint_returns_conflict_response(self, _mock_check_status):
        start_at, end_at = self._week_slot(weeks_ahead=1)
        blocking_proposal = self._proposal(
            title='Blocking proposal',
            proposal_status=Proposal.QUEUED,
            start_at=start_at,
            end_at=end_at,
        )
        ProposalQueueSlot.objects.create(
            proposal=blocking_proposal,
            start_at=start_at,
            end_at=end_at,
        )
        proposal = self._proposal(
            title='Pending proposal',
            transaction_hash='4' * 64,
            action=Proposal.TO_SUBMIT,
            new_start_at=start_at,
            new_end_at=end_at,
            new_envelope_xdr='submit-xdr',
            new_transaction_hash='5' * 64,
        )

        response = self.client.post(f'/api/proposal/{proposal.id}/check_payment/', {}, format='json')

        self.assertEqual(response.status_code, 409)
        body = response.json()
        self.assertEqual(body['code'], 'proposal_queue_slot_conflict')
        self.assertEqual(
            set(body.keys()),
            {'detail', 'code', 'selected_slot', 'conflict'},
        )
        self.assertEqual(body['selected_slot'], {
            'start_at': start_at.isoformat().replace('+00:00', 'Z'),
            'end_at': end_at.isoformat().replace('+00:00', 'Z'),
        })
        self.assertEqual(body['conflict'], {
            'proposal': blocking_proposal.id,
            'proposal_status': blocking_proposal.proposal_status,
            'start_at': start_at.isoformat().replace('+00:00', 'Z'),
            'end_at': end_at.isoformat().replace('+00:00', 'Z'),
        })

    def test_queued_proposal_is_not_publicly_editable(self):
        start_at, end_at = self._week_slot(weeks_ahead=1)
        proposal = self._proposal(
            proposal_status=Proposal.QUEUED,
            start_at=start_at,
            end_at=end_at,
        )
        ProposalQueueSlot.objects.create(proposal=proposal, start_at=start_at, end_at=end_at)

        response = self.client.put(f'/api/proposal/{proposal.id}/', {}, format='json')

        self.assertEqual(response.status_code, 404)

    @patch('aqua_governance.governance.proposal_transactions.sentry_sdk')
    @patch(
        'aqua_governance.governance.proposal_transactions.check_proposal_status',
        return_value=Proposal.HORIZON_ERROR,
    )
    def test_horizon_error_does_not_capture_sentry_event(self, _mock_check_status, mock_sentry):
        start_at, end_at = self._week_slot(weeks_ahead=1)
        proposal = self._proposal(
            action=Proposal.TO_SUBMIT,
            new_start_at=start_at,
            new_end_at=end_at,
            new_envelope_xdr='submit-xdr',
            new_transaction_hash='7' * 64,
        )

        proposal_transactions.check_transaction(proposal)

        mock_sentry.push_scope.assert_not_called()
        mock_sentry.capture_message.assert_not_called()

    @patch('aqua_governance.governance.proposal_transactions.sentry_sdk')
    @patch('aqua_governance.governance.proposal_transactions.check_proposal_status', return_value=Proposal.FINE)
    def test_slot_conflict_alerts_via_sentry(self, _mock_check_status, mock_sentry):
        start_at, end_at = self._week_slot(weeks_ahead=1)
        blocking_proposal = self._proposal(
            title='Blocking',
            proposal_status=Proposal.QUEUED,
            start_at=start_at,
            end_at=end_at,
        )
        ProposalQueueSlot.objects.create(
            proposal=blocking_proposal,
            start_at=start_at,
            end_at=end_at,
        )
        proposal = self._proposal(
            title='Conflict target',
            transaction_hash='8' * 64,
            action=Proposal.TO_SUBMIT,
            new_start_at=start_at,
            new_end_at=end_at,
            new_envelope_xdr='submit-xdr',
            new_transaction_hash='9' * 64,
        )

        proposal_transactions.check_transaction(proposal)

        mock_sentry.push_scope.assert_called_once()
        scope = mock_sentry.push_scope.return_value.__enter__.return_value
        set_extra_keys = {call.args[0] for call in scope.set_extra.call_args_list}
        self.assertIn('proposal_id', set_extra_keys)
        self.assertIn('transaction_hash', set_extra_keys)
        self.assertIn('payment_status', set_extra_keys)
        self.assertIn('action', set_extra_keys)
        self.assertIn('proposal_status', set_extra_keys)
        self.assertIn('selected_start_at', set_extra_keys)
        self.assertIn('selected_end_at', set_extra_keys)
        self.assertIn('conflicting_proposal_id', set_extra_keys)
        self.assertIn('conflicting_proposal_status', set_extra_keys)
        mock_sentry.capture_message.assert_called_once()
