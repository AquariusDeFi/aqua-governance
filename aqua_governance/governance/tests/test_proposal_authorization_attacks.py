"""The composition proof: what authorizes a proposal transition is the payment, nothing else.

Every case here drives the real API against real, offline-built envelopes settled on a fake
Horizon.  ``check_proposal_status`` is never patched, and neither is the declared-owner
check - each attack supplies an envelope whose ``source`` names the victim, which is exactly
the input that check used to treat as proof of ownership.  A test that mocked either would
prove nothing about the composition.

Read the module as a table of vectors: for each one, the attack is played end to end and the
assertions say what the attacker got.  Where an attack is refused before it reaches the
ledger, the case also plays the promotion-level variant against a row staged behind the
serializer, because a defence that only exists at the staging layer is a defence an
unauthenticated caller can route around.
"""
import json

from django.conf import settings
from django.test import override_settings

from stellar_sdk import TransactionEnvelope

from aqua_governance.governance import tasks
from aqua_governance.governance.models import ConsumedTransaction, Proposal, ProposalQueueSlot
from aqua_governance.governance.tests._chain import (
    CREATE_COST,
    OTHER_ACCOUNT,
    OWNER,
    PAYMENTS_LOGGER,
    SUBMIT_COST,
    OnChainTestCase,
    quill,
    utc_second_iso,
)
from aqua_governance.governance.tests._factories import (
    DEFAULT_CODE,
    DEFAULT_ISSUER,
    OWNER_KEYPAIR,
    asset_narratives,
    distinct_hash,
    make_general_proposal,
)
from aqua_governance.utils.memo import legacy_memo_digest


VICTIM = OWNER
ATTACKER = OTHER_ACCOUNT

VICTIM_TEXT = '<p>The proposal the owner wrote.</p>'
ATTACKER_TEXT = '<p>The proposal the attacker wants voted on.</p>'


@override_settings(DEBUG=False)
class AuthorizationAttackTestCase(OnChainTestCase):
    """Shared fixtures: an owner proposal that was really created and really paid for."""

    @staticmethod
    def _signed(envelope_xdr, keypair):
        envelope = TransactionEnvelope.from_xdr(envelope_xdr, settings.NETWORK_PASSPHRASE)
        envelope.sign(keypair)
        return envelope.to_xdr()

    def _post_create(self, *, proposed_by, title, text_html, envelope_xdr, transaction_hash,
                     path='/api/proposal/', extra=None):
        body = {
            'proposed_by': proposed_by,
            'title': title,
            'text': text_html,
            'transaction_hash': transaction_hash,
            'envelope_xdr': envelope_xdr,
            'discord_username': 'proposer',
        }
        body.update(extra or {})
        return self.post_create(body, path=path)

    def _confirmed_proposal(self, *, title='Owner proposal', text_html=VICTIM_TEXT,
                            proposed_by=VICTIM):
        """A proposal the owner really created and really paid for."""
        envelope_xdr, transaction_hash = self.burn(
            source=proposed_by,
            amount=CREATE_COST,
            memo_bytes=legacy_memo_digest(text_html),
        )
        response = self._post_create(
            proposed_by=proposed_by,
            title=title,
            text_html=text_html,
            envelope_xdr=envelope_xdr,
            transaction_hash=transaction_hash,
        )
        self.assertEqual(response.status_code, 201, response.data)

        proposal = Proposal.objects.get(id=response.data['id'])
        confirmation = self.confirm(proposal)
        self.assertEqual(confirmation.status_code, 200, confirmation.data)

        proposal.refresh_from_db()
        self.assertEqual(proposal.payment_status, Proposal.FINE)
        self.assertEqual(proposal.action, Proposal.NONE)
        self.assertFalse(proposal.draft)
        return proposal

    # -- assertions -------------------------------------------------------

    def assertNotConsumed(self, transaction_hash):
        self.assertFalse(
            ConsumedTransaction.objects.filter(transaction_hash=transaction_hash.lower()).exists(),
            'the payment was burned by a transition that must not have happened',
        )

    def assertConsumedBy(self, transaction_hash, proposal, purpose):
        claim = ConsumedTransaction.objects.get(transaction_hash=transaction_hash.lower())
        self.assertEqual(claim.proposal_id, proposal.id)
        self.assertEqual(claim.purpose, purpose)


class CreatePaymentBindingAttackTests(AuthorizationAttackTestCase):
    """A creation is attributed to whoever paid for it, not to whoever typed the request."""

    def test_a_create_cannot_be_confirmed_for_an_account_that_did_not_pay(self):
        envelope_xdr, transaction_hash = self.burn(
            source=VICTIM,
            amount=CREATE_COST,
            memo_bytes=legacy_memo_digest(ATTACKER_TEXT),
            op_source=ATTACKER,
        )
        staged = self._post_create(
            proposed_by=VICTIM,
            title='Proposal attributed to someone else',
            text_html=ATTACKER_TEXT,
            envelope_xdr=envelope_xdr,
            transaction_hash=transaction_hash,
        )
        self.assertEqual(staged.status_code, 201, staged.data)
        proposal = Proposal.objects.get(id=staged.data['id'])

        with self.assertLogs(PAYMENTS_LOGGER, level='WARNING') as logs:
            confirmation = self.confirm(proposal)

        self.assertEqual(confirmation.status_code, 200, confirmation.data)
        self.assertEqual(
            [record.reason for record in logs.records if hasattr(record, 'reason')],
            ['payer_mismatch'],
        )

        proposal.refresh_from_db()
        self.assertEqual(proposal.payment_status, Proposal.INVALID_PAYMENT)
        self.assertEqual(proposal.action, Proposal.NONE)
        self.assertTrue(proposal.hide)
        self.assertNotConsumed(transaction_hash)

    def test_the_asset_create_path_binds_the_payer_too(self):
        """The asset path promotes through a separately atomic function and is easy to miss."""
        envelope_xdr, transaction_hash = self.burn(
            source=VICTIM,
            amount=CREATE_COST,
            memo_bytes=legacy_memo_digest(ATTACKER_TEXT),
            op_source=ATTACKER,
        )
        staged = self._post_create(
            proposed_by=VICTIM,
            title='Asset proposal attributed to someone else',
            text_html=ATTACKER_TEXT,
            envelope_xdr=envelope_xdr,
            transaction_hash=transaction_hash,
            path='/api/asset-proposal/',
            extra=dict(
                {
                    'proposal_type': Proposal.PROPOSAL_TYPE_ADD_ASSET,
                    'asset_code': DEFAULT_CODE,
                    'asset_issuer': DEFAULT_ISSUER,
                },
                **asset_narratives(),
            ),
        )
        self.assertEqual(staged.status_code, 201, staged.data)
        proposal = Proposal.objects.get(id=staged.data['id'])

        confirmation = self.confirm(proposal)

        self.assertEqual(confirmation.status_code, 200, confirmation.data)
        proposal.refresh_from_db()
        self.assertEqual(proposal.payment_status, Proposal.INVALID_PAYMENT)
        self.assertTrue(proposal.hide)
        self.assertNotConsumed(transaction_hash)

    def test_a_creation_payment_cannot_be_spent_a_second_time(self):
        proposal = self._confirmed_proposal(title='Original proposal')
        self.assertConsumedBy(proposal.transaction_hash, proposal, ConsumedTransaction.PURPOSE_CREATE)

        envelope_xdr, _unused = self.burn(
            source=ATTACKER,
            amount=CREATE_COST,
            memo_bytes=legacy_memo_digest(ATTACKER_TEXT),
            settle=False,
        )
        replay = self._post_create(
            proposed_by=ATTACKER,
            title='Free proposal',
            text_html=ATTACKER_TEXT,
            envelope_xdr=envelope_xdr,
            transaction_hash=proposal.transaction_hash,
        )

        self.assertEqual(replay.status_code, 400)
        self.assertIn('transaction_hash', replay.data)
        self.assertEqual(ConsumedTransaction.objects.count(), 1)

    def test_a_superseded_creation_payment_cannot_be_staged_again(self):
        """The creation hash moves into ``HistoryProposal`` on update, freeing the column.

        The ledger keeps it spent anyway, which is the whole reason the ledger is a table of
        its own rather than a uniqueness constraint on a column.
        """
        proposal = self._updated_proposal()
        creation_hash = proposal.history_proposal.get().transaction_hash

        self.assertFalse(
            Proposal.objects.filter(transaction_hash=creation_hash).exists(),
            'the column is free again, so only the ledger can refuse the replay',
        )

        attacker_proposal = make_general_proposal(
            proposed_by=ATTACKER,
            title='Attacker proposal',
            text=quill(ATTACKER_TEXT),
            transaction_hash=distinct_hash(800),
        )
        envelope_xdr, _unused = self.burn(
            source=ATTACKER, amount=CREATE_COST,
            memo_bytes=legacy_memo_digest(ATTACKER_TEXT), settle=False,
        )
        replay = self.patch_update(attacker_proposal, {
            'new_title': 'Attacker proposal',
            'new_text': ATTACKER_TEXT,
            'new_transaction_hash': creation_hash,
            'new_envelope_xdr': envelope_xdr,
        })

        self.assertEqual(replay.status_code, 400)
        self.assertIn('new_transaction_hash', replay.data)

    def test_the_ledger_refuses_a_superseded_creation_payment_that_reaches_promotion(self):
        """A row staged before the pre-check existed still cannot spend a burned payment.

        The staging pre-check is a read on an unauthenticated endpoint, so it can only ever
        be a courtesy; the claim taken inside the promotion's own transaction is the control.
        """
        proposal = self._updated_proposal()
        creation_hash = proposal.history_proposal.get().transaction_hash

        attacker_proposal = make_general_proposal(
            proposed_by=ATTACKER,
            title='Attacker proposal',
            text=quill(ATTACKER_TEXT),
            transaction_hash=distinct_hash(810),
            action=Proposal.TO_UPDATE,
            new_title='Attacker proposal',
            new_text=quill(ATTACKER_TEXT),
            new_transaction_hash=creation_hash,
            new_envelope_xdr='attacker-xdr',
        )

        confirmation = self.confirm(attacker_proposal)

        self.assertEqual(confirmation.status_code, 200, confirmation.data)
        attacker_proposal.refresh_from_db()
        self.assertEqual(attacker_proposal.payment_status, Proposal.INVALID_PAYMENT)
        self.assertEqual(attacker_proposal.title, 'Attacker proposal')
        self.assertEqual(
            ConsumedTransaction.objects.filter(transaction_hash=creation_hash).count(), 1)

    def _updated_proposal(self):
        """An owner proposal that has been created, paid for, updated and paid for again."""
        proposal = self._confirmed_proposal(title='Original proposal')
        revised_text = '<p>The owner revised it.</p>'
        envelope_xdr, transaction_hash = self.burn(
            source=VICTIM,
            amount=CREATE_COST,
            memo_bytes=legacy_memo_digest(revised_text),
        )
        staged = self.patch_update(proposal, {
            'new_title': 'Revised proposal',
            'new_text': revised_text,
            'new_transaction_hash': transaction_hash,
            'new_envelope_xdr': envelope_xdr,
        })
        self.assertEqual(staged.status_code, 200, staged.data)

        confirmation = self.confirm(proposal)
        self.assertEqual(confirmation.status_code, 200, confirmation.data)

        proposal.refresh_from_db()
        self.assertEqual(proposal.title, 'Revised proposal')
        return proposal


class UpdateAuthorizationAttackTests(AuthorizationAttackTestCase):
    """An unauthenticated caller can still stage; it buys them nothing."""

    def test_an_unsigned_envelope_naming_the_victim_grants_nothing(self):
        """The envelope's ``source`` is a claim in the request body, not a credential.

        The attacker pays for real, from their own account, and dresses the payment in an
        envelope that names the owner as the transaction source - which is precisely what
        the old declared-owner check accepted.  The payment operation says who paid.
        """
        proposal = self._confirmed_proposal()
        envelope_xdr, transaction_hash = self.burn(
            source=VICTIM,
            amount=CREATE_COST,
            memo_bytes=legacy_memo_digest(ATTACKER_TEXT),
            op_source=ATTACKER,
        )

        staged = self.patch_update(proposal, {
            'new_title': 'Substituted title',
            'new_text': ATTACKER_TEXT,
            'new_transaction_hash': transaction_hash,
            'new_envelope_xdr': envelope_xdr,
        })
        self.assertEqual(staged.status_code, 200, staged.data)

        with self.assertLogs(PAYMENTS_LOGGER, level='WARNING') as logs:
            confirmation = self.confirm(proposal)

        self.assertEqual(confirmation.status_code, 200, confirmation.data)
        self.assertIn('payer_mismatch', [getattr(record, 'reason', None) for record in logs.records])

        proposal.refresh_from_db()
        self.assertEqual(proposal.title, 'Owner proposal')
        self.assertEqual(proposal.text.html, VICTIM_TEXT)
        self.assertEqual(proposal.payment_status, Proposal.INVALID_PAYMENT)
        self.assertFalse(proposal.history_proposal.exists())
        self.assertNotConsumed(transaction_hash)

    def test_a_signed_envelope_replayed_from_the_owners_own_history_grants_nothing(self):
        """The executable form of "signature verification would not fix this".

        Horizon serves the signed envelope of any settled transaction, so a valid signature
        by the owner proves only that the owner once paid for something - never that they
        asked for the transition in front of us.  What refuses the replay is that the
        payment it names has already been spent.
        """
        proposal = self._confirmed_proposal()
        replayed_envelope = self._signed(proposal.envelope_xdr, OWNER_KEYPAIR)

        decoded = TransactionEnvelope.from_xdr(replayed_envelope, settings.NETWORK_PASSPHRASE)
        self.assertEqual(decoded.transaction.source.account_id, VICTIM)
        self.assertEqual(len(decoded.signatures), 1)

        replay = self.patch_update(proposal, {
            'new_title': 'Substituted title',
            'new_text': VICTIM_TEXT,
            'new_transaction_hash': proposal.transaction_hash,
            'new_envelope_xdr': replayed_envelope,
        })

        self.assertEqual(replay.status_code, 400)
        self.assertIn('new_transaction_hash', replay.data)
        proposal.refresh_from_db()
        self.assertEqual(proposal.title, 'Owner proposal')
        self.assertEqual(proposal.action, Proposal.NONE)

    def test_title_substitution_with_a_consumed_payment_is_rejected(self):
        """Rewrite the title, leave the text alone, pay with the owner's own create payment.

        The legacy memo commits to the text only, so this is the shape that costs nothing.
        Single use is what closes it in v1.
        """
        proposal = self._confirmed_proposal()
        envelope_xdr, _unused = self.burn(
            source=VICTIM, amount=CREATE_COST,
            memo_bytes=legacy_memo_digest(VICTIM_TEXT), settle=False,
        )

        substitution = self.patch_update(proposal, {
            'new_title': 'Substituted title',
            'new_text': VICTIM_TEXT,
            'new_transaction_hash': proposal.transaction_hash,
            'new_envelope_xdr': envelope_xdr,
        })

        self.assertEqual(substitution.status_code, 400)
        proposal.refresh_from_db()
        self.assertEqual(proposal.title, 'Owner proposal')
        self.assertEqual(proposal.action, Proposal.NONE)

    def test_a_proposal_cannot_be_made_to_pay_itself_with_its_own_confirmed_hash(self):
        """The staged pre-check spans the target row too, not just the other ones.

        The row's own ``transaction_hash`` is a payment that has already been spent on this
        proposal's creation.  A row created through the admin never had its hash written to
        the ledger, so excluding the target from the lookup would leave a free rewrite of
        any admin-created proposal for whoever reads its text back off the API.
        """
        proposal = make_general_proposal(
            proposed_by=VICTIM,
            title='Admin-created proposal',
            text=quill(VICTIM_TEXT),
            transaction_hash=distinct_hash(830),
        )
        self.assertFalse(ConsumedTransaction.objects.filter(
            transaction_hash=proposal.transaction_hash).exists())

        substitution = self.patch_update(proposal, {
            'new_title': 'Attacker title',
            'new_text': VICTIM_TEXT,
            'new_transaction_hash': proposal.transaction_hash,
            'new_envelope_xdr': 'attacker-xdr',
        })

        self.assertEqual(substitution.status_code, 400)
        self.assertIn('new_transaction_hash', substitution.data)
        proposal.refresh_from_db()
        self.assertEqual(proposal.action, Proposal.NONE)
        self.assertIsNone(proposal.new_title)

    def test_the_same_substitution_is_refused_again_at_promotion(self):
        """Staged behind the serializer, so only the promotion-time claim can refuse it."""
        proposal = self._confirmed_proposal()
        Proposal.objects.filter(id=proposal.id).update(
            action=Proposal.TO_UPDATE,
            new_title='Substituted title',
            new_text=quill(VICTIM_TEXT),
            new_transaction_hash=proposal.transaction_hash,
            new_envelope_xdr='attacker-xdr',
        )
        proposal.refresh_from_db()

        confirmation = self.confirm(proposal)

        self.assertEqual(confirmation.status_code, 200, confirmation.data)
        proposal.refresh_from_db()
        self.assertEqual(proposal.title, 'Owner proposal')
        self.assertEqual(proposal.payment_status, Proposal.INVALID_PAYMENT)
        self.assertEqual(ConsumedTransaction.objects.count(), 1)

    def test_staging_stays_open_and_the_gate_is_promotion(self):
        """Stated positively so nothing later adds false security at the staging layer.

        Staging writes the ``new_*`` columns and nothing else; no read serializer exposes
        them, and only a payment that Horizon attests can move them into the public columns.
        """
        proposal = self._confirmed_proposal()
        envelope_xdr, _unused = self.burn(
            source=VICTIM, amount=CREATE_COST,
            memo_bytes=legacy_memo_digest(ATTACKER_TEXT), settle=False,
        )

        staged = self.patch_update(proposal, {
            'new_title': 'Substituted title',
            'new_text': ATTACKER_TEXT,
            'new_transaction_hash': distinct_hash(820),
            'new_envelope_xdr': envelope_xdr,
        })
        self.assertEqual(staged.status_code, 200, staged.data)

        proposal.refresh_from_db()
        self.assertEqual(proposal.action, Proposal.TO_UPDATE)
        self.assertEqual(proposal.new_title, 'Substituted title')

        detail = self.client.get('/api/proposal/{0}/'.format(proposal.id))
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.data['title'], 'Owner proposal')
        self.assertNotIn('new_title', detail.data)
        self.assertNotIn('new_text', detail.data)

        confirmation = self.confirm(proposal)

        self.assertEqual(confirmation.status_code, 200, confirmation.data)
        proposal.refresh_from_db()
        self.assertEqual(proposal.title, 'Owner proposal')
        self.assertEqual(proposal.text.html, VICTIM_TEXT)
        self.assertEqual(proposal.payment_status, Proposal.HORIZON_ERROR)
        self.assertFalse(ConsumedTransaction.objects.exclude(
            purpose=ConsumedTransaction.PURPOSE_CREATE).exists())

    def test_an_update_cannot_overwrite_the_published_text(self):
        proposal = self._confirmed_proposal()
        envelope_xdr, transaction_hash = self.burn(
            source=VICTIM, amount=CREATE_COST,
            memo_bytes=legacy_memo_digest(ATTACKER_TEXT), settle=False,
        )

        response = self.patch_update(proposal, {
            'text': '<p>Rewritten in place.</p>',
            'new_title': 'Staged title',
            'new_text': ATTACKER_TEXT,
            'new_transaction_hash': transaction_hash,
            'new_envelope_xdr': envelope_xdr,
        })

        self.assertEqual(response.status_code, 200, response.data)
        proposal.refresh_from_db()
        self.assertEqual(proposal.text.html, VICTIM_TEXT)

    def test_an_update_cannot_overwrite_the_discord_username(self):
        proposal = self._confirmed_proposal()
        envelope_xdr, transaction_hash = self.burn(
            source=VICTIM, amount=CREATE_COST,
            memo_bytes=legacy_memo_digest(ATTACKER_TEXT), settle=False,
        )

        response = self.patch_update(proposal, {
            'discord_username': 'attacker',
            'new_title': 'Staged title',
            'new_text': ATTACKER_TEXT,
            'new_transaction_hash': transaction_hash,
            'new_envelope_xdr': envelope_xdr,
        })

        self.assertEqual(response.status_code, 200, response.data)
        proposal.refresh_from_db()
        self.assertEqual(proposal.discord_username, 'proposer')

    def test_an_update_cannot_write_the_confirmed_transaction_hash_or_envelope(self):
        proposal = self._confirmed_proposal()
        confirmed_hash = proposal.transaction_hash
        confirmed_envelope = Proposal.objects.get(id=proposal.id).envelope_xdr
        envelope_xdr, transaction_hash = self.burn(
            source=VICTIM, amount=CREATE_COST,
            memo_bytes=legacy_memo_digest(ATTACKER_TEXT), settle=False,
        )

        response = self.patch_update(proposal, {
            'transaction_hash': distinct_hash(830),
            'envelope_xdr': 'attacker-envelope',
            'new_title': 'Staged title',
            'new_text': ATTACKER_TEXT,
            'new_transaction_hash': transaction_hash,
            'new_envelope_xdr': envelope_xdr,
        })

        self.assertEqual(response.status_code, 200, response.data)
        proposal.refresh_from_db()
        self.assertEqual(proposal.transaction_hash, confirmed_hash)
        self.assertEqual(proposal.envelope_xdr, confirmed_envelope)

    def test_a_pending_payment_cannot_be_squatted_onto_another_proposal(self):
        """The fund-loss variant of the write that used to be allowed.

        Parking the owner's pending hash in another row's confirmed-hash column used to make
        the owner's own promotion raise on the unique index, after they had already paid.
        """
        proposal = self._confirmed_proposal()
        revised_text = '<p>The owner revised it.</p>'
        owner_envelope, owner_hash = self.burn(
            source=VICTIM, amount=CREATE_COST, memo_bytes=legacy_memo_digest(revised_text))
        staged = self.patch_update(proposal, {
            'new_title': 'Revised proposal',
            'new_text': revised_text,
            'new_transaction_hash': owner_hash,
            'new_envelope_xdr': owner_envelope,
        })
        self.assertEqual(staged.status_code, 200, staged.data)

        squat_envelope, _unused = self.burn(
            source=ATTACKER, amount=CREATE_COST,
            memo_bytes=legacy_memo_digest(ATTACKER_TEXT), settle=False,
        )
        squat = self._post_create(
            proposed_by=ATTACKER,
            title='Squatting proposal',
            text_html=ATTACKER_TEXT,
            envelope_xdr=squat_envelope,
            transaction_hash=owner_hash,
        )
        self.assertEqual(squat.status_code, 400)
        self.assertIn('transaction_hash', squat.data)

        confirmation = self.confirm(proposal)

        self.assertEqual(confirmation.status_code, 200, confirmation.data)
        proposal.refresh_from_db()
        self.assertEqual(proposal.title, 'Revised proposal')
        self.assertConsumedBy(owner_hash, proposal, ConsumedTransaction.PURPOSE_UPDATE)


class SubmitAuthorizationAttackTests(AuthorizationAttackTestCase):
    """Publication costs 900 000 AQUA, so the submit payment is the one worth stealing."""

    def _staged_submit(self, proposal, *, weeks_ahead=1, settle=True):
        start_at, end_at = self.week(weeks_ahead)
        envelope_xdr, transaction_hash = self.burn(
            source=proposal.proposed_by,
            amount=SUBMIT_COST,
            memo_bytes=legacy_memo_digest(proposal.text.html),
            settle=settle,
        )
        response = self.post_submit(self.open_for_submit(proposal), {
            'start_at': utc_second_iso(start_at),
            'end_at': utc_second_iso(end_at),
            'new_transaction_hash': transaction_hash,
            'new_envelope_xdr': envelope_xdr,
        })
        self.assertEqual(response.status_code, 200, response.data)
        proposal.refresh_from_db()
        return transaction_hash

    def test_a_confirmed_submit_payment_cannot_be_replayed_on_another_proposal(self):
        proposal = self._confirmed_proposal()
        submit_hash = self._staged_submit(proposal)
        confirmation = self.confirm(proposal)
        self.assertEqual(confirmation.status_code, 200, confirmation.data)
        proposal.refresh_from_db()
        self.assertEqual(proposal.proposal_status, Proposal.QUEUED)
        self.assertConsumedBy(submit_hash, proposal, ConsumedTransaction.PURPOSE_SUBMIT)

        attacker_proposal = self.open_for_submit(make_general_proposal(
            proposed_by=ATTACKER,
            title='Attacker proposal',
            text=quill(ATTACKER_TEXT),
            transaction_hash=distinct_hash(840),
        ))
        start_at, end_at = self.week(weeks_ahead=2)
        envelope_xdr, _unused = self.burn(
            source=ATTACKER, amount=SUBMIT_COST,
            memo_bytes=legacy_memo_digest(ATTACKER_TEXT), settle=False,
        )
        replay = self.post_submit(attacker_proposal, {
            'start_at': utc_second_iso(start_at),
            'end_at': utc_second_iso(end_at),
            'new_transaction_hash': submit_hash,
            'new_envelope_xdr': envelope_xdr,
        })

        self.assertEqual(replay.status_code, 400)
        self.assertIn('new_transaction_hash', replay.data)

    def test_the_replayed_submit_payment_is_refused_again_at_promotion(self):
        proposal = self._confirmed_proposal()
        submit_hash = self._staged_submit(proposal)
        self.assertEqual(self.confirm(proposal).status_code, 200)

        start_at, end_at = self.week(weeks_ahead=2)
        attacker_proposal = make_general_proposal(
            proposed_by=ATTACKER,
            title='Attacker proposal',
            text=quill(ATTACKER_TEXT),
            transaction_hash=distinct_hash(850),
            action=Proposal.TO_SUBMIT,
            new_start_at=start_at,
            new_end_at=end_at,
            new_transaction_hash=submit_hash,
            new_envelope_xdr='attacker-xdr',
        )

        confirmation = self.confirm(attacker_proposal)

        self.assertEqual(confirmation.status_code, 200, confirmation.data)
        attacker_proposal.refresh_from_db()
        self.assertEqual(attacker_proposal.payment_status, Proposal.INVALID_PAYMENT)
        self.assertEqual(attacker_proposal.proposal_status, Proposal.DISCUSSION)
        self.assertFalse(ProposalQueueSlot.objects.filter(proposal=attacker_proposal).exists())
        self.assertEqual(
            ConsumedTransaction.objects.filter(transaction_hash=submit_hash).count(), 1)

    def test_a_forged_create_cannot_squat_a_pending_submit_payment(self):
        """Exact-amount matching is what keeps a 900 000 payment from settling a 100 000 debt.

        The forged creation copies the owner's text, so under the legacy memo the payment's
        memo matches; the owner really is the payer, so the payer binding matches too.  Only
        the amount separates the two obligations while the legacy memo is still accepted.
        """
        proposal = self._confirmed_proposal()
        submit_hash = self._staged_submit(proposal)

        squat_envelope, _unused = self.burn(
            source=VICTIM, amount=CREATE_COST,
            memo_bytes=legacy_memo_digest(VICTIM_TEXT), settle=False,
        )
        squat = self._post_create(
            proposed_by=VICTIM,
            title='Forged creation',
            text_html=VICTIM_TEXT,
            envelope_xdr=squat_envelope,
            transaction_hash=submit_hash,
        )
        self.assertEqual(squat.status_code, 400)

        forged = make_general_proposal(
            proposed_by=VICTIM,
            title='Forged creation',
            text=quill(VICTIM_TEXT),
            draft=True,
            action=Proposal.TO_CREATE,
            transaction_hash=submit_hash,
        )
        with self.assertLogs(PAYMENTS_LOGGER, level='WARNING') as logs:
            forged_confirmation = self.confirm(forged)

        self.assertEqual(forged_confirmation.status_code, 200, forged_confirmation.data)
        self.assertIn('no_matching_payment', [getattr(r, 'reason', None) for r in logs.records])
        forged.refresh_from_db()
        self.assertEqual(forged.payment_status, Proposal.INVALID_PAYMENT)
        self.assertTrue(forged.hide)
        self.assertNotConsumed(submit_hash)

        # The forged row only exists because it was planted behind the staging pre-check,
        # and a row parking the hash in the unique column is exactly the state that
        # pre-check refuses to create.  Drop it, then let the owner spend their own payment.
        Proposal.objects.filter(id=forged.id).delete()
        confirmation = self.confirm(proposal)

        self.assertEqual(confirmation.status_code, 200, confirmation.data)
        proposal.refresh_from_db()
        self.assertEqual(proposal.proposal_status, Proposal.QUEUED)
        self.assertConsumedBy(submit_hash, proposal, ConsumedTransaction.PURPOSE_SUBMIT)


class UnhashableTextAttackTests(AuthorizationAttackTestCase):
    """A proposal text is hashed on every sweep tick, ahead of the Horizon call."""

    def test_a_surrogate_in_the_staged_text_is_a_400_not_a_500(self):
        proposal = self._confirmed_proposal()

        response = self.client.patch(
            '/api/proposal/{0}/'.format(proposal.id),
            json.dumps({
                'new_title': 'Staged title',
                'new_text': '<p>\ud800</p>',
                'new_transaction_hash': distinct_hash(860),
                'new_envelope_xdr': 'staged-xdr',
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('new_text', response.data)

    def test_a_non_string_staged_text_is_a_400_not_a_500(self):
        proposal = self._confirmed_proposal()

        response = self.patch_update(proposal, {
            'new_title': 'Staged title',
            'new_text': {'html': '<p>x</p>'},
            'new_transaction_hash': distinct_hash(870),
            'new_envelope_xdr': 'staged-xdr',
        })

        self.assertEqual(response.status_code, 400)
        self.assertIn('new_text', response.data)

    def test_a_row_whose_text_cannot_be_hashed_does_not_stop_the_sweep(self):
        """A row planted before the input guard existed is retired, not raised on.

        The memo builders are total, so an unrepresentable field degrades that expectation to
        "nothing is acceptable" and the row is rejected on its own account; the rows queued
        behind it are still swept.
        """
        poison_text = '<p>\ud800</p>'
        poisoned_hash = self.burn(
            source=VICTIM, amount=CREATE_COST, memo_bytes=legacy_memo_digest(VICTIM_TEXT))[1]
        poisoned = make_general_proposal(
            proposed_by=VICTIM,
            title='Poisoned row',
            text=quill(VICTIM_TEXT),
            transaction_hash=distinct_hash(880),
            action=Proposal.TO_UPDATE,
            new_title='Poisoned title',
            new_text=quill(poison_text),
            new_transaction_hash=poisoned_hash,
            new_envelope_xdr='poisoned-xdr',
        )

        healthy_text = '<p>A healthy revision.</p>'
        healthy_envelope, healthy_hash = self.burn(
            source=VICTIM, amount=CREATE_COST, memo_bytes=legacy_memo_digest(healthy_text))
        healthy = make_general_proposal(
            proposed_by=VICTIM,
            title='Healthy row',
            text=quill(VICTIM_TEXT),
            transaction_hash=distinct_hash(890),
            action=Proposal.TO_UPDATE,
            new_title='Healthy revision',
            new_text=quill(healthy_text),
            new_transaction_hash=healthy_hash,
            new_envelope_xdr=healthy_envelope,
        )
        self.assertLess(poisoned.id, healthy.id)

        tasks.task_check_pending_proposal_payments()

        poisoned.refresh_from_db()
        healthy.refresh_from_db()
        self.assertEqual(poisoned.payment_status, Proposal.BAD_MEMO)
        self.assertEqual(poisoned.title, 'Poisoned row')
        self.assertEqual(healthy.title, 'Healthy revision')
        self.assertConsumedBy(healthy_hash, healthy, ConsumedTransaction.PURPOSE_UPDATE)
        self.assertNotConsumed(poisoned_hash)
