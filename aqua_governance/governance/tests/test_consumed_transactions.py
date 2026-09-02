import os
from datetime import timedelta
from unittest.mock import patch

from django.conf import settings
from django.db import transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from aqua_governance.governance import proposal_transactions
from aqua_governance.governance.consumed_transactions import claim_transaction_hashes
from aqua_governance.governance.exceptions import TransactionAlreadyConsumedError
from aqua_governance.governance.models import ConsumedTransaction, HistoryProposal, Proposal, ProposalQueueSlot
from aqua_governance.governance.proposal_queue import get_queue_week_start
from aqua_governance.governance.tests._factories import (
    DEFAULT_PROPOSED_BY,
    SECONDARY_ACCOUNT,
    distinct_hash,
    make_general_proposal,
)
from aqua_governance.governance.tests._promotions import ALERT, CHECK_STATUS, PromotionTestCase, fine, make, quill


CLAIM_LOGGER = 'aqua_governance.governance.consumed_transactions'
MUXED_PAYER = 'MA7QYNF7SOWQ3GLR2BGMZEHXAVIRZA4KVWLTJJFC7MGXUA74P7UJVAAAAAAAAAAD5GJ4'


def claim_transaction_hash(*, transaction_hash, proposal, purpose, payer=None):
    """Single-hash spelling, for the unit cases below that are about one hash.

    A test-only helper on purpose: production always claims the whole set a payment
    resolves under, and a readable singular spelling in the module would invite a caller to
    burn the outer hash of a fee-bumped payment and leave the inner one spendable.
    """
    return claim_transaction_hashes(
        transaction_hashes=(transaction_hash,),
        proposal=proposal,
        purpose=purpose,
        payer=payer,
    )


@override_settings(DEBUG=False)
class ClaimTransactionHashesTests(TestCase):
    """The claim helper on its own.

    Django wraps every test method in a transaction, so `in_atomic_block` is true
    throughout and the claim's own guard is satisfied without any extra scaffolding.
    """

    def setUp(self):
        self.proposal = make_general_proposal(proposed_by=DEFAULT_PROPOSED_BY)

    def test_a_claim_writes_one_row_carrying_its_purpose_proposal_and_payer(self):
        transaction_hash = distinct_hash(1)

        claim_transaction_hash(
            transaction_hash=transaction_hash,
            proposal=self.proposal,
            purpose=ConsumedTransaction.PURPOSE_CREATE,
            payer=DEFAULT_PROPOSED_BY,
        )

        row = ConsumedTransaction.objects.get()
        self.assertEqual(row.transaction_hash, transaction_hash)
        self.assertEqual(row.proposal_id, self.proposal.id)
        self.assertEqual(row.purpose, ConsumedTransaction.PURPOSE_CREATE)
        self.assertEqual(row.payer, DEFAULT_PROPOSED_BY)
        self.assertIsNotNone(row.created_at)
        self.assertIsNotNone(row.pk)

    def test_every_resolvable_hash_of_one_payment_is_burned_together(self):
        hashes = (distinct_hash(1), distinct_hash(2), distinct_hash(3))

        claim_transaction_hashes(
            transaction_hashes=hashes,
            proposal=self.proposal,
            purpose=ConsumedTransaction.PURPOSE_SUBMIT,
        )

        self.assertEqual(
            sorted(ConsumedTransaction.objects.values_list('transaction_hash', flat=True)),
            sorted(hashes),
        )
        self.assertEqual(
            set(ConsumedTransaction.objects.values_list('purpose', flat=True)),
            {ConsumedTransaction.PURPOSE_SUBMIT},
        )

    def test_a_hash_is_stored_stripped_and_lowercase(self):
        claim_transaction_hash(
            transaction_hash='  {0}  '.format(distinct_hash(1).upper()),
            proposal=self.proposal,
            purpose=ConsumedTransaction.PURPOSE_CREATE,
        )

        self.assertEqual(ConsumedTransaction.objects.get().transaction_hash, distinct_hash(1))

    def test_duplicates_inside_one_call_collapse_into_a_single_row(self):
        transaction_hash = distinct_hash(7)

        claim_transaction_hashes(
            transaction_hashes=(transaction_hash, transaction_hash.upper(), ' {0}'.format(transaction_hash)),
            proposal=self.proposal,
            purpose=ConsumedTransaction.PURPOSE_SUBMIT,
        )

        self.assertEqual(ConsumedTransaction.objects.count(), 1)

    def test_a_consumed_hash_cannot_be_claimed_a_second_time(self):
        transaction_hash = distinct_hash(1)
        claim_transaction_hash(
            transaction_hash=transaction_hash,
            proposal=self.proposal,
            purpose=ConsumedTransaction.PURPOSE_CREATE,
        )

        with self.assertRaises(TransactionAlreadyConsumedError) as caught:
            claim_transaction_hash(
                transaction_hash=transaction_hash,
                proposal=self.proposal,
                purpose=ConsumedTransaction.PURPOSE_UPDATE,
            )

        self.assertEqual(caught.exception.transaction_hash, transaction_hash)
        self.assertEqual(caught.exception.existing.purpose, ConsumedTransaction.PURPOSE_CREATE)
        self.assertEqual(ConsumedTransaction.objects.count(), 1)

    def test_an_uppercase_variant_cannot_reclaim_a_consumed_hash(self):
        transaction_hash = distinct_hash(4)
        claim_transaction_hash(
            transaction_hash=transaction_hash,
            proposal=self.proposal,
            purpose=ConsumedTransaction.PURPOSE_SUBMIT,
        )

        with self.assertRaises(TransactionAlreadyConsumedError):
            claim_transaction_hash(
                transaction_hash=transaction_hash.upper(),
                proposal=self.proposal,
                purpose=ConsumedTransaction.PURPOSE_SUBMIT,
            )

        self.assertEqual(ConsumedTransaction.objects.count(), 1)

    def test_a_submit_payment_cannot_be_respent_on_another_proposal(self):
        transaction_hash = distinct_hash(5)
        other_proposal = make_general_proposal(proposed_by=SECONDARY_ACCOUNT)
        claim_transaction_hash(
            transaction_hash=transaction_hash,
            proposal=self.proposal,
            purpose=ConsumedTransaction.PURPOSE_SUBMIT,
        )

        with self.assertRaises(TransactionAlreadyConsumedError):
            claim_transaction_hash(
                transaction_hash=transaction_hash,
                proposal=other_proposal,
                purpose=ConsumedTransaction.PURPOSE_CREATE,
            )

        self.assertEqual(ConsumedTransaction.objects.get().proposal_id, self.proposal.id)

    def test_one_burned_hash_in_a_batch_rejects_the_whole_batch(self):
        burned = distinct_hash(1)
        fresh = distinct_hash(2)
        claim_transaction_hash(
            transaction_hash=burned,
            proposal=self.proposal,
            purpose=ConsumedTransaction.PURPOSE_CREATE,
        )

        with self.assertRaises(TransactionAlreadyConsumedError):
            claim_transaction_hashes(
                transaction_hashes=(fresh, burned),
                proposal=self.proposal,
                purpose=ConsumedTransaction.PURPOSE_SUBMIT,
            )

        self.assertFalse(ConsumedTransaction.objects.filter(transaction_hash=fresh).exists())

    def test_a_rejected_claim_leaves_the_surrounding_transaction_usable(self):
        transaction_hash = distinct_hash(1)
        claim_transaction_hash(
            transaction_hash=transaction_hash,
            proposal=self.proposal,
            purpose=ConsumedTransaction.PURPOSE_CREATE,
        )

        with self.assertRaises(TransactionAlreadyConsumedError):
            claim_transaction_hash(
                transaction_hash=transaction_hash,
                proposal=self.proposal,
                purpose=ConsumedTransaction.PURPOSE_UPDATE,
            )

        Proposal.objects.filter(id=self.proposal.id).update(title='Still writable')
        self.assertEqual(Proposal.objects.get(id=self.proposal.id).title, 'Still writable')

    def test_a_race_lost_at_the_unique_index_reports_the_winning_row(self):
        transaction_hash = distinct_hash(9)
        competitor = ConsumedTransaction.objects.create(
            transaction_hash=transaction_hash,
            proposal=None,
            purpose=ConsumedTransaction.PURPOSE_SUBMIT,
        )

        real_filter = ConsumedTransaction.objects.filter
        seen = []

        def blind_first_read(*args, **kwargs):
            seen.append(True)
            if len(seen) == 1:
                return ConsumedTransaction.objects.none()
            return real_filter(*args, **kwargs)

        with patch.object(ConsumedTransaction.objects, 'filter', side_effect=blind_first_read):
            with self.assertRaises(TransactionAlreadyConsumedError) as caught:
                claim_transaction_hash(
                    transaction_hash=transaction_hash,
                    proposal=self.proposal,
                    purpose=ConsumedTransaction.PURPOSE_CREATE,
                )

        self.assertEqual(caught.exception.transaction_hash, transaction_hash)
        self.assertEqual(caught.exception.existing.pk, competitor.pk)
        self.assertEqual(ConsumedTransaction.objects.count(), 1)

        Proposal.objects.filter(id=self.proposal.id).update(title='Still writable')
        self.assertEqual(Proposal.objects.get(id=self.proposal.id).title, 'Still writable')

    def test_a_race_lost_to_a_winner_that_vanished_still_names_the_hash(self):
        transaction_hash = distinct_hash(10)
        real_bulk_create = ConsumedTransaction.objects.bulk_create

        def squat_then_insert(objs, *args, **kwargs):
            ConsumedTransaction.objects.create(
                transaction_hash=transaction_hash,
                proposal=None,
                purpose=ConsumedTransaction.PURPOSE_SUBMIT,
            )
            return real_bulk_create(objs, *args, **kwargs)

        with patch.object(ConsumedTransaction.objects, 'bulk_create', side_effect=squat_then_insert):
            with self.assertRaises(TransactionAlreadyConsumedError) as caught:
                claim_transaction_hash(
                    transaction_hash=transaction_hash,
                    proposal=self.proposal,
                    purpose=ConsumedTransaction.PURPOSE_CREATE,
                )

        self.assertEqual(caught.exception.transaction_hash, transaction_hash)
        self.assertIsNone(caught.exception.existing)
        self.assertEqual(ConsumedTransaction.objects.count(), 0)

        Proposal.objects.filter(id=self.proposal.id).update(title='Still writable')
        self.assertEqual(Proposal.objects.get(id=self.proposal.id).title, 'Still writable')

    def test_claiming_nothing_is_a_programming_error(self):
        for hashes in ((), (None,), ('',), ('   ',), (None, '')):
            with self.subTest(hashes=hashes):
                with self.assertRaisesMessage(ValueError, 'Cannot claim an empty transaction hash.'):
                    claim_transaction_hashes(
                        transaction_hashes=hashes,
                        proposal=self.proposal,
                        purpose=ConsumedTransaction.PURPOSE_CREATE,
                    )
        self.assertEqual(ConsumedTransaction.objects.count(), 0)

    def test_a_muxed_payer_is_discarded_rather_than_overflowing_the_column(self):
        with self.assertLogs(CLAIM_LOGGER, level='WARNING') as logs:
            claim_transaction_hash(
                transaction_hash=distinct_hash(1),
                proposal=self.proposal,
                purpose=ConsumedTransaction.PURPOSE_CREATE,
                payer=MUXED_PAYER,
            )

        self.assertIsNone(ConsumedTransaction.objects.get().payer)
        self.assertIn('not a 56-character account id', logs.output[0])

    def test_a_hash_that_is_not_64_hexadecimal_characters_is_claimed_but_logged(self):
        with self.assertLogs(CLAIM_LOGGER, level='WARNING') as logs:
            claim_transaction_hash(
                transaction_hash='dev-bypass-hash',
                proposal=self.proposal,
                purpose=ConsumedTransaction.PURPOSE_CREATE,
            )

        self.assertEqual(ConsumedTransaction.objects.get().transaction_hash, 'dev-bypass-hash')
        self.assertIn('64 hexadecimal characters', logs.output[0])

    def test_deleting_a_proposal_leaves_the_hash_burned(self):
        transaction_hash = distinct_hash(1)
        claim_transaction_hash(
            transaction_hash=transaction_hash,
            proposal=self.proposal,
            purpose=ConsumedTransaction.PURPOSE_CREATE,
            payer=DEFAULT_PROPOSED_BY,
        )

        self.proposal.delete()

        row = ConsumedTransaction.objects.get()
        self.assertEqual(row.transaction_hash, transaction_hash)
        self.assertIsNone(row.proposal_id)
        self.assertEqual(row.payer, DEFAULT_PROPOSED_BY)

    def test_a_claim_without_a_proposal_is_allowed(self):
        claim_transaction_hash(
            transaction_hash=distinct_hash(1),
            proposal=None,
            purpose=ConsumedTransaction.PURPOSE_LEGACY,
        )

        self.assertIsNone(ConsumedTransaction.objects.get().proposal_id)


@override_settings(DEBUG=False)
class ClaimOutsideAnAtomicBlockTests(TransactionTestCase):
    """The atomic-block guard needs a test case that is not itself wrapped in a transaction."""

    def test_claiming_outside_a_transaction_is_refused(self):
        self.assertFalse(transaction.get_connection().in_atomic_block)

        with self.assertRaisesMessage(RuntimeError, 'must run inside the applying transaction'):
            claim_transaction_hash(
                transaction_hash=distinct_hash(1),
                proposal=None,
                purpose=ConsumedTransaction.PURPOSE_CREATE,
            )

        self.assertEqual(ConsumedTransaction.objects.count(), 0)

    def test_claiming_inside_a_transaction_is_allowed(self):
        with transaction.atomic():
            claim_transaction_hash(
                transaction_hash=distinct_hash(1),
                proposal=None,
                purpose=ConsumedTransaction.PURPOSE_CREATE,
            )

        self.assertEqual(ConsumedTransaction.objects.count(), 1)


@override_settings(DEBUG=False)
class PromotionClaimTests(PromotionTestCase):
    """A payment is burned by promotion, and only by promotion.

    Staging must claim nothing: staging is unauthenticated, so a claim there would let
    anyone spend a hash they do not own and turn single-use enforcement into the denial of
    service it exists to prevent.
    """

    def test_a_confirmed_create_update_and_submit_each_burn_their_own_hash(self):
        for label, build, purpose in (
            ('create', self.pending_create, ConsumedTransaction.PURPOSE_CREATE),
            ('update', self.pending_update, ConsumedTransaction.PURPOSE_UPDATE),
            ('submit', self.pending_submit, ConsumedTransaction.PURPOSE_SUBMIT),
        ):
            with self.subTest(transition=label):
                ConsumedTransaction.objects.all().delete()
                ProposalQueueSlot.objects.all().delete()
                Proposal.objects.all().delete()

                proposal = build(101)
                paid_hash = proposal.transaction_hash if label == 'create' else proposal.new_transaction_hash

                with patch(CHECK_STATUS, return_value=fine(paid_hash)):
                    proposal_transactions.check_transaction(proposal)

                row = ConsumedTransaction.objects.get()
                self.assertEqual(row.transaction_hash, paid_hash)
                self.assertEqual(row.purpose, purpose)
                self.assertEqual(row.proposal_id, proposal.id)

    @patch('aqua_governance.governance.views.ProposalViewSet._reject_declared_owner_mismatch')
    @patch('aqua_governance.governance.serializers_v2.inspect_envelope', return_value=Proposal.FINE)
    def test_staging_a_submit_burns_nothing(self, _mock_check_xdr, _mock_owner):
        proposal = make(transaction_hash=distinct_hash(111))
        Proposal.objects.filter(id=proposal.id).update(
            last_updated_at=timezone.now() - settings.DISCUSSION_TIME - timedelta(seconds=1),
        )
        start_at = get_queue_week_start(timezone.now()) + timedelta(weeks=1)

        response = self.client.post(
            '/api/proposal/{0}/submit/'.format(proposal.id),
            {
                'start_at': start_at.isoformat().replace('+00:00', 'Z'),
                'end_at': (start_at + timedelta(days=7, seconds=-1)).isoformat().replace('+00:00', 'Z'),
                'new_envelope_xdr': 'submit-xdr',
                'new_transaction_hash': distinct_hash(112),
            },
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        proposal.refresh_from_db()
        self.assertEqual(proposal.action, Proposal.TO_SUBMIT)
        self.assertEqual(ConsumedTransaction.objects.count(), 0)

    @patch('aqua_governance.governance.views.ProposalViewSet._reject_declared_owner_mismatch')
    @patch('aqua_governance.governance.serializers_v2.inspect_envelope', return_value=Proposal.FINE)
    def test_staging_an_update_burns_nothing(self, _mock_check_xdr, _mock_owner):
        proposal = make(transaction_hash=distinct_hash(113))

        response = self.client.put(
            '/api/proposal/{0}/'.format(proposal.id),
            {
                'new_title': 'Staged title',
                'new_text': '<p>Staged text</p>',
                'new_envelope_xdr': 'update-xdr',
                'new_transaction_hash': distinct_hash(114),
            },
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        proposal.refresh_from_db()
        self.assertEqual(proposal.action, Proposal.TO_UPDATE)
        self.assertEqual(ConsumedTransaction.objects.count(), 0)

    def test_an_unconfirmed_payment_burns_nothing(self):
        for status in (Proposal.HORIZON_ERROR, Proposal.BAD_MEMO, Proposal.INVALID_PAYMENT,
                       Proposal.FAILED_TRANSACTION):
            with self.subTest(payment_status=status):
                ConsumedTransaction.objects.all().delete()
                Proposal.objects.all().delete()

                proposal = self.pending_update(121)

                with patch(ALERT):
                    with patch(CHECK_STATUS, return_value=status):
                        proposal_transactions.check_transaction(proposal)

                self.assertEqual(ConsumedTransaction.objects.count(), 0)

    def _burn_and_displace(self, index, *, kind):
        """Burn a hash through a real promotion, then move it out of the unique column.

        A later update promotion migrates the paid hash into ``HistoryProposal``, which frees
        ``Proposal.transaction_hash`` - the exact state in which the ledger is the only thing
        standing between the payment and a second transition.
        """
        if kind == 'create':
            proposal = self.pending_create(index)
            paid_hash = proposal.transaction_hash
        elif kind == 'submit':
            proposal = self.pending_submit(index)
            paid_hash = proposal.new_transaction_hash
        else:
            proposal = self.pending_update(index)
            paid_hash = proposal.new_transaction_hash

        with patch(CHECK_STATUS, return_value=fine(paid_hash)):
            proposal_transactions.check_transaction(proposal)

        displacing_hash = distinct_hash(index + 3)
        Proposal.objects.filter(id=proposal.id).update(
            action=Proposal.TO_UPDATE,
            new_title='Displacing title',
            new_text=quill('<p>Displacing text</p>').json_string,
            new_transaction_hash=displacing_hash,
        )
        proposal.refresh_from_db()
        with patch(CHECK_STATUS, return_value=fine(displacing_hash)):
            proposal_transactions.check_transaction(proposal)

        self.assertFalse(Proposal.objects.filter(transaction_hash=paid_hash).exists())
        return proposal, paid_hash

    def test_a_creation_hash_cannot_be_respent_once_an_update_has_displaced_it(self):
        _proposal, creation_hash = self._burn_and_displace(131, kind='create')
        replay = self.pending_create(139, transaction_hash=creation_hash)

        with patch(ALERT) as mock_alert:
            with patch(CHECK_STATUS, return_value=fine(creation_hash)):
                result = proposal_transactions.check_transaction(replay)

        self.assertEqual(result['outcome'], 'transaction_already_consumed')
        mock_alert.assert_called_once()
        replay.refresh_from_db()
        self.assertEqual(replay.payment_status, Proposal.INVALID_PAYMENT)
        self.assertEqual(replay.action, Proposal.NONE)
        self.assertTrue(replay.hide)
        self.assertEqual(
            ConsumedTransaction.objects.get(transaction_hash=creation_hash).purpose,
            ConsumedTransaction.PURPOSE_CREATE,
        )

    def test_a_confirmed_submit_hash_cannot_be_respent_on_another_proposal(self):
        _proposal, paid_hash = self._burn_and_displace(141, kind='submit')
        replay = self.pending_update(149, new_transaction_hash=paid_hash)

        with patch(ALERT) as mock_alert:
            with patch(CHECK_STATUS, return_value=fine(paid_hash)):
                first = proposal_transactions.check_transaction(replay)
                repeat = proposal_transactions.check_transaction(replay)

        self.assertEqual(first['outcome'], 'transaction_already_consumed')
        self.assertEqual(repeat['outcome'], 'skipped')
        mock_alert.assert_called_once()
        replay.refresh_from_db()
        self.assertEqual(replay.payment_status, Proposal.INVALID_PAYMENT)
        self.assertEqual(replay.action, Proposal.TO_UPDATE)
        self.assertEqual(replay.title, 'Test general proposal')
        self.assertEqual(HistoryProposal.objects.filter(proposal=replay).count(), 0)
        self.assertFalse(ProposalQueueSlot.objects.filter(proposal=replay).exists())
        self.assertEqual(
            ConsumedTransaction.objects.get(transaction_hash=paid_hash).purpose,
            ConsumedTransaction.PURPOSE_SUBMIT,
        )

    def test_an_update_payment_cannot_be_respent_as_a_submit_payment(self):
        _proposal, paid_hash = self._burn_and_displace(151, kind='update')
        replay = self.pending_submit(159, new_transaction_hash=paid_hash)

        with patch(ALERT):
            with patch(CHECK_STATUS, return_value=fine(paid_hash)):
                result = proposal_transactions.check_transaction(replay)

        self.assertEqual(result['outcome'], 'transaction_already_consumed')
        replay.refresh_from_db()
        self.assertEqual(replay.action, Proposal.TO_SUBMIT)
        self.assertIsNone(replay.start_at)
        self.assertFalse(ProposalQueueSlot.objects.filter(proposal=replay).exists())
        self.assertEqual(
            ConsumedTransaction.objects.get(transaction_hash=paid_hash).purpose,
            ConsumedTransaction.PURPOSE_UPDATE,
        )

    @override_settings(DEBUG=True)
    def test_the_development_payment_bypass_still_burns_the_hash(self):
        proposal = self.pending_create(161)

        with patch.dict(os.environ, {'PROPOSAL_PAYMENT_BYPASS': '1'}):
            result = proposal_transactions.check_transaction(proposal)

        self.assertEqual(result['outcome'], 'created')
        self.assertEqual(ConsumedTransaction.objects.get().transaction_hash, proposal.transaction_hash)
