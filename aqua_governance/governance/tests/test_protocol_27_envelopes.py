from types import SimpleNamespace

from django.conf import settings
from django.test import SimpleTestCase, override_settings

from rest_framework.exceptions import PermissionDenied

from aqua_governance.governance import payment_statuses
from aqua_governance.governance.models import Proposal
from aqua_governance.governance.views import ProposalViewSet
from aqua_governance.utils.memo import PURPOSE_CREATE, build_memo_expectation
from aqua_governance.utils.payments import inspect_envelope


# Built with stellar-sdk 15.0.0. The operation carries CAP-71
# SOROBAN_CREDENTIALS_ADDRESS_V2 credentials, which stellar-sdk 13.1 cannot decode.
PROTOCOL_27_ENVELOPE_XDR = (
    'AAAAAgAAAABYt8SiyPKXqo89JHEoH9/M7K/kjlZjMT7BjhKnPsqYoQAAAGQAAAAAAAHiQQAAAAEAAAAAAAAAAAAAAABqYKHRAAAA'
    'AAAAAAEAAAAAAAAAGAAAAAAAAAABxYsr+8TwVOcyT2vyDK0+Am5Bu60abSDD19SRje0WVBEAAAAJaW5jcmVtZW50AAAAAAAAAgAA'
    'ABIAAAAAAAAAAFi3xKLI8peqjz0kcSgf38zsr+SOVmMxPsGOEqc+ypihAAAAAwAAAAoAAAABAAAAAgAAAAAAAAAAWLfEosjyl6qP'
    'PSRxKB/fzOyv5I5WYzE+wY4Spz7KmKEAAAAAB1vNFQAJ/UAAAAABAAAAAAAAAAHFiyv7xPBU5zJPa/IMrT4CbkG7rRptIMPX1JGN'
    '7RZUEQAAAAlpbmNyZW1lbnQAAAAAAAACAAAAEgAAAAAAAAAAWLfEosjyl6qPPSRxKB/fzOyv5I5WYzE+wY4Spz7KmKEAAAADAAAA'
    'CgAAAAAAAAAAAAAAAA=='
)
PROTOCOL_27_SOURCE_ACCOUNT = 'GBMLPRFCZDZJPKUPHUSHCKA737GOZL7ERZLGGMJ6YGHBFJZ6ZKMKCZTM'
PROTOCOL_27_PROPOSAL_TEXT = '<p>Protocol 27 proposal</p>'


@override_settings(DEBUG=False)
class Protocol27EnvelopeTests(SimpleTestCase):
    def test_payment_verification_decodes_address_v2_credentials_before_rejecting_missing_payment(self):
        status = inspect_envelope(
            envelope_xdr=PROTOCOL_27_ENVELOPE_XDR,
            expected_payer=PROTOCOL_27_SOURCE_ACCOUNT,
            memo_expectation=build_memo_expectation(
                PURPOSE_CREATE,
                proposed_by=PROTOCOL_27_SOURCE_ACCOUNT,
                proposal_type=Proposal.PROPOSAL_TYPE_GENERAL,
                title='Protocol 27 proposal',
                text_html=PROTOCOL_27_PROPOSAL_TEXT,
            ),
            payment_amount=settings.PROPOSAL_SUBMIT_COST,
        )

        self.assertEqual(status, payment_statuses.INVALID_PAYMENT)

    def test_owner_verification_decodes_address_v2_credentials(self):
        ProposalViewSet()._reject_declared_owner_mismatch(
            SimpleNamespace(proposed_by=PROTOCOL_27_SOURCE_ACCOUNT),
            {'new_envelope_xdr': PROTOCOL_27_ENVELOPE_XDR},
        )

    def test_owner_verification_rejects_mismatched_owner_after_protocol_27_decode(self):
        with self.assertRaisesMessage(PermissionDenied, 'You are not the proposal owner'):
            ProposalViewSet()._reject_declared_owner_mismatch(
                SimpleNamespace(proposed_by='GAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAWHF'),
                {'new_envelope_xdr': PROTOCOL_27_ENVELOPE_XDR},
            )
