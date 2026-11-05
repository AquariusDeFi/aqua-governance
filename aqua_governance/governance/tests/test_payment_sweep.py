"""The beat sweep and the two operator commands that go with it.

The sweep is the only caller of ``check_transaction`` that runs unattended, so what it
selects, in what order, and what it does when one row raises are all part of the security
change: v1 removes the blanket ``except Exception`` that used to make the loop accidentally
safe, and adds a claim rejection, a deadlock and every programming error as raise sites
inside it.
"""
import json
from datetime import timedelta
from io import StringIO
from unittest.mock import call, patch

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase
from django.utils import timezone

from rest_framework.test import APIClient

from django_quill.quill import Quill

from aqua_governance.governance import proposal_transactions, tasks
from aqua_governance.governance.models import ConsumedTransaction, HistoryProposal, Proposal, ProposalQueueSlot
from aqua_governance.governance.tests._factories import (
    DEFAULT_PROPOSED_BY,
    distinct_hash,
    make_asset_proposal_raw,
    make_general_proposal,
    patch_ice_circulating_supply,
)
from aqua_governance.governance.tests._promotions import ALERT, VERIFY_PAYMENT, PromotionTestCase, make, verifies
from aqua_governance.utils.payments import REASON_HORIZON_UNAVAILABLE, PaymentCheckResult


CHECK_TRANSACTION = 'aqua_governance.governance.proposal_transactions.check_transaction'
INSPECT_ENVELOPE = 'aqua_governance.governance.serializers_v2.inspect_envelope'
OWNER_CHECK = 'aqua_governance.governance.views.ProposalViewSet._reject_declared_owner_mismatch'
TRY_ACQUIRE = 'aqua_governance.governance.tasks._try_acquire_payment_sweep_lock'
RELEASE = 'aqua_governance.governance.tasks._release_payment_sweep_lock'


def _quill(html):
    return Quill(json.dumps({'delta': '', 'html': html}))


def not_found_since(age):
    """A not-found clock that started ``age`` ago."""
    return timezone.now() - age


def sweep_lock_is_held():
    """Whether this session holds the sweep's session-level advisory lock right now."""
    lock_id = settings.PROPOSAL_PAYMENT_SWEEP_ADVISORY_LOCK_ID
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid() "
            "AND classid = %s AND objid = %s AND objsubid = 1",
            [lock_id >> 32, lock_id & 0xFFFFFFFF],
        )
        return cursor.fetchone()[0] > 0


class ProposalSweepBase(TestCase):
    def setUp(self):
        super().setUp()
        self.ice_supply_patcher = patch_ice_circulating_supply()
        self.ice_supply_patcher.start()
        self.addCleanup(self.ice_supply_patcher.stop)

    def pending(self, index, **overrides):
        defaults = {
            'proposed_by': DEFAULT_PROPOSED_BY,
            'action': Proposal.TO_UPDATE,
            'transaction_hash': distinct_hash(index),
            'new_transaction_hash': distinct_hash(index + 1),
        }
        defaults.update(overrides)
        return make_general_proposal(**defaults)


class PaymentSweepIsolationTests(ProposalSweepBase):
    def test_a_raising_row_does_not_stop_the_rows_behind_it(self):
        first = self.pending(400)
        second = self.pending(410)

        def explode(proposal):
            if proposal.id == first.id:
                raise RuntimeError('poisoned row')
            return None

        with patch(CHECK_TRANSACTION, side_effect=explode) as mock_check:
            tasks.task_check_pending_proposal_payments()

        self.assertEqual(
            [args[0].id for args, _kwargs in mock_check.call_args_list],
            [first.id, second.id],
        )

    def test_a_raising_row_is_logged_with_its_proposal_id(self):
        proposal = self.pending(420)

        with patch(CHECK_TRANSACTION, side_effect=RuntimeError('poisoned row')):
            with self.assertLogs('aqua_governance.governance.tasks', level='ERROR') as logs:
                tasks.task_check_pending_proposal_payments()

        self.assertIn('Pending payment check failed.', logs.output[0])
        self.assertIn('RuntimeError: poisoned row', logs.output[0])
        self.assertEqual(logs.records[0].proposal_id, proposal.id)

    def test_rows_are_swept_in_id_order(self):
        first = self.pending(430)
        second = self.pending(440)
        third = self.pending(450)
        # An UPDATE rewrites the tuple at the end of the heap, so an unordered scan would
        # return the oldest row last.  Only order_by('id') makes a poison row diagnosable
        # from the position of the last log line.
        Proposal.objects.filter(id=first.id).update(title='Moved to the end of the heap')

        with patch(CHECK_TRANSACTION, return_value=None) as mock_check:
            tasks.task_check_pending_proposal_payments()

        self.assertEqual(
            [args[0].id for args, _kwargs in mock_check.call_args_list],
            [first.id, second.id, third.id],
        )


class PaymentSweepOverlapGuardTests(ProposalSweepBase):
    def test_a_tick_that_cannot_take_the_lock_touches_nothing(self):
        self.pending(460)

        with patch(TRY_ACQUIRE, return_value=False):
            with patch(CHECK_TRANSACTION) as mock_check:
                with self.assertLogs('aqua_governance.governance.tasks', level='INFO') as logs:
                    tasks.task_check_pending_proposal_payments()

        mock_check.assert_not_called()
        self.assertIn('Payment sweep already running', logs.output[0])

    def test_a_tick_that_cannot_take_the_lock_does_not_release_it(self):
        with patch(TRY_ACQUIRE, return_value=False):
            with patch(RELEASE) as mock_release:
                tasks.task_check_pending_proposal_payments()

        mock_release.assert_not_called()

    def test_the_lock_is_held_during_the_sweep_and_released_afterwards(self):
        self.pending(470)
        held_during_sweep = []

        def observe(proposal):
            held_during_sweep.append(sweep_lock_is_held())

        self.assertFalse(sweep_lock_is_held())
        with patch(CHECK_TRANSACTION, side_effect=observe):
            tasks.task_check_pending_proposal_payments()

        self.assertEqual(held_during_sweep, [True])
        self.assertFalse(sweep_lock_is_held())

    def test_the_lock_is_released_when_a_row_raises_out_of_the_loop(self):
        self.pending(480)

        # BaseException is not caught by the per-row handler, so it exercises the
        # ``finally`` rather than the ``except``.
        with patch(CHECK_TRANSACTION, side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                tasks.task_check_pending_proposal_payments()

        self.assertFalse(sweep_lock_is_held())


class PaymentSweepSelectionTests(ProposalSweepBase):
    def swept_ids(self):
        with patch(CHECK_TRANSACTION, return_value=None) as mock_check:
            tasks.task_check_pending_proposal_payments()
        return {args[0].id for args, _kwargs in mock_check.call_args_list}

    def test_a_row_that_was_never_rejected_is_swept(self):
        proposal = self.pending(500)

        self.assertEqual(proposal.payment_check_rejected_hash, None)
        self.assertIn(proposal.id, self.swept_ids())

    def test_a_row_rejected_under_its_own_staged_hash_is_not_swept(self):
        proposal = self.pending(510)
        Proposal.objects.filter(id=proposal.id).update(
            payment_check_rejected_hash=proposal.new_transaction_hash,
        )

        self.assertNotIn(proposal.id, self.swept_ids())

    def test_a_pending_create_rejected_under_its_creation_hash_is_not_swept(self):
        proposal = self.pending(
            520,
            action=Proposal.TO_CREATE,
            draft=True,
            new_transaction_hash=None,
        )
        Proposal.objects.filter(id=proposal.id).update(
            payment_check_rejected_hash=proposal.transaction_hash,
        )

        self.assertNotIn(proposal.id, self.swept_ids())

    def test_the_rejection_marker_is_read_from_the_column_the_action_uses(self):
        # A TO_CREATE row reads transaction_hash, so a marker matching new_transaction_hash
        # is somebody else's rejection and must not retire it - and the mirror case for an
        # update, whose pending hash is new_transaction_hash.
        create = self.pending(530, action=Proposal.TO_CREATE, draft=True)
        update = self.pending(540)
        Proposal.objects.filter(id=create.id).update(
            payment_check_rejected_hash=create.new_transaction_hash,
        )
        Proposal.objects.filter(id=update.id).update(
            payment_check_rejected_hash=update.transaction_hash,
        )

        swept = self.swept_ids()
        self.assertIn(create.id, swept)
        self.assertIn(update.id, swept)

    def test_a_row_rejected_under_a_different_hash_is_still_swept(self):
        proposal = self.pending(550)
        Proposal.objects.filter(id=proposal.id).update(
            payment_check_rejected_hash=distinct_hash(559),
        )

        self.assertIn(proposal.id, self.swept_ids())

    def test_an_empty_rejection_marker_retires_a_row_with_no_staged_hash(self):
        proposal = self.pending(560, new_transaction_hash=None)
        Proposal.objects.filter(id=proposal.id).update(
            new_transaction_hash='',
            payment_check_rejected_hash='',
        )

        self.assertNotIn(proposal.id, self.swept_ids())

    def test_a_corrective_re_stage_of_the_same_hash_re_arms_the_sweep(self):
        # The recovery from a memo mismatch is to fix the content and re-present the same
        # payment.  The marker would still match that hash, so if a staging write did not
        # clear it the row would leave the sweep for good and the payment would strand.
        proposal = self.pending(
            595,
            proposal_status=Proposal.DISCUSSION,
            new_title='Staged title',
            new_text=_quill('<p>Wrong text</p>'),
            new_envelope_xdr='update-xdr',
        )
        Proposal.objects.filter(id=proposal.id).update(
            payment_status=Proposal.BAD_MEMO,
            payment_check_rejected_hash=proposal.new_transaction_hash,
        )
        self.assertNotIn(proposal.id, self.swept_ids())

        with patch(INSPECT_ENVELOPE, return_value=Proposal.FINE), patch(OWNER_CHECK):
            response = APIClient().patch(
                '/api/proposal/{0}/'.format(proposal.id),
                {
                    'new_title': 'Staged title',
                    'new_text': '<p>Corrected text</p>',
                    'new_transaction_hash': proposal.new_transaction_hash,
                    'new_envelope_xdr': 'update-xdr',
                },
                format='json',
            )

        self.assertEqual(response.status_code, 200, response.data)
        proposal.refresh_from_db()
        self.assertIsNone(proposal.payment_check_rejected_hash)
        self.assertIn(proposal.id, self.swept_ids())

    def test_a_hash_not_found_for_the_whole_timeout_is_not_swept(self):
        create = self.pending(900, action=Proposal.TO_CREATE, draft=True, new_transaction_hash=None)
        submit = self.pending(910, action=Proposal.TO_SUBMIT)
        Proposal.objects.filter(id__in=[create.id, submit.id]).update(
            payment_status=Proposal.HORIZON_ERROR,
            payment_not_found_since=not_found_since(settings.PROPOSAL_PAYMENT_NOT_FOUND_TIMEOUT),
        )

        swept = self.swept_ids()
        self.assertNotIn(create.id, swept)
        self.assertNotIn(submit.id, swept)

    def test_a_hash_not_found_for_less_than_the_timeout_is_still_swept(self):
        proposal = self.pending(920)
        Proposal.objects.filter(id=proposal.id).update(
            payment_status=Proposal.HORIZON_ERROR,
            payment_not_found_since=not_found_since(settings.PROPOSAL_PAYMENT_NOT_FOUND_TIMEOUT - timedelta(minutes=5)),
        )

        self.assertIn(proposal.id, self.swept_ids())

    def test_hidden_and_actionless_rows_stay_out_of_the_sweep(self):
        hidden = self.pending(570, hide=True)
        idle = self.pending(580, action=Proposal.NONE)
        pending = self.pending(590)

        swept = self.swept_ids()
        self.assertEqual(swept, {pending.id})
        self.assertNotIn(hidden.id, swept)
        self.assertNotIn(idle.id, swept)


class BackfillConsumedTransactionsCommandTests(ProposalSweepBase):
    def test_it_burns_the_hashes_the_migration_would_have_burned(self):
        confirmed = self.pending(600, action=Proposal.NONE, new_transaction_hash=None)
        in_flight = self.pending(610, action=Proposal.TO_CREATE, draft=True, new_transaction_hash=None)
        HistoryProposal.objects.create(
            proposal=confirmed,
            version=1,
            title='Older version',
            text=confirmed.text,
            transaction_hash=distinct_hash(620),
            created_at=timezone.now(),
        )

        stdout = StringIO()
        call_command('backfill_consumed_transactions', stdout=stdout)

        burned = set(ConsumedTransaction.objects.values_list('transaction_hash', flat=True))
        self.assertEqual(burned, {confirmed.transaction_hash, distinct_hash(620)})
        self.assertNotIn(in_flight.transaction_hash, burned)
        self.assertIn('Source hashes read:      2', stdout.getvalue())
        self.assertIn('Ledger rows created:     2', stdout.getvalue())
        self.assertIn('Rows already present:    0', stdout.getvalue())
        self.assertIn('In-flight TO_CREATE:     1', stdout.getvalue())

    def test_a_second_run_writes_nothing_and_reports_the_rows_as_pre_existing(self):
        self.pending(630, action=Proposal.NONE, new_transaction_hash=None)
        call_command('backfill_consumed_transactions', stdout=StringIO())

        stdout = StringIO()
        call_command('backfill_consumed_transactions', stdout=stdout)

        self.assertEqual(ConsumedTransaction.objects.count(), 1)
        self.assertIn('Ledger rows created:     0', stdout.getvalue())
        self.assertIn('Rows already present:    1', stdout.getvalue())

    def test_a_dry_run_writes_no_ledger_row(self):
        self.pending(640, action=Proposal.NONE, new_transaction_hash=None)

        stdout = StringIO()
        call_command('backfill_consumed_transactions', '--dry-run', stdout=stdout)

        self.assertEqual(ConsumedTransaction.objects.count(), 0)
        self.assertIn('Dry run: no ledger row was written.', stdout.getvalue())
        self.assertIn('Source hashes read:      1', stdout.getvalue())
        self.assertIn('Hash case check:         no column holds two rows', stdout.getvalue())

    def test_a_dry_run_names_the_rows_that_will_abort_the_migration(self):
        # The dry run is the operator's only chance to find the migration's hard stop
        # before the maintenance window.
        upper = self.pending(660, action=Proposal.NONE, transaction_hash='AbCd' * 16, new_transaction_hash=None)
        lower = self.pending(670, action=Proposal.NONE, transaction_hash='abcd' * 16, new_transaction_hash=None)

        stdout = StringIO()
        call_command('backfill_consumed_transactions', '--dry-run', stdout=stdout)

        self.assertIn(
            'Proposal.transaction_hash holds 2 rows differing only in case',
            stdout.getvalue(),
        )
        self.assertIn(str(sorted([upper.id, lower.id])), stdout.getvalue())
        self.assertIn('Migration 0031 will abort until a human resolves them.', stdout.getvalue())


class RearmProposalPaymentCheckCommandTests(ProposalSweepBase):
    def rejected_submit(self, index):
        proposal = self.pending(index, action=Proposal.TO_SUBMIT)
        Proposal.objects.filter(id=proposal.id).update(
            payment_status=Proposal.INVALID_PAYMENT,
            payment_check_rejected_hash=proposal.new_transaction_hash,
        )
        proposal.refresh_from_db()
        return proposal

    def test_it_clears_the_rejection_and_puts_the_row_back_in_the_sweep(self):
        proposal = self.rejected_submit(700)

        call_command(
            'rearm_proposal_payment_check', proposal.id,
            action=Proposal.TO_SUBMIT, stdout=StringIO(),
        )

        proposal.refresh_from_db()
        self.assertIsNone(proposal.payment_check_rejected_hash)
        self.assertEqual(proposal.payment_status, Proposal.FINE)
        self.assertEqual(proposal.action, Proposal.TO_SUBMIT)

        with patch(CHECK_TRANSACTION, return_value=None) as mock_check:
            tasks.task_check_pending_proposal_payments()
        self.assertIn(proposal.id, {args[0].id for args, _kwargs in mock_check.call_args_list})

    def test_it_re_sets_the_action_a_rejected_creation_cleared(self):
        # A rejected creation is retired with draft=False, action=NONE, hide=True.
        proposal = self.pending(710, action=Proposal.NONE, draft=False, hide=True)
        Proposal.objects.filter(id=proposal.id).update(
            payment_status=Proposal.INVALID_PAYMENT,
            payment_check_rejected_hash=proposal.transaction_hash,
        )

        call_command(
            'rearm_proposal_payment_check', proposal.id,
            action=Proposal.TO_CREATE, unhide=True, stdout=StringIO(),
        )

        proposal.refresh_from_db()
        self.assertEqual(proposal.action, Proposal.TO_CREATE)
        self.assertFalse(proposal.hide)
        self.assertIsNone(proposal.payment_check_rejected_hash)
        # The command re-arms the payment check and nothing else, so draft stays where the
        # rejection left it.
        self.assertFalse(proposal.draft)

    def test_it_restarts_the_not_found_clock(self):
        proposal = self.pending(930, action=Proposal.TO_CREATE, draft=True, new_transaction_hash=None)
        Proposal.objects.filter(id=proposal.id).update(
            payment_status=Proposal.HORIZON_ERROR,
            payment_not_found_since=not_found_since(settings.PROPOSAL_PAYMENT_NOT_FOUND_TIMEOUT),
        )

        call_command(
            'rearm_proposal_payment_check', proposal.id,
            action=Proposal.TO_CREATE, stdout=StringIO(),
        )

        proposal.refresh_from_db()
        self.assertIsNone(proposal.payment_not_found_since)
        with patch(CHECK_TRANSACTION, return_value=None) as mock_check:
            tasks.task_check_pending_proposal_payments()
        self.assertIn(proposal.id, {args[0].id for args, _kwargs in mock_check.call_args_list})

    def test_hide_survives_unless_unhide_is_asked_for(self):
        proposal = self.pending(720, hide=True)

        call_command(
            'rearm_proposal_payment_check', proposal.id,
            action=Proposal.TO_UPDATE, stdout=StringIO(),
        )

        proposal.refresh_from_db()
        self.assertTrue(proposal.hide)

    def test_every_invocation_is_logged_at_error_with_the_before_and_after(self):
        proposal = self.rejected_submit(730)

        with self.assertLogs(
            'aqua_governance.governance.management.commands.rearm_proposal_payment_check',
            level='ERROR',
        ) as logs:
            call_command(
                'rearm_proposal_payment_check', proposal.id,
                action=Proposal.TO_SUBMIT, stdout=StringIO(),
            )

        self.assertIn('re-armed by hand', logs.output[0])
        self.assertEqual(logs.records[0].proposal_id, proposal.id)
        self.assertEqual(
            logs.records[0].rearm_before['payment_check_rejected_hash'],
            proposal.new_transaction_hash,
        )
        self.assertEqual(logs.records[0].rearm_before['payment_status'], Proposal.INVALID_PAYMENT)
        self.assertEqual(logs.records[0].rearm_after['payment_status'], Proposal.FINE)
        self.assertIsNone(logs.records[0].rearm_after['payment_check_rejected_hash'])

    def test_it_does_not_release_a_burned_hash(self):
        proposal = self.rejected_submit(740)
        ConsumedTransaction.objects.create(
            transaction_hash=proposal.new_transaction_hash,
            purpose=ConsumedTransaction.PURPOSE_SUBMIT,
        )

        call_command(
            'rearm_proposal_payment_check', proposal.id,
            action=Proposal.TO_SUBMIT, stdout=StringIO(),
        )

        self.assertTrue(
            ConsumedTransaction.objects.filter(transaction_hash=proposal.new_transaction_hash).exists(),
        )

    def test_an_unknown_proposal_is_an_error_and_changes_nothing(self):
        with self.assertRaises(CommandError):
            call_command(
                'rearm_proposal_payment_check', 10 ** 7,
                action=Proposal.TO_SUBMIT, stdout=StringIO(),
            )

    def test_an_unknown_action_is_rejected_by_the_parser(self):
        proposal = self.rejected_submit(750)

        with self.assertRaises(CommandError):
            call_command(
                'rearm_proposal_payment_check', proposal.id,
                action='TO_DELETE', stdout=StringIO(),
            )

        proposal.refresh_from_db()
        self.assertEqual(proposal.payment_status, Proposal.INVALID_PAYMENT)


class PaymentNotFoundClockTests(PromotionTestCase):
    """The clock that retires a pending hash Horizon has never found.

    Horizon answers 404 for a transaction that never reached the network, and that answer is
    retryable: the payment may land a moment after staging.  It stops being retryable once
    the hash has stayed unknown for ``PROPOSAL_PAYMENT_NOT_FOUND_TIMEOUT``.
    """

    def start_clock(self, proposal, age):
        Proposal.objects.filter(id=proposal.id).update(
            payment_status=Proposal.HORIZON_ERROR,
            payment_not_found_since=not_found_since(age),
        )

    def test_the_sweep_stops_asking_about_a_hash_not_found_for_the_timeout(self):
        proposal = self.pending_update(940)

        with patch(VERIFY_PAYMENT, side_effect=verifies(Proposal.HORIZON_ERROR)) as mock_verify:
            tasks.task_check_pending_proposal_payments()

        self.assertEqual(mock_verify.call_count, 1)
        proposal.refresh_from_db()
        self.assertEqual(proposal.payment_status, Proposal.HORIZON_ERROR)
        self.assertIsNotNone(proposal.payment_not_found_since)

        Proposal.objects.filter(id=proposal.id).update(
            payment_not_found_since=proposal.payment_not_found_since - settings.PROPOSAL_PAYMENT_NOT_FOUND_TIMEOUT,
        )
        with patch(VERIFY_PAYMENT, side_effect=verifies(Proposal.HORIZON_ERROR)) as mock_verify:
            tasks.task_check_pending_proposal_payments()

        mock_verify.assert_not_called()
        proposal.refresh_from_db()
        self.assertEqual(proposal.action, Proposal.TO_UPDATE)
        self.assertEqual(proposal.payment_status, Proposal.HORIZON_ERROR)
        self.assertFalse(proposal.hide)
        self.assertIsNone(proposal.payment_check_rejected_hash)

    def test_check_payment_still_asks_horizon_about_an_expired_hash(self):
        proposal = self.pending_update(1030)
        self.start_clock(proposal, settings.PROPOSAL_PAYMENT_NOT_FOUND_TIMEOUT)

        with patch(VERIFY_PAYMENT, side_effect=verifies(Proposal.HORIZON_ERROR)) as mock_verify:
            response = self.client.post('/api/proposal/{0}/check_payment/'.format(proposal.id))

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(mock_verify.call_count, 1)

    def test_a_repeated_not_found_answer_keeps_the_first_time(self):
        proposal = self.pending_update(950)
        self.start_clock(proposal, timedelta(hours=3))
        proposal.refresh_from_db()
        first_seen = proposal.payment_not_found_since

        with patch(VERIFY_PAYMENT, side_effect=verifies(Proposal.HORIZON_ERROR)):
            proposal_transactions.check_transaction(proposal)

        proposal.refresh_from_db()
        self.assertEqual(proposal.payment_not_found_since, first_seen)

    def test_an_unreachable_horizon_does_not_start_the_clock(self):
        proposal = self.pending_update(960)
        unreachable = PaymentCheckResult(
            payment_status=Proposal.HORIZON_ERROR,
            reason=REASON_HORIZON_UNAVAILABLE,
            transaction_hash=proposal.new_transaction_hash,
        )

        with patch(VERIFY_PAYMENT, return_value=unreachable):
            result = proposal_transactions.check_transaction(proposal)

        self.assertEqual(result['outcome'], 'payment_not_confirmed')
        proposal.refresh_from_db()
        self.assertEqual(proposal.payment_status, Proposal.HORIZON_ERROR)
        self.assertIsNone(proposal.payment_not_found_since)

    def test_a_promotion_clears_the_clock(self):
        for label, proposal in (
            ('create', self.pending_create(970)),
            ('update', self.pending_update(980)),
            ('submit', self.pending_submit(990)),
            ('asset create', make_asset_proposal_raw(
                proposed_by=DEFAULT_PROPOSED_BY,
                transaction_hash=distinct_hash(1000),
                draft=True,
                action=Proposal.TO_CREATE,
            )),
        ):
            with self.subTest(transition=label):
                self.start_clock(proposal, timedelta(hours=3))
                proposal.refresh_from_db()

                with patch(VERIFY_PAYMENT, side_effect=verifies()):
                    result = proposal_transactions.check_transaction(proposal)

                self.assertIn(result['outcome'], {'created', 'updated', 'booked'})
                proposal.refresh_from_db()
                self.assertEqual(proposal.action, Proposal.NONE)
                self.assertIsNone(proposal.payment_not_found_since)

    def test_a_found_payment_that_cannot_be_applied_yet_stops_the_clock(self):
        # The first sweep tick usually lands before the owner's transaction does, so a
        # payment that is later found but blocked - a taken queue slot, a same-asset
        # proposal still in flight - would otherwise be dropped a day after that 404.
        submit = self.pending_submit(1040)
        slot_holder = make(
            transaction_hash=distinct_hash(1050),
            proposal_status=Proposal.QUEUED,
            start_at=submit.new_start_at,
            end_at=submit.new_end_at,
        )
        ProposalQueueSlot.objects.create(
            proposal=slot_holder,
            start_at=submit.new_start_at,
            end_at=submit.new_end_at,
        )
        make_asset_proposal_raw(
            proposed_by=DEFAULT_PROPOSED_BY,
            transaction_hash=distinct_hash(1060),
            proposal_status=Proposal.QUEUED,
        )
        asset_create = make_asset_proposal_raw(
            proposed_by=DEFAULT_PROPOSED_BY,
            transaction_hash=distinct_hash(1070),
            draft=True,
            action=Proposal.TO_CREATE,
        )

        for label, proposal, outcome, action in (
            ('submit', submit, 'slot_conflict', Proposal.TO_SUBMIT),
            ('asset create', asset_create, 'asset_proposal_conflict', Proposal.TO_CREATE),
        ):
            with self.subTest(transition=label):
                self.start_clock(proposal, timedelta(hours=3))
                proposal.refresh_from_db()

                with patch(ALERT), patch(VERIFY_PAYMENT, side_effect=verifies()):
                    result = proposal_transactions.check_transaction(proposal)

                self.assertEqual(result['outcome'], outcome)
                proposal.refresh_from_db()
                self.assertEqual(proposal.action, action)
                self.assertEqual(proposal.payment_status, Proposal.FINE)
                self.assertIsNone(proposal.payment_not_found_since)

    @patch(OWNER_CHECK)
    @patch(INSPECT_ENVELOPE, return_value=Proposal.FINE)
    def test_re_staging_an_update_restarts_the_clock(self, _mock_inspect, _mock_owner):
        proposal = self.pending_update(1010)
        self.start_clock(proposal, settings.PROPOSAL_PAYMENT_NOT_FOUND_TIMEOUT)

        response = self.client.put(
            '/api/proposal/{0}/'.format(proposal.id),
            {
                'new_title': 'Staged title',
                'new_text': '<p>Staged text</p>',
                'new_transaction_hash': proposal.new_transaction_hash,
                'new_envelope_xdr': 'update-xdr',
            },
            format='json',
        )

        self.assertEqual(response.status_code, 200, response.data)
        proposal.refresh_from_db()
        self.assertIsNone(proposal.payment_not_found_since)

    @patch(OWNER_CHECK)
    @patch(INSPECT_ENVELOPE, return_value=Proposal.FINE)
    def test_re_staging_a_submit_restarts_the_clock(self, _mock_inspect, _mock_owner):
        proposal = self.pending_submit(1020)
        self.start_clock(proposal, settings.PROPOSAL_PAYMENT_NOT_FOUND_TIMEOUT)

        response = self.client.post(
            '/api/proposal/{0}/submit/'.format(proposal.id),
            {
                'start_at': proposal.new_start_at.isoformat().replace('+00:00', 'Z'),
                'end_at': proposal.new_end_at.isoformat().replace('+00:00', 'Z'),
                'new_envelope_xdr': 'submit-xdr',
                'new_transaction_hash': proposal.new_transaction_hash,
            },
            format='json',
        )

        self.assertEqual(response.status_code, 200, response.data)
        proposal.refresh_from_db()
        self.assertIsNone(proposal.payment_not_found_since)


class SweepCallShapeTests(ProposalSweepBase):
    def test_the_sweep_confirms_the_staged_copy(self):
        proposal = self.pending(760)

        with patch(CHECK_TRANSACTION, return_value=None) as mock_check:
            tasks.task_check_pending_proposal_payments()

        self.assertEqual(mock_check.call_args_list, [call(proposal)])
