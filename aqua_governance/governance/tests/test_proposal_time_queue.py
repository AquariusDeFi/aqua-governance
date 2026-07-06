from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings
from django.utils import timezone

from aqua_governance.governance import proposal_transactions
from aqua_governance.governance.models import Proposal, ProposalQueueSlot
from aqua_governance.governance.proposal_queue import get_queue_week_start
from aqua_governance.governance.serializers_v2 import AssetProposalCreateSerializer, SubmitSerializer
from aqua_governance.governance.tasks import (
    task_check_expired_proposals,
    task_sync_proposal_statuses_by_time,
)
from aqua_governance.governance.tests._factories import (
    DEFAULT_PROPOSED_BY,
    make_asset_proposal_raw,
    patch_ice_circulating_supply,
)


class ProposalTimeQueueTests(TestCase):
    def _queue_slot(self, *, weeks_ahead=1):
        start_at = get_queue_week_start(timezone.now()) + timedelta(weeks=weeks_ahead)
        end_at = start_at + timedelta(days=7, seconds=-1)
        return start_at, end_at

    def _attach_queue_slot(self, proposal, *, start_at, end_at):
        if proposal.start_at != start_at or proposal.end_at != end_at:
            proposal.start_at = start_at
            proposal.end_at = end_at
            proposal.save(update_fields=['start_at', 'end_at'])
        return ProposalQueueSlot.objects.create(
            proposal=proposal,
            start_at=start_at,
            end_at=end_at,
        )

    def _create_proposal(self, **overrides):
        kwargs = {
            'title': overrides.pop('title', 'Queued asset proposal'),
            'proposal_type': overrides.pop('proposal_type', Proposal.PROPOSAL_TYPE_ADD_ASSET),
            'draft': overrides.pop('draft', False),
            'action': overrides.pop('action', Proposal.NONE),
            'proposal_status': overrides.pop('proposal_status', Proposal.DISCUSSION),
        }
        kwargs.update(overrides)
        return make_asset_proposal_raw(**kwargs)

    @override_settings(ASSET_MIN_VOTING_DURATION_DAYS=7)
    def test_submit_serializer_rejects_asset_interval_overlap(self):
        start_at, end_at = self._queue_slot(weeks_ahead=1)
        ProposalQueueSlot.objects.create(
            proposal=self._create_proposal(
                title='Blocked slot',
                proposal_status=Proposal.QUEUED,
                start_at=start_at,
                end_at=end_at,
            ),
            start_at=start_at,
            end_at=end_at,
        )
        proposal = self._create_proposal(asset_contract_address='CBQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAC')

        serializer = SubmitSerializer(
            proposal,
            data={
                'start_at': start_at,
                'end_at': end_at,
                'new_envelope_xdr': 'xdr',
                'new_transaction_hash': 'a' * 64,
            },
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn('start_at', serializer.errors)
        self.assertIn('end_at', serializer.errors)

    def test_submit_serializer_rejects_already_finished_window(self):
        proposal = self._create_proposal()
        current_week_start = get_queue_week_start(timezone.now())
        start_at = current_week_start - timedelta(weeks=1)
        end_at = start_at + timedelta(days=7, seconds=-1)

        serializer = SubmitSerializer(
            proposal,
            data={
                'start_at': start_at,
                'end_at': end_at,
                'new_envelope_xdr': 'xdr',
                'new_transaction_hash': 'a' * 64,
            },
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn('end_at', serializer.errors)

    def test_submit_serializer_ignores_overlapping_pending_window(self):
        start_at, end_at = self._queue_slot(weeks_ahead=1)
        self._create_proposal(
            proposal_type=Proposal.PROPOSAL_TYPE_GENERAL,
            action=Proposal.TO_SUBMIT,
            proposal_status=Proposal.DISCUSSION,
            new_start_at=start_at,
            new_end_at=end_at,
            new_envelope_xdr='blocker-xdr',
            new_transaction_hash='b' * 64,
        )
        proposal = self._create_proposal(
            proposal_type=Proposal.PROPOSAL_TYPE_GENERAL,
            transaction_hash='c' * 64,
        )

        serializer = SubmitSerializer(
            proposal,
            data={
                'start_at': start_at,
                'end_at': end_at,
                'new_envelope_xdr': 'xdr',
                'new_transaction_hash': 'a' * 64,
            },
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_submit_serializer_ignores_non_fine_pending_window(self):
        start_at, end_at = self._queue_slot(weeks_ahead=1)
        self._create_proposal(
            proposal_type=Proposal.PROPOSAL_TYPE_GENERAL,
            action=Proposal.TO_SUBMIT,
            payment_status=Proposal.INVALID_PAYMENT,
            proposal_status=Proposal.DISCUSSION,
            new_start_at=start_at,
            new_end_at=end_at,
            new_envelope_xdr='blocker-xdr',
            new_transaction_hash='b' * 64,
        )
        proposal = self._create_proposal(
            proposal_type=Proposal.PROPOSAL_TYPE_GENERAL,
            transaction_hash='c' * 64,
        )

        serializer = SubmitSerializer(
            proposal,
            data={
                'start_at': start_at,
                'end_at': end_at,
                'new_envelope_xdr': 'xdr',
                'new_transaction_hash': 'a' * 64,
            },
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_submit_serializer_ignores_finished_pending_window(self):
        start_at, end_at = self._queue_slot(weeks_ahead=1)
        self._create_proposal(
            proposal_type=Proposal.PROPOSAL_TYPE_GENERAL,
            action=Proposal.TO_SUBMIT,
            payment_status=Proposal.FINE,
            proposal_status=Proposal.EXPIRED,
            new_start_at=start_at,
            new_end_at=end_at,
            new_envelope_xdr='blocker-xdr',
            new_transaction_hash='b' * 64,
        )
        proposal = self._create_proposal(
            proposal_type=Proposal.PROPOSAL_TYPE_GENERAL,
            transaction_hash='c' * 64,
        )

        serializer = SubmitSerializer(
            proposal,
            data={
                'start_at': start_at,
                'end_at': end_at,
                'new_envelope_xdr': 'xdr',
                'new_transaction_hash': 'a' * 64,
            },
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)

    @patch('aqua_governance.governance.proposal_transactions.check_proposal_status', return_value=Proposal.FINE)
    def test_check_transaction_keeps_paid_submit_when_slot_conflicts(self, _mock_check_status):
        start_at, end_at = self._queue_slot(weeks_ahead=1)
        ProposalQueueSlot.objects.create(
            proposal=self._create_proposal(
                proposal_type=Proposal.PROPOSAL_TYPE_GENERAL,
                title='Blocked slot',
                proposal_status=Proposal.QUEUED,
                start_at=start_at,
                end_at=end_at,
            ),
            start_at=start_at,
            end_at=end_at,
        )
        proposal = self._create_proposal(
            proposal_type=Proposal.PROPOSAL_TYPE_GENERAL,
            action=Proposal.TO_SUBMIT,
            proposal_status=Proposal.DISCUSSION,
            payment_status=Proposal.HORIZON_ERROR,
            new_start_at=start_at,
            new_end_at=end_at,
            new_envelope_xdr='target-xdr',
            new_transaction_hash='a' * 64,
        )

        proposal_transactions.check_transaction(proposal)
        proposal.refresh_from_db()

        self.assertEqual(proposal.proposal_status, Proposal.DISCUSSION)
        self.assertEqual(proposal.action, Proposal.TO_SUBMIT)
        self.assertEqual(proposal.payment_status, Proposal.FINE)
        self.assertIsNone(proposal.start_at)
        self.assertIsNone(proposal.end_at)
        self.assertEqual(proposal.new_start_at, start_at)
        self.assertEqual(proposal.new_end_at, end_at)
        self.assertEqual(proposal.new_envelope_xdr, 'target-xdr')
        self.assertEqual(proposal.new_transaction_hash, 'a' * 64)

    @patch('aqua_governance.governance.proposal_transactions.check_proposal_status', return_value=Proposal.FINE)
    def test_check_transaction_books_future_slot_as_queued(self, _mock_check_status):
        start_at, end_at = self._queue_slot(weeks_ahead=1)
        proposal = self._create_proposal(
            proposal_type=Proposal.PROPOSAL_TYPE_GENERAL,
            action=Proposal.TO_SUBMIT,
            proposal_status=Proposal.DISCUSSION,
            payment_status=Proposal.HORIZON_ERROR,
            new_start_at=start_at,
            new_end_at=end_at,
            new_envelope_xdr='target-xdr',
            new_transaction_hash='d' * 64,
        )

        proposal_transactions.check_transaction(proposal)
        proposal.refresh_from_db()

        self.assertEqual(proposal.proposal_status, Proposal.QUEUED)
        self.assertEqual(proposal.action, Proposal.NONE)
        self.assertEqual(proposal.start_at, start_at)
        self.assertEqual(proposal.end_at, end_at)
        self.assertTrue(ProposalQueueSlot.objects.filter(proposal=proposal, start_at=start_at, end_at=end_at).exists())

    @patch('aqua_governance.governance.serializers_v2.check_transaction_xdr', return_value=Proposal.FINE)
    @patch('aqua_governance.governance.serializers_v2.acquire_proposal_transition_lock')
    def test_submit_serializer_uses_global_transition_lock_for_general_proposal(
        self,
        mock_lock,
        _mock_check_xdr,
    ):
        proposal = self._create_proposal(
            proposal_type=Proposal.PROPOSAL_TYPE_GENERAL,
            transaction_hash='d' * 64,
        )

        serializer = SubmitSerializer(
            proposal,
            data={
                'start_at': self._queue_slot(weeks_ahead=1)[0],
                'end_at': self._queue_slot(weeks_ahead=1)[1],
                'new_envelope_xdr': 'xdr',
                'new_transaction_hash': 'e' * 64,
            },
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()

        mock_lock.assert_called_once_with()

    @patch('aqua_governance.governance.tasks.task_update_proposal_results.delay')
    def test_sync_task_starts_due_queued_proposal(self, mock_update_results):
        start_at = timezone.now() - timedelta(minutes=1)
        end_at = timezone.now() + timedelta(days=1)
        proposal = self._create_proposal(
            proposal_status=Proposal.QUEUED,
            start_at=start_at,
            end_at=end_at,
        )
        self._attach_queue_slot(proposal, start_at=start_at, end_at=end_at)

        task_sync_proposal_statuses_by_time()

        proposal.refresh_from_db()
        self.assertEqual(proposal.proposal_status, Proposal.VOTING)
        mock_update_results.assert_not_called()

    @patch('aqua_governance.governance.tasks.task_update_proposal_results.delay')
    def test_sync_task_fails_closed_for_slotless_due_queued_proposal(self, mock_update_results):
        start_at = timezone.now() - timedelta(minutes=1)
        end_at = timezone.now() + timedelta(days=1)
        slotless = self._create_proposal(
            title='Legacy queued without slot',
            proposal_status=Proposal.QUEUED,
            start_at=start_at,
            end_at=end_at,
        )
        slotted = self._create_proposal(
            title='Queue-backed proposal',
            proposal_status=Proposal.QUEUED,
            start_at=start_at,
            end_at=end_at,
            transaction_hash='1' * 64,
        )
        self._attach_queue_slot(slotted, start_at=start_at, end_at=end_at)

        task_sync_proposal_statuses_by_time()

        slotless.refresh_from_db()
        slotted.refresh_from_db()
        self.assertEqual(slotless.proposal_status, Proposal.QUEUED)
        self.assertEqual(slotted.proposal_status, Proposal.VOTING)
        mock_update_results.assert_not_called()

    @patch('aqua_governance.governance.tasks.task_update_proposal_results.delay')
    @patch('aqua_governance.governance.tasks.Proposal.objects.select_for_update')
    def test_start_due_post_lock_recheck_skips_race_modified_proposal(self, mock_sfu, mock_update_results):
        """The post-lock re-check should skip a proposal that was modified
        between the initial query and lock acquisition (e.g. hidden by
        another process)."""
        now = timezone.now()
        start_at = now - timedelta(minutes=1)
        end_at = now + timedelta(days=1)
        proposal = self._create_proposal(
            proposal_status=Proposal.QUEUED,
            start_at=start_at,
            end_at=end_at,
        )
        self._attach_queue_slot(proposal, start_at=start_at, end_at=end_at)

        mock_locked = MagicMock()
        mock_locked.proposal_status = Proposal.QUEUED
        mock_locked.hide = True  # Race: another process hid this proposal
        mock_locked.draft = False
        mock_locked.action = Proposal.NONE
        mock_locked.start_at = start_at
        mock_locked.end_at = end_at
        mock_locked.id = proposal.id

        mock_sfu.return_value.get.return_value = mock_locked

        from aqua_governance.governance.tasks import _start_due_scheduled_proposals
        _start_due_scheduled_proposals(now)

        mock_locked.save.assert_not_called()

    @patch('aqua_governance.governance.tasks.task_update_proposal_results.delay')
    @patch('aqua_governance.governance.tasks.Proposal.objects.select_for_update')
    def test_start_due_post_lock_recheck_skips_status_changed(self, mock_sfu, mock_update_results):
        """The post-lock re-check should skip a proposal whose status was
        changed to a non-QUEUED value by a concurrent process."""
        now = timezone.now()
        start_at = now - timedelta(minutes=1)
        end_at = now + timedelta(days=1)
        proposal = self._create_proposal(
            proposal_status=Proposal.QUEUED,
            start_at=start_at,
            end_at=end_at,
        )
        self._attach_queue_slot(proposal, start_at=start_at, end_at=end_at)

        mock_locked = MagicMock()
        mock_locked.proposal_status = Proposal.DISCUSSION
        mock_locked.hide = False
        mock_locked.draft = False
        mock_locked.action = Proposal.NONE
        mock_locked.start_at = start_at
        mock_locked.end_at = end_at
        mock_locked.id = proposal.id

        mock_sfu.return_value.get.return_value = mock_locked

        from aqua_governance.governance.tasks import _start_due_scheduled_proposals
        _start_due_scheduled_proposals(now)

        mock_locked.save.assert_not_called()

    @patch('aqua_governance.governance.tasks.task_update_proposal_results.delay')
    def test_sync_task_does_not_start_due_queued_proposal_when_any_proposal_is_voting(self, mock_update_results):
        now = timezone.now()
        blocker = self._create_proposal(
            proposal_type=Proposal.PROPOSAL_TYPE_GENERAL,
            proposal_status=Proposal.VOTING,
            start_at=now - timedelta(days=1),
            end_at=now + timedelta(days=1),
        )
        queued = self._create_proposal(
            proposal_status=Proposal.QUEUED,
            start_at=now - timedelta(minutes=1),
            end_at=now + timedelta(days=1),
            asset_contract_address='CBQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAC',
        )
        self._attach_queue_slot(
            queued,
            start_at=queued.start_at,
            end_at=queued.end_at,
        )

        task_sync_proposal_statuses_by_time()

        blocker.refresh_from_db()
        queued.refresh_from_db()
        self.assertEqual(blocker.proposal_status, Proposal.VOTING)
        self.assertEqual(queued.proposal_status, Proposal.QUEUED)
        mock_update_results.assert_not_called()

    @patch('aqua_governance.governance.tasks.task_update_proposal_results.delay')
    def test_sync_task_does_not_start_due_discussion_proposal_without_queue_slot(self, mock_update_results):
        proposal = self._create_proposal(
            proposal_type=Proposal.PROPOSAL_TYPE_GENERAL,
            proposal_status=Proposal.DISCUSSION,
            start_at=timezone.now() - timedelta(minutes=1),
            end_at=timezone.now() + timedelta(days=1),
        )

        task_sync_proposal_statuses_by_time()

        proposal.refresh_from_db()
        self.assertEqual(proposal.proposal_status, Proposal.DISCUSSION)
        mock_update_results.assert_not_called()

    @patch('aqua_governance.governance.tasks.task_update_proposal_results.delay')
    def test_sync_task_finishes_due_voting_proposal(self, mock_update_results):
        start_at = timezone.now() - timedelta(days=2)
        end_at = timezone.now() - timedelta(minutes=1)
        proposal = self._create_proposal(
            proposal_status=Proposal.VOTING,
            start_at=start_at,
            end_at=end_at,
        )
        self._attach_queue_slot(proposal, start_at=start_at, end_at=end_at)

        task_sync_proposal_statuses_by_time()

        proposal.refresh_from_db()
        self.assertEqual(proposal.proposal_status, Proposal.VOTED)
        mock_update_results.assert_called_once_with(proposal.id, True)
        self.assertFalse(ProposalQueueSlot.objects.filter(proposal=proposal).exists())

    @patch('aqua_governance.governance.tasks.task_update_proposal_results.delay')
    def test_sync_task_finishes_before_starting_adjacent_next_proposal(self, mock_update_results):
        now = timezone.now()
        finishing = self._create_proposal(
            proposal_status=Proposal.VOTING,
            start_at=now - timedelta(days=7),
            end_at=now - timedelta(seconds=1),
        )
        starting = self._create_proposal(
            proposal_status=Proposal.QUEUED,
            start_at=now - timedelta(seconds=1),
            end_at=now + timedelta(days=7),
            asset_contract_address='CBQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAC',
        )
        self._attach_queue_slot(starting, start_at=starting.start_at, end_at=starting.end_at)

        task_sync_proposal_statuses_by_time()

        finishing.refresh_from_db()
        starting.refresh_from_db()
        self.assertEqual(finishing.proposal_status, Proposal.VOTED)
        self.assertEqual(starting.proposal_status, Proposal.VOTING)
        mock_update_results.assert_called_once_with(finishing.id, True)

    def test_expiration_task_does_not_expire_queued_proposal(self):
        start_at, end_at = self._queue_slot(weeks_ahead=1)
        proposal = self._create_proposal(
            proposal_status=Proposal.QUEUED,
            start_at=start_at,
            end_at=end_at,
        )
        self._attach_queue_slot(proposal, start_at=start_at, end_at=end_at)
        Proposal.objects.filter(id=proposal.id).update(
            last_updated_at=timezone.now() - timedelta(days=31),
        )

        task_check_expired_proposals()

        proposal.refresh_from_db()
        self.assertEqual(proposal.proposal_status, Proposal.QUEUED)

    def test_expiration_task_expires_stale_slotless_discussion_with_legacy_window(self):
        proposal = self._create_proposal(
            proposal_type=Proposal.PROPOSAL_TYPE_GENERAL,
            start_at=timezone.now() + timedelta(days=1),
            end_at=timezone.now() + timedelta(days=8),
        )
        Proposal.objects.filter(id=proposal.id).update(
            last_updated_at=timezone.now() - timedelta(days=31),
        )

        task_check_expired_proposals()

        proposal.refresh_from_db()
        self.assertEqual(proposal.proposal_status, Proposal.EXPIRED)

    @patch('aqua_governance.governance.tasks.task_update_proposal_results.delay')
    def test_sync_task_ignores_legacy_voting_proposal_without_end_at(self, mock_update_results):
        proposal = self._create_proposal(
            proposal_status=Proposal.VOTING,
            start_at=None,
            end_at=None,
        )

        task_sync_proposal_statuses_by_time()

        proposal.refresh_from_db()
        self.assertEqual(proposal.proposal_status, Proposal.VOTING)
        mock_update_results.assert_not_called()

    @patch('aqua_governance.governance.tasks.task_update_proposal_results.delay')
    def test_sync_task_does_not_start_when_legacy_voting_proposal_has_no_window(self, mock_update_results):
        self._create_proposal(
            proposal_status=Proposal.VOTING,
            start_at=None,
            end_at=None,
        )
        queued = self._create_proposal(
            proposal_status=Proposal.QUEUED,
            transaction_hash='c' * 64,
            start_at=timezone.now() - timedelta(minutes=1),
            end_at=timezone.now() + timedelta(days=7),
        )
        self._attach_queue_slot(queued, start_at=queued.start_at, end_at=queued.end_at)

        task_sync_proposal_statuses_by_time()

        queued.refresh_from_db()
        self.assertEqual(queued.proposal_status, Proposal.QUEUED)
        mock_update_results.assert_not_called()

    @patch('aqua_governance.governance.tasks.task_update_proposal_results.delay')
    def test_sync_task_expires_stale_slotless_discussion_proposal(self, mock_update_results):
        proposal = self._create_proposal(
            proposal_type=Proposal.PROPOSAL_TYPE_GENERAL,
            start_at=None,
            end_at=None,
        )
        Proposal.objects.filter(id=proposal.id).update(
            last_updated_at=timezone.now() - timedelta(days=31),
        )

        task_sync_proposal_statuses_by_time()

        proposal.refresh_from_db()
        self.assertEqual(proposal.proposal_status, Proposal.EXPIRED)
        mock_update_results.assert_not_called()

    @patch('aqua_governance.governance.proposal_transactions._alert_operator')
    @patch('aqua_governance.governance.proposal_transactions.check_proposal_status', return_value=Proposal.FINE)
    def test_late_submit_payment_keeps_to_submit_when_window_is_no_longer_valid(self, _mock_check_status, mock_alert):
        proposal = self._create_proposal(
            action=Proposal.TO_SUBMIT,
            new_start_at=timezone.now() - timedelta(days=8),
            new_end_at=timezone.now() - timedelta(days=1),
            new_transaction_hash='a' * 64,
            new_envelope_xdr='xdr',
        )

        proposal_transactions.check_transaction(proposal)

        proposal.refresh_from_db()
        self.assertEqual(proposal.payment_status, Proposal.FINE)
        self.assertEqual(proposal.proposal_status, Proposal.DISCUSSION)
        self.assertEqual(proposal.action, Proposal.TO_SUBMIT)
        self.assertIsNone(proposal.start_at)
        self.assertIsNone(proposal.end_at)
        self.assertFalse(ProposalQueueSlot.objects.filter(proposal=proposal).exists())
        mock_alert.assert_called_once()


@patch('aqua_governance.governance.serializers_v2.check_transaction_xdr', return_value=Proposal.HORIZON_ERROR)
class AssetProposalCreateWithoutQueueBookingTests(TestCase):
    def setUp(self):
        super().setUp()
        self.ice_supply_patcher = patch_ice_circulating_supply()
        self.ice_supply_patcher.start()
        self.addCleanup(self.ice_supply_patcher.stop)

    def _asset_payload(self, **overrides):
        data = {
            'proposed_by': DEFAULT_PROPOSED_BY,
            'title': 'Queue-window proposal',
            'text': '<p>test</p>',
            'transaction_hash': 'c' * 64,
            'envelope_xdr': 'AAAA',
            'discord_username': 'tester',
            'proposal_type': Proposal.PROPOSAL_TYPE_ADD_ASSET,
            'asset_code': 'AQUA',
            'asset_issuer': 'GBNZILSTVQZ4R7IKQDGHYGY2QXL5QOFJYQMXPKWRRM5PAV7Y4M67AQUA',
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
        data.update(overrides)
        return data

    @patch('aqua_governance.governance.serializers_v2.validate_asset_payload', return_value=['CBMOCK'])
    def test_create_leaves_start_and_end_empty_and_unbooked(self, _mock_validate, _mock_check):
        serializer = AssetProposalCreateSerializer(data=self._asset_payload())
        self.assertTrue(serializer.is_valid(), serializer.errors)
        proposal = serializer.save()

        self.assertIsNone(proposal.start_at)
        self.assertIsNone(proposal.end_at)
        self.assertFalse(ProposalQueueSlot.objects.filter(proposal=proposal).exists())

    @patch('aqua_governance.governance.serializers_v2.validate_asset_payload', return_value=['CBMOCK'])
    def test_create_keeps_start_and_end_fields_in_response_as_null(self, _mock_validate, _mock_check):
        serializer = AssetProposalCreateSerializer(data=self._asset_payload())
        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()

        self.assertIn('start_at', serializer.data)
        self.assertIn('end_at', serializer.data)
        self.assertIsNone(serializer.data['start_at'])
        self.assertIsNone(serializer.data['end_at'])

    @patch('aqua_governance.governance.serializers_v2.validate_asset_payload', return_value=['CBMOCK'])
    def test_create_does_not_acquire_transition_lock(self, _mock_validate, mock_check):
        with patch('aqua_governance.governance.serializers_v2.acquire_proposal_transition_lock') as mock_lock:
            serializer = AssetProposalCreateSerializer(data=self._asset_payload())
            self.assertTrue(serializer.is_valid(), serializer.errors)
            serializer.save()

            mock_lock.assert_not_called()

    @patch('aqua_governance.governance.serializers_v2.validate_asset_payload', return_value=['CBMOCK'])
    def test_create_keeps_draft_true(self, _mock_validate, _mock_check):
        serializer = AssetProposalCreateSerializer(data=self._asset_payload())
        self.assertTrue(serializer.is_valid(), serializer.errors)
        proposal = serializer.save()

        self.assertTrue(proposal.draft)
        self.assertEqual(proposal.action, Proposal.TO_CREATE)
        self.assertEqual(proposal.onchain_execution_status, Proposal.ONCHAIN_EXECUTION_PENDING)
