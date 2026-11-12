from decimal import Decimal
from types import SimpleNamespace

from django.test import SimpleTestCase

from aqua_governance.governance.proposal_outcome import (
    ProposalOutcome,
    QuorumSide,
    compute_outcome,
    compute_quorum_side,
    compute_quorum_votes,
    get_proposal_outcome,
    get_quorum_votes,
)


ACCEPTED = ProposalOutcome.ACCEPTED
REJECTED = ProposalOutcome.REJECTED
NO_QUORUM = ProposalOutcome.NO_QUORUM

PERCENT_FOR_QUORUM = 10

# Each row: name, For, Against, Abstain, supply, pooled outcome, separate side outcome.
REFERENCE_MATRIX = (
    ('M1', '9', '1', '0.1', '100', ACCEPTED, NO_QUORUM),
    ('M2', '9.1', '9', '0', '100', ACCEPTED, NO_QUORUM),
    ('M3', '1', '9', '0.1', '100', REJECTED, NO_QUORUM),
    ('M4', '10', '9', '0', '100', ACCEPTED, ACCEPTED),
    ('M5', '5', '4', '6', '100', ACCEPTED, ACCEPTED),
    ('M6', '4', '5', '6', '100', REJECTED, REJECTED),
    ('M7', '6', '6', '4', '100', REJECTED, REJECTED),
    ('M8', '6', '6', '3', '100', REJECTED, NO_QUORUM),
    ('M9', '0', '0', '0', '100', NO_QUORUM, NO_QUORUM),
    ('M10', '9.9', '0', '0', '100', NO_QUORUM, NO_QUORUM),
    ('M11', '5', '0', '0', '0', NO_QUORUM, NO_QUORUM),
)


def _proposal(vote_for, vote_against, vote_abstain, separate_side_quorum, is_simple_proposal=True):
    return SimpleNamespace(
        id=1,
        is_simple_proposal=is_simple_proposal,
        vote_for_result=Decimal(vote_for),
        vote_against_result=Decimal(vote_against),
        vote_abstain_result=Decimal(vote_abstain),
        ice_circulating_supply=Decimal('100'),
        percent_for_quorum=PERCENT_FOR_QUORUM,
        separate_side_quorum=separate_side_quorum,
    )


class ComputeOutcomeTests(SimpleTestCase):
    def test_reference_matrix(self):
        for name, vote_for, vote_against, vote_abstain, supply, pooled, separate in REFERENCE_MATRIX:
            for separate_side_quorum, expected in ((False, pooled), (True, separate)):
                with self.subTest(row=name, separate_side_quorum=separate_side_quorum):
                    outcome = compute_outcome(
                        Decimal(vote_for),
                        Decimal(vote_against),
                        Decimal(vote_abstain),
                        Decimal(supply),
                        PERCENT_FOR_QUORUM,
                        separate_side_quorum,
                    )
                    self.assertEqual(outcome, expected)

    def test_float_boundary_m12(self):
        for separate_side_quorum in (False, True):
            with self.subTest(separate_side_quorum=separate_side_quorum):
                outcome = compute_outcome(
                    Decimal('8882723891.8178415'),
                    0,
                    0,
                    Decimal('44413619459.0892075'),
                    20,
                    separate_side_quorum,
                )
                self.assertEqual(outcome, ACCEPTED)

    def test_abstain_only_is_rejected_when_quorum_met(self):
        for separate_side_quorum in (False, True):
            with self.subTest(separate_side_quorum=separate_side_quorum):
                self.assertEqual(compute_outcome(0, 0, 10, 100, 10, separate_side_quorum), REJECTED)
        self.assertEqual(compute_quorum_side(0, 0, True), QuorumSide.AGAINST)

    def test_none_tallies_and_supply(self):
        self.assertEqual(compute_quorum_side(None, None, True), QuorumSide.AGAINST)
        for separate_side_quorum in (False, True):
            with self.subTest(separate_side_quorum=separate_side_quorum):
                self.assertEqual(compute_quorum_votes(None, None, None, separate_side_quorum), Decimal('0'))
                self.assertEqual(compute_outcome(None, None, None, 100, 10, separate_side_quorum), NO_QUORUM)
                self.assertEqual(compute_outcome(Decimal('10'), None, None, 100, 10, separate_side_quorum), ACCEPTED)
                self.assertEqual(compute_outcome(None, Decimal('10'), None, 100, 10, separate_side_quorum), REJECTED)
            for supply in (None, 0, Decimal('-1')):
                with self.subTest(separate_side_quorum=separate_side_quorum, supply=supply):
                    outcome = compute_outcome(Decimal('5'), 0, 0, supply, 10, separate_side_quorum)
                    self.assertEqual(outcome, NO_QUORUM)

    def test_quorum_side(self):
        self.assertEqual(compute_quorum_side(Decimal('5'), Decimal('4'), True), QuorumSide.FOR)
        self.assertEqual(compute_quorum_side(Decimal('4'), Decimal('5'), True), QuorumSide.AGAINST)
        self.assertEqual(compute_quorum_side(Decimal('6'), Decimal('6'), True), QuorumSide.AGAINST)
        self.assertIsNone(compute_quorum_side(Decimal('5'), Decimal('4'), False))

    def test_quorum_votes(self):
        self.assertEqual(compute_quorum_votes(Decimal('5'), Decimal('4'), Decimal('6'), False), Decimal('15'))
        self.assertEqual(compute_quorum_votes(Decimal('5'), Decimal('4'), Decimal('6'), True), Decimal('11'))
        self.assertEqual(compute_quorum_votes(Decimal('4'), Decimal('5'), Decimal('6'), True), Decimal('11'))
        self.assertEqual(compute_quorum_votes(Decimal('6'), Decimal('6'), Decimal('4'), True), Decimal('10'))


class ProposalOutcomeTests(SimpleTestCase):
    def test_get_proposal_outcome_reads_proposal_fields(self):
        self.assertEqual(get_proposal_outcome(_proposal('5', '4', '6', True)), ACCEPTED)
        self.assertEqual(get_proposal_outcome(_proposal('4', '5', '6', True)), REJECTED)
        self.assertEqual(get_proposal_outcome(_proposal('9', '1', '0.1', True)), NO_QUORUM)
        self.assertEqual(get_proposal_outcome(_proposal('9', '1', '0.1', False)), ACCEPTED)
        self.assertEqual(get_quorum_votes(_proposal('5', '4', '6', True)), Decimal('11'))
        self.assertEqual(get_quorum_votes(_proposal('5', '4', '6', False)), Decimal('15'))

    def test_get_proposal_outcome_rejects_non_simple(self):
        proposal = _proposal('5', '4', '6', True, is_simple_proposal=False)
        with self.assertRaises(ValueError):
            get_proposal_outcome(proposal)
        with self.assertRaises(ValueError):
            get_quorum_votes(proposal)
