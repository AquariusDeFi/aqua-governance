"""Resolved proposal transitions, as pure value objects.

A transition is *what the backend is about to apply*, resolved once, before Horizon is
asked anything.  Everything downstream - the memo the payment must carry, the fingerprint
the locked row is re-checked against, and the values written on promotion - reads from the
same object, so the memo and the write can never disagree about which transition was paid
for.

The module imports no model, which is what keeps it unit-testable without a database and,
more importantly, keeps the staged ``new_*`` columns out of the promotion path: a caller
that re-reads them after verification has re-opened the race the fingerprint guard closes.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from aqua_governance.utils.memo import (
    PURPOSE_CREATE,
    PURPOSE_SUBMIT,
    PURPOSE_UPDATE,
    MemoPayloadError,
    build_memo_expectation,
    iso_utc_seconds,
)


UNREPRESENTABLE_DATETIME = '<unrepresentable>'


def _iso_or_none(value):
    """Fingerprint spelling of an instant, through the memo's own normalisation.

    Comparing datetimes the way the memo renders them is what stops the fingerprint and the
    memo disagreeing about what "this window" means.  A value the memo cannot render - a
    naive or sub-second datetime reachable only through the admin - degrades to a sentinel
    carrying the value rather than raising, so one hand-edited row cannot abort a sweep.
    The sentinel keeps the comparison honest in both directions: two spellings of the same
    unrenderable instant still match, two different ones still do not.
    """
    if value is None:
        return None

    try:
        return iso_utc_seconds(value)
    except MemoPayloadError:
        return '{0}:{1}'.format(UNREPRESENTABLE_DATETIME, value)


@dataclass(frozen=True)
class CreateTransition:
    """A pending proposal creation, paid for by ``transaction_hash``."""

    transaction_hash: Optional[str]
    proposed_by: Optional[str]
    proposal_type: Optional[str]
    title: Optional[str]
    text_html: Optional[str]

    @classmethod
    def from_proposal(cls, proposal):
        """Resolve from the row itself: a creation has no staged copy to diverge from."""
        return cls(
            transaction_hash=proposal.transaction_hash,
            proposed_by=proposal.proposed_by,
            proposal_type=proposal.proposal_type,
            title=proposal.title,
            text_html=proposal.text.html,
        )

    def memo_expectation(self):
        return build_memo_expectation(
            PURPOSE_CREATE,
            proposed_by=self.proposed_by,
            proposal_type=self.proposal_type,
            title=self.title,
            text_html=self.text_html,
        )

    def fingerprint(self):
        return (self.transaction_hash, self.proposed_by, self.proposal_type, self.title, self.text_html)

    def matches_locked(self, locked):
        return self.fingerprint() == CreateTransition.from_proposal(locked).fingerprint()


@dataclass(frozen=True)
class UpdateTransition:
    """A pending content update: a new title and text, paid for by a new hash."""

    proposal_id: Optional[int]
    transaction_hash: Optional[str]
    title: Optional[str]
    text_html: Optional[str]
    envelope_xdr: Optional[str]

    @classmethod
    def resolve(cls, proposal):
        return cls(
            proposal_id=proposal.id,
            transaction_hash=proposal.new_transaction_hash,
            title=proposal.new_title,
            text_html=proposal.new_text.html,
            envelope_xdr=proposal.new_envelope_xdr,
        )

    def memo_expectation(self):
        return build_memo_expectation(
            PURPOSE_UPDATE,
            proposal_id=self.proposal_id,
            title=self.title,
            text_html=self.text_html,
        )

    def fingerprint(self):
        return (self.transaction_hash, self.title, self.text_html)

    def matches_staged(self, locked):
        """True while the row still stages exactly what was verified.

        ``django_quill`` hands back an empty ``FieldQuill`` for a NULL column rather than
        ``None``, so this deterministically fails to match after a promotion cleared
        ``new_text`` instead of raising.
        """
        return self.fingerprint() == (locked.new_transaction_hash, locked.new_title, locked.new_text.html)


@dataclass(frozen=True)
class SubmitTransition:
    """A pending publication: a voting window, paid for by a new hash.

    ``legacy_text_html`` is the *current* proposal text, because the pre-v1 submit memo
    hashes the proposal's text rather than the window it books.  It is carried explicitly
    so the expectation never has to reach back into the row.
    """

    proposal_id: Optional[int]
    transaction_hash: Optional[str]
    start_at: Optional[datetime]
    end_at: Optional[datetime]
    envelope_xdr: Optional[str]
    legacy_text_html: Optional[str]

    @classmethod
    def resolve(cls, proposal):
        return cls(
            proposal_id=proposal.id,
            transaction_hash=proposal.new_transaction_hash,
            start_at=proposal.new_start_at,
            end_at=proposal.new_end_at,
            envelope_xdr=proposal.new_envelope_xdr,
            legacy_text_html=proposal.text.html,
        )

    def memo_expectation(self):
        return build_memo_expectation(
            PURPOSE_SUBMIT,
            proposal_id=self.proposal_id,
            start_at=self.start_at,
            end_at=self.end_at,
            legacy_text_html=self.legacy_text_html,
        )

    def fingerprint(self):
        return (self.transaction_hash, _iso_or_none(self.start_at), _iso_or_none(self.end_at))

    def matches_staged(self, locked):
        return self.fingerprint() == (
            locked.new_transaction_hash,
            _iso_or_none(locked.new_start_at),
            _iso_or_none(locked.new_end_at),
        )
