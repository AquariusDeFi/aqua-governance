import json
from unittest.mock import Mock, patch

from django.test import TestCase
from django.utils import timezone
from django_quill.quill import Quill
from rest_framework.test import APIClient

from aqua_governance.governance.models import LogVote, Proposal
from aqua_governance.governance.tests._factories import (
    DEFAULT_PROPOSED_BY,
    SECONDARY_ACCOUNT,
    make_asset_proposal_raw,
    patch_ice_circulating_supply,
)


TARGET_ACCOUNT = DEFAULT_PROPOSED_BY
OTHER_ACCOUNT = SECONDARY_ACCOUNT


def _make_general_proposal(**overrides):
    defaults = {
        'proposed_by': TARGET_ACCOUNT,
        'title': 'General proposal',
        'text': Quill(json.dumps({'delta': '', 'html': '<p>x</p>'})),
        'proposal_type': Proposal.PROPOSAL_TYPE_GENERAL,
        'draft': False,
        'action': Proposal.NONE,
        'proposal_status': Proposal.VOTED,
    }
    defaults.update(overrides)
    with patch_ice_circulating_supply():
        return Proposal.objects.create(**defaults)


def _make_vote(proposal, *, account, claimable_balance_id, claimed):
    return LogVote.objects.create(
        proposal=proposal,
        account_issuer=account,
        claimable_balance_id=claimable_balance_id,
        transaction_link=f'https://horizon.stellar.org/claimable_balances/{claimable_balance_id}/transactions',
        vote_choice=LogVote.VOTE_FOR,
        asset_code='AQUA',
        amount='1.0000000',
        original_amount='1.0000000',
        created_at=timezone.now(),
        claimed=claimed,
        hide=False,
    )


class ProposalFilterBackendTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_proposal_type_filter_supports_asset_shortcut_and_alias_parameter(self):
        add_asset = make_asset_proposal_raw(title='Add asset')
        remove_asset = make_asset_proposal_raw(
            title='Remove asset',
            proposal_type=Proposal.PROPOSAL_TYPE_REMOVE_ASSET,
            asset_contract_address='CBQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1',
        )
        general = _make_general_proposal(title='General')

        asset_response = self.client.get('/api/proposal/', {'proposal_type': 'asset'})
        mixed_response = self.client.get('/api/proposal/', {'proposal_types': 'general,asset'})

        self.assertEqual(asset_response.status_code, 200)
        self.assertEqual(mixed_response.status_code, 200)

        asset_ids = {proposal['id'] for proposal in asset_response.json()['results']}
        mixed_ids = {proposal['id'] for proposal in mixed_response.json()['results']}

        self.assertEqual(asset_ids, {add_asset.id, remove_asset.id})
        self.assertEqual(mixed_ids, {add_asset.id, remove_asset.id, general.id})

    def test_active_flag_filters_prefetched_votes_without_owner_filter(self):
        proposal = _make_general_proposal(title='Vote activity')
        claimed_only_proposal = _make_general_proposal(title='Claimed only activity')
        active_vote = _make_vote(
            proposal,
            account=TARGET_ACCOUNT,
            claimable_balance_id='active-balance',
            claimed=False,
        )
        _make_vote(
            proposal,
            account=OTHER_ACCOUNT,
            claimable_balance_id='claimed-balance',
            claimed=True,
        )
        _make_vote(
            claimed_only_proposal,
            account=TARGET_ACCOUNT,
            claimable_balance_id='claimed-only-balance',
            claimed=True,
        )

        response = self.client.get('/api/proposal/', {'active': ' On ', 'ordering': '-created_at'})

        self.assertEqual(response.status_code, 200)
        results = response.json()['results']
        result_ids = {item['id'] for item in results}
        self.assertNotIn(claimed_only_proposal.id, result_ids)

        result = next(item for item in results if item['id'] == proposal.id)
        self.assertEqual(
            [vote['claimable_balance_id'] for vote in result['logvote_set']],
            [active_vote.claimable_balance_id],
        )

    def test_status_filter_accepts_queued_value(self):
        _make_general_proposal(title='Discussion', proposal_status=Proposal.DISCUSSION)
        _make_general_proposal(title='Voting', proposal_status=Proposal.VOTING)
        queued = _make_general_proposal(title='Queued', proposal_status=Proposal.QUEUED)

        response = self.client.get('/api/proposal/', {'status': 'queued', 'ordering': '-created_at'})

        self.assertEqual(response.status_code, 200)
        results = response.json()['results']
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['id'], queued.id)

    def test_status_filter_with_unsupported_value_returns_all_visible(self):
        queued = _make_general_proposal(title='Queued', proposal_status=Proposal.QUEUED)
        voting = _make_general_proposal(title='Voting', proposal_status=Proposal.VOTING)

        response = self.client.get('/api/proposal/', {'status': 'unknown_value', 'ordering': '-created_at'})

        self.assertEqual(response.status_code, 200)
        result_ids = {item['id'] for item in response.json()['results']}
        self.assertIn(queued.id, result_ids)
        self.assertIn(voting.id, result_ids)
