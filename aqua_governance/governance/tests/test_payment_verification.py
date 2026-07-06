import base64
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.conf import settings
from django.test import SimpleTestCase
from django.test import override_settings
from django_quill.quill import Quill
from stellar_sdk import HashMemo

from aqua_governance.governance import proposal_transactions
from aqua_governance.governance.models import Proposal
from aqua_governance.governance import payment_statuses
from aqua_governance.utils.payments import check_proposal_status, check_transaction_xdr


def _quill_text(html='<p>Payment text</p>'):
    return Quill(json.dumps({'delta': {'ops': []}, 'html': html}))


def _memo_for_text(text: str) -> str:
    text_hash = hashlib.sha256(text.encode('utf-8')).hexdigest()
    return base64.b64encode(HashMemo(text_hash).memo_hash).decode()


@override_settings(DEBUG=False)
class PaymentVerificationTests(SimpleTestCase):
    @patch('aqua_governance.utils.payments.Server')
    def test_check_proposal_status_returns_horizon_error_when_lookup_fails(self, mock_server):
        mock_server.return_value.transactions.return_value.transaction.return_value.call.side_effect = RuntimeError('boom')

        status = check_proposal_status('a' * 64, '<p>Payment text</p>')

        self.assertEqual(status, payment_statuses.HORIZON_ERROR)

    @patch('aqua_governance.utils.payments.check_payment', return_value=False)
    @patch('aqua_governance.utils.payments.Server')
    def test_check_proposal_status_rejects_missing_payment(self, mock_server, mock_check_payment):
        mock_server.return_value.transactions.return_value.transaction.return_value.call.return_value = {
            'successful': True,
            'memo': _memo_for_text('<p>Payment text</p>'),
        }

        status = check_proposal_status('a' * 64, '<p>Payment text</p>')

        self.assertEqual(status, payment_statuses.INVALID_PAYMENT)
        mock_check_payment.assert_called_once_with('a' * 64, settings.PROPOSAL_COST)

    @patch('aqua_governance.utils.payments.check_payment')
    @patch('aqua_governance.utils.payments.Server')
    def test_check_proposal_status_rejects_unsuccessful_transaction(self, mock_server, mock_check_payment):
        mock_server.return_value.transactions.return_value.transaction.return_value.call.return_value = {
            'successful': False,
            'memo': _memo_for_text('<p>Payment text</p>'),
        }

        status = check_proposal_status('a' * 64, '<p>Payment text</p>')

        self.assertEqual(status, payment_statuses.FAILED_TRANSACTION)
        mock_check_payment.assert_not_called()

    @patch('aqua_governance.utils.payments.check_payment', return_value=True)
    @patch('aqua_governance.utils.payments.Server')
    def test_check_proposal_status_rejects_bad_memo(self, mock_server, _mock_check_payment):
        mock_server.return_value.transactions.return_value.transaction.return_value.call.return_value = {
            'successful': True,
            'memo': _memo_for_text('<p>Different text</p>'),
        }

        status = check_proposal_status('a' * 64, '<p>Payment text</p>')

        self.assertEqual(status, payment_statuses.BAD_MEMO)

    @patch('aqua_governance.utils.payments.check_payment', return_value=True)
    @patch('aqua_governance.utils.payments.Server')
    def test_check_proposal_status_rejects_missing_memo(self, mock_server, _mock_check_payment):
        transaction_call = mock_server.return_value.transactions.return_value.transaction.return_value.call

        for transaction_info in ({'successful': True}, {'successful': True, 'memo': None}):
            with self.subTest(transaction_info=transaction_info):
                transaction_call.return_value = transaction_info

                status = check_proposal_status('a' * 64, '<p>Payment text</p>')

                self.assertEqual(status, payment_statuses.BAD_MEMO)

    @patch('aqua_governance.utils.payments.check_payment', return_value=True)
    @patch('aqua_governance.utils.payments.Server')
    def test_check_proposal_status_accepts_matching_payment_and_memo(self, mock_server, _mock_check_payment):
        text = '<p>Payment text</p>'
        mock_server.return_value.transactions.return_value.transaction.return_value.call.return_value = {
            'successful': True,
            'memo': _memo_for_text(text),
        }

        status = check_proposal_status('a' * 64, text)

        self.assertEqual(status, payment_statuses.FINE)

    @patch('aqua_governance.utils.payments.check_xdr_payment', return_value=False)
    @patch('aqua_governance.utils.payments.TransactionEnvelope.from_xdr')
    def test_check_transaction_xdr_rejects_missing_payment(self, mock_from_xdr, mock_check_xdr_payment):
        mock_from_xdr.return_value = Mock(transaction=Mock(memo=HashMemo('0' * 64)))

        status = check_transaction_xdr({'envelope_xdr': 'AAAA', 'text': _quill_text()})

        self.assertEqual(status, payment_statuses.INVALID_PAYMENT)
        mock_check_xdr_payment.assert_called_once()

    @patch('aqua_governance.utils.payments.check_xdr_payment', return_value=True)
    @patch('aqua_governance.utils.payments.TransactionEnvelope.from_xdr')
    def test_check_transaction_xdr_rejects_bad_hash_memo(self, mock_from_xdr, _mock_check_xdr_payment):
        mock_from_xdr.return_value = Mock(transaction=Mock(memo=HashMemo('1' * 64)))

        status = check_transaction_xdr({'envelope_xdr': 'AAAA', 'text': _quill_text()})

        self.assertEqual(status, payment_statuses.BAD_MEMO)

    @patch('aqua_governance.utils.payments.check_xdr_payment', return_value=True)
    @patch('aqua_governance.utils.payments.TransactionEnvelope.from_xdr')
    def test_check_transaction_xdr_accepts_matching_hash_memo(self, mock_from_xdr, _mock_check_xdr_payment):
        text = _quill_text()
        text_hash = hashlib.sha256(text.html.encode('utf-8')).hexdigest()
        mock_from_xdr.return_value = Mock(transaction=Mock(memo=HashMemo(text_hash)))

        status = check_transaction_xdr({'envelope_xdr': 'AAAA', 'text': text})

        self.assertEqual(status, payment_statuses.FINE)


def _proposal_stub(**overrides):
    proposal = SimpleNamespace(
        id=1,
        TO_CREATE=Proposal.TO_CREATE,
        TO_UPDATE=Proposal.TO_UPDATE,
        TO_SUBMIT=Proposal.TO_SUBMIT,
        NONE=Proposal.NONE,
        FINE=Proposal.FINE,
        HORIZON_ERROR=Proposal.HORIZON_ERROR,
        action=Proposal.TO_CREATE,
        status=Proposal.DISCUSSION,
        proposal_status=Proposal.DISCUSSION,
        payment_status=None,
        draft=True,
        hide=False,
        is_asset_proposal=False,
        transaction_hash='a' * 64,
        new_transaction_hash='b' * 64,
        new_start_at=None,
        new_end_at=None,
        text=_quill_text('<p>Current payment text</p>'),
        new_text=_quill_text('<p>Updated payment text</p>'),
        save=Mock(),
    )
    proposal.__dict__.update(overrides)
    return proposal


class ProposalTransactionPaymentAmountTests(SimpleTestCase):
    @patch(
        'aqua_governance.governance.proposal_transactions.check_proposal_status',
        return_value=Proposal.BAD_MEMO,
    )
    def test_create_path_uses_create_or_update_payment_amount(self, mock_check_status):
        proposal = _proposal_stub(action=Proposal.TO_CREATE)

        proposal_transactions.check_transaction(proposal)

        mock_check_status.assert_called_once_with(
            proposal.transaction_hash,
            proposal.text.html,
            settings.PROPOSAL_CREATE_OR_UPDATE_COST,
        )

    @patch(
        'aqua_governance.governance.proposal_transactions.check_proposal_status',
        return_value=Proposal.BAD_MEMO,
    )
    def test_update_path_uses_create_or_update_payment_amount(self, mock_check_status):
        proposal = _proposal_stub(action=Proposal.TO_UPDATE)

        proposal_transactions.check_transaction(proposal)

        mock_check_status.assert_called_once_with(
            proposal.new_transaction_hash,
            proposal.new_text.html,
            settings.PROPOSAL_CREATE_OR_UPDATE_COST,
        )

    @patch(
        'aqua_governance.governance.proposal_transactions.check_proposal_status',
        return_value=Proposal.BAD_MEMO,
    )
    def test_submit_path_uses_submit_payment_amount(self, mock_check_status):
        proposal = _proposal_stub(action=Proposal.TO_SUBMIT)

        proposal_transactions.check_transaction(proposal)

        mock_check_status.assert_called_once_with(
            proposal.new_transaction_hash,
            proposal.text.html,
            settings.PROPOSAL_SUBMIT_COST,
        )
