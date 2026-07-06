import json
from datetime import datetime, timedelta, timezone as datetime_timezone
from unittest.mock import patch

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django_quill.quill import Quill

from aqua_governance.governance.tests._factories import (
    DEFAULT_CODE,
    DEFAULT_ISSUER,
    DEFAULT_PROPOSED_BY,
    QUATERNARY_ACCOUNT,
    TERTIARY_ACCOUNT,
)


UTC = datetime_timezone.utc
FIXED_NOW = datetime(2026, 6, 10, 12, 0, 0, tzinfo=UTC)
MIGRATION_NOW_PATCH = 'aqua_governance.governance.migrations.0029_proposal_queue_slot.timezone.now'


class ProposalQueueSlotBackfillMigrationTests(TransactionTestCase):
    migrate_from = [('governance', '0028_asset_token_and_proposal_fk')]
    migrate_to = [('governance', '0029_proposal_queue_slot')]

    def setUp(self):
        super().setUp()
        self.executor = MigrationExecutor(connection)
        self.executor.migrate(self.migrate_from)
        self.apps_0028 = self.executor.loader.project_state(self.migrate_from).apps

    def _proposal_defaults(self):
        return {
            'proposed_by': DEFAULT_PROPOSED_BY,
            'text': Quill(json.dumps({'delta': {'ops': []}, 'html': '<p>x</p>'})),
            'vote_for_issuer': TERTIARY_ACCOUNT,
            'vote_against_issuer': QUATERNARY_ACCOUNT,
            'draft': False,
            'hide': False,
            'payment_status': 'FINE',
            'action': 'NONE',
            'proposal_type': 'ADD_ASSET',
            'asset_code': DEFAULT_CODE,
            'asset_issuer': DEFAULT_ISSUER,
        }

    def _create_proposal(self, **overrides):
        Proposal = self.apps_0028.get_model('governance', 'Proposal')
        defaults = self._proposal_defaults()
        defaults.update(overrides)
        return Proposal.objects.create(**defaults)

    def _migrate_forward(self):
        with patch(MIGRATION_NOW_PATCH, return_value=FIXED_NOW):
            self.executor = MigrationExecutor(connection)
            self.executor.migrate(self.migrate_to)
        return self.executor.loader.project_state(self.migrate_to).apps

    def test_forward_backfills_only_current_future_real_asset_slots(self):
        current_legacy_start = datetime(2026, 6, 8, 9, 10, 6, 786247, tzinfo=UTC)
        current_legacy_end = current_legacy_start + timedelta(days=7)
        current_start = datetime(2026, 6, 8, 0, 0, 0, tzinfo=UTC)
        current_end = current_start + timedelta(days=7, seconds=-1)
        future_legacy_start = datetime(2026, 6, 22, 9, 10, 8, 786247, tzinfo=UTC)
        future_legacy_end = future_legacy_start + timedelta(days=7)
        future_start = datetime(2026, 6, 22, 0, 0, 0, tzinfo=UTC)
        future_end = future_start + timedelta(days=7, seconds=-1)
        strict_future_start = datetime(2026, 6, 29, 0, 0, 0, tzinfo=UTC)
        strict_future_end = strict_future_start + timedelta(days=7, seconds=-1)
        past_start = datetime(2026, 6, 1, 0, 0, 0, tzinfo=UTC)
        past_end = past_start + timedelta(days=7, seconds=-1)

        active_voting = self._create_proposal(
            title='Active asset vote',
            proposal_status='VOTING',
            start_at=current_legacy_start,
            end_at=current_legacy_end,
        )
        future_discussion = self._create_proposal(
            title='Future asset discussion',
            proposal_status='DISCUSSION',
            start_at=future_legacy_start,
            end_at=future_legacy_end,
        )
        future_remove = self._create_proposal(
            title='Future remove asset discussion',
            proposal_type='REMOVE_ASSET',
            proposal_status='DISCUSSION',
            start_at=strict_future_start,
            end_at=strict_future_end,
        )

        self._create_proposal(
            title='Historical voted asset',
            proposal_status='VOTED',
            start_at=past_start,
            end_at=past_end,
        )
        self._create_proposal(
            title='Hidden technical sync',
            hide=True,
            proposal_status='VOTED',
            start_at=current_start,
            end_at=current_start,
        )
        general = self._create_proposal(
            title='General proposal',
            proposal_type='GENERAL',
            proposal_status='DISCUSSION',
            start_at=datetime(2026, 6, 29, 0, 0, 0, tzinfo=UTC),
            end_at=datetime(2026, 7, 5, 23, 59, 59, tzinfo=UTC),
            asset_code=None,
            asset_issuer=None,
        )
        invalid_payment = self._create_proposal(
            title='Invalid payment asset',
            payment_status='INVALID_PAYMENT',
            proposal_status='DISCUSSION',
            start_at=datetime(2026, 7, 6, 0, 0, 0, tzinfo=UTC),
            end_at=datetime(2026, 7, 12, 23, 59, 59, tzinfo=UTC),
        )
        draft_asset = self._create_proposal(
            title='Draft asset',
            draft=True,
            proposal_status='DISCUSSION',
            start_at=datetime(2026, 7, 13, 0, 0, 0, tzinfo=UTC),
            end_at=datetime(2026, 7, 19, 23, 59, 59, tzinfo=UTC),
        )

        apps_0029 = self._migrate_forward()
        Proposal = apps_0029.get_model('governance', 'Proposal')
        ProposalQueueSlot = apps_0029.get_model('governance', 'ProposalQueueSlot')

        self.assertEqual(ProposalQueueSlot.objects.count(), 3)

        migrated_slots = {
            slot.proposal_id: (slot.start_at, slot.end_at, slot.occupied_at)
            for slot in ProposalQueueSlot.objects.order_by('start_at', 'proposal_id')
        }
        self.assertEqual(
            migrated_slots,
            {
                active_voting.id: (current_start, current_end, FIXED_NOW),
                future_discussion.id: (future_start, future_end, FIXED_NOW),
                future_remove.id: (strict_future_start, strict_future_end, FIXED_NOW),
            },
        )

        active_voting_migrated = Proposal.objects.get(id=active_voting.id)
        future_discussion_migrated = Proposal.objects.get(id=future_discussion.id)
        future_remove_migrated = Proposal.objects.get(id=future_remove.id)

        self.assertEqual(active_voting_migrated.proposal_status, 'VOTING')
        self.assertEqual(active_voting_migrated.start_at, current_start)
        self.assertEqual(active_voting_migrated.end_at, current_end)
        self.assertEqual(future_discussion_migrated.proposal_status, 'QUEUED')
        self.assertEqual(future_discussion_migrated.start_at, future_start)
        self.assertEqual(future_discussion_migrated.end_at, future_end)
        self.assertEqual(future_remove_migrated.proposal_status, 'QUEUED')
        self.assertEqual(future_remove_migrated.start_at, strict_future_start)
        self.assertEqual(future_remove_migrated.end_at, strict_future_end)
        self.assertEqual(Proposal.objects.get(id=general.id).proposal_status, 'DISCUSSION')
        self.assertEqual(Proposal.objects.get(id=invalid_payment.id).proposal_status, 'DISCUSSION')
        self.assertEqual(Proposal.objects.get(id=draft_asset.id).proposal_status, 'DISCUSSION')

        self.assertFalse(ProposalQueueSlot.objects.filter(proposal_id=general.id).exists())
        self.assertFalse(ProposalQueueSlot.objects.filter(proposal_id=invalid_payment.id).exists())
        self.assertFalse(ProposalQueueSlot.objects.filter(proposal_id=draft_asset.id).exists())

    def test_forward_fails_when_legacy_normalization_creates_duplicate_slot(self):
        start_at = datetime(2026, 6, 15, 9, 10, 0, tzinfo=UTC)
        end_at = start_at + timedelta(days=7)
        duplicate_start_at = datetime(2026, 6, 15, 15, 45, 0, tzinfo=UTC)
        duplicate_end_at = duplicate_start_at + timedelta(days=7)

        self._create_proposal(
            title='First duplicate asset slot',
            proposal_status='DISCUSSION',
            start_at=start_at,
            end_at=end_at,
        )
        self._create_proposal(
            title='Second duplicate asset slot',
            proposal_type='REMOVE_ASSET',
            proposal_status='DISCUSSION',
            start_at=duplicate_start_at,
            end_at=duplicate_end_at,
        )

        with patch(MIGRATION_NOW_PATCH, return_value=FIXED_NOW):
            with self.assertRaisesMessage(ValueError, 'overlap or duplicate'):
                self.executor = MigrationExecutor(connection)
                self.executor.migrate(self.migrate_to)

    def test_forward_fails_on_invalid_weekly_range(self):
        self._create_proposal(
            title='Invalid weekly asset slot',
            proposal_status='DISCUSSION',
            start_at=datetime(2026, 6, 15, 9, 10, 0, tzinfo=UTC),
            end_at=datetime(2026, 6, 22, 9, 10, 1, tzinfo=UTC),
        )

        with patch(MIGRATION_NOW_PATCH, return_value=FIXED_NOW):
            with self.assertRaisesMessage(ValueError, 'invalid queue slot range'):
                self.executor = MigrationExecutor(connection)
                self.executor.migrate(self.migrate_to)

    def test_forward_fails_on_non_monday_legacy_range(self):
        self._create_proposal(
            title='Tuesday legacy asset slot',
            proposal_status='DISCUSSION',
            start_at=datetime(2026, 6, 16, 9, 10, 0, tzinfo=UTC),
            end_at=datetime(2026, 6, 23, 9, 10, 0, tzinfo=UTC),
        )

        with patch(MIGRATION_NOW_PATCH, return_value=FIXED_NOW):
            with self.assertRaisesMessage(ValueError, 'invalid queue slot range'):
                self.executor = MigrationExecutor(connection)
                self.executor.migrate(self.migrate_to)
