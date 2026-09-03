"""The backward-compatibility contract with the shipped frontends, as executable assertions.

Every request body below is the one a currently deployed, unmodified client sends, copied
key for key, with the call site named in the test's docstring.  The payment checks this
change introduces are all satisfied by those clients by construction - the payer is the
connected account, the amount is a constant matching the backend's, and the legacy memo is
still accepted - so the contract is that nothing in the flow moves.

Two clauses are pinned here and nowhere else: ``POST /api/proposals/`` now answers 405, and
``POST /check_payment/`` reports the verdict the call actually computed rather than whatever
is persisted on the row, so a poll cannot read success off a transition that never happened.
"""
from unittest.mock import patch

from django.conf import settings
from django.test import override_settings

from stellar_sdk import TransactionEnvelope

from aqua_governance.governance import payment_statuses
from aqua_governance.governance.models import ConsumedTransaction, Proposal
from aqua_governance.governance.tests._chain import (
    CREATE_COST,
    OTHER_ACCOUNT,
    OWNER,
    SUBMIT_COST,
    OnChainTestCase,
    utc_second_iso,
)
from aqua_governance.governance.tests._factories import DEFAULT_CODE, DEFAULT_ISSUER, asset_narratives
from aqua_governance.utils.memo import legacy_memo_digest


PROPOSAL_TEXT = '<p>A proposal written in the browser.</p>'
REVISED_TEXT = '<p>A proposal revised in the browser.</p>'

CHECK_STATUS = 'aqua_governance.governance.proposal_transactions.check_proposal_status'

# Response shapes, frozen.  A key added or removed on any of these is a client-visible
# change even when every value is unchanged, so the sets are spelled out rather than derived
# from the serializers they are meant to pin.
CREATE_RESPONSE_KEYS = {
    'created_at', 'discord_channel_name', 'discord_channel_url', 'discord_username', 'draft',
    'end_at', 'envelope_xdr', 'id', 'last_updated_at', 'onchain_action_args',
    'onchain_action_type', 'onchain_execution_poll_count', 'onchain_execution_started_at',
    'onchain_execution_status', 'onchain_execution_submitted_at', 'onchain_execution_tx_hash',
    'payment_status', 'payment_verification_status', 'proposal_status', 'proposal_type',
    'proposed_by', 'start_at', 'text', 'title', 'transaction_hash',
}

ASSET_CREATE_RESPONSE_KEYS = CREATE_RESPONSE_KEYS | {
    'asset_aquarius_traction', 'asset_audit_info', 'asset_code', 'asset_community_references',
    'asset_contract_address', 'asset_holder_distribution', 'asset_issuer',
    'asset_issuer_commitments', 'asset_issuer_information', 'asset_liquidity',
    'asset_related_projects', 'asset_stellar_flags', 'asset_token_description',
    'asset_trading_volume',
}

# `transaction_hash` and `envelope_xdr` are deliberately absent from the update shape and
# `envelope_xdr` from the submit shape: staging is unauthenticated, so echoing the victim's
# confirmed hash and archived envelope back to any caller was the reconnaissance step for
# the squat the staging pre-check now refuses.  Neither frontend reads either field.
UPDATE_RESPONSE_KEYS = {
    'created_at', 'discord_channel_name', 'discord_channel_url', 'discord_username', 'end_at',
    'id', 'last_updated_at', 'new_envelope_xdr', 'new_text', 'new_title',
    'new_transaction_hash', 'onchain_action_args', 'onchain_action_type',
    'onchain_execution_poll_count', 'onchain_execution_started_at', 'onchain_execution_status',
    'onchain_execution_submitted_at', 'onchain_execution_tx_hash', 'payment_status',
    'payment_verification_status', 'proposal_status', 'proposal_type', 'proposed_by',
    'start_at', 'text', 'title', 'version',
}

SUBMIT_RESPONSE_KEYS = {
    'asset_aquarius_traction', 'asset_audit_info', 'asset_code', 'asset_community_references',
    'asset_contract_address', 'asset_holder_distribution', 'asset_issuer',
    'asset_issuer_commitments', 'asset_issuer_information', 'asset_liquidity',
    'asset_related_projects', 'asset_stellar_flags', 'asset_token_description',
    'asset_trading_volume', 'created_at', 'discord_channel_name', 'discord_channel_url',
    'discord_username', 'end_at', 'id', 'last_updated_at', 'new_envelope_xdr',
    'new_transaction_hash', 'onchain_action_args', 'onchain_action_type',
    'onchain_execution_poll_count', 'onchain_execution_started_at', 'onchain_execution_status',
    'onchain_execution_submitted_at', 'onchain_execution_tx_hash', 'payment_status',
    'payment_verification_status', 'proposal_status', 'proposal_type', 'proposed_by',
    'start_at', 'text', 'title',
}

CHECK_PAYMENT_RESPONSE_KEYS = {
    'abstain_issuer', 'aqua_circulating_supply', 'asset_aquarius_traction', 'asset_audit_info',
    'asset_code', 'asset_community_references', 'asset_contract_address',
    'asset_holder_distribution', 'asset_issuer', 'asset_issuer_commitments',
    'asset_issuer_information', 'asset_liquidity', 'asset_related_projects',
    'asset_stellar_flags', 'asset_token_description', 'asset_trading_volume', 'created_at',
    'discord_channel_name', 'discord_channel_url', 'discord_username', 'end_at',
    'history_proposal', 'ice_circulating_supply', 'id', 'is_simple_proposal', 'last_updated_at',
    'onchain_action_args', 'onchain_action_type', 'onchain_execution_poll_count',
    'onchain_execution_started_at', 'onchain_execution_status',
    'onchain_execution_submitted_at', 'onchain_execution_tx_hash', 'payment_status',
    'payment_verification_status', 'percent_for_quorum', 'proposal_status', 'proposal_type',
    'proposed_by', 'start_at', 'text', 'title', 'version', 'vote_abstain_result',
    'vote_against_issuer', 'vote_against_result', 'vote_for_issuer', 'vote_for_result',
}

KNOWN_PAYMENT_STATUSES = {
    payment_statuses.FINE,
    payment_statuses.HORIZON_ERROR,
    payment_statuses.BAD_MEMO,
    payment_statuses.INVALID_PAYMENT,
    payment_statuses.FAILED_TRANSACTION,
}


@override_settings(DEBUG=False)
class FrontendCompatContractTests(OnChainTestCase):
    def setUp(self):
        super().setUp()
        self.observed_payment_statuses = set()

    # -- the client's own payment ----------------------------------------

    def _burn(self, *, amount, text_html, source=OWNER):
        """The burn both clients build: one source-less payment operation, memo over the text."""
        return self.burn(amount=amount, memo_bytes=legacy_memo_digest(text_html), source=source)

    def _record(self, response):
        payment_status = response.data.get('payment_status') if hasattr(response, 'data') else None
        if payment_status is not None:
            self.observed_payment_statuses.add(payment_status)
        return response

    # -- the verbatim client bodies --------------------------------------

    def _v2_create_body(self, transaction_hash, envelope_xdr, *, title='Browser proposal'):
        """``aquarius-frontend`` CreateDiscussionModal.tsx:122-131 via governance.ts:150."""
        return {
            'proposed_by': OWNER,
            'title': title,
            'text': PROPOSAL_TEXT,
            'start_at': None,
            'end_at': None,
            'transaction_hash': transaction_hash,
            'discord_username': 'browser-user',
            'envelope_xdr': envelope_xdr,
        }

    def _v2_edit_body(self, transaction_hash, envelope_xdr):
        """``aquarius-frontend`` CreateDiscussionModal.tsx:113-121 via governance.ts:153."""
        return {
            'new_title': 'Browser proposal, revised',
            'new_text': REVISED_TEXT,
            'new_transaction_hash': transaction_hash,
            'new_envelope_xdr': envelope_xdr,
        }

    def _v2_publish_body(self, transaction_hash, envelope_xdr, start_at, end_at):
        """``aquarius-frontend`` PublishProposalModal.tsx:194-202 via governance.ts:166."""
        return {
            'start_at': utc_second_iso(start_at),
            'end_at': utc_second_iso(end_at),
            'new_transaction_hash': transaction_hash,
            'new_envelope_xdr': envelope_xdr,
        }

    def _v2_asset_create_body(self, transaction_hash, envelope_xdr):
        """``aquarius-frontend`` CreateAssetProposalModal.tsx:127-142 via asset-registry.ts:22.

        A classic pair is sent as code plus issuer with ``asset_contract_address: null``; the
        backend derives the address after insert, which is why no asset value can appear in
        the memo preimage.
        """
        return dict(
            {
                'proposed_by': OWNER,
                'title': 'Browser asset proposal',
                'text': PROPOSAL_TEXT,
                'start_at': None,
                'end_at': None,
                'transaction_hash': transaction_hash,
                'discord_username': 'browser-user',
                'envelope_xdr': envelope_xdr,
                'proposal_type': Proposal.PROPOSAL_TYPE_ADD_ASSET,
                'asset_code': DEFAULT_CODE,
                'asset_issuer': DEFAULT_ISSUER,
                'asset_contract_address': None,
            },
            **asset_narratives(),
        )

    # -- flows ------------------------------------------------------------

    def _created_proposal(self, title='Browser proposal'):
        envelope_xdr, transaction_hash = self._burn(amount=CREATE_COST, text_html=PROPOSAL_TEXT)
        response = self._record(self.client.post(
            '/api/proposal/', self._v2_create_body(transaction_hash, envelope_xdr, title=title),
            format='json'))
        self.assertEqual(response.status_code, 201, response.data)
        return Proposal.objects.get(id=response.data['id']), response

    def _confirm(self, proposal, path='/api/proposal/{0}/check_payment/'):
        """``checkProposalStatus`` posts no body at all (governance.ts:179)."""
        return self._record(self.confirm(proposal, path=path))

    # -- the create / confirm round trip ----------------------------------

    def test_the_v2_create_body_still_creates_and_still_confirms(self):
        proposal, response = self._created_proposal()

        self.assertEqual(set(response.data.keys()), CREATE_RESPONSE_KEYS)
        self.assertEqual(response.data['payment_status'], payment_statuses.FINE)
        self.assertTrue(response.data['draft'])

        confirmation = self._confirm(proposal)

        self.assertEqual(confirmation.status_code, 200, confirmation.data)
        self.assertEqual(set(confirmation.data.keys()), CHECK_PAYMENT_RESPONSE_KEYS)
        self.assertEqual(confirmation.data['payment_status'], payment_statuses.FINE)
        proposal.refresh_from_db()
        self.assertFalse(proposal.draft)
        self.assertEqual(proposal.action, Proposal.NONE)

    def test_a_source_less_payment_operation_binds_to_the_transaction_source(self):
        """Both clients' ``createBurnAquaOperation`` sets no operation source.

        The payer binding therefore has to fall back to the transaction source, or every
        honest payment fails.
        """
        envelope_xdr, transaction_hash = self._burn(amount=CREATE_COST, text_html=PROPOSAL_TEXT)

        envelope = TransactionEnvelope.from_xdr(envelope_xdr, settings.NETWORK_PASSPHRASE)
        self.assertIsNone(envelope.transaction.operations[0].source)
        self.assertEqual(envelope.transaction.source.account_id, OWNER)

        response = self._record(self.client.post(
            '/api/proposal/', self._v2_create_body(transaction_hash, envelope_xdr),
            format='json'))

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['payment_status'], payment_statuses.FINE)

    def test_the_v2_asset_create_body_still_creates_and_still_confirms(self):
        envelope_xdr, transaction_hash = self._burn(amount=CREATE_COST, text_html=PROPOSAL_TEXT)

        response = self._record(self.client.post(
            '/api/asset-proposal/', self._v2_asset_create_body(transaction_hash, envelope_xdr),
            format='json'))

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(set(response.data.keys()), ASSET_CREATE_RESPONSE_KEYS)
        self.assertEqual(response.data['payment_status'], payment_statuses.FINE)
        self.assertIsNotNone(response.data['asset_contract_address'])

        proposal = Proposal.objects.get(id=response.data['id'])
        confirmation = self._confirm(proposal)

        self.assertEqual(confirmation.status_code, 200, confirmation.data)
        self.assertEqual(confirmation.data['payment_status'], payment_statuses.FINE)
        proposal.refresh_from_db()
        self.assertFalse(proposal.draft)

    # -- the edit round trip ----------------------------------------------

    def test_the_v2_edit_body_still_stages_and_still_confirms(self):
        """The live client sends ``PATCH``; a ``PUT``-only test would miss the real path."""
        proposal, _created = self._created_proposal()
        self.assertEqual(self._confirm(proposal).status_code, 200)

        envelope_xdr, transaction_hash = self._burn(amount=CREATE_COST, text_html=REVISED_TEXT)
        staged = self._record(self.client.patch(
            '/api/proposal/{0}/'.format(proposal.id),
            self._v2_edit_body(transaction_hash, envelope_xdr),
            format='json'))

        self.assertEqual(staged.status_code, 200, staged.data)
        self.assertEqual(set(staged.data.keys()), UPDATE_RESPONSE_KEYS)
        self.assertEqual(staged.data['payment_status'], payment_statuses.FINE)

        confirmation = self._confirm(proposal)

        self.assertEqual(confirmation.status_code, 200, confirmation.data)
        self.assertEqual(confirmation.data['payment_status'], payment_statuses.FINE)
        proposal.refresh_from_db()
        self.assertEqual(proposal.title, 'Browser proposal, revised')
        self.assertEqual(proposal.text.html, REVISED_TEXT)

    # -- the publish round trip -------------------------------------------

    def test_the_v2_publish_body_still_books_the_selected_slot(self):
        proposal, _created = self._created_proposal()
        self.assertEqual(self._confirm(proposal).status_code, 200)
        self.open_for_submit(proposal)

        start_at, end_at = self.week()
        envelope_xdr, transaction_hash = self._burn(
            amount=SUBMIT_COST, text_html=proposal.text.html)
        staged = self._record(self.client.post(
            '/api/proposal/{0}/submit/'.format(proposal.id),
            self._v2_publish_body(transaction_hash, envelope_xdr, start_at, end_at),
            format='json'))

        self.assertEqual(staged.status_code, 200, staged.data)
        self.assertEqual(set(staged.data.keys()), SUBMIT_RESPONSE_KEYS)
        self.assertEqual(staged.data['payment_status'], payment_statuses.FINE)

        confirmation = self._confirm(proposal)

        self.assertEqual(confirmation.status_code, 200, confirmation.data)
        self.assertEqual(confirmation.data['payment_status'], payment_statuses.FINE)
        proposal.refresh_from_db()
        self.assertEqual(proposal.proposal_status, Proposal.QUEUED)
        self.assertEqual(proposal.start_at, start_at)
        self.assertEqual(proposal.end_at, end_at)

    # -- the legacy client -------------------------------------------------

    def test_the_dao_aquarius_bodies_are_byte_identical_and_get_the_same_verdicts(self):
        """``dao-aquarius`` governance.ts:109 / :112 / :138 send the same three shapes.

        Its publish flow is left out on purpose: it posts ``new_start_at``/``new_end_at``
        against a serializer that declares ``start_at``/``end_at``, which is a 400 that
        predates this change and is not repaired by it.
        """
        create_envelope, create_hash = self._burn(amount=CREATE_COST, text_html=PROPOSAL_TEXT)
        legacy_create_body = {
            'proposed_by': OWNER,
            'title': 'Legacy client proposal',
            'text': PROPOSAL_TEXT,
            'start_at': None,
            'end_at': None,
            'transaction_hash': create_hash,
            'discord_username': 'legacy-user',
            'envelope_xdr': create_envelope,
        }

        with self.subTest('create'):
            response = self._record(self.client.post(
                '/api/proposal/', legacy_create_body, format='json'))
            self.assertEqual(response.status_code, 201, response.data)
            self.assertEqual(set(response.data.keys()), CREATE_RESPONSE_KEYS)
            proposal = Proposal.objects.get(id=response.data['id'])
            self.assertEqual(self._confirm(proposal).data['payment_status'], payment_statuses.FINE)

        edit_envelope, edit_hash = self._burn(amount=CREATE_COST, text_html=REVISED_TEXT)
        with self.subTest('edit'):
            proposal.refresh_from_db()
            response = self._record(self.client.patch(
                '/api/proposal/{0}/'.format(proposal.id),
                {
                    'new_title': 'Legacy client proposal, revised',
                    'new_text': REVISED_TEXT,
                    'new_transaction_hash': edit_hash,
                    'new_envelope_xdr': edit_envelope,
                },
                format='json',
            ))
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(set(response.data.keys()), UPDATE_RESPONSE_KEYS)

        with self.subTest('check_payment'):
            confirmation = self._confirm(proposal)
            self.assertEqual(confirmation.status_code, 200, confirmation.data)
            self.assertEqual(set(confirmation.data.keys()), CHECK_PAYMENT_RESPONSE_KEYS)
            proposal.refresh_from_db()
            self.assertEqual(proposal.title, 'Legacy client proposal, revised')

    # -- the contract clauses ----------------------------------------------

    def test_no_flow_emits_a_payment_status_outside_the_documented_set(self):
        proposal, _created = self._created_proposal()
        self._confirm(proposal)

        envelope_xdr, transaction_hash = self._burn(
            amount=CREATE_COST, text_html=REVISED_TEXT, source=OTHER_ACCOUNT)
        self._record(self.client.patch(
            '/api/proposal/{0}/'.format(proposal.id),
            self._v2_edit_body(transaction_hash, envelope_xdr),
            format='json'))
        self._confirm(proposal)

        self.assertTrue(self.observed_payment_statuses)
        self.assertLessEqual(self.observed_payment_statuses, KNOWN_PAYMENT_STATUSES)

    def test_check_payment_does_not_report_success_for_a_transition_it_did_not_apply(self):
        """The staged copy moving under a confirmation must not read as success.

        Both clients' polling loops resolve on ``FINE`` and retry only on ``HORIZON_ERROR``,
        so reporting the persisted column here would navigate the owner away from a proposal
        that was never updated.
        """
        proposal, _created = self._created_proposal()
        self.assertEqual(self._confirm(proposal).status_code, 200)

        envelope_xdr, transaction_hash = self._burn(amount=CREATE_COST, text_html=REVISED_TEXT)
        staged = self._record(self.client.patch(
            '/api/proposal/{0}/'.format(proposal.id),
            self._v2_edit_body(transaction_hash, envelope_xdr),
            format='json'))
        self.assertEqual(staged.data['payment_status'], payment_statuses.FINE)

        def restage_underneath(**kwargs):
            Proposal.objects.filter(id=proposal.id).update(new_title='Someone else entirely')
            return Proposal.FINE

        with patch(CHECK_STATUS, side_effect=restage_underneath):
            confirmation = self._confirm(proposal)

        self.assertEqual(confirmation.status_code, 200, confirmation.data)
        self.assertEqual(confirmation.data['payment_status'], payment_statuses.HORIZON_ERROR)

        proposal.refresh_from_db()
        self.assertEqual(proposal.title, 'Browser proposal')
        self.assertEqual(proposal.payment_status, payment_statuses.FINE)
        self.assertFalse(ConsumedTransaction.objects.filter(
            transaction_hash=transaction_hash).exists())

    def test_the_deprecated_bulk_create_endpoint_answers_405(self):
        """Nothing else pins the removal, and re-adding the mixin would restore it silently."""
        envelope_xdr, transaction_hash = self._burn(amount=CREATE_COST, text_html=PROPOSAL_TEXT)

        response = self.client.post(
            '/api/proposals/', self._v2_create_body(transaction_hash, envelope_xdr),
            format='json')

        self.assertEqual(response.status_code, 405)
        self.assertFalse(Proposal.objects.filter(transaction_hash=transaction_hash).exists())

    def test_the_deprecated_bulk_read_endpoint_still_lists(self):
        listing = self.client.get('/api/proposals/')

        self.assertEqual(listing.status_code, 200)
        self.assertIn('results', listing.data)

    def test_the_shared_test_route_enforces_the_same_payer_binding(self):
        """``api/test/proposal/`` subclasses the viewset, so it inherits every check.

        Only the URL literals in these tests are route-specific, which is why one attack is
        driven through the other route rather than trusted to shared code.
        """
        envelope_xdr, transaction_hash = self._burn(
            amount=CREATE_COST, text_html=PROPOSAL_TEXT, source=OTHER_ACCOUNT)
        response = self._record(self.client.post('/api/proposal/', {
            'proposed_by': OWNER,
            'title': 'Proposal paid for by someone else',
            'text': PROPOSAL_TEXT,
            'start_at': None,
            'end_at': None,
            'transaction_hash': transaction_hash,
            'discord_username': 'browser-user',
            'envelope_xdr': envelope_xdr,
        }, format='json'))
        self.assertEqual(response.status_code, 201, response.data)
        proposal = Proposal.objects.get(id=response.data['id'])

        confirmation = self._confirm(
            proposal, path='/api/test/proposal/{0}/check_payment/')

        self.assertEqual(confirmation.status_code, 200, confirmation.data)
        self.assertEqual(confirmation.data['payment_status'], payment_statuses.INVALID_PAYMENT)
        proposal.refresh_from_db()
        self.assertTrue(proposal.hide)
        self.assertFalse(ConsumedTransaction.objects.exists())
