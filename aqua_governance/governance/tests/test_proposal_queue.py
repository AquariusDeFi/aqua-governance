from datetime import datetime, timedelta, timezone as datetime_timezone
from unittest.mock import patch

from django.contrib.admin.sites import AdminSite
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIClient

from aqua_governance.governance.admin import ProposalQueueSlotAdmin
from aqua_governance.governance.models import Proposal, ProposalQueueSlot
from aqua_governance.governance.proposal_queue import (
    get_max_booking_datetime,
    has_exact_weekly_range,
    is_utc_monday_start,
    validate_weekly_queue_slot,
)
from aqua_governance.governance.proposal_queue_slots import (
    is_queue_slot_available,
    sync_proposal_queue_slot,
)
from aqua_governance.governance.tests._factories import make_asset_proposal_raw


UTC = datetime_timezone.utc


class ProposalQueueHelperTests(SimpleTestCase):
    def test_is_utc_monday_start_accepts_exact_utc_boundary(self):
        self.assertTrue(is_utc_monday_start(datetime(2026, 6, 8, 0, 0, 0, tzinfo=UTC)))

    def test_is_utc_monday_start_rejects_non_utc_aligned_datetime(self):
        plus_two = datetime_timezone(timedelta(hours=2))
        self.assertFalse(is_utc_monday_start(datetime(2026, 6, 8, 0, 0, 0, tzinfo=plus_two)))

    def test_has_exact_weekly_range_requires_last_sunday_second_boundary(self):
        start_at = datetime(2026, 6, 8, 0, 0, 0, tzinfo=UTC)
        self.assertTrue(has_exact_weekly_range(start_at, start_at + timedelta(days=7, seconds=-1)))
        self.assertFalse(has_exact_weekly_range(start_at, start_at + timedelta(days=7)))

    def test_get_max_booking_datetime_uses_last_sunday_second(self):
        now = datetime(2026, 6, 10, 15, 30, 0, tzinfo=UTC)

        self.assertEqual(
            get_max_booking_datetime(now=now, booking_horizon_weeks=6),
            datetime(2026, 7, 19, 23, 59, 59, tzinfo=UTC),
        )

    @override_settings(PROPOSAL_QUEUE_BOOKING_HORIZON_WEEKS=2)
    def test_validate_weekly_queue_slot_accepts_last_allowed_week(self):
        now = datetime(2026, 6, 10, 15, 30, 0, tzinfo=UTC)
        start_at = datetime(2026, 6, 15, 0, 0, 0, tzinfo=UTC)
        end_at = datetime(2026, 6, 21, 23, 59, 59, tzinfo=UTC)

        validate_weekly_queue_slot(start_at, end_at, now=now)

    def test_validate_weekly_queue_slot_rejects_next_monday_midnight_end(self):
        start_at = datetime(2026, 6, 15, 0, 0, 0, tzinfo=UTC)
        end_at = datetime(2026, 6, 22, 0, 0, 0, tzinfo=UTC)

        with self.assertRaises(ValidationError) as error:
            validate_weekly_queue_slot(start_at, end_at, now=datetime(2026, 6, 10, 15, 30, 0, tzinfo=UTC))

        self.assertEqual(
            error.exception.message_dict,
            {'end_at': ['end_at must be the following Sunday 23:59:59 UTC for a weekly queue slot.']},
        )

    @override_settings(PROPOSAL_QUEUE_BOOKING_HORIZON_WEEKS=2)
    def test_validate_weekly_queue_slot_rejects_outside_booking_horizon(self):
        now = datetime(2026, 6, 10, 15, 30, 0, tzinfo=UTC)
        start_at = datetime(2026, 6, 22, 0, 0, 0, tzinfo=UTC)
        end_at = datetime(2026, 6, 28, 23, 59, 59, tzinfo=UTC)

        with self.assertRaises(ValidationError) as error:
            validate_weekly_queue_slot(start_at, end_at, now=now)

        self.assertEqual(
            error.exception.message_dict,
            {'end_at': ['Selected queue slot falls outside the booking horizon.']},
        )

    def test_validate_weekly_queue_slot_rejects_non_monday_start(self):
        start_at = datetime(2026, 6, 9, 0, 0, 0, tzinfo=UTC)
        end_at = datetime(2026, 6, 15, 23, 59, 59, tzinfo=UTC)

        with self.assertRaises(ValidationError) as error:
            validate_weekly_queue_slot(start_at, end_at, now=datetime(2026, 6, 1, 0, 0, 0, tzinfo=UTC))

        self.assertEqual(
            error.exception.message_dict,
            {'start_at': ['start_at must be a UTC Monday 00:00:00.']},
        )


class ProposalQueueSlotModelTests(TestCase):
    def _make_proposal(self, **overrides):
        return make_asset_proposal_raw(
            proposal_type=overrides.pop('proposal_type', Proposal.PROPOSAL_TYPE_GENERAL),
            **overrides,
        )

    def test_slot_start_at_must_be_unique(self):
        start_at = datetime(2026, 6, 8, 0, 0, 0, tzinfo=UTC)
        end_at = datetime(2026, 6, 14, 23, 59, 59, tzinfo=UTC)
        ProposalQueueSlot.objects.create(
            proposal=self._make_proposal(title='One'),
            start_at=start_at,
            end_at=end_at,
        )

        with self.assertRaises(IntegrityError):
            ProposalQueueSlot.objects.create(
                proposal=self._make_proposal(title='Two'),
                start_at=start_at,
                end_at=end_at,
            )

    def test_proposal_can_only_have_one_slot(self):
        proposal = self._make_proposal(title='One')
        ProposalQueueSlot.objects.create(
            proposal=proposal,
            start_at=datetime(2026, 6, 8, 0, 0, 0, tzinfo=UTC),
            end_at=datetime(2026, 6, 14, 23, 59, 59, tzinfo=UTC),
        )

        with self.assertRaises(IntegrityError):
            ProposalQueueSlot.objects.create(
                proposal=proposal,
                start_at=datetime(2026, 6, 15, 0, 0, 0, tzinfo=UTC),
                end_at=datetime(2026, 6, 21, 23, 59, 59, tzinfo=UTC),
            )

    def test_queue_slot_availability_checks_overlaps(self):
        proposal = self._make_proposal(
            title='Occupied',
            proposal_status=Proposal.QUEUED,
            start_at=datetime(2026, 6, 8, 0, 0, 0, tzinfo=UTC),
            end_at=datetime(2026, 6, 14, 23, 59, 59, tzinfo=UTC),
        )
        ProposalQueueSlot.objects.create(
            proposal=proposal,
            start_at=datetime(2026, 6, 8, 0, 0, 0, tzinfo=UTC),
            end_at=datetime(2026, 6, 14, 23, 59, 59, tzinfo=UTC),
        )

        self.assertFalse(
            is_queue_slot_available(
                datetime(2026, 6, 8, 0, 0, 0, tzinfo=UTC),
                datetime(2026, 6, 14, 23, 59, 59, tzinfo=UTC),
            )
        )
        self.assertFalse(
            is_queue_slot_available(
                datetime(2026, 6, 14, 23, 0, 0, tzinfo=UTC),
                datetime(2026, 6, 21, 23, 59, 59, tzinfo=UTC),
            )
        )
        self.assertTrue(
            is_queue_slot_available(
                datetime(2026, 6, 15, 0, 0, 0, tzinfo=UTC),
                datetime(2026, 6, 21, 23, 59, 59, tzinfo=UTC),
            )
        )
        self.assertTrue(
            is_queue_slot_available(
                datetime(2026, 6, 8, 0, 0, 0, tzinfo=UTC),
                datetime(2026, 6, 14, 23, 59, 59, tzinfo=UTC),
                exclude_proposal_id=proposal.id,
            )
        )

    def test_queue_slot_availability_ignores_slotless_queued_and_voting_rows(self):
        start_at = datetime(2026, 6, 8, 0, 0, 0, tzinfo=UTC)
        end_at = datetime(2026, 6, 14, 23, 59, 59, tzinfo=UTC)

        self._make_proposal(
            title='Legacy queued proposal',
            start_at=start_at,
            end_at=end_at,
            proposal_status=Proposal.QUEUED,
            action=Proposal.NONE,
        )
        self._make_proposal(
            title='Legacy voting proposal',
            start_at=start_at,
            end_at=end_at,
            proposal_status=Proposal.VOTING,
            action=Proposal.NONE,
            transaction_hash='f' * 64,
        )

        self.assertTrue(is_queue_slot_available(start_at, end_at))

    def test_queue_slot_availability_ignores_mismatched_queued_and_voting_slot_rows(self):
        occupied_start = datetime(2026, 6, 8, 0, 0, 0, tzinfo=UTC)
        occupied_end = datetime(2026, 6, 14, 23, 59, 59, tzinfo=UTC)
        actual_start = datetime(2026, 6, 15, 0, 0, 0, tzinfo=UTC)
        actual_end = datetime(2026, 6, 21, 23, 59, 59, tzinfo=UTC)

        for status in (Proposal.QUEUED, Proposal.VOTING):
            with self.subTest(status=status):
                ProposalQueueSlot.objects.all().delete()
                Proposal.objects.all().delete()

                ghost = self._make_proposal(
                    title=f'Ghost {status.lower()} proposal',
                    proposal_status=status,
                    start_at=actual_start,
                    end_at=actual_end,
                )
                ProposalQueueSlot.objects.create(
                    proposal=ghost,
                    start_at=occupied_start,
                    end_at=occupied_end,
                )

                self.assertTrue(is_queue_slot_available(occupied_start, occupied_end))

    def test_queue_slot_availability_ignores_slotless_discussion_rows(self):
        self._make_proposal(
            title='Obsolete discussion reservation',
            start_at=datetime(2026, 6, 8, 0, 0, 0, tzinfo=UTC),
            end_at=datetime(2026, 6, 14, 23, 59, 59, tzinfo=UTC),
            proposal_status=Proposal.DISCUSSION,
            action=Proposal.NONE,
        )

        self.assertTrue(
            is_queue_slot_available(
                datetime(2026, 6, 8, 0, 0, 0, tzinfo=UTC),
                datetime(2026, 6, 14, 23, 59, 59, tzinfo=UTC),
            )
        )

    def test_sync_removes_slots_for_non_public_or_pending_proposals(self):
        start_at = datetime(2026, 6, 8, 0, 0, 0, tzinfo=UTC)
        end_at = datetime(2026, 6, 14, 23, 59, 59, tzinfo=UTC)
        scenarios = [
            ('hidden', {'hide': True}),
            ('draft', {'draft': True}),
            ('pending', {'action': Proposal.TO_SUBMIT}),
        ]

        for label, updates in scenarios:
            with self.subTest(label=label):
                proposal = self._make_proposal(
                    title=f'{label.title()} queued proposal',
                    proposal_status=Proposal.QUEUED,
                    action=Proposal.NONE,
                    start_at=start_at,
                    end_at=end_at,
                )
                ProposalQueueSlot.objects.create(
                    proposal=proposal,
                    start_at=start_at,
                    end_at=end_at,
                )

                for field_name, value in updates.items():
                    setattr(proposal, field_name, value)

                sync_proposal_queue_slot(proposal)

                self.assertFalse(ProposalQueueSlot.objects.filter(proposal=proposal).exists())

    def test_queue_slot_availability_ignores_hidden_draft_and_pending_slot_rows(self):
        start_at = datetime(2026, 6, 8, 0, 0, 0, tzinfo=UTC)
        end_at = datetime(2026, 6, 14, 23, 59, 59, tzinfo=UTC)
        scenarios = [
            ('hidden', {'hide': True}),
            ('draft', {'draft': True}),
            ('pending', {'action': Proposal.TO_SUBMIT}),
        ]

        for label, overrides in scenarios:
            with self.subTest(label=label):
                ProposalQueueSlot.objects.all().delete()
                proposal = self._make_proposal(
                    title=f'{label.title()} slot row',
                    proposal_status=Proposal.QUEUED,
                    start_at=start_at,
                    end_at=end_at,
                    **overrides,
                )
                ProposalQueueSlot.objects.create(
                    proposal=proposal,
                    start_at=start_at,
                    end_at=end_at,
                )

                self.assertTrue(is_queue_slot_available(start_at, end_at))

    def test_sync_reclaims_mismatched_queued_and_voting_slot_rows_for_same_start(self):
        target_start = datetime(2026, 6, 8, 0, 0, 0, tzinfo=UTC)
        target_end = datetime(2026, 6, 14, 23, 59, 59, tzinfo=UTC)
        ghost_start = datetime(2026, 6, 15, 0, 0, 0, tzinfo=UTC)
        ghost_end = datetime(2026, 6, 21, 23, 59, 59, tzinfo=UTC)

        for status in (Proposal.QUEUED, Proposal.VOTING):
            with self.subTest(status=status):
                ProposalQueueSlot.objects.all().delete()
                Proposal.objects.all().delete()

                ghost = self._make_proposal(
                    title=f'Ghost {status.lower()} proposal',
                    proposal_status=status,
                    start_at=ghost_start,
                    end_at=ghost_end,
                )
                ProposalQueueSlot.objects.create(
                    proposal=ghost,
                    start_at=target_start,
                    end_at=target_end,
                )

                target = self._make_proposal(
                    title='Target proposal',
                    proposal_status=Proposal.QUEUED,
                    start_at=target_start,
                    end_at=target_end,
                )

                sync_proposal_queue_slot(target)

                self.assertFalse(ProposalQueueSlot.objects.filter(proposal=ghost).exists())
                self.assertTrue(
                    ProposalQueueSlot.objects.filter(
                        proposal=target,
                        start_at=target_start,
                        end_at=target_end,
                    ).exists()
                )

    def test_sync_update_recovers_from_integrity_error_inside_outer_transaction(self):
        original_start = datetime(2026, 6, 8, 0, 0, 0, tzinfo=UTC)
        original_end = datetime(2026, 6, 14, 23, 59, 59, tzinfo=UTC)
        target_start = datetime(2026, 6, 15, 0, 0, 0, tzinfo=UTC)
        target_end = datetime(2026, 6, 21, 23, 59, 59, tzinfo=UTC)
        target = self._make_proposal(
            title='Target proposal',
            proposal_status=Proposal.QUEUED,
            start_at=original_start,
            end_at=original_end,
        )
        ProposalQueueSlot.objects.create(
            proposal=target,
            start_at=original_start,
            end_at=original_end,
        )
        stale = self._make_proposal(
            title='Hidden stale proposal',
            proposal_status=Proposal.QUEUED,
            hide=True,
            start_at=target_start,
            end_at=target_end,
        )
        ProposalQueueSlot.objects.create(
            proposal=stale,
            start_at=target_start,
            end_at=target_end,
        )
        target.start_at = target_start
        target.end_at = target_end

        from aqua_governance.governance import proposal_queue_slots as proposal_queue_module

        original_delete = proposal_queue_module._delete_stale_queue_slots_for_start_at
        delete_call_count = {'count': 0}

        def delayed_delete(*args, **kwargs):
            delete_call_count['count'] += 1
            if delete_call_count['count'] == 1:
                return 0
            return original_delete(*args, **kwargs)

        with patch(
            'aqua_governance.governance.proposal_queue_slots._delete_stale_queue_slots_for_start_at',
            side_effect=delayed_delete,
        ):
            with transaction.atomic():
                sync_proposal_queue_slot(target)
                self.assertTrue(
                    ProposalQueueSlot.objects.filter(
                        proposal=target,
                        start_at=target_start,
                        end_at=target_end,
                    ).exists()
                )

        self.assertFalse(ProposalQueueSlot.objects.filter(proposal=stale).exists())


EXPECTED_SLOT_KEYS = {
    'proposal',
    'proposal_title',
    'proposal_status',
    'start_at',
    'end_at',
    'occupied_at',
}


class ProposalQueueApiTests(TestCase):
    def _make_proposal(self, **overrides):
        return make_asset_proposal_raw(
            proposal_type=overrides.pop('proposal_type', Proposal.PROPOSAL_TYPE_GENERAL),
            proposal_status=overrides.pop('proposal_status', Proposal.QUEUED),
            draft=overrides.pop('draft', False),
            hide=overrides.pop('hide', False),
            **overrides,
        )

    def _make_slot(self, proposal=None, start_at=None, end_at=None):
        if proposal is None:
            proposal = self._make_proposal()
        if start_at is None:
            start_at = datetime(2026, 6, 8, 0, 0, 0, tzinfo=UTC)
        if end_at is None:
            end_at = datetime(2026, 6, 14, 23, 59, 59, tzinfo=UTC)
        if proposal.start_at != start_at or proposal.end_at != end_at:
            proposal.start_at = start_at
            proposal.end_at = end_at
            proposal.save(update_fields=['start_at', 'end_at'])
        return ProposalQueueSlot.objects.create(
            proposal=proposal,
            start_at=start_at,
            end_at=end_at,
        )

    def setUp(self):
        Proposal.objects.all().delete()
        ProposalQueueSlot.objects.all().delete()
        self.client = APIClient()

    def test_response_shape(self):
        proposal = self._make_proposal(title='Queued proposal')
        self._make_slot(proposal=proposal)
        response = self.client.get('/api/proposal-queue/')

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['count'], 1)
        self.assertEqual(len(body['results']), 1)
        self.assertEqual(
            set(body.keys()),
            {'count', 'next', 'previous', 'results', 'max_booking_datetime', 'booking_horizon_weeks'},
        )

        slot = body['results'][0]
        self.assertEqual(set(slot.keys()), EXPECTED_SLOT_KEYS)
        self.assertEqual(slot['proposal'], proposal.id)
        self.assertEqual(slot['proposal_title'], proposal.title)
        self.assertEqual(slot['proposal_status'], proposal.proposal_status)

    def test_response_includes_max_booking_datetime_and_horizon(self):
        self._make_slot()
        response = self.client.get('/api/proposal-queue/')

        body = response.json()
        self.assertIn('max_booking_datetime', body)
        self.assertIsNotNone(body['max_booking_datetime'])
        self.assertIn('booking_horizon_weeks', body)
        self.assertGreater(body['booking_horizon_weeks'], 0)

    def test_returns_only_current_and_future_slots(self):
        # past slot
        past_start = datetime(2026, 1, 5, 0, 0, 0, tzinfo=UTC)
        past_end = datetime(2026, 1, 11, 23, 59, 59, tzinfo=UTC)
        self._make_slot(
            proposal=self._make_proposal(title='Past'),
            start_at=past_start,
            end_at=past_end,
        )
        # future slot
        future_start = datetime(2026, 7, 6, 0, 0, 0, tzinfo=UTC)
        future_end = datetime(2026, 7, 12, 23, 59, 59, tzinfo=UTC)
        future_slot = self._make_slot(
            proposal=self._make_proposal(title='Future'),
            start_at=future_start,
            end_at=future_end,
        )

        response = self.client.get('/api/proposal-queue/')

        body = response.json()
        self.assertEqual(body['count'], 1)
        self.assertEqual(body['results'][0]['proposal'], future_slot.proposal_id)

    def test_returns_multiple_occupied_slots(self):
        slot1 = self._make_slot(
            proposal=self._make_proposal(title='One'),
            start_at=datetime(2026, 7, 6, 0, 0, 0, tzinfo=UTC),
            end_at=datetime(2026, 7, 12, 23, 59, 59, tzinfo=UTC),
        )
        slot2 = self._make_slot(
            proposal=self._make_proposal(title='Two'),
            start_at=datetime(2026, 7, 13, 0, 0, 0, tzinfo=UTC),
            end_at=datetime(2026, 7, 19, 23, 59, 59, tzinfo=UTC),
        )

        response = self.client.get('/api/proposal-queue/')

        body = response.json()
        self.assertEqual(body['count'], 2)
        result_ids = {r['proposal'] for r in body['results']}
        self.assertEqual(result_ids, {slot1.proposal_id, slot2.proposal_id})

    def test_only_get_method_allowed(self):
        # POST to list endpoint not allowed (no CreateModelMixin)
        response = self.client.post('/api/proposal-queue/', {}, format='json')
        self.assertEqual(response.status_code, 405)

        # PUT/DELETE on detail rout not available (no detail actions registered)
        response = self.client.put('/api/proposal-queue/1/', {}, format='json')
        self.assertEqual(response.status_code, 404)

        response = self.client.delete('/api/proposal-queue/1/')
        self.assertEqual(response.status_code, 404)

    def test_queue_excludes_hidden_proposal_slot(self):
        start_at_hidden = datetime(2026, 7, 6, 0, 0, 0, tzinfo=UTC)
        end_at_hidden = datetime(2026, 7, 12, 23, 59, 59, tzinfo=UTC)
        hidden = self._make_proposal(title='Hidden proposal', hide=True)
        self._make_slot(proposal=hidden, start_at=start_at_hidden, end_at=end_at_hidden)

        start_at_visible = datetime(2026, 7, 13, 0, 0, 0, tzinfo=UTC)
        end_at_visible = datetime(2026, 7, 19, 23, 59, 59, tzinfo=UTC)
        visible = self._make_proposal(title='Visible proposal')
        visible_slot = self._make_slot(proposal=visible, start_at=start_at_visible, end_at=end_at_visible)

        response = self.client.get('/api/proposal-queue/')

        body = response.json()
        self.assertEqual(body['count'], 1)
        self.assertEqual(body['results'][0]['proposal'], visible_slot.proposal_id)

    def test_queue_excludes_draft_proposal_slot(self):
        start_at_draft = datetime(2026, 7, 6, 0, 0, 0, tzinfo=UTC)
        end_at_draft = datetime(2026, 7, 12, 23, 59, 59, tzinfo=UTC)
        draft = self._make_proposal(title='Draft proposal', draft=True)
        self._make_slot(proposal=draft, start_at=start_at_draft, end_at=end_at_draft)

        start_at_published = datetime(2026, 7, 13, 0, 0, 0, tzinfo=UTC)
        end_at_published = datetime(2026, 7, 19, 23, 59, 59, tzinfo=UTC)
        published = self._make_proposal(title='Published proposal')
        published_slot = self._make_slot(proposal=published, start_at=start_at_published, end_at=end_at_published)

        response = self.client.get('/api/proposal-queue/')

        body = response.json()
        self.assertEqual(body['count'], 1)
        self.assertEqual(body['results'][0]['proposal'], published_slot.proposal_id)

    def test_queue_excludes_pending_action_proposal_slot(self):
        start_at_pending = datetime(2026, 7, 6, 0, 0, 0, tzinfo=UTC)
        end_at_pending = datetime(2026, 7, 12, 23, 59, 59, tzinfo=UTC)
        pending = self._make_proposal(title='Pending proposal', action=Proposal.TO_SUBMIT)
        self._make_slot(proposal=pending, start_at=start_at_pending, end_at=end_at_pending)

        start_at_visible = datetime(2026, 7, 13, 0, 0, 0, tzinfo=UTC)
        end_at_visible = datetime(2026, 7, 19, 23, 59, 59, tzinfo=UTC)
        visible = self._make_proposal(title='Visible proposal')
        visible_slot = self._make_slot(proposal=visible, start_at=start_at_visible, end_at=end_at_visible)

        response = self.client.get('/api/proposal-queue/')

        body = response.json()
        self.assertEqual(body['count'], 1)
        self.assertEqual(body['results'][0]['proposal'], visible_slot.proposal_id)

    def test_queue_excludes_discussion_and_expired_proposals_with_slots(self):
        slot_week_1_start = datetime(2026, 7, 6, 0, 0, 0, tzinfo=UTC)
        slot_week_1_end = datetime(2026, 7, 12, 23, 59, 59, tzinfo=UTC)
        slot_week_2_start = datetime(2026, 7, 13, 0, 0, 0, tzinfo=UTC)
        slot_week_2_end = datetime(2026, 7, 19, 23, 59, 59, tzinfo=UTC)
        slot_week_3_start = datetime(2026, 7, 20, 0, 0, 0, tzinfo=UTC)
        slot_week_3_end = datetime(2026, 7, 26, 23, 59, 59, tzinfo=UTC)
        slot_week_4_start = datetime(2026, 7, 27, 0, 0, 0, tzinfo=UTC)
        slot_week_4_end = datetime(2026, 8, 2, 23, 59, 59, tzinfo=UTC)

        discussion = self._make_proposal(
            title='Discussion with slot',
            proposal_status=Proposal.DISCUSSION,
        )
        self._make_slot(proposal=discussion, start_at=slot_week_1_start, end_at=slot_week_1_end)

        expired = self._make_proposal(
            title='Expired with slot',
            proposal_status=Proposal.EXPIRED,
        )
        self._make_slot(proposal=expired, start_at=slot_week_2_start, end_at=slot_week_2_end)

        voted = self._make_proposal(
            title='Voted with slot',
            proposal_status=Proposal.VOTED,
        )
        self._make_slot(proposal=voted, start_at=slot_week_3_start, end_at=slot_week_3_end)

        queued = self._make_proposal(title='Queued proposal')
        queued_slot = self._make_slot(proposal=queued, start_at=slot_week_4_start, end_at=slot_week_4_end)

        response = self.client.get('/api/proposal-queue/')

        body = response.json()
        # Only QUEUED and VOTING proposals should appear.
        self.assertEqual(body['count'], 1)
        self.assertEqual(body['results'][0]['proposal'], queued_slot.proposal_id)

    def test_queue_includes_public_voting_slot(self):
        slot_start = datetime(2026, 7, 6, 0, 0, 0, tzinfo=UTC)
        slot_end = datetime(2026, 7, 12, 23, 59, 59, tzinfo=UTC)
        voting_proposal = self._make_proposal(
            title='Active voting proposal',
            proposal_status=Proposal.VOTING,
        )
        voting_slot = self._make_slot(
            proposal=voting_proposal,
            start_at=slot_start,
            end_at=slot_end,
        )

        response = self.client.get('/api/proposal-queue/')

        body = response.json()
        self.assertEqual(body['count'], 1)
        self.assertEqual(body['results'][0]['proposal'], voting_slot.proposal_id)
        self.assertEqual(body['results'][0]['proposal_status'], Proposal.VOTING)

    def test_queue_api_and_availability_ignore_slotless_legacy_rows_but_include_slot_backed_rows(self):
        queued_start = datetime(2026, 7, 6, 0, 0, 0, tzinfo=UTC)
        queued_end = datetime(2026, 7, 12, 23, 59, 59, tzinfo=UTC)
        voting_start = datetime(2026, 7, 13, 0, 0, 0, tzinfo=UTC)
        voting_end = datetime(2026, 7, 19, 23, 59, 59, tzinfo=UTC)

        self._make_proposal(
            title='Legacy queued without slot',
            proposal_status=Proposal.QUEUED,
            start_at=queued_start,
            end_at=queued_end,
        )
        self._make_proposal(
            title='Legacy voting without slot',
            proposal_status=Proposal.VOTING,
            start_at=voting_start,
            end_at=voting_end,
            transaction_hash='a' * 64,
        )

        self.assertTrue(is_queue_slot_available(queued_start, queued_end))
        self.assertTrue(is_queue_slot_available(voting_start, voting_end))

        response = self.client.get('/api/proposal-queue/')
        self.assertEqual(response.json()['count'], 0)

        queued_slot = self._make_slot(
            proposal=self._make_proposal(
                title='Slot-backed queued proposal',
                proposal_status=Proposal.QUEUED,
                transaction_hash='b' * 64,
            ),
            start_at=queued_start,
            end_at=queued_end,
        )
        voting_slot = self._make_slot(
            proposal=self._make_proposal(
                title='Slot-backed voting proposal',
                proposal_status=Proposal.VOTING,
                transaction_hash='c' * 64,
            ),
            start_at=voting_start,
            end_at=voting_end,
        )

        response = self.client.get('/api/proposal-queue/')

        body = response.json()
        self.assertFalse(is_queue_slot_available(queued_start, queued_end))
        self.assertFalse(is_queue_slot_available(voting_start, voting_end))
        self.assertEqual(body['count'], 2)
        self.assertEqual(
            {result['proposal'] for result in body['results']},
            {queued_slot.proposal_id, voting_slot.proposal_id},
        )

    def test_queue_api_excludes_mismatched_queued_and_voting_slot_rows(self):
        slot_start = datetime(2026, 7, 6, 0, 0, 0, tzinfo=UTC)
        slot_end = datetime(2026, 7, 12, 23, 59, 59, tzinfo=UTC)
        actual_start = datetime(2026, 7, 13, 0, 0, 0, tzinfo=UTC)
        actual_end = datetime(2026, 7, 19, 23, 59, 59, tzinfo=UTC)

        for status in (Proposal.QUEUED, Proposal.VOTING):
            with self.subTest(status=status):
                ProposalQueueSlot.objects.all().delete()
                Proposal.objects.all().delete()

                ghost = self._make_proposal(
                    title=f'Ghost {status.lower()} proposal',
                    proposal_status=status,
                    start_at=actual_start,
                    end_at=actual_end,
                )
                ProposalQueueSlot.objects.create(
                    proposal=ghost,
                    start_at=slot_start,
                    end_at=slot_end,
                )

                response = self.client.get('/api/proposal-queue/')

                body = response.json()
                self.assertEqual(body['count'], 0)
                self.assertEqual(body['results'], [])


class ProposalQueueSlotAdminTests(TestCase):
    def setUp(self):
        self.site = AdminSite()
        self.admin = ProposalQueueSlotAdmin(ProposalQueueSlot, self.site)

    def test_admin_is_registered(self):
        """Smoke test: admin class can be instantiated."""
        self.assertIsNotNone(self.admin)

    def test_admin_has_no_add_permission(self):
        self.assertFalse(self.admin.has_add_permission(None))

    def test_admin_has_no_change_permission(self):
        self.assertFalse(self.admin.has_change_permission(None))

    def test_admin_has_no_delete_permission(self):
        self.assertFalse(self.admin.has_delete_permission(None))

    def test_admin_list_display_includes_key_fields(self):
        list_display = self.admin.get_list_display(None)
        self.assertNotIn('id', list_display)
        self.assertIn('proposal_id', list_display)
        self.assertIn('proposal_title', list_display)
        self.assertIn('proposal_status', list_display)
        self.assertIn('start_at', list_display)
        self.assertIn('end_at', list_display)
        self.assertIn('occupied_at', list_display)
        self.assertNotIn('created_at', list_display)
        self.assertNotIn('updated_at', list_display)
