"""Regression coverage for asset narrative representation in proposal detail."""
import json

from django.test import TestCase
from django_quill.quill import Quill
from rest_framework.test import APIClient

from aqua_governance.governance.models import AssetToken, Proposal
from aqua_governance.governance.tests._factories import (
    DEFAULT_PROPOSED_BY,
    make_asset_proposal,
    patch_ice_circulating_supply,
)


class AssetNarrativeNullCoalescingTests(TestCase):
    def setUp(self):
        Proposal.objects.all().delete()
        AssetToken.objects.all().delete()
        self.client = APIClient()
        self.ice_supply_patcher = patch_ice_circulating_supply()
        self.ice_supply_patcher.start()
        self.addCleanup(self.ice_supply_patcher.stop)

    def test_empty_narrative_serializes_as_empty_string_in_v2_detail(self):
        proposal = make_asset_proposal(
            title='Empty narratives',
            narratives={
                'asset_issuer_information': '',
                'asset_token_description': '',
                'asset_holder_distribution': '',
            },
        )

        response = self.client.get(f'/api/proposal/{proposal.id}/')
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()

        self.assertEqual(body['asset_issuer_information'], '')
        self.assertEqual(body['asset_token_description'], '')
        self.assertEqual(body['asset_holder_distribution'], '')
        # Non-empty narrative still appears as the string.
        self.assertEqual(body['asset_liquidity'], 'liq')

    def test_general_proposal_has_null_for_all_asset_fields(self):
        proposal = Proposal.objects.create(
            proposed_by=DEFAULT_PROPOSED_BY,
            title='General',
            proposal_type=Proposal.PROPOSAL_TYPE_GENERAL,
            transaction_hash='a' * 64,
            payment_status=Proposal.FINE,
            text=Quill(json.dumps({'delta': '', 'html': '<p>x</p>'})),
        )

        response = self.client.get(f'/api/proposal/{proposal.id}/')
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()

        self.assertIsNone(body['asset_code'])
        self.assertIsNone(body['asset_issuer'])
        self.assertIsNone(body['asset_contract_address'])
        self.assertIsNone(body['asset_issuer_information'])
