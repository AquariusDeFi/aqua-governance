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
from aqua_governance.utils.payments import (
    REASON_MEMO_MISMATCH,
    REASON_NO_MATCHING_PAYMENT,
    REASON_OK,
    REASON_TRANSACTION_FAILED,
    REASON_TRANSACTION_NOT_FOUND,
    PaymentCheckResult,
)


VERIFY_PAYMENT = 'aqua_governance.governance.proposal_transactions.verify_payment'
CLAIM = 'aqua_governance.governance.proposal_transactions.claim_transaction_hashes'
ALERT = 'aqua_governance.governance.proposal_transactions._alert_operator'

DEFAULT_TITLE = 'Test general proposal'

# The reason ``verify_payment`` reports alongside each non-FINE status.  One per status is
# enough here: the promotion paths branch on the status, and the reason only reaches logs.
REASON_FOR_STATUS = {
    Proposal.HORIZON_ERROR: REASON_TRANSACTION_NOT_FOUND,
    Proposal.BAD_MEMO: REASON_MEMO_MISMATCH,
    Proposal.INVALID_PAYMENT: REASON_NO_MATCHING_PAYMENT,
    Proposal.FAILED_TRANSACTION: REASON_TRANSACTION_FAILED,
}


def quill(html):
    """The shape ``QuillField.to_internal_value`` stores, so ``.html`` survives a round trip."""
    return Quill(json.dumps({'delta': '', 'html': html}))


def fine(transaction_hash, *, resolved_hashes=None, payer=DEFAULT_PROPOSED_BY):
    """The verdict Horizon produces for a payment that authorizes the transition.

    ``amount`` is left unset: production fills it with the cost the caller asked for, which
    differs between a creation and a publication, and no promotion path reads it back.
    """
    return PaymentCheckResult(
        payment_status=Proposal.FINE,
        reason=REASON_OK,
        transaction_hash=transaction_hash,
        resolved_hashes=resolved_hashes if resolved_hashes is not None else (transaction_hash,),
        payer=payer,
        memo_format=MEMO_FORMAT_CANONICAL,
    )


def verdict(payment_status, transaction_hash, *, payer=DEFAULT_PROPOSED_BY):
    """The verdict ``verify_payment`` builds for ``payment_status`` on this hash.

    A non-FINE verdict authorizes nothing, so it carries neither a payer nor a claimable
    hash - which is what makes it worth returning rather than a bare status: a promotion
    path that reached for either on a rejected payment would be caught here.
    """
    if payment_status == Proposal.FINE:
        return fine(transaction_hash, payer=payer)

    return PaymentCheckResult(
        payment_status=payment_status,
        reason=REASON_FOR_STATUS[payment_status],
        transaction_hash=transaction_hash,
    )


def verifies(payment_status=Proposal.FINE):
    """A ``verify_payment`` stand-in for a flow whose hashes are not known up front.

    Every call is answered about the hash it was actually asked about, bound to the payer
    it was asked to bind to, so the claim burns the hash the transition really presented.
    A fixed ``return_value`` cannot do that once one test drives several transitions, or
    builds its hashes inside the loop that runs them.
    """
    def _verify(*, transaction_hash, expected_payer, **kwargs):
        return verdict(payment_status, transaction_hash, payer=expected_payer)

    return _verify


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
