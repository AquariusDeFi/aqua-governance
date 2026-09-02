"""The claim and the transition it pays for commit together, or not at all.

A claim that outlives a rolled-back transition burns a legitimate payment forever, which is
worse than the replay it exists to stop; a transition that commits without its claim leaves
the payment spendable again.  These tests drive both directions, plus the conditional-UPDATE
guard (I-4') that stops a loser's stale verdict landing on a row another worker has already
promoted, and the fingerprint guard that stops a verdict being applied to a restaged copy.
"""
from datetime import timedelta
from unittest.mock import patch

from django.test import override_settings

from aqua_governance.governance import proposal_transactions
from aqua_governance.governance.models import ConsumedTransaction, HistoryProposal, Proposal, ProposalQueueSlot
from aqua_governance.governance.tests._factories import DEFAULT_PROPOSED_BY, distinct_hash, make_asset_proposal_raw
from aqua_governance.governance.tests._promotions import (
    ALERT,
    CHECK_STATUS,
    CLAIM,
    DEFAULT_TITLE,
    PromotionTestCase,
    competing_claim_then_delegate,
    fine,
)
from aqua_governance.governance.transitions import UpdateTransition


@override_settings(DEBUG=False)
class PromotionAtomicityTests(PromotionTestCase):
    def test_a_failed_update_promotion_leaves_no_orphan_history_row(self):
        proposal = self.pending_update(1)

        with patch(CHECK_STATUS, return_value=fine(proposal.new_transaction_hash)):
            with patch.object(Proposal, 'save', side_effect=RuntimeError('write failed')):
                with self.assertRaises(RuntimeError):
                    proposal_transactions.check_transaction(proposal)

        self.assertEqual(HistoryProposal.objects.count(), 0)
        self.assertEqual(ConsumedTransaction.objects.count(), 0)
        proposal.refresh_from_db()
        self.assertEqual(proposal.action, Proposal.TO_UPDATE)
        self.assertEqual(proposal.title, DEFAULT_TITLE)
        self.assertEqual(proposal.version, 1)

    def test_an_update_commits_its_claim_and_its_state_change_together(self):
        proposal = self.pending_update(3)
        paid_hash = proposal.new_transaction_hash

        with patch(CHECK_STATUS, return_value=fine(paid_hash)):
            result = proposal_transactions.check_transaction(proposal)

        self.assertEqual(result['outcome'], 'updated')
        proposal.refresh_from_db()
        self.assertEqual(proposal.action, Proposal.NONE)
        self.assertEqual(proposal.title, 'Staged title')
        self.assertEqual(proposal.text.html, '<p>Staged text</p>')
        self.assertEqual(proposal.transaction_hash, paid_hash)
        self.assertEqual(proposal.version, 2)
        self.assertEqual(HistoryProposal.objects.filter(proposal=proposal).count(), 1)
        claim = ConsumedTransaction.objects.get()
        self.assertEqual(claim.transaction_hash, paid_hash)
        self.assertEqual(claim.purpose, ConsumedTransaction.PURPOSE_UPDATE)
        self.assertEqual(claim.payer, DEFAULT_PROPOSED_BY)

    def test_an_update_promotion_clears_the_staged_columns_it_consumed(self):
        proposal = self.pending_update(5)

        with patch(CHECK_STATUS, return_value=fine(proposal.new_transaction_hash)):
            proposal_transactions.check_transaction(proposal)

        proposal.refresh_from_db()
        self.assertIsNone(proposal.new_title)
        self.assertIsNone(proposal.new_transaction_hash)
        self.assertIsNone(proposal.new_envelope_xdr)
        # A QuillField assigned None persists the empty-Quill sentinel, because the
        # descriptor re-wraps it on the way out; the column itself must still go NULL.
        self.assertTrue(Proposal.objects.filter(id=proposal.id, new_text__isnull=True).exists())

    def test_a_claim_that_raises_rolls_the_whole_update_back(self):
        proposal = self.pending_update(7)

        with patch(CHECK_STATUS, return_value=fine(proposal.new_transaction_hash)):
            with patch(CLAIM, side_effect=RuntimeError('ledger unavailable')):
                with self.assertRaises(RuntimeError):
                    proposal_transactions.check_transaction(proposal)

        proposal.refresh_from_db()
        self.assertEqual(proposal.action, Proposal.TO_UPDATE)
        self.assertEqual(proposal.title, DEFAULT_TITLE)
        self.assertEqual(HistoryProposal.objects.count(), 0)
        self.assertEqual(ConsumedTransaction.objects.count(), 0)

    def test_a_general_create_commits_its_claim_and_its_state_change_together(self):
        proposal = self.pending_create(9)

        with patch(CHECK_STATUS, return_value=fine(proposal.transaction_hash)):
            result = proposal_transactions.check_transaction(proposal)

        self.assertEqual(result['outcome'], 'created')
        proposal.refresh_from_db()
        self.assertFalse(proposal.draft)
        self.assertEqual(proposal.action, Proposal.NONE)
        self.assertEqual(proposal.payment_status, Proposal.FINE)
        claim = ConsumedTransaction.objects.get()
        self.assertEqual(claim.transaction_hash, proposal.transaction_hash)
        self.assertEqual(claim.purpose, ConsumedTransaction.PURPOSE_CREATE)

    def test_a_claim_that_raises_rolls_the_whole_general_create_back(self):
        proposal = self.pending_create(10)

        with patch(CHECK_STATUS, return_value=fine(proposal.transaction_hash)):
            with patch(CLAIM, side_effect=RuntimeError('ledger unavailable')):
                with self.assertRaises(RuntimeError):
                    proposal_transactions.check_transaction(proposal)

        proposal.refresh_from_db()
        self.assertTrue(proposal.draft)
        self.assertEqual(proposal.action, Proposal.TO_CREATE)
        self.assertEqual(ConsumedTransaction.objects.count(), 0)

    def test_a_submit_commits_its_claim_together_with_the_booked_slot(self):
        proposal = self.pending_submit(11)
        paid_hash = proposal.new_transaction_hash

        with patch(CHECK_STATUS, return_value=fine(paid_hash)):
            result = proposal_transactions.check_transaction(proposal)

        self.assertEqual(result['outcome'], 'booked')
        proposal.refresh_from_db()
        self.assertEqual(proposal.action, Proposal.NONE)
        self.assertTrue(ProposalQueueSlot.objects.filter(proposal=proposal).exists())
        claim = ConsumedTransaction.objects.get()
        self.assertEqual(claim.transaction_hash, paid_hash)
        self.assertEqual(claim.purpose, ConsumedTransaction.PURPOSE_SUBMIT)

    def test_a_claim_that_raises_rolls_back_the_booking_as_well(self):
        proposal = self.pending_submit(13)

        with patch(CHECK_STATUS, return_value=fine(proposal.new_transaction_hash)):
            with patch(CLAIM, side_effect=RuntimeError('ledger unavailable')):
                with self.assertRaises(RuntimeError):
                    proposal_transactions.check_transaction(proposal)

        proposal.refresh_from_db()
        self.assertEqual(proposal.action, Proposal.TO_SUBMIT)
        self.assertEqual(proposal.proposal_status, Proposal.DISCUSSION)
        self.assertIsNone(proposal.start_at)
        self.assertFalse(ProposalQueueSlot.objects.filter(proposal=proposal).exists())
        self.assertEqual(HistoryProposal.objects.count(), 0)
        self.assertEqual(ConsumedTransaction.objects.count(), 0)

    def test_a_lost_claim_race_answers_200_and_leaves_the_proposal_retryable(self):
        proposal = self.pending_update(15)
        paid_hash = proposal.new_transaction_hash

        with patch(CHECK_STATUS, return_value=fine(paid_hash)):
            with patch(CLAIM, side_effect=competing_claim_then_delegate(paid_hash)):
                response = self.client.post(
                    '/api/proposal/{0}/check_payment/'.format(proposal.id), {}, format='json')

        self.assertEqual(response.status_code, 200)
        proposal.refresh_from_db()
        self.assertEqual(proposal.action, Proposal.TO_UPDATE)
        self.assertEqual(proposal.title, DEFAULT_TITLE)
        self.assertEqual(proposal.payment_status, Proposal.INVALID_PAYMENT)
        self.assertEqual(HistoryProposal.objects.count(), 0)
        self.assertFalse(ConsumedTransaction.objects.filter(purpose=ConsumedTransaction.PURPOSE_UPDATE).exists())

    def test_a_lost_claim_race_on_submit_books_no_slot_and_keeps_to_submit(self):
        proposal = self.pending_submit(17)
        paid_hash = proposal.new_transaction_hash

        with patch(CHECK_STATUS, return_value=fine(paid_hash)):
            with patch(CLAIM, side_effect=competing_claim_then_delegate(paid_hash)):
                result = proposal_transactions.check_transaction(proposal)

        self.assertEqual(result['outcome'], 'transaction_already_consumed')
        proposal.refresh_from_db()
        self.assertEqual(proposal.action, Proposal.TO_SUBMIT)
        self.assertEqual(proposal.payment_status, Proposal.INVALID_PAYMENT)
        self.assertEqual(proposal.proposal_status, Proposal.DISCUSSION)
        self.assertIsNone(proposal.start_at)
        self.assertFalse(ProposalQueueSlot.objects.filter(proposal=proposal).exists())
        self.assertEqual(HistoryProposal.objects.count(), 0)
        self.assertFalse(ConsumedTransaction.objects.filter(purpose=ConsumedTransaction.PURPOSE_SUBMIT).exists())

    def test_two_sequential_confirmations_apply_the_transition_once(self):
        proposal = self.pending_update(19)
        stale = Proposal.objects.get(id=proposal.id)

        with patch(CHECK_STATUS, return_value=fine(proposal.new_transaction_hash)):
            first = proposal_transactions.check_transaction(proposal)
            second = proposal_transactions.check_transaction(stale)

        self.assertEqual(first['outcome'], 'updated')
        self.assertEqual(second['outcome'], 'skipped')
        proposal.refresh_from_db()
        self.assertEqual(proposal.version, 2)
        self.assertEqual(ConsumedTransaction.objects.count(), 1)
        self.assertEqual(HistoryProposal.objects.count(), 1)

    def test_every_hash_a_fee_bumped_payment_resolves_under_is_burned_together(self):
        proposal = self.pending_update(21)
        inner_hash = proposal.new_transaction_hash
        outer_hash = distinct_hash(23)

        with patch(CHECK_STATUS, return_value=fine(inner_hash, resolved_hashes=(inner_hash, outer_hash))):
            result = proposal_transactions.check_transaction(proposal)

        self.assertEqual(result['outcome'], 'updated')
        self.assertEqual(
            sorted(ConsumedTransaction.objects.values_list('transaction_hash', flat=True)),
            sorted((inner_hash, outer_hash)),
        )


@override_settings(DEBUG=False)
class TerminalWriteGuardTests(PromotionTestCase):
    """I-4': a terminal verdict may only land on a row still awaiting that action.

    The 5 s browser poll and the 60 s sweep can hold different Horizon verdicts for the same
    row, so an unguarded write lets the loser stamp a rejection - and, on the create path,
    ``hide=True`` - onto a proposal whose payment was just confirmed and consumed.
    """

    def _verdict_after_the_row_moves_on(self, proposal, status):
        def _answer(**kwargs):
            Proposal.objects.filter(id=proposal.id).update(action=Proposal.NONE)
            return status

        return _answer

    def test_a_terminal_update_verdict_for_a_promoted_row_is_skipped(self):
        proposal = self.pending_update(31)

        with patch(ALERT) as mock_alert:
            with patch(CHECK_STATUS, side_effect=self._verdict_after_the_row_moves_on(
                    proposal, Proposal.BAD_MEMO)):
                result = proposal_transactions.check_transaction(proposal)

        self.assertEqual(result['outcome'], 'skipped')
        mock_alert.assert_not_called()
        proposal.refresh_from_db()
        self.assertEqual(proposal.payment_status, Proposal.FINE)
        self.assertIsNone(proposal.payment_check_rejected_hash)

    def test_a_terminal_create_verdict_for_a_promoted_row_neither_hides_nor_stamps_it(self):
        proposal = self.pending_create(33)

        with patch(ALERT) as mock_alert:
            with patch(CHECK_STATUS, side_effect=self._verdict_after_the_row_moves_on(
                    proposal, Proposal.INVALID_PAYMENT)):
                result = proposal_transactions.check_transaction(proposal)

        self.assertEqual(result['outcome'], 'skipped')
        mock_alert.assert_not_called()
        proposal.refresh_from_db()
        self.assertFalse(proposal.hide)
        self.assertTrue(proposal.draft)
        self.assertEqual(proposal.payment_status, Proposal.FINE)

    def test_a_terminal_verdict_persists_and_alerts_while_the_action_is_still_pending(self):
        proposal = self.pending_update(35)

        with patch(ALERT) as mock_alert:
            with patch(CHECK_STATUS, return_value=Proposal.BAD_MEMO):
                result = proposal_transactions.check_transaction(proposal)

        self.assertEqual(result['outcome'], 'payment_rejected')
        self.assertEqual(result['payment_status'], Proposal.BAD_MEMO)
        mock_alert.assert_called_once()
        proposal.refresh_from_db()
        self.assertEqual(proposal.payment_status, Proposal.BAD_MEMO)
        self.assertEqual(proposal.payment_check_rejected_hash, distinct_hash(36))
        self.assertEqual(proposal.action, Proposal.TO_UPDATE)

    def test_a_terminal_create_verdict_retires_the_draft(self):
        proposal = self.pending_create(37)

        with patch(ALERT):
            with patch(CHECK_STATUS, return_value=Proposal.BAD_MEMO):
                result = proposal_transactions.check_transaction(proposal)

        self.assertEqual(result['outcome'], 'payment_rejected')
        proposal.refresh_from_db()
        self.assertTrue(proposal.hide)
        self.assertFalse(proposal.draft)
        self.assertEqual(proposal.action, Proposal.NONE)
        self.assertEqual(proposal.payment_status, Proposal.BAD_MEMO)

    def test_the_same_rejection_pages_the_operator_once_however_often_it_is_swept(self):
        proposal = self.pending_update(39)

        with patch(ALERT) as mock_alert:
            with patch(CHECK_STATUS, return_value=Proposal.BAD_MEMO):
                proposal_transactions.check_transaction(proposal)
                repeat = proposal_transactions.check_transaction(proposal)

        self.assertEqual(repeat['outcome'], 'skipped')
        mock_alert.assert_called_once()

    def test_a_different_staged_hash_pages_again(self):
        proposal = self.pending_update(41)

        with patch(ALERT) as mock_alert:
            with patch(CHECK_STATUS, return_value=Proposal.BAD_MEMO):
                proposal_transactions.check_transaction(proposal)
                Proposal.objects.filter(id=proposal.id).update(new_transaction_hash=distinct_hash(43))
                proposal.refresh_from_db()
                proposal_transactions.check_transaction(proposal)

        self.assertEqual(mock_alert.call_count, 2)

    def test_a_skip_reports_the_row_rather_than_the_advisory_fine_it_was_read_with(self):
        # The browser poll and the sweep can compute the same terminal verdict seconds
        # apart.  The loser writes nothing, and reporting its own pre-request copy would
        # hand the owner the FINE staging wrote and tell them a burned payment succeeded.
        proposal = self.pending_update(49)
        Proposal.objects.filter(id=proposal.id).update(
            payment_status=Proposal.BAD_MEMO,
            payment_check_rejected_hash=proposal.new_transaction_hash,
        )
        self.assertEqual(proposal.payment_status, Proposal.FINE)

        with patch(ALERT) as mock_alert:
            with patch(CHECK_STATUS, return_value=Proposal.BAD_MEMO):
                result = proposal_transactions.check_transaction(proposal)

        self.assertEqual(result['outcome'], 'skipped')
        self.assertEqual(result['payment_status'], Proposal.BAD_MEMO)
        mock_alert.assert_not_called()

    def test_a_malformed_hash_already_reported_is_skipped_without_claiming_success(self):
        proposal = self.pending_update(51, new_transaction_hash='z' * 64)
        Proposal.objects.filter(id=proposal.id).update(
            payment_status=Proposal.INVALID_PAYMENT,
            payment_check_rejected_hash='z' * 64,
        )

        with patch(ALERT) as mock_alert:
            result = proposal_transactions.check_transaction(proposal)

        self.assertEqual(result['outcome'], 'skipped')
        self.assertEqual(result['payment_status'], Proposal.INVALID_PAYMENT)
        mock_alert.assert_not_called()

    def test_a_retryable_verdict_records_no_rejection_and_pages_nobody(self):
        proposal = self.pending_update(45)

        with patch(ALERT) as mock_alert:
            with patch(CHECK_STATUS, return_value=Proposal.HORIZON_ERROR):
                result = proposal_transactions.check_transaction(proposal)

        self.assertEqual(result['outcome'], 'payment_not_confirmed')
        mock_alert.assert_not_called()
        proposal.refresh_from_db()
        self.assertEqual(proposal.payment_status, Proposal.HORIZON_ERROR)
        self.assertIsNone(proposal.payment_check_rejected_hash)
        self.assertEqual(proposal.action, Proposal.TO_UPDATE)

    def test_a_hash_horizon_can_never_resolve_is_rejected_without_asking_horizon(self):
        proposal = self.pending_update(47, new_transaction_hash='z' * 64)

        with patch(ALERT) as mock_alert:
            with patch(CHECK_STATUS) as mock_check_status:
                result = proposal_transactions.check_transaction(proposal)

        mock_check_status.assert_not_called()
        mock_alert.assert_called_once()
        self.assertEqual(result['outcome'], 'payment_rejected')
        proposal.refresh_from_db()
        self.assertEqual(proposal.payment_status, Proposal.INVALID_PAYMENT)
        self.assertEqual(proposal.payment_check_rejected_hash, 'z' * 64)


@override_settings(DEBUG=False)
class StaleTransitionGuardTests(PromotionTestCase):
    """Staging is unauthenticated, so the staged copy can change under a verified verdict."""

    def test_a_restage_between_the_horizon_answer_and_the_row_lock_applies_nothing(self):
        proposal = self.pending_update(51)

        def _answer_then_restage(**kwargs):
            Proposal.objects.filter(id=proposal.id).update(new_title='Substituted title')
            return fine(distinct_hash(52))

        with patch(CHECK_STATUS, side_effect=_answer_then_restage):
            result = proposal_transactions.check_transaction(proposal)

        self.assertEqual(result['outcome'], 'stale_transition')
        proposal.refresh_from_db()
        self.assertEqual(proposal.action, Proposal.TO_UPDATE)
        self.assertEqual(proposal.title, DEFAULT_TITLE)
        self.assertEqual(proposal.new_title, 'Substituted title')
        self.assertEqual(proposal.payment_status, Proposal.FINE)
        self.assertEqual(ConsumedTransaction.objects.count(), 0)
        self.assertEqual(HistoryProposal.objects.count(), 0)

    def test_a_rewritten_submit_window_is_not_booked_against_the_old_verdict(self):
        proposal = self.pending_submit(53)
        start_at = proposal.new_start_at

        def _answer_then_restage(**kwargs):
            moved = start_at + timedelta(weeks=1)
            Proposal.objects.filter(id=proposal.id).update(
                new_start_at=moved,
                new_end_at=moved + timedelta(days=7, seconds=-1),
            )
            return fine(distinct_hash(54))

        with patch(CHECK_STATUS, side_effect=_answer_then_restage):
            result = proposal_transactions.check_transaction(proposal)

        self.assertEqual(result['outcome'], 'stale_transition')
        proposal.refresh_from_db()
        self.assertEqual(proposal.action, Proposal.TO_SUBMIT)
        self.assertIsNone(proposal.start_at)
        self.assertFalse(ProposalQueueSlot.objects.filter(proposal=proposal).exists())
        self.assertEqual(ConsumedTransaction.objects.count(), 0)

    def test_a_restaged_creation_hash_is_not_promoted_against_the_old_verdict(self):
        proposal = self.pending_create(55)

        def _answer_then_restage(**kwargs):
            Proposal.objects.filter(id=proposal.id).update(transaction_hash=distinct_hash(56))
            return fine(distinct_hash(55))

        with patch(CHECK_STATUS, side_effect=_answer_then_restage):
            result = proposal_transactions.check_transaction(proposal)

        self.assertEqual(result['outcome'], 'stale_transition')
        proposal.refresh_from_db()
        self.assertTrue(proposal.draft)
        self.assertEqual(proposal.action, Proposal.TO_CREATE)
        self.assertEqual(ConsumedTransaction.objects.count(), 0)

    def test_a_restaged_asset_creation_hash_is_not_promoted_against_the_old_verdict(self):
        # The asset creation takes its own path - transition lock, conflict check, its own
        # writes - so the guard has to hold there too, not only on the general one.
        proposal = make_asset_proposal_raw(
            proposed_by=DEFAULT_PROPOSED_BY,
            transaction_hash=distinct_hash(57),
            draft=True,
            action=Proposal.TO_CREATE,
            payment_status=Proposal.HORIZON_ERROR,
        )

        def _answer_then_restage(**kwargs):
            Proposal.objects.filter(id=proposal.id).update(transaction_hash=distinct_hash(58))
            return fine(distinct_hash(57))

        with patch(CHECK_STATUS, side_effect=_answer_then_restage):
            result = proposal_transactions.check_transaction(proposal)

        self.assertEqual(result['outcome'], 'stale_transition')
        proposal.refresh_from_db()
        self.assertTrue(proposal.draft)
        self.assertEqual(proposal.action, Proposal.TO_CREATE)
        self.assertEqual(proposal.payment_status, Proposal.HORIZON_ERROR)
        self.assertEqual(proposal.transaction_hash, distinct_hash(58))
        self.assertEqual(ConsumedTransaction.objects.count(), 0)


@override_settings(DEBUG=False)
class StagedTransitionMemoTests(PromotionTestCase):
    """The memo a staged transition demands, while the pre-v1 grammar is still accepted."""

    def test_a_staged_transition_still_accepts_the_legacy_memo(self):
        proposal = self.pending_update(201)

        expectation = UpdateTransition.resolve(proposal).memo_expectation()

        self.assertIsNotNone(expectation.canonical_digest)
        self.assertIsNotNone(expectation.legacy_digest)
