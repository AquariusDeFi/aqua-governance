import base64
import hashlib
import json
from decimal import Decimal
from unittest.mock import patch

from django.conf import settings
from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django_quill.quill import Quill
from stellar_sdk import HashMemo
from stellar_sdk.exceptions import ConnectionError as SdkConnectionError

from aqua_governance.governance import proposal_transactions
from aqua_governance.governance.models import Proposal
from aqua_governance.governance import payment_statuses
from aqua_governance.governance.tests._factories import (
    DEFAULT_PROPOSED_BY,
    build_aqua_burn_envelope,
    distinct_hash,
    make_general_proposal,
)
from aqua_governance.governance.tests._horizon import (
    FakePaymentHorizonServer,
    payment_op_record,
    transaction_record,
)
from aqua_governance.governance.tests._promotions import VERIFY_PAYMENT, verdict, verifies
from aqua_governance.utils.memo import (
    MEMO_FORMAT_CANONICAL,
    MEMO_FORMAT_LEGACY,
    PURPOSE_CREATE,
    PURPOSE_SUBMIT,
    PURPOSE_UPDATE,
    build_memo_expectation,
)
from aqua_governance.utils.payments import inspect_envelope, logger as payments_logger, verify_payment


PAYMENT_TEXT = '<p>Payment text</p>'
PAYMENT_AMOUNT = Decimal(settings.PROPOSAL_CREATE_OR_UPDATE_COST)


def _quill_text(html=PAYMENT_TEXT):
    return Quill(json.dumps({'delta': {'ops': []}, 'html': html}))


def _memo_for_text(text: str) -> str:
    text_hash = hashlib.sha256(text.encode('utf-8')).hexdigest()
    return base64.b64encode(HashMemo(text_hash).memo_hash).decode()


def _create_expectation(text_html=PAYMENT_TEXT):
    return build_memo_expectation(
        PURPOSE_CREATE,
        proposed_by=DEFAULT_PROPOSED_BY,
        proposal_type=Proposal.PROPOSAL_TYPE_GENERAL,
        title='Payment title',
        text_html=text_html,
    )


def _aqua_payment(amount=None):
    return payment_op_record(
        from_account=DEFAULT_PROPOSED_BY,
        amount=str(PAYMENT_AMOUNT if amount is None else amount),
    )


@override_settings(DEBUG=False)
class PaymentVerificationTests(SimpleTestCase):
    def _verify(self, **overrides):
        kwargs = {
            'transaction_hash': 'a' * 64,
            'expected_payer': DEFAULT_PROPOSED_BY,
            'memo_expectation': _create_expectation(),
            'payment_amount': PAYMENT_AMOUNT,
        }
        kwargs.update(overrides)
        return verify_payment(**kwargs)

    @patch('aqua_governance.utils.payments.Server')
    def test_verify_payment_returns_horizon_error_when_lookup_fails(self, mock_server):
        mock_server.return_value = FakePaymentHorizonServer(
            transaction_error=SdkConnectionError('boom'),
        )

        with self.assertLogs('aqua_governance.utils.payments', level='WARNING'):
            result = self._verify()

        self.assertEqual(result.payment_status, payment_statuses.HORIZON_ERROR)

    @patch('aqua_governance.utils.payments.Server')
    def test_verify_payment_rejects_missing_payment(self, mock_server):
        horizon_server = FakePaymentHorizonServer(
            transaction=transaction_record(memo=_memo_for_text(PAYMENT_TEXT)),
            operations=[_aqua_payment(amount='1.0000000')],
        )
        mock_server.return_value = horizon_server

        with self.assertLogs('aqua_governance.utils.payments', level='WARNING'):
            result = self._verify()

        self.assertEqual(result.payment_status, payment_statuses.INVALID_PAYMENT)
        self.assertEqual(horizon_server.operation_calls[0], 'a' * 64)

    @patch('aqua_governance.utils.payments.Server')
    def test_verify_payment_rejects_unsuccessful_transaction(self, mock_server):
        horizon_server = FakePaymentHorizonServer(
            transaction=transaction_record(successful=False, memo=_memo_for_text(PAYMENT_TEXT)),
            operations=[_aqua_payment()],
        )
        mock_server.return_value = horizon_server

        result = self._verify()

        self.assertEqual(result.payment_status, payment_statuses.FAILED_TRANSACTION)
        self.assertEqual(horizon_server.operation_calls, [])

    @patch('aqua_governance.utils.payments.Server')
    def test_verify_payment_rejects_bad_memo(self, mock_server):
        mock_server.return_value = FakePaymentHorizonServer(
            transaction=transaction_record(memo=_memo_for_text('<p>Different text</p>')),
            operations=[_aqua_payment()],
        )

        with self.assertLogs('aqua_governance.utils.payments', level='WARNING'):
            result = self._verify()

        self.assertEqual(result.payment_status, payment_statuses.BAD_MEMO)

    @patch('aqua_governance.utils.payments.Server')
    def test_verify_payment_rejects_missing_memo(self, mock_server):
        for memo in (None, ''):
            with self.subTest(memo=memo):
                mock_server.return_value = FakePaymentHorizonServer(
                    transaction=transaction_record(memo=memo),
                    operations=[_aqua_payment()],
                )

                with self.assertLogs('aqua_governance.utils.payments', level='WARNING'):
                    result = self._verify()

                self.assertEqual(result.payment_status, payment_statuses.BAD_MEMO)

    @patch('aqua_governance.utils.payments.Server')
    def test_verify_payment_accepts_the_legacy_text_memo(self, mock_server):
        mock_server.return_value = FakePaymentHorizonServer(
            transaction=transaction_record(memo=_memo_for_text(PAYMENT_TEXT)),
            operations=[_aqua_payment()],
        )

        # The WARNING is the point of the assertLogs wrapper: accepting a memo that only
        # matches the pre-v1 format is what v3 will stop doing.
        with self.assertLogs('aqua_governance.utils.payments', level='WARNING'):
            result = self._verify()

        self.assertEqual(result.payment_status, payment_statuses.FINE)
        self.assertEqual(result.memo_format, MEMO_FORMAT_LEGACY)

    @patch('aqua_governance.utils.payments.Server')
    def test_verify_payment_accepts_the_canonical_memo_without_warning(self, mock_server):
        expectation = _create_expectation(PAYMENT_TEXT)
        mock_server.return_value = FakePaymentHorizonServer(
            transaction=transaction_record(
                memo=base64.b64encode(expectation.canonical_digest).decode(),
            ),
            operations=[_aqua_payment()],
        )

        with patch.object(payments_logger, 'warning') as mock_warning:
            result = self._verify()

        self.assertEqual(result.payment_status, payment_statuses.FINE)
        self.assertEqual(result.memo_format, MEMO_FORMAT_CANONICAL)
        mock_warning.assert_not_called()

    def _envelope(self, *, text_html=PAYMENT_TEXT, memo_hash_hex=None, amount=PAYMENT_AMOUNT):
        expectation = _create_expectation(text_html)
        if memo_hash_hex is None:
            memo_hash_hex = expectation.canonical_digest.hex()
        envelope_xdr, _ = build_aqua_burn_envelope(
            source=DEFAULT_PROPOSED_BY,
            amount=amount,
            memo_hash_hex=memo_hash_hex,
        )
        return envelope_xdr, expectation

    def _inspect(self, envelope_xdr, expectation):
        return inspect_envelope(
            envelope_xdr=envelope_xdr,
            expected_payer=DEFAULT_PROPOSED_BY,
            memo_expectation=expectation,
            payment_amount=PAYMENT_AMOUNT,
        )

    def test_inspect_envelope_rejects_missing_payment(self):
        envelope_xdr, expectation = self._envelope(amount=Decimal('1'))

        self.assertEqual(self._inspect(envelope_xdr, expectation), payment_statuses.INVALID_PAYMENT)

    def test_inspect_envelope_rejects_bad_hash_memo(self):
        envelope_xdr, expectation = self._envelope(memo_hash_hex='1' * 64)

        self.assertEqual(self._inspect(envelope_xdr, expectation), payment_statuses.BAD_MEMO)

    def test_inspect_envelope_accepts_matching_hash_memo(self):
        envelope_xdr, expectation = self._envelope()

        self.assertEqual(self._inspect(envelope_xdr, expectation), payment_statuses.FINE)

    def test_inspect_envelope_accepts_the_legacy_text_memo(self):
        text_hash = hashlib.sha256(PAYMENT_TEXT.encode('utf-8')).hexdigest()
        envelope_xdr, expectation = self._envelope(memo_hash_hex=text_hash)

        self.assertEqual(self._inspect(envelope_xdr, expectation), payment_statuses.FINE)


@override_settings(DEBUG=False)
class ProposalTransactionPaymentAmountTests(TestCase):
    """Each promotion path asks Horizon about its own hash, payer, purpose and price.

    Real rows rather than stubs: I-4' turned the terminal bookkeeping into a conditional
    UPDATE.  The verdict is still mocked, and the assertions below double as the guard for
    I-6 - the call arguments are all the promotion knows before the verdict comes back.
    """

    def _proposal(self, **overrides):
        defaults = {
            'proposed_by': DEFAULT_PROPOSED_BY,
            'title': 'Current title',
            'text': _quill_text(),
            'draft': True,
            'transaction_hash': 'a' * 64,
            'new_title': 'Updated title',
            'new_text': _quill_text('<p>Updated payment text</p>'),
            'new_transaction_hash': 'b' * 64,
        }
        defaults.update(overrides)
        return make_general_proposal(**defaults)

    def _assert_asked_about(self, mock_verify_payment, *, transaction_hash, purpose, payment_amount):
        mock_verify_payment.assert_called_once()
        self.assertEqual(mock_verify_payment.call_args.args, ())
        kwargs = mock_verify_payment.call_args.kwargs
        self.assertEqual(kwargs['transaction_hash'], transaction_hash)
        self.assertEqual(kwargs['expected_payer'], DEFAULT_PROPOSED_BY)
        self.assertEqual(kwargs['payment_amount'], payment_amount)
        self.assertEqual(kwargs['memo_expectation'].purpose, purpose)

    def test_no_promotion_path_queries_the_database_before_the_verdict(self):
        """I-6, asserted rather than assumed.

        A pre-verdict read would cost a query per pending row on every sweep tick and
        would reopen the window the fingerprint guard closes, so it has to fail a test
        rather than merely be true today.
        """
        actions = (Proposal.TO_CREATE, Proposal.TO_UPDATE, Proposal.TO_SUBMIT)
        for index, action in enumerate(actions):
            with self.subTest(action=action):
                proposal = self._proposal(
                    action=action,
                    transaction_hash=distinct_hash(900 + index * 2),
                    new_transaction_hash=distinct_hash(901 + index * 2),
                )

                self.assertEqual(self._queries_before_the_verdict(proposal), [0])

    @staticmethod
    def _queries_before_the_verdict(proposal):
        """How many queries each Horizon call was preceded by, one entry per call."""
        counts = []

        with CaptureQueriesContext(connection) as captured:
            def _verdict(*, transaction_hash, **kwargs):
                counts.append(len(connection.queries) - captured.initial_queries)
                return verdict(Proposal.BAD_MEMO, transaction_hash)

            with patch(VERIFY_PAYMENT, side_effect=_verdict):
                proposal_transactions.check_transaction(proposal)

        return counts

    @patch(VERIFY_PAYMENT, side_effect=verifies(Proposal.BAD_MEMO))
    def test_create_path_uses_create_or_update_payment_amount(self, mock_verify_payment):
        proposal = self._proposal(action=Proposal.TO_CREATE)

        proposal_transactions.check_transaction(proposal)

        self._assert_asked_about(
            mock_verify_payment,
            transaction_hash=proposal.transaction_hash,
            purpose=PURPOSE_CREATE,
            payment_amount=settings.PROPOSAL_CREATE_OR_UPDATE_COST,
        )

    @patch(VERIFY_PAYMENT, side_effect=verifies(Proposal.BAD_MEMO))
    def test_update_path_uses_create_or_update_payment_amount(self, mock_verify_payment):
        proposal = self._proposal(action=Proposal.TO_UPDATE)

        proposal_transactions.check_transaction(proposal)

        self._assert_asked_about(
            mock_verify_payment,
            transaction_hash=proposal.new_transaction_hash,
            purpose=PURPOSE_UPDATE,
            payment_amount=settings.PROPOSAL_CREATE_OR_UPDATE_COST,
        )

    @patch(VERIFY_PAYMENT, side_effect=verifies(Proposal.BAD_MEMO))
    def test_submit_path_uses_submit_payment_amount(self, mock_verify_payment):
        proposal = self._proposal(action=Proposal.TO_SUBMIT)

        proposal_transactions.check_transaction(proposal)

        self._assert_asked_about(
            mock_verify_payment,
            transaction_hash=proposal.new_transaction_hash,
            purpose=PURPOSE_SUBMIT,
            payment_amount=settings.PROPOSAL_SUBMIT_COST,
        )
