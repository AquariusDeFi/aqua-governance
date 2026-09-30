import json

from django.test import TestCase
from django.utils import timezone
from django_quill.quill import Quill
from rest_framework.test import APIClient

from aqua_governance.governance.models import LogVote, Proposal
from aqua_governance.governance.tests._factories import (
    DEFAULT_PROPOSED_BY,
    SECONDARY_ACCOUNT,
    patch_ice_circulating_supply,
)


TARGET_ACCOUNT = DEFAULT_PROPOSED_BY
OTHER_ACCOUNT = SECONDARY_ACCOUNT


def _make_proposal(title, proposal_status=Proposal.VOTED):
    """Create a minimal general proposal."""
    with patch_ice_circulating_supply():
        return Proposal.objects.create(
            proposed_by=TARGET_ACCOUNT,
            title=title,
            text=Quill(json.dumps({'delta': '', 'html': '<p>x</p>'})),
            proposal_type=Proposal.PROPOSAL_TYPE_GENERAL,
            draft=False,
            action=Proposal.NONE,
            proposal_status=proposal_status,
        )


def _make_vote(proposal, account, claimable_balance_id, claimed):
    return LogVote.objects.create(
        proposal=proposal,
        account_issuer=account,
        claimable_balance_id=claimable_balance_id,
        transaction_link=(
            'https://horizon.stellar.org/claimable_balances/'
            f'{claimable_balance_id}/transactions'
        ),
        vote_choice=LogVote.VOTE_FOR,
        asset_code='AQUA',
        amount='1.0000000',
        original_amount='1.0000000',
        created_at=timezone.now(),
        claimed=claimed,
        hide=False,
    )


class ProposalVoteOwnerFilterTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_active_vote_owner_filter_does_not_match_other_accounts_votes(self):
        """Regression: active=true + vote_owner_public_key must not return
        proposals where the target account only has claimed votes, even if
        another account has active votes on that proposal.

        The returned proposal's nested logvote_set must contain only the
        target account's active vote.
        """
        # Proposal A: target has claimed vote, other has active vote.
        # Must NOT be returned (target has no active vote here).
        proposal_with_only_claimed_target_vote = _make_proposal(
            'Only claimed target vote',
        )
        _make_vote(
            proposal_with_only_claimed_target_vote,
            TARGET_ACCOUNT,
            'target-claimed-balance',
            claimed=True,
        )
        _make_vote(
            proposal_with_only_claimed_target_vote,
            OTHER_ACCOUNT,
            'other-active-balance',
            claimed=False,
        )

        # Proposal B: target has active vote. Must be returned.
        proposal_with_active_target_vote = _make_proposal('Active target vote')
        active_vote = _make_vote(
            proposal_with_active_target_vote,
            TARGET_ACCOUNT,
            'target-active-balance',
            claimed=False,
        )

        response = self.client.get(
            '/api/proposal/',
            {
                'vote_owner_public_key': TARGET_ACCOUNT,
                'active': 'true',
                'ordering': '-created_at',
            },
        )

        self.assertEqual(response.status_code, 200)
        results = response.json()['results']
        result_ids = {proposal['id'] for proposal in results}

        # Proposal A must NOT be in results.
        self.assertNotIn(
            proposal_with_only_claimed_target_vote.id,
            result_ids,
        )
        # Proposal B must be in results.
        self.assertIn(
            proposal_with_active_target_vote.id,
            result_ids,
        )

        # Proposal B's nested logvote_set must contain only the active vote.
        active_result = next(
            proposal for proposal in results
            if proposal['id'] == proposal_with_active_target_vote.id
        )
        self.assertEqual(
            [vote['claimable_balance_id'] for vote in active_result['logvote_set']],
            [active_vote.claimable_balance_id],
        )
