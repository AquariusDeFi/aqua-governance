"""What the canonical memo grammar enforces once it is the only grammar accepted.

The memo is the part of a payment that says *which* transition was paid for.  The canonical
grammar commits to the purpose, the proposal, the title, the text and the voting window; the
pre-v1 grammar committed to the text alone.  Everything the wider grammar adds is asserted
here, in the state the backend reaches when legacy acceptance is withdrawn - the class
carries ``PROPOSAL_LEGACY_MEMO_ACCEPTED=False``, which is the one production setting that
changes.

Every case is skipped while production still accepts the pre-v1 grammar, because these are
properties of that later configuration rather than of the one now deployed.  Turning them on
is deleting the decorators; no assertion below needs rewriting first.  The order of
operations is the other way round from usual, and deliberately so: the drain criteria are
checked against production traffic, the flag is flipped, and these tests are what say the
grammar behaves as designed once it is.
"""
from unittest import skip

from django.test import override_settings

from aqua_governance.governance.models import ConsumedTransaction, Proposal, ProposalQueueSlot
from aqua_governance.governance.tests._chain import (
    CREATE_COST,
    OWNER,
    SUBMIT_COST,
    OnChainTestCase,
    quill,
    utc_second_iso,
)
from aqua_governance.governance.tests._factories import distinct_hash, make_general_proposal
from aqua_governance.utils.memo import (
    create_memo_payload,
    legacy_memo_digest,
    memo_digest,
    submit_memo_payload,
    update_memo_payload,
)


SKIP_REASON = 'Asserted once the canonical memo is the only grammar the backend accepts.'

PROPOSAL_TITLE = 'A proposal the owner wrote'
PROPOSAL_TEXT = '<p>The proposal the owner wrote.</p>'
REVISED_TITLE = 'A proposal the owner revised'
REVISED_TEXT = '<p>The proposal the owner revised.</p>'


def canonical_create(*, proposed_by, proposal_type, title, text_html):
    return memo_digest(create_memo_payload(
        proposed_by=proposed_by,
        proposal_type=proposal_type,
        title=title,
        text_html=text_html,
    ))


def canonical_update(*, proposal_id, new_title, new_text_html):
    return memo_digest(update_memo_payload(
        proposal_id=proposal_id,
        new_title=new_title,
        new_text_html=new_text_html,
    ))


def canonical_submit(*, proposal_id, start_at, end_at):
    return memo_digest(submit_memo_payload(
        proposal_id=proposal_id,
        start_at=start_at,
        end_at=end_at,
    ))


@override_settings(DEBUG=False, PROPOSAL_LEGACY_MEMO_ACCEPTED=False)
class CanonicalMemoEnforcementTests(OnChainTestCase):
    """The wider preimage, exercised through the same endpoints a browser drives."""

    def _create_body(self, transaction_hash, envelope_xdr, *, title=PROPOSAL_TITLE):
        return {
            'proposed_by': OWNER,
            'title': title,
            'text': PROPOSAL_TEXT,
            'transaction_hash': transaction_hash,
            'envelope_xdr': envelope_xdr,
            'discord_username': 'proposer',
        }

    def _confirmed_proposal(self, *, title=PROPOSAL_TITLE):
        envelope_xdr, transaction_hash = self.burn(
            amount=CREATE_COST,
            memo_bytes=canonical_create(
                proposed_by=OWNER,
                proposal_type=Proposal.PROPOSAL_TYPE_GENERAL,
                title=title,
                text_html=PROPOSAL_TEXT,
            ),
        )
        created = self.post_create(self._create_body(transaction_hash, envelope_xdr, title=title))
        self.assertEqual(created.status_code, 201, created.data)

        proposal = Proposal.objects.get(id=created.data['id'])
        self.assertEqual(self.confirm(proposal).status_code, 200)
        proposal.refresh_from_db()
        self.assertEqual(proposal.payment_status, Proposal.FINE)
        return proposal

    def _stage_update(self, proposal, *, title, text_html, transaction_hash, envelope_xdr):
        return self.patch_update(proposal, {
            'new_title': title,
            'new_text': text_html,
            'new_transaction_hash': transaction_hash,
            'new_envelope_xdr': envelope_xdr,
        })

    def _stage_submit(self, proposal, *, start_at, end_at, transaction_hash, envelope_xdr):
        return self.post_submit(self.open_for_submit(proposal), {
            'start_at': utc_second_iso(start_at),
            'end_at': utc_second_iso(end_at),
            'new_transaction_hash': transaction_hash,
            'new_envelope_xdr': envelope_xdr,
        })

    # -- the grammar itself -----------------------------------------------

    @skip(SKIP_REASON)
    def test_a_payment_carrying_the_pre_v1_memo_no_longer_confirms_a_creation(self):
        """The pre-v1 preimage is the proposal text and nothing else, so it is not accepted."""
        envelope_xdr, transaction_hash = self.burn(
            amount=CREATE_COST, memo_bytes=legacy_memo_digest(PROPOSAL_TEXT))
        created = self.post_create(self._create_body(transaction_hash, envelope_xdr))
        self.assertEqual(created.status_code, 201, created.data)
        proposal = Proposal.objects.get(id=created.data['id'])

        confirmation = self.confirm(proposal)

        self.assertEqual(confirmation.status_code, 200, confirmation.data)
        proposal.refresh_from_db()
        self.assertEqual(proposal.payment_status, Proposal.BAD_MEMO)
        self.assertTrue(proposal.hide)
        self.assertFalse(ConsumedTransaction.objects.exists())

    @skip(SKIP_REASON)
    def test_the_canonical_create_memo_confirms_a_creation(self):
        proposal = self._confirmed_proposal()

        self.assertFalse(proposal.draft)
        self.assertEqual(proposal.action, Proposal.NONE)
        self.assertEqual(
            ConsumedTransaction.objects.get(proposal=proposal).purpose,
            ConsumedTransaction.PURPOSE_CREATE,
        )

    @skip(SKIP_REASON)
    def test_the_canonical_update_memo_confirms_an_update(self):
        proposal = self._confirmed_proposal()
        envelope_xdr, transaction_hash = self.burn(
            amount=CREATE_COST,
            memo_bytes=canonical_update(
                proposal_id=proposal.id,
                new_title=REVISED_TITLE,
                new_text_html=REVISED_TEXT,
            ),
        )
        staged = self._stage_update(
            proposal, title=REVISED_TITLE, text_html=REVISED_TEXT,
            transaction_hash=transaction_hash, envelope_xdr=envelope_xdr)
        self.assertEqual(staged.status_code, 200, staged.data)

        confirmation = self.confirm(proposal)

        self.assertEqual(confirmation.status_code, 200, confirmation.data)
        proposal.refresh_from_db()
        self.assertEqual(proposal.title, REVISED_TITLE)
        self.assertEqual(proposal.text.html, REVISED_TEXT)

    @skip(SKIP_REASON)
    def test_the_canonical_submit_memo_books_the_window_it_names(self):
        proposal = self._confirmed_proposal()
        start_at, end_at = self.week()
        envelope_xdr, transaction_hash = self.burn(
            amount=SUBMIT_COST,
            memo_bytes=canonical_submit(
                proposal_id=proposal.id, start_at=start_at, end_at=end_at),
        )
        staged = self._stage_submit(
            proposal, start_at=start_at, end_at=end_at,
            transaction_hash=transaction_hash, envelope_xdr=envelope_xdr)
        self.assertEqual(staged.status_code, 200, staged.data)

        confirmation = self.confirm(proposal)

        self.assertEqual(confirmation.status_code, 200, confirmation.data)
        proposal.refresh_from_db()
        self.assertEqual(proposal.proposal_status, Proposal.QUEUED)
        self.assertEqual(proposal.start_at, start_at)
        self.assertTrue(ProposalQueueSlot.objects.filter(proposal=proposal).exists())

    # -- what the wider preimage closes -----------------------------------

    @skip(SKIP_REASON)
    def test_the_memo_commits_to_the_title_so_a_substituted_title_does_not_confirm(self):
        """The canonical UPDATE preimage carries the title, which the pre-v1 one did not.

        A staged copy whose title no longer matches the one the payment names is a copy the
        payment does not pay for, whoever wrote it.
        """
        proposal = self._confirmed_proposal()
        envelope_xdr, transaction_hash = self.burn(
            amount=CREATE_COST,
            memo_bytes=canonical_update(
                proposal_id=proposal.id,
                new_title=REVISED_TITLE,
                new_text_html=REVISED_TEXT,
            ),
        )
        staged = self._stage_update(
            proposal, title='A title the owner never chose', text_html=REVISED_TEXT,
            transaction_hash=transaction_hash, envelope_xdr=envelope_xdr)
        self.assertEqual(staged.status_code, 200, staged.data)

        confirmation = self.confirm(proposal)

        self.assertEqual(confirmation.status_code, 200, confirmation.data)
        proposal.refresh_from_db()
        self.assertEqual(proposal.title, PROPOSAL_TITLE)
        self.assertEqual(proposal.payment_status, Proposal.BAD_MEMO)
        self.assertFalse(proposal.history_proposal.exists())

    @skip(SKIP_REASON)
    def test_the_memo_commits_to_the_voting_window_so_a_substituted_window_does_not_confirm(self):
        """The canonical SUBMIT preimage carries both instants, to the second."""
        proposal = self._confirmed_proposal()
        paid_start_at, paid_end_at = self.week(weeks_ahead=1)
        other_start_at, other_end_at = self.week(weeks_ahead=3)
        envelope_xdr, transaction_hash = self.burn(
            amount=SUBMIT_COST,
            memo_bytes=canonical_submit(
                proposal_id=proposal.id, start_at=paid_start_at, end_at=paid_end_at),
        )
        staged = self._stage_submit(
            proposal, start_at=other_start_at, end_at=other_end_at,
            transaction_hash=transaction_hash, envelope_xdr=envelope_xdr)
        self.assertEqual(staged.status_code, 200, staged.data)

        confirmation = self.confirm(proposal)

        self.assertEqual(confirmation.status_code, 200, confirmation.data)
        proposal.refresh_from_db()
        self.assertEqual(proposal.proposal_status, Proposal.DISCUSSION)
        self.assertEqual(proposal.payment_status, Proposal.BAD_MEMO)
        self.assertFalse(ProposalQueueSlot.objects.filter(proposal=proposal).exists())

    @skip(SKIP_REASON)
    def test_the_memo_commits_to_the_proposal_so_an_update_payment_does_not_travel(self):
        """The proposal id is in the UPDATE preimage, so the payment names one row only."""
        first = self._confirmed_proposal(title=PROPOSAL_TITLE)
        second = make_general_proposal(
            proposed_by=OWNER,
            title='Another proposal by the same owner',
            text=quill(PROPOSAL_TEXT),
            transaction_hash=distinct_hash(1000),
        )
        envelope_xdr, transaction_hash = self.burn(
            amount=CREATE_COST,
            memo_bytes=canonical_update(
                proposal_id=first.id,
                new_title=REVISED_TITLE,
                new_text_html=REVISED_TEXT,
            ),
        )
        staged = self._stage_update(
            second, title=REVISED_TITLE, text_html=REVISED_TEXT,
            transaction_hash=transaction_hash, envelope_xdr=envelope_xdr)
        self.assertEqual(staged.status_code, 200, staged.data)

        confirmation = self.confirm(second)

        self.assertEqual(confirmation.status_code, 200, confirmation.data)
        second.refresh_from_db()
        self.assertEqual(second.title, 'Another proposal by the same owner')
        self.assertEqual(second.payment_status, Proposal.BAD_MEMO)

    @skip(SKIP_REASON)
    def test_the_memo_commits_to_the_purpose_so_a_submit_payment_cannot_settle_a_creation(self):
        """The purpose literal separates the three obligations even at an equal amount.

        In the deployed configuration only the amount does, which is why over-payment is
        refused there; once the purpose is bound, the separation no longer depends on price.
        """
        existing = self._confirmed_proposal()
        start_at, end_at = self.week()
        envelope_xdr, transaction_hash = self.burn(
            amount=CREATE_COST,
            memo_bytes=canonical_submit(
                proposal_id=existing.id, start_at=start_at, end_at=end_at),
        )
        created = self.post_create(self._create_body(
            transaction_hash, envelope_xdr, title='A creation paid for by a publication'))
        self.assertEqual(created.status_code, 201, created.data)
        proposal = Proposal.objects.get(id=created.data['id'])

        confirmation = self.confirm(proposal)

        self.assertEqual(confirmation.status_code, 200, confirmation.data)
        proposal.refresh_from_db()
        self.assertEqual(proposal.payment_status, Proposal.BAD_MEMO)
        self.assertTrue(proposal.hide)
        self.assertFalse(
            ConsumedTransaction.objects.filter(transaction_hash=transaction_hash).exists())
