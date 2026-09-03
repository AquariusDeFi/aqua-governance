"""A test case that owns an offline chain, for the modules that drive the API end to end.

The payment is the only authenticated channel in this flow, so the tests that matter most
have to build real envelopes, settle them on something that answers like Horizon, and then
go through the live endpoints with nothing patched in between.  This is the plumbing all of
them share: a ledger keyed by transaction hash, the burn a client builds before signing, and
the four request shapes those flows need.
"""
import base64
import json
from datetime import timedelta
from unittest.mock import patch

from django.conf import settings
from django.test import TestCase
from django.utils import timezone

from rest_framework.test import APIClient

from django_quill.quill import Quill

from aqua_governance.governance.models import Proposal
from aqua_governance.governance.proposal_queue import get_queue_week_start
from aqua_governance.governance.tests._factories import (
    DEFAULT_PROPOSED_BY,
    SECONDARY_ACCOUNT,
    build_aqua_burn_envelope,
    patch_ice_circulating_supply,
)
from aqua_governance.governance.tests._horizon import FakeLedgerHorizonServer


OWNER = DEFAULT_PROPOSED_BY
OTHER_ACCOUNT = SECONDARY_ACCOUNT

CREATE_COST = settings.PROPOSAL_CREATE_OR_UPDATE_COST
SUBMIT_COST = settings.PROPOSAL_SUBMIT_COST

HORIZON_SERVER = 'aqua_governance.utils.payments.Server'
PAYMENTS_LOGGER = 'aqua_governance.utils.payments'


def quill(html):
    """The shape ``QuillField.to_internal_value`` stores, so ``.html`` survives a round trip."""
    return Quill(json.dumps({'delta': '', 'html': html}))


def utc_second_iso(value):
    """What the frontend's ``toUtcSecondIso`` produces, and what the API is handed verbatim."""
    return value.replace(microsecond=0).isoformat().replace('+00:00', 'Z')


class OnChainTestCase(TestCase):
    """A live API against an offline chain, with none of the payment controls patched out."""

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.ice_supply_patcher = patch_ice_circulating_supply()
        self.ice_supply_patcher.start()
        self.addCleanup(self.ice_supply_patcher.stop)

        self.ledger = FakeLedgerHorizonServer()
        server_patcher = patch(HORIZON_SERVER, return_value=self.ledger)
        server_patcher.start()
        self.addCleanup(server_patcher.stop)

        self._sequence = 0

    # -- the chain --------------------------------------------------------

    def next_sequence(self):
        """A distinct source-account sequence per envelope, so no two share a hash."""
        self._sequence += 1
        return self._sequence

    def settle(self, transaction_hash, *, memo_bytes, payer, amount):
        self.ledger.settle_payment(
            transaction_hash,
            memo=base64.b64encode(memo_bytes).decode(),
            from_account=payer,
            amount='{0}.0000000'.format(amount),
        )

    def burn(self, *, amount, memo_bytes, source=OWNER, op_source=None, settle=True):
        """Build the burn envelope a client posts before signing, and put it on the chain.

        ``op_source`` is the only way to make the account that pays differ from the account
        the envelope names as its source, which is the whole distance between the declared
        owner and the payer.  Left unset it reproduces what both clients build: one payment
        operation with no source of its own, inheriting the transaction's.
        """
        envelope_xdr, transaction_hash = build_aqua_burn_envelope(
            source=source,
            amount=amount,
            memo_hash_hex=memo_bytes.hex(),
            op_source=op_source,
            sequence=self.next_sequence(),
        )
        if settle:
            self.settle(
                transaction_hash,
                memo_bytes=memo_bytes,
                payer=op_source or source,
                amount=amount,
            )
        return envelope_xdr, transaction_hash

    # -- the API ----------------------------------------------------------

    def post_create(self, body, path='/api/proposal/'):
        return self.client.post(path, body, format='json')

    def patch_update(self, proposal, body):
        return self.client.patch('/api/proposal/{0}/'.format(proposal.id), body, format='json')

    def post_submit(self, proposal, body):
        return self.client.post('/api/proposal/{0}/submit/'.format(proposal.id), body,
                                format='json')

    def confirm(self, proposal, path='/api/proposal/{0}/check_payment/'):
        """``checkProposalStatus`` posts no body at all."""
        return self.client.post(path.format(proposal.id))

    # -- time -------------------------------------------------------------

    def week(self, weeks_ahead=1):
        start_at = get_queue_week_start(timezone.now()) + timedelta(weeks=weeks_ahead)
        return start_at, start_at + timedelta(days=7, seconds=-1)

    def open_for_submit(self, proposal):
        """Age the row past the discussion window the submit queryset requires."""
        Proposal.objects.filter(id=proposal.id).update(
            last_updated_at=timezone.now() - settings.DISCUSSION_TIME - timedelta(days=1),
        )
        proposal.refresh_from_db()
        return proposal
