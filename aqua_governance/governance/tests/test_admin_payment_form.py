"""The admin's advisory payment check on ``ProposalAdminForm``.

The form decides ``payment_status`` and, on a create, ``hide`` for an admin-created general
proposal, and nothing tested it before v1: the one admin test that supplies ``envelope_xdr``
fails ``proposal_type`` validation long before the payment call.
"""
import json

from django.conf import settings
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase, override_settings

from django_quill.quill import Quill

from aqua_governance.governance.admin import ProposalAdmin
from aqua_governance.governance.models import Proposal
from aqua_governance.governance.tests._factories import (
    DEFAULT_PROPOSED_BY,
    build_aqua_burn_envelope,
    distinct_hash,
    patch_ice_circulating_supply,
)
from aqua_governance.utils.memo import create_memo_payload, memo_digest


PROPOSAL_TITLE = 'Admin general proposal'
PROPOSAL_TEXT = '<p>Admin general proposal body</p>'


@override_settings(DEBUG=False)
class AdminPaymentFormTests(TestCase):
    def setUp(self):
        super().setUp()
        self.site = AdminSite()
        self.admin = ProposalAdmin(Proposal, self.site)
        self.factory = RequestFactory()
        self.request = self.factory.post('/admin/')
        self.request.user = get_user_model().objects.create_user(
            username='admin_payment_form', password='password', is_staff=True, is_superuser=True,
        )

    def _quill_form_value(self, html=PROPOSAL_TEXT):
        return json.dumps({'delta': '', 'html': html})

    def _create_envelope(self, *, title=PROPOSAL_TITLE, text_html=PROPOSAL_TEXT,
                         proposed_by=DEFAULT_PROPOSED_BY):
        memo = memo_digest(create_memo_payload(
            proposed_by=proposed_by,
            proposal_type=Proposal.PROPOSAL_TYPE_GENERAL,
            title=title,
            text_html=text_html,
        ))
        envelope_xdr, _tx_hash = build_aqua_burn_envelope(
            source=proposed_by,
            amount=settings.PROPOSAL_CREATE_OR_UPDATE_COST,
            memo_hash_hex=memo.hex(),
        )
        return envelope_xdr

    def _create_form(self, **overrides):
        data = {
            'proposed_by': DEFAULT_PROPOSED_BY,
            'title': PROPOSAL_TITLE,
            'text': self._quill_form_value(),
            'proposal_type': Proposal.PROPOSAL_TYPE_GENERAL,
            'transaction_hash': distinct_hash(300),
            'envelope_xdr': self._create_envelope(),
            'discord_username': 'admin',
        }
        data.update(overrides)
        form_class = self.admin.get_form(self.request)
        return form_class(data=data)

    def test_a_valid_burn_envelope_scores_fine_and_leaves_the_row_visible(self):
        form = self._create_form()

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.instance.payment_status, Proposal.FINE)
        self.assertFalse(form.instance.hide)

    def test_an_envelope_paying_the_wrong_amount_is_not_fine(self):
        memo = memo_digest(create_memo_payload(
            proposed_by=DEFAULT_PROPOSED_BY,
            proposal_type=Proposal.PROPOSAL_TYPE_GENERAL,
            title=PROPOSAL_TITLE,
            text_html=PROPOSAL_TEXT,
        ))
        envelope_xdr, _tx_hash = build_aqua_burn_envelope(
            source=DEFAULT_PROPOSED_BY,
            amount=settings.PROPOSAL_SUBMIT_COST,
            memo_hash_hex=memo.hex(),
        )

        form = self._create_form(envelope_xdr=envelope_xdr)

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.instance.payment_status, Proposal.INVALID_PAYMENT)

    def test_an_envelope_sourced_at_another_account_is_not_fine(self):
        envelope_xdr = self._create_envelope(proposed_by=DEFAULT_PROPOSED_BY)

        form = self._create_form(
            proposed_by='GAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAWHF',
            envelope_xdr=envelope_xdr,
        )

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.instance.payment_status, Proposal.INVALID_PAYMENT)

    def test_a_memo_over_a_different_title_is_rejected_as_a_bad_memo(self):
        form = self._create_form(envelope_xdr=self._create_envelope(title='Some other title'))

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.instance.payment_status, Proposal.BAD_MEMO)

    def test_a_malformed_envelope_scores_terminal_without_hiding_the_new_row(self):
        """Sec. 2.15 keeps the admin ``hide`` flip; sec. 5.2 expects no hiding.  Both land here.

        ``clean()`` does set ``hide = True``, and ``_post_clean`` then overwrites it from the
        form's own unchecked ``hide`` checkbox - which a superuser always has, and only a
        superuser can add a general proposal.  So the flip is inert through the admin UI and
        the row stays visible, which is what sec. 4.3 wanted anyway.
        """
        form = self._create_form(envelope_xdr='not-an-envelope')

        self.assertTrue(form.is_valid(), form.errors)
        self.assertNotEqual(form.instance.payment_status, Proposal.FINE)
        self.assertFalse(form.instance.hide)

    def test_editing_an_existing_proposal_does_not_rescore_its_payment(self):
        """Why the unchanged v1 trigger condition costs nothing through the admin UI.

        Sec. 2.15 leaves the trigger as "``envelope_xdr`` is present" rather than narrowing
        it to ``_state.adding``, and the worry is that an admin renaming a proposal would be
        re-scored against a *create* memo it was never going to carry.  It cannot happen
        here: ``get_readonly_fields`` makes ``envelope_xdr`` read-only once ``obj`` exists,
        so the change form has no such field and the payment branch is unreachable.  This is
        the guard for whoever narrows the condition later.
        """
        with patch_ice_circulating_supply():
            proposal = Proposal.objects.create(
                proposed_by=DEFAULT_PROPOSED_BY,
                title=PROPOSAL_TITLE,
                text=Quill(self._quill_form_value()),
                proposal_type=Proposal.PROPOSAL_TYPE_GENERAL,
                transaction_hash=distinct_hash(310),
                envelope_xdr=self._create_envelope(),
                payment_status=Proposal.FINE,
            )

        form_class = self.admin.get_form(self.request, obj=proposal, change=True)
        form = form_class(
            instance=proposal,
            data={
                'proposed_by': proposal.proposed_by,
                'title': 'A renamed proposal',
                'text': self._quill_form_value(),
                'proposal_type': Proposal.PROPOSAL_TYPE_GENERAL,
                'proposal_status': Proposal.DISCUSSION,
                'transaction_hash': proposal.transaction_hash,
                'envelope_xdr': proposal.envelope_xdr,
                'discord_username': 'admin',
            },
        )

        self.assertNotIn('envelope_xdr', form.fields)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.instance.payment_status, Proposal.FINE)
        self.assertFalse(form.instance.hide)
