import base64
from decimal import Decimal

from django.conf import settings
from django.test import SimpleTestCase, override_settings

from stellar_sdk import MuxedAccount

from aqua_governance.governance import payment_statuses
from aqua_governance.governance.tests._factories import (
    DEFAULT_PROPOSED_BY,
    SECONDARY_ACCOUNT,
    TERTIARY_ACCOUNT,
    build_aqua_burn_envelope,
)
from aqua_governance.governance.tests._horizon import (
    FakePaymentHorizonServer,
    native_payment_op_record,
    non_payment_op_record,
    payment_op_record,
    transaction_record,
)
from aqua_governance.utils import payments
from aqua_governance.utils.memo import PURPOSE_CREATE, build_memo_expectation


TRANSACTION_HASH = 'a' * 64
AMOUNT = Decimal('100000')
HORIZON_AMOUNT = '100000.0000000'


def _create_expectation(proposed_by=DEFAULT_PROPOSED_BY):
    return build_memo_expectation(
        PURPOSE_CREATE,
        proposed_by=proposed_by,
        proposal_type='GENERAL',
        title='Test proposal',
        text_html='<p>Hello</p>',
    )


def _memo_base64(expectation):
    return base64.b64encode(expectation.canonical_digest).decode()


def _muxed(account_id, muxed_id=1234):
    return MuxedAccount(account_id, muxed_id).universal_account_id


@override_settings(DEBUG=False)
class PaymentPayerBindingTests(SimpleTestCase):
    """The payment is the only authenticated channel, so who paid is part of the verdict."""

    def _verify(self, operations, *, expected_payer=DEFAULT_PROPOSED_BY, expectation=None,
                payment_amount=AMOUNT):
        expectation = expectation if expectation is not None else _create_expectation()
        horizon_server = FakePaymentHorizonServer(
            transaction=transaction_record(memo=_memo_base64(expectation)),
            operations=operations,
        )
        result = payments.verify_payment(
            transaction_hash=TRANSACTION_HASH,
            expected_payer=expected_payer,
            memo_expectation=expectation,
            payment_amount=payment_amount,
            horizon_server=horizon_server,
        )
        return result, horizon_server

    def test_accepts_the_payment_made_by_the_expected_payer(self):
        result, _ = self._verify([
            payment_op_record(from_account=DEFAULT_PROPOSED_BY, amount=HORIZON_AMOUNT),
        ])

        self.assertEqual(result.payment_status, payment_statuses.FINE)
        self.assertEqual(result.reason, payments.REASON_OK)
        self.assertEqual(result.payer, DEFAULT_PROPOSED_BY)

    def test_rejects_a_payment_made_by_another_account(self):
        result, _ = self._verify([
            payment_op_record(from_account=SECONDARY_ACCOUNT, amount=HORIZON_AMOUNT),
        ])

        self.assertEqual(result.payment_status, payment_statuses.INVALID_PAYMENT)
        self.assertEqual(result.reason, payments.REASON_PAYER_MISMATCH)

    def test_binds_to_the_operation_payer_not_to_the_transaction_source(self):
        """`from` is the operation's own source; a transaction sourced elsewhere is irrelevant."""
        result, _ = self._verify([
            payment_op_record(
                from_account=DEFAULT_PROPOSED_BY,
                amount=HORIZON_AMOUNT,
                source=SECONDARY_ACCOUNT,
            ),
        ])

        self.assertEqual(result.payment_status, payment_statuses.FINE)
        self.assertEqual(result.payer, DEFAULT_PROPOSED_BY)

    def test_rejects_a_payment_whose_operation_payer_differs_from_the_transaction_source(self):
        result, _ = self._verify([
            payment_op_record(
                from_account=SECONDARY_ACCOUNT,
                amount=HORIZON_AMOUNT,
                source=DEFAULT_PROPOSED_BY,
            ),
        ])

        self.assertEqual(result.payment_status, payment_statuses.INVALID_PAYMENT)
        self.assertEqual(result.reason, payments.REASON_PAYER_MISMATCH)

    def test_accepts_a_muxed_payer_that_resolves_to_the_expected_account(self):
        result, _ = self._verify([
            payment_op_record(from_account=_muxed(DEFAULT_PROPOSED_BY), amount=HORIZON_AMOUNT),
        ])

        self.assertEqual(result.payment_status, payment_statuses.FINE)
        self.assertEqual(result.payer, DEFAULT_PROPOSED_BY)

    def test_accepts_a_record_carrying_both_from_and_from_muxed(self):
        result, _ = self._verify([
            payment_op_record(
                from_account=DEFAULT_PROPOSED_BY,
                amount=HORIZON_AMOUNT,
                from_muxed=_muxed(DEFAULT_PROPOSED_BY),
            ),
        ])

        self.assertEqual(result.payment_status, payment_statuses.FINE)
        self.assertEqual(result.payer, DEFAULT_PROPOSED_BY)

    def test_rejects_a_muxed_payer_for_a_different_account(self):
        result, _ = self._verify([
            payment_op_record(from_account=_muxed(SECONDARY_ACCOUNT), amount=HORIZON_AMOUNT),
        ])

        self.assertEqual(result.payment_status, payment_statuses.INVALID_PAYMENT)
        self.assertEqual(result.reason, payments.REASON_PAYER_MISMATCH)

    def test_rejects_a_malformed_payer_without_letting_an_exception_escape(self):
        result, _ = self._verify([
            payment_op_record(from_account='not-an-account', amount=HORIZON_AMOUNT),
        ])

        self.assertEqual(result.payment_status, payment_statuses.INVALID_PAYMENT)
        self.assertEqual(result.reason, payments.REASON_PAYER_MISMATCH)

    def test_rejects_a_record_with_no_payer_at_all(self):
        record = payment_op_record(from_account=DEFAULT_PROPOSED_BY, amount=HORIZON_AMOUNT)
        del record['from']

        result, _ = self._verify([record])

        self.assertEqual(result.payment_status, payment_statuses.INVALID_PAYMENT)
        self.assertEqual(result.reason, payments.REASON_PAYER_MISMATCH)

    def test_skips_non_payment_operations(self):
        result, _ = self._verify([
            non_payment_op_record(operation_type='create_account'),
            non_payment_op_record(operation_type='invoke_host_function'),
            payment_op_record(from_account=DEFAULT_PROPOSED_BY, amount=HORIZON_AMOUNT),
        ])

        self.assertEqual(result.payment_status, payment_statuses.FINE)

    def test_finds_the_aqua_payment_behind_a_native_payment_in_the_same_transaction(self):
        """A native payment carries no asset_code, and reading one used to abort the scan."""
        result, _ = self._verify([
            native_payment_op_record(from_account=DEFAULT_PROPOSED_BY, amount='1.0000000'),
            payment_op_record(from_account=DEFAULT_PROPOSED_BY, amount=HORIZON_AMOUNT),
        ])

        self.assertEqual(result.payment_status, payment_statuses.FINE)
        self.assertEqual(result.payer, DEFAULT_PROPOSED_BY)

    def test_skips_a_failed_operation_inside_a_successful_transaction(self):
        result, _ = self._verify([
            payment_op_record(
                from_account=DEFAULT_PROPOSED_BY,
                amount=HORIZON_AMOUNT,
                transaction_successful=False,
            ),
        ])

        self.assertEqual(result.payment_status, payment_statuses.INVALID_PAYMENT)
        self.assertEqual(result.reason, payments.REASON_NO_MATCHING_PAYMENT)

    def test_rejects_a_payment_sent_to_the_wrong_destination(self):
        result, _ = self._verify([
            payment_op_record(
                from_account=DEFAULT_PROPOSED_BY,
                amount=HORIZON_AMOUNT,
                to=TERTIARY_ACCOUNT,
            ),
        ])

        self.assertEqual(result.payment_status, payment_statuses.INVALID_PAYMENT)
        self.assertEqual(result.reason, payments.REASON_NO_MATCHING_PAYMENT)

    def test_rejects_a_payment_of_an_asset_issued_by_someone_else(self):
        result, _ = self._verify([
            payment_op_record(
                from_account=DEFAULT_PROPOSED_BY,
                amount=HORIZON_AMOUNT,
                asset_issuer=TERTIARY_ACCOUNT,
            ),
        ])

        self.assertEqual(result.payment_status, payment_statuses.INVALID_PAYMENT)
        self.assertEqual(result.reason, payments.REASON_NO_MATCHING_PAYMENT)

    def test_rejects_a_payment_of_another_asset_code(self):
        result, _ = self._verify([
            payment_op_record(
                from_account=DEFAULT_PROPOSED_BY,
                amount=HORIZON_AMOUNT,
                asset_code='YXLM',
            ),
        ])

        self.assertEqual(result.payment_status, payment_statuses.INVALID_PAYMENT)
        self.assertEqual(result.reason, payments.REASON_NO_MATCHING_PAYMENT)

    def test_amount_and_payer_are_checked_against_the_same_operation(self):
        """The owner's token payment must not lend its payer to the attacker's full payment."""
        result, _ = self._verify([
            payment_op_record(from_account=DEFAULT_PROPOSED_BY, amount='1.0000000'),
            payment_op_record(from_account=SECONDARY_ACCOUNT, amount=HORIZON_AMOUNT),
        ])

        self.assertEqual(result.payment_status, payment_statuses.INVALID_PAYMENT)
        self.assertEqual(result.reason, payments.REASON_PAYER_MISMATCH)

    def test_reports_no_matching_payment_when_nobody_paid_the_issuer(self):
        result, _ = self._verify([
            non_payment_op_record(operation_type='create_account'),
        ])

        self.assertEqual(result.payment_status, payment_statuses.INVALID_PAYMENT)
        self.assertEqual(result.reason, payments.REASON_NO_MATCHING_PAYMENT)

    def test_rejects_an_unusable_expected_payer_before_reaching_horizon(self):
        expectation = _create_expectation()
        horizon_server = FakePaymentHorizonServer(
            transaction=transaction_record(memo=_memo_base64(expectation)),
            operations=[payment_op_record(from_account=DEFAULT_PROPOSED_BY, amount=HORIZON_AMOUNT)],
        )

        with self.assertLogs('aqua_governance.utils.payments', level='ERROR'):
            result = payments.verify_payment(
                transaction_hash=TRANSACTION_HASH,
                expected_payer=None,
                memo_expectation=expectation,
                payment_amount=AMOUNT,
                horizon_server=horizon_server,
            )

        self.assertEqual(result.payment_status, payment_statuses.INVALID_PAYMENT)
        self.assertEqual(result.reason, payments.REASON_PAYER_MISMATCH)
        self.assertEqual(horizon_server.transaction_calls, [])
        self.assertEqual(horizon_server.operation_calls, [])

    def test_verify_payment_requires_an_expected_payer(self):
        with self.assertRaises(TypeError):
            payments.verify_payment(
                transaction_hash=TRANSACTION_HASH,
                memo_expectation=_create_expectation(),
            )


@override_settings(DEBUG=False)
class EnvelopePayerBindingTests(SimpleTestCase):
    """`inspect_envelope` binds the payer the same way, at the operation level."""

    def _expectation(self):
        return _create_expectation()

    def _envelope(self, *, source, op_source=None, amount=AMOUNT, extra_ops=()):
        expectation = self._expectation()
        envelope_xdr, _ = build_aqua_burn_envelope(
            source=source,
            amount=amount,
            memo_hash_hex=expectation.canonical_digest.hex(),
            op_source=op_source,
            extra_ops=extra_ops,
        )
        return envelope_xdr, expectation

    def test_accepts_an_envelope_whose_transaction_source_is_the_expected_payer(self):
        envelope_xdr, expectation = self._envelope(source=DEFAULT_PROPOSED_BY)

        status = payments.inspect_envelope(
            envelope_xdr=envelope_xdr,
            expected_payer=DEFAULT_PROPOSED_BY,
            memo_expectation=expectation,
            payment_amount=AMOUNT,
        )

        self.assertEqual(status, payment_statuses.FINE)

    def test_uses_the_operation_source_when_the_operation_declares_one(self):
        envelope_xdr, expectation = self._envelope(
            source=SECONDARY_ACCOUNT,
            op_source=DEFAULT_PROPOSED_BY,
        )

        status = payments.inspect_envelope(
            envelope_xdr=envelope_xdr,
            expected_payer=DEFAULT_PROPOSED_BY,
            memo_expectation=expectation,
            payment_amount=AMOUNT,
        )

        self.assertEqual(status, payment_statuses.FINE)

    def test_rejects_an_envelope_paid_by_another_account(self):
        envelope_xdr, expectation = self._envelope(
            source=DEFAULT_PROPOSED_BY,
            op_source=SECONDARY_ACCOUNT,
        )

        status = payments.inspect_envelope(
            envelope_xdr=envelope_xdr,
            expected_payer=DEFAULT_PROPOSED_BY,
            memo_expectation=expectation,
            payment_amount=AMOUNT,
        )

        self.assertEqual(status, payment_statuses.INVALID_PAYMENT)

    def test_rejects_an_envelope_paying_the_wrong_amount(self):
        envelope_xdr, expectation = self._envelope(source=DEFAULT_PROPOSED_BY, amount=AMOUNT * 9)

        status = payments.inspect_envelope(
            envelope_xdr=envelope_xdr,
            expected_payer=DEFAULT_PROPOSED_BY,
            memo_expectation=expectation,
            payment_amount=AMOUNT,
        )

        self.assertEqual(status, payment_statuses.INVALID_PAYMENT)

    def test_rejects_an_envelope_whose_memo_commits_to_another_transition(self):
        envelope_xdr, _ = self._envelope(source=DEFAULT_PROPOSED_BY)
        other_expectation = build_memo_expectation(
            PURPOSE_CREATE,
            proposed_by=DEFAULT_PROPOSED_BY,
            proposal_type='ADD_ASSET',
            title='Test proposal',
            text_html='<p>Hello</p>',
        )

        status = payments.inspect_envelope(
            envelope_xdr=envelope_xdr,
            expected_payer=DEFAULT_PROPOSED_BY,
            memo_expectation=other_expectation,
            payment_amount=AMOUNT,
        )

        self.assertEqual(status, payment_statuses.BAD_MEMO)

    def test_an_unparseable_envelope_is_an_invalid_payment_not_a_horizon_error(self):
        for envelope_xdr in ('', 'AAAA', 'not base64!!', None, 123):
            with self.subTest(envelope_xdr=envelope_xdr):
                status = payments.inspect_envelope(
                    envelope_xdr=envelope_xdr,
                    expected_payer=DEFAULT_PROPOSED_BY,
                    memo_expectation=self._expectation(),
                    payment_amount=AMOUNT,
                )

                self.assertEqual(status, payment_statuses.INVALID_PAYMENT)

    def test_resolves_the_payment_amount_from_settings_when_none_is_supplied(self):
        envelope_xdr, expectation = self._envelope(
            source=DEFAULT_PROPOSED_BY,
            amount=Decimal(settings.PROPOSAL_COST),
        )

        status = payments.inspect_envelope(
            envelope_xdr=envelope_xdr,
            expected_payer=DEFAULT_PROPOSED_BY,
            memo_expectation=expectation,
        )

        self.assertEqual(status, payment_statuses.FINE)
