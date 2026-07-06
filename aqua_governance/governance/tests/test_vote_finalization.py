import json
from decimal import Decimal

from django.conf import settings
from django.test import TestCase
from django_quill.quill import Quill

from aqua_governance.governance.models import LogVote, Proposal
from aqua_governance.governance.parser import parse_vote
from aqua_governance.governance.serializers import LogVoteSerializer
from aqua_governance.governance.task_logic.proposal_finalization import (
    _sum_votes_for_proposal,
)
from aqua_governance.governance.tests._factories import (
    patch_ice_circulating_supply,
)


def _quill_text(html='<p>x</p>'):
    return Quill(json.dumps({'delta': {'ops': []}, 'html': html}))


def _create_proposal(**overrides):
    defaults = {
        'proposed_by': 'GAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAWHF',
        'title': 'Test proposal',
        'text': _quill_text(),
        'draft': False,
        'action': Proposal.NONE,
        'proposal_status': Proposal.DISCUSSION,
    }
    defaults.update(overrides)
    with patch_ice_circulating_supply():
        return Proposal.objects.create(**defaults)


def _make_log_vote(proposal, **overrides):
    defaults = {
        'claimable_balance_id': '0' * 72,
        'proposal': proposal,
        'vote_choice': LogVote.VOTE_FOR,
        'asset_code': settings.GOVERNANCE_ICE_ASSET_CODE,
        'amount': Decimal('1000'),
        'voted_amount': None,
        'hide': False,
        'claimed': False,
        'account_issuer': proposal.proposed_by,
        'key': 'test-key',
        'group_index': 0,
    }
    defaults.update(overrides)
    return LogVote.objects.create(**defaults)


def _make_voted_proposal(**overrides):
    return _create_proposal(proposal_status=Proposal.VOTED, **overrides)


class ParseVoteTests(TestCase):
    """Tests for parse_vote(): voted_amount freeze & non-freeze behaviour."""

    def _minimal_claimable_balance(self, asset='governICE:GAXSGZ2JM3LNWOO4WRGADISNMWO4HQLG4QBGUZRKH5ZHL3EQBGX73ICE', amount='500'):
        return {
            'id': '0' * 72,
            'asset': asset,
            'amount': amount,
            'sponsor': 'GDEST',
            'claimants': [
                {
                    'destination': 'GDEST',
                    'predicate': {'not': {'abs_before': '2025-01-01T00:00:00Z'}},
                },
            ],
            '_links': {'transactions': {'href': 'https://horizon.stellar.org/transactions/{?cursor,limit,order}'}},
        }

    def test_freezing_sets_voted_amount_to_current_amount(self):
        """When freezing_amount=True, voted_amount must equal the CB amount."""
        proposal = _create_proposal(proposal_status=Proposal.VOTED)
        cb = self._minimal_claimable_balance(amount='500')
        vote = parse_vote(
            vote_key='test-key',
            vote_group_index=0,
            claimable_balance=cb,
            proposal=proposal,
            vote_choice=LogVote.VOTE_FOR,
            created_at='2025-01-01T00:00:00Z',
            original_amount='500',
            vote_id=None,
            freezing_amount=True,
            original_voted_amount=None,
        )
        self.assertIsNotNone(vote)
        # parse_vote sets voted_amount from the raw CB amount (string), not yet
        # coerced by the ORM — verify the value was set from the current amount.
        self.assertEqual(Decimal(vote.voted_amount), Decimal('500'))

    def test_freezing_sets_voted_amount_with_existing_snapshot(self):
        """Freezing sets voted_amount even if original_voted_amount exists (no overwrite)."""
        proposal = _create_proposal(proposal_status=Proposal.VOTED)
        cb = self._minimal_claimable_balance(amount='600')
        vote = parse_vote(
            vote_key='test-key',
            vote_group_index=0,
            claimable_balance=cb,
            proposal=proposal,
            vote_choice=LogVote.VOTE_FOR,
            created_at='2025-01-01T00:00:00Z',
            original_amount='600',
            vote_id=None,
            freezing_amount=True,
            original_voted_amount=Decimal('500'),
        )
        self.assertIsNotNone(vote)
        # freezing_amount wins — voted_amount is the current amount, not the old snapshot
        self.assertEqual(Decimal(vote.voted_amount), Decimal('600'))

    def test_non_freezing_preserves_original_voted_amount(self):
        """When freezing_amount=False, voted_amount must be original_voted_amount."""
        proposal = _create_proposal(proposal_status=Proposal.VOTING)
        cb = self._minimal_claimable_balance(amount='700')
        vote = parse_vote(
            vote_key='test-key',
            vote_group_index=0,
            claimable_balance=cb,
            proposal=proposal,
            vote_choice=LogVote.VOTE_FOR,
            created_at='2025-01-01T00:00:00Z',
            original_amount='700',
            vote_id=None,
            freezing_amount=False,
            original_voted_amount=Decimal('600'),
        )
        self.assertIsNotNone(vote)
        self.assertEqual(vote.voted_amount, Decimal('600'))

    def test_non_freezing_with_none_preserves_none(self):
        """When freezing_amount=False and no prior snapshot, voted_amount stays None."""
        proposal = _create_proposal(proposal_status=Proposal.VOTING)
        cb = self._minimal_claimable_balance(amount='700')
        vote = parse_vote(
            vote_key='test-key',
            vote_group_index=0,
            claimable_balance=cb,
            proposal=proposal,
            vote_choice=LogVote.VOTE_FOR,
            created_at='2025-01-01T00:00:00Z',
            original_amount='700',
            vote_id=None,
            freezing_amount=False,
            original_voted_amount=None,
        )
        self.assertIsNotNone(vote)
        self.assertIsNone(vote.voted_amount)


class SumVotesForVOTEDProposalTests(TestCase):
    """Tests for _sum_votes_for_proposal() with VOTED proposals."""

    def test_voted_proposal_uses_voted_amount_includes_claimed(self):
        """VOTED: sum voted_amount, include claimed rows, ignore current amount."""
        proposal = _make_voted_proposal()

        # VOTED snapshot at 1000, later CB melted to 800
        _make_log_vote(proposal,
                       claimable_balance_id='1' * 72,
                       vote_choice=LogVote.VOTE_FOR,
                       amount=Decimal('800'),     # current (melted)
                       voted_amount=Decimal('1000'),
                       claimed=True)               # already claimed — still counted

        _make_log_vote(proposal,
                       claimable_balance_id='2' * 72,
                       vote_choice=LogVote.VOTE_FOR,
                       amount=Decimal('300'),
                       voted_amount=Decimal('300'),
                       claimed=False)

        result = _sum_votes_for_proposal(proposal, LogVote.VOTE_FOR)
        self.assertEqual(result, Decimal('1300'))

    def test_voted_proposal_ignores_hidden_rows(self):
        """VOTED: hidden rows excluded even with voted_amount set."""
        proposal = _make_voted_proposal()
        _make_log_vote(proposal,
                       claimable_balance_id='1' * 72,
                       vote_choice=LogVote.VOTE_FOR,
                       amount=Decimal('1000'),
                       voted_amount=Decimal('1000'),
                       hide=True)

        result = _sum_votes_for_proposal(proposal, LogVote.VOTE_FOR)
        self.assertEqual(result, Decimal('0'))

    def test_voted_proposal_falls_back_to_amount_when_voted_amount_is_none(self):
        """VOTED: rows with voted_amount=None fall back to current amount."""
        proposal = _make_voted_proposal()
        _make_log_vote(proposal,
                       claimable_balance_id='1' * 72,
                       vote_choice=LogVote.VOTE_FOR,
                       amount=Decimal('1000'),
                       voted_amount=None,
                       claimed=False)

        result = _sum_votes_for_proposal(proposal, LogVote.VOTE_FOR)
        self.assertEqual(result, Decimal('1000'))

    def test_voted_proposal_fallback_counts_claimed_rows_with_none_snapshot(self):
        """VOTED: claimed row with voted_amount=None still counted via fallback."""
        proposal = _make_voted_proposal()
        _make_log_vote(proposal,
                       claimable_balance_id='1' * 72,
                       vote_choice=LogVote.VOTE_FOR,
                       amount=Decimal('10'),
                       voted_amount=None,
                       claimed=True)

        result = _sum_votes_for_proposal(proposal, LogVote.VOTE_FOR)
        self.assertEqual(result, Decimal('10'))

    def test_voted_proposal_zero_snapshot_not_treated_as_none(self):
        """VOTED: voted_amount=0 is a legitimate freeze, not a fallback trigger."""
        proposal = _make_voted_proposal()
        _make_log_vote(proposal,
                       claimable_balance_id='1' * 72,
                       vote_choice=LogVote.VOTE_FOR,
                       amount=Decimal('100'),
                       voted_amount=Decimal('0'),
                       claimed=False)

        result = _sum_votes_for_proposal(proposal, LogVote.VOTE_FOR)
        self.assertEqual(result, Decimal('0'))

    def test_voted_proposal_single_vote_against(self):
        """VOTED: vote_against also uses voted_amount."""
        proposal = _make_voted_proposal()
        _make_log_vote(proposal,
                       claimable_balance_id='1' * 72,
                       vote_choice=LogVote.VOTE_AGAINST,
                       amount=Decimal('999'),
                       voted_amount=Decimal('500'),
                       claimed=True)

        result = _sum_votes_for_proposal(proposal, LogVote.VOTE_AGAINST)
        self.assertEqual(result, Decimal('500'))


class SumVotesForNonVOTEDProposalTests(TestCase):
    """Tests for _sum_votes_for_proposal() with non-VOTED proposals."""

    def test_voting_proposal_uses_amount_excludes_claimed(self):
        """VOTING: sum current amount, exclude claimed rows."""
        proposal = _create_proposal(proposal_status=Proposal.VOTING)
        _make_log_vote(proposal,
                       claimable_balance_id='1' * 72,
                       vote_choice=LogVote.VOTE_FOR,
                       amount=Decimal('1000'),
                       voted_amount=Decimal('999'),  # frozen snapshot ignored
                       claimed=False)
        _make_log_vote(proposal,
                       claimable_balance_id='2' * 72,
                       vote_choice=LogVote.VOTE_FOR,
                       amount=Decimal('500'),
                       voted_amount=Decimal('400'),
                       claimed=True)  # excluded

        result = _sum_votes_for_proposal(proposal, LogVote.VOTE_FOR)
        self.assertEqual(result, Decimal('1000'))

    def test_discussion_proposal_uses_amount_excludes_claimed(self):
        """DISCUSSION: same as VOTING behaviour."""
        proposal = _create_proposal(proposal_status=Proposal.DISCUSSION)
        _make_log_vote(proposal,
                       claimable_balance_id='1' * 72,
                       vote_choice=LogVote.VOTE_FOR,
                       amount=Decimal('200'),
                       claimed=False)
        _make_log_vote(proposal,
                       claimable_balance_id='2' * 72,
                       vote_choice=LogVote.VOTE_FOR,
                       amount=Decimal('999'),
                       claimed=True)

        result = _sum_votes_for_proposal(proposal, LogVote.VOTE_FOR)
        self.assertEqual(result, Decimal('200'))


class LogVoteSerializerTests(TestCase):
    """Tests that LogVoteSerializer includes voted_amount and claimed."""

    def test_serializer_includes_voted_amount_and_claimed(self):
        proposal = _create_proposal()
        vote = _make_log_vote(proposal, voted_amount=Decimal('700'), claimed=True)
        serializer = LogVoteSerializer(instance=vote)
        data = serializer.data
        self.assertIn('voted_amount', data)
        self.assertEqual(Decimal(data['voted_amount']), Decimal('700'))
        self.assertIn('claimed', data)
        self.assertTrue(data['claimed'])
