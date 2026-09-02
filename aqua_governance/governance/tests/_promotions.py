"""Shared harness for driving a proposal promotion without Horizon.

The verdict is the only thing mocked: everything downstream of it - the row lock, the
fingerprint guard, the conditional UPDATE, the claim - runs for real against the database,
because that is the part these tests exist to pin.
"""
import json
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from rest_framework.test import APIClient

from django_quill.quill import Quill

from aqua_governance.governance.consumed_transactions import claim_transaction_hashes
from aqua_governance.governance.models import ConsumedTransaction, Proposal
from aqua_governance.governance.proposal_queue import get_queue_week_start
from aqua_governance.governance.tests._factories import (
    DEFAULT_PROPOSED_BY,
    distinct_hash,
    make_general_proposal,
    patch_ice_circulating_supply,
)
from aqua_governance.utils.memo import MEMO_FORMAT_CANONICAL
from aqua_governance.utils.payments import PaymentCheckResult


CHECK_STATUS = 'aqua_governance.governance.proposal_transactions.check_proposal_status'
CLAIM = 'aqua_governance.governance.proposal_transactions.claim_transaction_hashes'
ALERT = 'aqua_governance.governance.proposal_transactions._alert_operator'

DEFAULT_TITLE = 'Test general proposal'


def quill(html):
    """The shape ``QuillField.to_internal_value`` stores, so ``.html`` survives a round trip."""
    return Quill(json.dumps({'delta': '', 'html': html}))


def fine(transaction_hash, *, resolved_hashes=None, payer=DEFAULT_PROPOSED_BY):
    """The verdict Horizon produces for a payment that authorizes the transition."""
    return PaymentCheckResult(
        payment_status=Proposal.FINE,
        reason='ok',
        transaction_hash=transaction_hash,
        resolved_hashes=resolved_hashes if resolved_hashes is not None else (transaction_hash,),
        payer=payer,
        memo_format=MEMO_FORMAT_CANONICAL,
    )


def make(**overrides):
    return make_general_proposal(proposed_by=DEFAULT_PROPOSED_BY, **overrides)


def competing_claim_then_delegate(transaction_hash):
    """The racing worker that reached the ledger first, followed by the real claim."""
    def _claim(**kwargs):
        ConsumedTransaction.objects.create(
            transaction_hash=transaction_hash,
            purpose=ConsumedTransaction.PURPOSE_LEGACY,
        )
        return claim_transaction_hashes(**kwargs)

    return _claim


class PromotionTestCase(TestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.ice_supply_patcher = patch_ice_circulating_supply()
        self.ice_supply_patcher.start()
        self.addCleanup(self.ice_supply_patcher.stop)

    def pending_update(self, index, **overrides):
        defaults = {
            'transaction_hash': distinct_hash(index),
            'action': Proposal.TO_UPDATE,
            'new_title': 'Staged title',
            'new_text': quill('<p>Staged text</p>'),
            'new_transaction_hash': distinct_hash(index + 1),
            'new_envelope_xdr': 'update-xdr',
        }
        defaults.update(overrides)
        return make(**defaults)

    def pending_create(self, index, **overrides):
        defaults = {
            'transaction_hash': distinct_hash(index),
            'draft': True,
            'action': Proposal.TO_CREATE,
        }
        defaults.update(overrides)
        return make(**defaults)

    def pending_submit(self, index, **overrides):
        start_at = get_queue_week_start(timezone.now()) + timedelta(weeks=1)
        defaults = {
            'transaction_hash': distinct_hash(index),
            'action': Proposal.TO_SUBMIT,
            'new_start_at': start_at,
            'new_end_at': start_at + timedelta(days=7, seconds=-1),
            'new_transaction_hash': distinct_hash(index + 1),
            'new_envelope_xdr': 'submit-xdr',
        }
        defaults.update(overrides)
        return make(**defaults)
