"""Outcome of a For/Against/Abstain proposal.

The leading side is For when For is strictly greater than Against, otherwise
Against, so a tie leads to Against.  The quorum votes are counted by one of two
rules:

* pooled: For + Against + Abstain;
* separate side: For + Abstain when For leads, Against + Abstain when Against
  leads.

Quorum is met when the quorum votes reach ``percent_for_quorum`` percent of the
supply, boundary included.  Without quorum the outcome is ``NO_QUORUM``; with it
the proposal is ``ACCEPTED`` when For leads and ``REJECTED`` when Against leads.
A missing or non-positive supply never meets quorum.
"""
from decimal import Decimal
from enum import Enum
from typing import Any


class ProposalOutcome(str, Enum):
    ACCEPTED = 'ACCEPTED'
    REJECTED = 'REJECTED'
    NO_QUORUM = 'NO_QUORUM'


class QuorumSide(str, Enum):
    FOR = 'FOR'
    AGAINST = 'AGAINST'


def as_decimal(value: Any) -> Decimal:
    if value is None:
        return Decimal('0')
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _leading_side(vote_for: Decimal, vote_against: Decimal) -> QuorumSide:
    if vote_for > vote_against:
        return QuorumSide.FOR
    return QuorumSide.AGAINST


def compute_quorum_side(vote_for, vote_against, separate_side_quorum: bool) -> QuorumSide | None:
    """Return the leading side, whose quorum decides the outcome, or ``None`` under the pooled rule."""
    if not separate_side_quorum:
        return None
    return _leading_side(as_decimal(vote_for), as_decimal(vote_against))


def compute_quorum_votes(vote_for, vote_against, vote_abstain, separate_side_quorum: bool) -> Decimal:
    vote_for = as_decimal(vote_for)
    vote_against = as_decimal(vote_against)
    vote_abstain = as_decimal(vote_abstain)
    quorum_side = compute_quorum_side(vote_for, vote_against, separate_side_quorum)
    if quorum_side is None:
        return vote_for + vote_against + vote_abstain
    if quorum_side == QuorumSide.FOR:
        return vote_for + vote_abstain
    return vote_against + vote_abstain


def compute_outcome(
    vote_for,
    vote_against,
    vote_abstain,
    supply,
    percent_for_quorum,
    separate_side_quorum: bool,
) -> ProposalOutcome:
    vote_for = as_decimal(vote_for)
    vote_against = as_decimal(vote_against)
    supply = as_decimal(supply)
    if supply <= 0:
        return ProposalOutcome.NO_QUORUM

    quorum_votes = compute_quorum_votes(vote_for, vote_against, vote_abstain, separate_side_quorum)
    # Cross-multiplied instead of divided, so the inclusive boundary is compared exactly.
    if quorum_votes * 100 < supply * as_decimal(percent_for_quorum):
        return ProposalOutcome.NO_QUORUM

    if _leading_side(vote_for, vote_against) == QuorumSide.FOR:
        return ProposalOutcome.ACCEPTED
    return ProposalOutcome.REJECTED


def get_quorum_votes(proposal) -> Decimal:
    _require_simple_proposal(proposal)
    return compute_quorum_votes(
        proposal.vote_for_result,
        proposal.vote_against_result,
        proposal.vote_abstain_result,
        proposal.separate_side_quorum,
    )


def get_proposal_outcome(proposal) -> ProposalOutcome:
    _require_simple_proposal(proposal)
    return compute_outcome(
        proposal.vote_for_result,
        proposal.vote_against_result,
        proposal.vote_abstain_result,
        proposal.ice_circulating_supply,
        proposal.percent_for_quorum,
        proposal.separate_side_quorum,
    )


def _require_simple_proposal(proposal) -> None:
    if not proposal.is_simple_proposal:
        raise ValueError(f'Proposal {proposal.id} is not a For/Against/Abstain proposal.')
