"""API contract regression for /api/asset-tokens/."""
from django.conf import settings
from django.test import TestCase
from rest_framework.test import APIClient
from stellar_sdk import Asset

from aqua_governance.governance.models import AssetToken, Proposal
from aqua_governance.governance.tests._factories import (
    DEFAULT_CODE,
    DEFAULT_ISSUER,
    make_asset_proposal,
)


EXPECTED_TOKEN_KEYS = {
    'asset_code',
    'asset_issuer',
    'asset_contract_address',
    'whitelisted',
    'proposals',
}

EXPECTED_PROPOSAL_KEYS = {
    'id', 'proposal_type', 'proposal_status', 'title',
    'start_at', 'end_at', 'new_start_at', 'new_end_at',
    'vote_for_result', 'vote_against_result', 'vote_abstain_result',
    'onchain_execution_status', 'onchain_execution_tx_hash',
    'created_at', 'last_updated_at',
}


class AssetTokensApiContractTests(TestCase):
    def setUp(self):
        Proposal.objects.all().delete()
        AssetToken.objects.all().delete()
        self.client = APIClient()

    def test_response_shape_matches_legacy(self):
        proposal = make_asset_proposal(title='Add AQUA')

        response = self.client.get('/api/asset-tokens/')

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['count'], 1)
        self.assertEqual(len(body['results']), 1)

        token_card = body['results'][0]
        self.assertEqual(set(token_card.keys()), EXPECTED_TOKEN_KEYS)

        derived = Asset(DEFAULT_CODE, DEFAULT_ISSUER).contract_id(settings.NETWORK_PASSPHRASE)
        self.assertEqual(token_card['asset_code'], DEFAULT_CODE)
        self.assertEqual(token_card['asset_issuer'], DEFAULT_ISSUER)
        self.assertEqual(token_card['asset_contract_address'], derived)
        self.assertIs(token_card['whitelisted'], False)
        self.assertEqual(len(token_card['proposals']), 1)
        self.assertEqual(set(token_card['proposals'][0].keys()), EXPECTED_PROPOSAL_KEYS)
        self.assertEqual(token_card['proposals'][0]['id'], proposal.id)
        self.assertEqual(token_card['proposals'][0]['proposal_type'], Proposal.PROPOSAL_TYPE_ADD_ASSET)

    def test_executed_token_ordered_above_unexecuted_token(self):
        """X6 regression: `.order_by('-last_execution_at')` on Postgres defaults
        to NULLS FIRST, so a token whose only proposals are still pending
        (last_execution_at IS NULL) would float ABOVE tokens with confirmed
        SUCCESS executions. After the fix (NULLS LAST), executed tokens lead
        and unexecuted tokens tail-anchor by created_at.
        """
        from datetime import timedelta
        from django.utils import timezone
        from aqua_governance.governance.tests._factories import (
            DEFAULT_CODE,
            DEFAULT_ISSUER,
            make_asset_proposal,
        )

        # Token #1 — unexecuted (DISCUSSION/PENDING token).
        unexecuted_proposal = make_asset_proposal(
            title='Unexecuted ADD',
            asset_code='UNEXEC',
            asset_issuer='GAXOKPHAONONRATXWD5GOQSS7XDPLKJ377NITZJDAG5JJ4F33SJ7YYT7',
        )

        # Token #2 — executed (force last_execution_at to a recent timestamp).
        executed_proposal = make_asset_proposal(
            title='Executed ADD',
            asset_code=DEFAULT_CODE,
            asset_issuer=DEFAULT_ISSUER,
        )
        executed_contract = Asset(DEFAULT_CODE, DEFAULT_ISSUER).contract_id(settings.NETWORK_PASSPHRASE)
        executed_at = timezone.now() - timedelta(days=7)
        AssetToken.objects.filter(contract_address=executed_contract).update(
            whitelisted=True,
            whitelisted_since=executed_at,
            last_execution_at=executed_at,
        )

        response = self.client.get('/api/asset-tokens/')

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['count'], 2)
        # Executed token leads, unexecuted tails (NULLS LAST).
        self.assertEqual(body['results'][0]['asset_contract_address'], executed_contract)
        self.assertIs(body['results'][0]['whitelisted'], True)
        self.assertIs(body['results'][1]['whitelisted'], False)
        self.assertEqual(body['results'][1]['proposals'][0]['title'], 'Unexecuted ADD')

    def test_token_with_draft_only_proposals_is_hidden(self):
        make_asset_proposal(title='Draft ADD', draft=True)

        response = self.client.get('/api/asset-tokens/')

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['count'], 0)
        self.assertEqual(body['results'], [])
