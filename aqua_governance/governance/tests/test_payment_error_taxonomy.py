import base64
import os
from decimal import Decimal
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from stellar_sdk.exceptions import ConnectionError as SdkConnectionError
from stellar_sdk.exceptions import NotFoundError

from aqua_governance.governance import payment_statuses
from aqua_governance.governance.tests._factories import DEFAULT_PROPOSED_BY, SECONDARY_ACCOUNT
from aqua_governance.governance.tests._horizon import FakePaymentHorizonServer, payment_op_record, transaction_record
from aqua_governance.utils import payments
from aqua_governance.utils.memo import (
    MEMO_FORMAT_CANONICAL,
    MEMO_FORMAT_LEGACY,
    PURPOSE_CREATE,
    PURPOSE_UPDATE,
    build_memo_expectation,
    legacy_memo_digest,
)


TRANSACTION_HASH = 'a' * 64
AMOUNT = Decimal('100000')
HORIZON_AMOUNT = '100000.0000000'
TEXT_HTML = '<p>Hello</p>'


def _create_expectation(**overrides):
    fields = {
        'proposed_by': DEFAULT_PROPOSED_BY,
        'proposal_type': 'GENERAL',
        'title': 'Test proposal',
        'text_html': TEXT_HTML,
    }
    fields.update(overrides)
    return build_memo_expectation(PURPOSE_CREATE, **fields)


def _memo_base64(digest):
    return base64.b64encode(digest).decode()


def _aqua_payment(from_account=DEFAULT_PROPOSED_BY, amount=HORIZON_AMOUNT):
    return payment_op_record(from_account=from_account, amount=amount)


def _not_found_error():
    return NotFoundError(_HorizonResponse())


class _HorizonResponse:
    """The minimum ``NotFoundError`` reads out of a response object."""

    status_code = 404
    text = '{}'
    url = 'https://horizon.example/transactions/{0}'.format(TRANSACTION_HASH)
    headers = {}

    def json(self):
        return {}


@override_settings(DEBUG=False)
class PaymentErrorTaxonomyTests(SimpleTestCase):
    """Which failures are retryable, which are terminal, and what each one is called."""

    def _verify(self, horizon_server, *, expectation=None, expected_payer=DEFAULT_PROPOSED_BY,
                transaction_hash=TRANSACTION_HASH, payment_amount=AMOUNT):
        return payments.verify_payment(
            transaction_hash=transaction_hash,
            expected_payer=expected_payer,
            memo_expectation=expectation if expectation is not None else _create_expectation(),
            payment_amount=payment_amount,
            horizon_server=horizon_server,
        )

    def _server(self, *, expectation=None, operations=None, **kwargs):
        expectation = expectation if expectation is not None else _create_expectation()
        operations = [_aqua_payment()] if operations is None else operations
        return FakePaymentHorizonServer(
            transaction=transaction_record(memo=_memo_base64(expectation.canonical_digest)),
            operations=operations,
            **kwargs,
        )

    def test_a_transaction_lookup_failure_is_retryable(self):
        horizon_server = self._server(transaction_error=SdkConnectionError('boom'))

        with self.assertLogs('aqua_governance.utils.payments', level='WARNING'):
            result = self._verify(horizon_server)

        self.assertEqual(result.payment_status, payment_statuses.HORIZON_ERROR)
        self.assertEqual(result.reason, payments.REASON_HORIZON_UNAVAILABLE)

    def test_an_operations_fetch_failure_is_retryable_and_not_an_invalid_payment(self):
        """The flap that used to become a terminal verdict through a blanket except."""
        horizon_server = self._server(operations_error=SdkConnectionError('boom'))

        with self.assertLogs('aqua_governance.utils.payments', level='WARNING'):
            result = self._verify(horizon_server)

        self.assertEqual(result.payment_status, payment_statuses.HORIZON_ERROR)
        self.assertEqual(result.reason, payments.REASON_HORIZON_UNAVAILABLE)

    def test_a_malformed_horizon_page_is_retryable(self):
        horizon_server = self._server(operations_error=KeyError('_embedded'))

        with self.assertLogs('aqua_governance.utils.payments', level='WARNING'):
            result = self._verify(horizon_server)

        self.assertEqual(result.payment_status, payment_statuses.HORIZON_ERROR)
        self.assertEqual(result.reason, payments.REASON_HORIZON_UNAVAILABLE)

    def test_an_unknown_transaction_is_reported_as_not_found(self):
        horizon_server = self._server(transaction_error=_not_found_error())

        result = self._verify(horizon_server)

        self.assertEqual(result.payment_status, payment_statuses.HORIZON_ERROR)
        self.assertEqual(result.reason, payments.REASON_TRANSACTION_NOT_FOUND)

    def test_an_operations_endpoint_404_is_reported_as_not_found(self):
        horizon_server = self._server(operations_error=_not_found_error())

        result = self._verify(horizon_server)

        self.assertEqual(result.payment_status, payment_statuses.HORIZON_ERROR)
        self.assertEqual(result.reason, payments.REASON_TRANSACTION_NOT_FOUND)

    def test_an_empty_operations_page_is_infrastructure_not_a_payment_verdict(self):
        horizon_server = self._server(operations=[])

        with self.assertLogs('aqua_governance.utils.payments', level='WARNING'):
            result = self._verify(horizon_server)

        self.assertEqual(result.payment_status, payment_statuses.HORIZON_ERROR)
        self.assertEqual(result.reason, payments.REASON_HORIZON_EMPTY_OPERATIONS)

    def test_an_unsuccessful_transaction_is_a_failed_transaction(self):
        expectation = _create_expectation()
        horizon_server = FakePaymentHorizonServer(
            transaction=transaction_record(
                successful=False,
                memo=_memo_base64(expectation.canonical_digest),
            ),
            operations=[_aqua_payment()],
        )

        result = self._verify(horizon_server, expectation=expectation)

        self.assertEqual(result.payment_status, payment_statuses.FAILED_TRANSACTION)
        self.assertEqual(result.reason, payments.REASON_TRANSACTION_FAILED)
        self.assertEqual(horizon_server.operation_calls, [])

    def test_no_matching_operation_is_an_invalid_payment(self):
        horizon_server = self._server(operations=[_aqua_payment(amount='1.0000000')])

        with self.assertLogs('aqua_governance.utils.payments', level='WARNING'):
            result = self._verify(horizon_server)

        self.assertEqual(result.payment_status, payment_statuses.INVALID_PAYMENT)
        self.assertEqual(result.reason, payments.REASON_NO_MATCHING_PAYMENT)

    def test_a_payer_mismatch_is_logged_with_the_hash_and_the_expected_payer(self):
        horizon_server = self._server(operations=[_aqua_payment(from_account=SECONDARY_ACCOUNT)])

        with self.assertLogs('aqua_governance.utils.payments', level='WARNING') as captured:
            result = self._verify(horizon_server)

        self.assertEqual(result.payment_status, payment_statuses.INVALID_PAYMENT)
        self.assertEqual(result.reason, payments.REASON_PAYER_MISMATCH)
        record = captured.records[-1]
        self.assertEqual(record.transaction_hash, TRANSACTION_HASH)
        self.assertEqual(record.expected_payer, DEFAULT_PROPOSED_BY)
        self.assertEqual(record.reason, payments.REASON_PAYER_MISMATCH)

    def test_failures_reach_the_logger_rather_than_stdout(self):
        horizon_server = self._server(operations_error=SdkConnectionError('boom'))

        with self.assertLogs('aqua_governance.utils.payments', level='WARNING') as captured:
            self._verify(horizon_server)

        self.assertTrue(captured.records)

    def test_amounts_are_compared_as_decimals_not_as_floats(self):
        horizon_server = self._server(operations=[_aqua_payment(amount='0.3000000')])

        result = self._verify(horizon_server, payment_amount=Decimal('0.1') + Decimal('0.2'))

        self.assertEqual(result.payment_status, payment_statuses.FINE)
        self.assertIsInstance(result.amount, Decimal)

    def test_the_amount_must_be_exact_so_a_submit_payment_cannot_buy_a_create(self):
        horizon_server = self._server(operations=[_aqua_payment(amount='900000.0000000')])

        with self.assertLogs('aqua_governance.utils.payments', level='WARNING'):
            result = self._verify(horizon_server, payment_amount=Decimal('100000'))

        self.assertEqual(result.payment_status, payment_statuses.INVALID_PAYMENT)
        self.assertEqual(result.reason, payments.REASON_NO_MATCHING_PAYMENT)

    def test_a_non_numeric_amount_is_an_invalid_payment_and_never_retried(self):
        horizon_server = self._server(operations=[_aqua_payment(amount='not-a-number')])

        with self.assertLogs('aqua_governance.utils.payments', level='WARNING'):
            result = self._verify(horizon_server)

        self.assertEqual(result.payment_status, payment_statuses.INVALID_PAYMENT)
        self.assertEqual(result.reason, payments.REASON_NO_MATCHING_PAYMENT)

    def test_a_malformed_hash_is_rejected_without_reaching_horizon(self):
        horizon_server = self._server()

        for transaction_hash in ('', 'g' * 64, 'a' * 63, 'a' * 65, None, 1234):
            with self.subTest(transaction_hash=transaction_hash):
                with self.assertLogs('aqua_governance.utils.payments', level='ERROR'):
                    result = self._verify(horizon_server, transaction_hash=transaction_hash)

                self.assertEqual(result.payment_status, payment_statuses.INVALID_PAYMENT)
                self.assertEqual(result.reason, payments.REASON_BAD_HASH_FORMAT)

        self.assertEqual(horizon_server.transaction_calls, [])
        self.assertEqual(horizon_server.operation_calls, [])

    def test_an_uppercase_hash_reaches_horizon_lowercased(self):
        horizon_server = self._server()

        result = self._verify(horizon_server, transaction_hash='A' * 64)

        self.assertEqual(result.payment_status, payment_statuses.FINE)
        self.assertEqual(result.transaction_hash, 'a' * 64)
        self.assertEqual(horizon_server.transaction_calls, ['a' * 64])
        self.assertEqual(horizon_server.operation_calls, ['a' * 64])

    def test_a_missing_memo_is_a_bad_memo(self):
        for transaction in (transaction_record(memo=None), transaction_record(memo='')):
            with self.subTest(transaction=transaction):
                horizon_server = FakePaymentHorizonServer(
                    transaction=transaction,
                    operations=[_aqua_payment()],
                )

                with self.assertLogs('aqua_governance.utils.payments', level='WARNING'):
                    result = self._verify(horizon_server)

                self.assertEqual(result.payment_status, payment_statuses.BAD_MEMO)
                self.assertEqual(result.reason, payments.REASON_MEMO_MISSING)

    def test_a_non_hash_memo_type_is_a_bad_memo_even_when_the_base64_matches(self):
        expectation = _create_expectation()
        horizon_server = FakePaymentHorizonServer(
            transaction=transaction_record(
                memo=_memo_base64(expectation.canonical_digest),
                memo_type='text',
            ),
            operations=[_aqua_payment()],
        )

        with self.assertLogs('aqua_governance.utils.payments', level='WARNING'):
            result = self._verify(horizon_server, expectation=expectation)

        self.assertEqual(result.payment_status, payment_statuses.BAD_MEMO)
        self.assertEqual(result.reason, payments.REASON_MEMO_TYPE_MISMATCH)

    def test_an_undecodable_or_wrong_length_memo_is_a_type_mismatch(self):
        for memo in ('not base64!!', base64.b64encode(b'short').decode()):
            with self.subTest(memo=memo):
                horizon_server = FakePaymentHorizonServer(
                    transaction=transaction_record(memo=memo),
                    operations=[_aqua_payment()],
                )

                with self.assertLogs('aqua_governance.utils.payments', level='WARNING'):
                    result = self._verify(horizon_server)

                self.assertEqual(result.payment_status, payment_statuses.BAD_MEMO)
                self.assertEqual(result.reason, payments.REASON_MEMO_TYPE_MISMATCH)

    def test_a_memo_committing_to_another_transition_is_a_mismatch(self):
        horizon_server = FakePaymentHorizonServer(
            transaction=transaction_record(memo=_memo_base64(b'\x00' * 32)),
            operations=[_aqua_payment()],
        )

        with self.assertLogs('aqua_governance.utils.payments', level='WARNING'):
            result = self._verify(horizon_server)

        self.assertEqual(result.payment_status, payment_statuses.BAD_MEMO)
        self.assertEqual(result.reason, payments.REASON_MEMO_MISMATCH)

    def test_an_expectation_that_can_build_no_digest_is_unrepresentable(self):
        expectation = build_memo_expectation(
            PURPOSE_UPDATE,
            proposal_id=None,
            title='New title',
            text_html='<p>Updated</p>',
            accept_legacy=False,
        )
        self.assertEqual(expectation.accepted(), ())
        horizon_server = FakePaymentHorizonServer(
            transaction=transaction_record(memo=_memo_base64(b'\x00' * 32)),
            operations=[_aqua_payment()],
        )

        with self.assertLogs('aqua_governance.utils.payments', level='WARNING'):
            result = self._verify(horizon_server, expectation=expectation)

        self.assertEqual(result.payment_status, payment_statuses.BAD_MEMO)
        self.assertEqual(result.reason, payments.REASON_MEMO_UNREPRESENTABLE)

    def test_a_legacy_memo_match_is_accepted_and_logged_as_the_v3_instrument(self):
        expectation = _create_expectation()
        horizon_server = FakePaymentHorizonServer(
            transaction=transaction_record(memo=_memo_base64(legacy_memo_digest(TEXT_HTML))),
            operations=[_aqua_payment()],
        )

        with self.assertLogs('aqua_governance.utils.payments', level='WARNING') as captured:
            result = self._verify(horizon_server, expectation=expectation)

        self.assertEqual(result.payment_status, payment_statuses.FINE)
        self.assertEqual(result.memo_format, MEMO_FORMAT_LEGACY)
        self.assertEqual(captured.records[-1].purpose, PURPOSE_CREATE)

    def test_a_canonical_memo_match_is_not_logged_as_legacy(self):
        horizon_server = self._server()

        result = self._verify(horizon_server)

        self.assertEqual(result.payment_status, payment_statuses.FINE)
        self.assertEqual(result.memo_format, MEMO_FORMAT_CANONICAL)

    def test_the_payment_check_precedes_the_memo_check(self):
        """A transaction wrong in both ways keeps reporting the payment verdict."""
        horizon_server = FakePaymentHorizonServer(
            transaction=transaction_record(memo=_memo_base64(b'\x00' * 32)),
            operations=[_aqua_payment(from_account=SECONDARY_ACCOUNT)],
        )

        with self.assertLogs('aqua_governance.utils.payments', level='WARNING'):
            result = self._verify(horizon_server)

        self.assertEqual(result.payment_status, payment_statuses.INVALID_PAYMENT)
        self.assertEqual(result.reason, payments.REASON_PAYER_MISMATCH)

    def test_a_fee_bumped_payment_resolves_under_every_hash_it_exposes(self):
        expectation = _create_expectation()
        inner_hash = 'b' * 64
        fee_bump_hash = 'c' * 64
        horizon_server = FakePaymentHorizonServer(
            transaction=transaction_record(
                memo=_memo_base64(expectation.canonical_digest),
                transaction_hash='C' * 64,
                inner_hash=inner_hash,
                fee_bump_hash=fee_bump_hash,
            ),
            operations=[_aqua_payment()],
        )

        result = self._verify(horizon_server, expectation=expectation)

        self.assertEqual(result.payment_status, payment_statuses.FINE)
        self.assertEqual(result.resolved_hashes, (TRANSACTION_HASH, inner_hash, fee_bump_hash))

    def test_a_plain_payment_resolves_under_exactly_one_hash(self):
        horizon_server = self._server()

        result = self._verify(horizon_server)

        self.assertEqual(result.resolved_hashes, (TRANSACTION_HASH,))

    def test_every_reason_a_call_can_return_belongs_to_the_closed_vocabulary(self):
        expectation = _create_expectation()
        cases = (
            ('bad hash', {'transaction_hash': 'zz'}, self._server()),
            ('not found', {}, self._server(transaction_error=_not_found_error())),
            ('unavailable', {}, self._server(transaction_error=SdkConnectionError('boom'))),
            ('empty page', {}, self._server(operations=[])),
            ('wrong payer', {}, self._server(
                operations=[_aqua_payment(from_account=SECONDARY_ACCOUNT)])),
            ('no payment', {}, self._server(operations=[_aqua_payment(amount='1.0000000')])),
            ('ok', {}, self._server(expectation=expectation)),
        )

        seen = set()
        with self.assertLogs('aqua_governance.utils.payments', level='WARNING'):
            # The FINE case logs nothing, so the block is entered for the failures around it.
            for label, overrides, horizon_server in cases:
                with self.subTest(case=label):
                    result = self._verify(horizon_server, expectation=expectation, **overrides)
                    self.assertIn(result.reason, payments.PAYMENT_CHECK_REASONS)
                    seen.add(result.reason)

        failed_transaction = self._verify(
            FakePaymentHorizonServer(transaction=transaction_record(successful=False)),
            expectation=expectation,
        )
        seen.add(failed_transaction.reason)

        self.assertIn(payments.REASON_OK, seen)
        self.assertTrue(seen.issubset(set(payments.PAYMENT_CHECK_REASONS)))


class PaymentBypassTests(SimpleTestCase):
    """The dev bypass short-circuits before a Horizon client is even constructed."""

    @override_settings(DEBUG=True)
    @patch.dict(os.environ, {'PROPOSAL_PAYMENT_BYPASS': '1'})
    @patch('aqua_governance.utils.payments.Server')
    def test_the_bypass_returns_fine_without_touching_horizon(self, mock_server):
        result = payments.verify_payment(
            transaction_hash='not-a-hash',
            expected_payer=None,
            memo_expectation=_create_expectation(),
        )

        self.assertEqual(result.payment_status, payment_statuses.FINE)
        self.assertEqual(result.reason, payments.REASON_BYPASS)
        mock_server.assert_not_called()

    @override_settings(DEBUG=False)
    @patch.dict(os.environ, {'PROPOSAL_PAYMENT_BYPASS': '1'})
    def test_the_bypass_env_var_is_inert_in_production(self):
        horizon_server = FakePaymentHorizonServer(
            transaction=transaction_record(memo=_memo_base64(b'\x00' * 32)),
            operations=[_aqua_payment()],
        )

        with self.assertLogs('aqua_governance.utils.payments', level='WARNING'):
            result = payments.verify_payment(
                transaction_hash=TRANSACTION_HASH,
                expected_payer=DEFAULT_PROPOSED_BY,
                memo_expectation=_create_expectation(),
                payment_amount=AMOUNT,
                horizon_server=horizon_server,
            )

        self.assertEqual(result.payment_status, payment_statuses.BAD_MEMO)

    @override_settings(DEBUG=True)
    @patch.dict(os.environ, {'PROPOSAL_PAYMENT_BYPASS': '1'})
    def test_the_bypass_also_short_circuits_the_advisory_envelope_check(self):
        status = payments.inspect_envelope(
            envelope_xdr='not an envelope',
            expected_payer=DEFAULT_PROPOSED_BY,
            memo_expectation=_create_expectation(),
        )

        self.assertEqual(status, payment_statuses.FINE)


class TransactionHashPrimitiveTests(SimpleTestCase):
    def test_accepts_hexadecimal_in_either_case_and_normalises_down(self):
        self.assertTrue(payments.is_valid_transaction_hash('A' * 64))
        self.assertEqual(payments.normalize_transaction_hash('A' * 64), 'a' * 64)

    def test_rejects_everything_that_is_not_sixty_four_hex_characters(self):
        for value in ('', 'a' * 63, 'a' * 65, 'g' * 64, ' ' + 'a' * 63, None, 42, b'a' * 64):
            with self.subTest(value=value):
                self.assertFalse(payments.is_valid_transaction_hash(value))
                with self.assertRaises(payments.InvalidTransactionHash):
                    payments.normalize_transaction_hash(value)
