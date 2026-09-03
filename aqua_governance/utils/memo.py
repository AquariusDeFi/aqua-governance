"""Canonical governance payment memo.

The on-chain memo is ``MemoHash(sha256(payload))`` over one of three ``|``-separated
payload grammars::

    CREATE : AQUA-GOV|v1|CREATE|{proposed_by}|{proposal_type}|{sha256hex(title)}|{sha256hex(text_html)}
    UPDATE : AQUA-GOV|v1|UPDATE|{proposal_id}|{sha256hex(new_title)}|{sha256hex(new_text_html)}
    SUBMIT : AQUA-GOV|v1|SUBMIT|{proposal_id}|{start_at_iso}|{end_at_iso}

Every element is either a closed-vocabulary literal, a canonical decimal, a fixed-width
ISO-8601 instant or a fixed-width inner hash, so the separator needs no escaping.  The
framing is injective because this module refuses an element that could break it - a
literal carrying the separator, or a proposal id spelled non-canonically - rather than
because every caller happens to constrain its input.  The payload is the cross-language
contract a wallet or frontend has to reproduce byte for byte: UTF-8, no BOM, no Unicode
normalisation, no case folding, one U+007C between elements, no trailing separator, and
``YYYY-MM-DDTHH:MM:SSZ`` for instants.

The module is pure - no model imports - because the grammar is a wire format, not
application state.  Two consequences of that purity are load-bearing:

* the memo may commit only to values the backend never transforms between staging and
  promotion, which is why the asset triple is absent from CREATE (the backend derives and
  rewrites ``asset_contract_address`` after the client has hashed what it sent);
* every serializer field feeding a memo must be declared ``trim_whitespace=False``,
  since a value the backend trims is a value the client hashed untrimmed.

The ``AQUA-GOV|v1`` prefix is a rotation handle, not domain separation.  The legacy
preimage is caller-supplied ``text_html``, so a caller can make it byte-equal to a
canonical payload and collide the two digests; :meth:`MemoExpectation.accepted` therefore
offers the canonical digest first, and a payment that only reaches that collision through
the legacy branch is reported as :data:`MEMO_FORMAT_LEGACY`.
"""
import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Tuple

from django.conf import settings


MEMO_DOMAIN = 'AQUA-GOV'
MEMO_VERSION = 'v1'
MEMO_SEPARATOR = '|'
MEMO_PREFIX = MEMO_SEPARATOR.join((MEMO_DOMAIN, MEMO_VERSION))

PURPOSE_CREATE = 'CREATE'
PURPOSE_UPDATE = 'UPDATE'
PURPOSE_SUBMIT = 'SUBMIT'

MEMO_PURPOSES = (PURPOSE_CREATE, PURPOSE_UPDATE, PURPOSE_SUBMIT)

MEMO_FORMAT_CANONICAL = 'canonical'
MEMO_FORMAT_LEGACY = 'legacy'

ISO_UTC_SECONDS_FORMAT = '%Y-%m-%dT%H:%M:%SZ'


class MemoPayloadError(ValueError):
    """A memo digest cannot be built from the supplied fields."""


def _encoded(value, field_name):
    """UTF-8 bytes of a memo element, or ``MemoPayloadError``.

    ``QuillField.to_internal_value`` accepts any JSON scalar and ``Quill`` stores it
    verbatim, so a persisted ``text.html`` can be an int, a dict or a lone surrogate that
    round-trips through the database as its escaped form.
    """
    if not isinstance(value, str):
        raise MemoPayloadError('{} must be a string, got {}.'.format(field_name, type(value).__name__))

    try:
        return value.encode('utf-8')
    except UnicodeEncodeError as exc:
        raise MemoPayloadError('{} is not encodable as UTF-8.'.format(field_name)) from exc


def field_digest(value: str, field_name: str = 'field') -> str:
    """Lowercase hex sha256 of a free-form field."""
    return hashlib.sha256(_encoded(value, field_name)).hexdigest()


def iso_utc_seconds(value: datetime, field_name: str = 'datetime') -> str:
    """Second-precision UTC ISO-8601, always with a literal ``Z``.

    Sub-second values are refused rather than truncated: a truncating format would let two
    different instants share a memo.  The real submit path cannot produce one, because
    ``validate_weekly_queue_slot`` pins the window to a UTC Monday midnight and to
    ``start_at + 7d - 1s``.
    """
    if not isinstance(value, datetime):
        raise MemoPayloadError('{} must be a datetime, got {}.'.format(field_name, type(value).__name__))
    if value.tzinfo is None or value.utcoffset() is None:
        raise MemoPayloadError('{} must be timezone-aware.'.format(field_name))
    if value.microsecond:
        raise MemoPayloadError('{} must not carry sub-second precision.'.format(field_name))

    return value.astimezone(timezone.utc).strftime(ISO_UTC_SECONDS_FORMAT)


def _account_element(value, field_name='proposed_by'):
    """Normalised ``G…`` form of an account id; a muxed ``M…`` folds to its underlying account."""
    from aqua_governance.utils.payments import normalize_account_id

    try:
        return normalize_account_id(value)
    except ValueError as exc:
        raise MemoPayloadError('{} is not a valid account id.'.format(field_name)) from exc


def _proposal_id_element(value, field_name='proposal_id'):
    """Canonical decimal proposal id: no padding, no sign, no fractional part.

    Only an ``int`` or its own canonical decimal spelling is accepted.  Coercing ``'042'``
    or ``42.0`` to ``'42'`` would let the Python side hash a value a JS helper renders
    differently, and the divergence would only surface as ``BAD_MEMO`` after the user paid.
    """
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise MemoPayloadError('{} must be an integer.'.format(field_name))

    try:
        number = int(value)
    except ValueError as exc:
        raise MemoPayloadError('{} must be an integer.'.format(field_name)) from exc

    canonical = str(number)
    if isinstance(value, str) and value != canonical:
        raise MemoPayloadError('{} must be a canonical decimal integer.'.format(field_name))

    return canonical


def _literal_element(value, field_name):
    """A closed-vocabulary element, carried verbatim.

    The separator is unescaped, so the framing is injective only while no literal can carry
    one.  Enforcing that here rather than trusting each caller's ``choices`` keeps the
    guarantee inside the module that owns the grammar.
    """
    _encoded(value, field_name)
    if MEMO_SEPARATOR in value:
        raise MemoPayloadError('{} must not contain {!r}.'.format(field_name, MEMO_SEPARATOR))
    return value


def create_memo_payload(*, proposed_by, proposal_type, title, text_html) -> str:
    """CREATE preimage.

    Carries no asset fields by design: ``upsert_asset_token_from_proposal`` normalises the
    code and issuer and writes back a derived contract address, so a memo over the asset
    triple could never be reproduced at promotion time for a classic pair.  ``proposal_type``
    is client-supplied, validated against a closed set and never rewritten, and it alone
    carries the purpose separation the asset triple was justified by.
    """
    return MEMO_SEPARATOR.join((
        MEMO_PREFIX,
        PURPOSE_CREATE,
        _account_element(proposed_by),
        _literal_element(proposal_type, 'proposal_type'),
        field_digest(title, 'title'),
        field_digest(text_html, 'text_html'),
    ))


def update_memo_payload(*, proposal_id, new_title, new_text_html) -> str:
    """UPDATE preimage, over the staged values the transition will promote."""
    return MEMO_SEPARATOR.join((
        MEMO_PREFIX,
        PURPOSE_UPDATE,
        _proposal_id_element(proposal_id),
        field_digest(new_title, 'new_title'),
        field_digest(new_text_html, 'new_text_html'),
    ))


def submit_memo_payload(*, proposal_id, start_at, end_at) -> str:
    """SUBMIT preimage, over the voting window rather than the proposal content."""
    return MEMO_SEPARATOR.join((
        MEMO_PREFIX,
        PURPOSE_SUBMIT,
        _proposal_id_element(proposal_id),
        iso_utc_seconds(start_at, 'start_at'),
        iso_utc_seconds(end_at, 'end_at'),
    ))


def memo_digest(payload: str) -> bytes:
    """Raw 32-byte digest carried by ``MemoHash``."""
    return hashlib.sha256(_encoded(payload, 'payload')).digest()


def legacy_memo_digest(text_html) -> bytes:
    """Raw 32-byte digest of the pre-v1 memo, whose whole preimage is the proposal text.

    Total in the same way the canonical builders are: a non-string or non-encodable
    ``text.html`` is a :class:`MemoPayloadError`, never an escaping ``UnicodeEncodeError``.
    """
    return hashlib.sha256(_encoded(text_html, 'text_html')).digest()


@dataclass(frozen=True)
class MemoExpectation:
    """The digests a payment for one transition is allowed to carry."""

    purpose: str
    canonical_digest: Optional[bytes]
    canonical_payload: Optional[str]
    legacy_digest: Optional[bytes]
    canonical_error: Optional[str] = None
    legacy_error: Optional[str] = None

    def accepted(self) -> Tuple[Tuple[str, bytes], ...]:
        """Accepted ``(format, digest)`` pairs, canonical first.

        The ordering is load-bearing.  The legacy preimage is attacker-chosen free text, so
        a caller can make the two digests collide; trying canonical first keeps the
        reported format honest, which is what the legacy-usage telemetry reads.
        """
        accepted = []
        if self.canonical_digest is not None:
            accepted.append((MEMO_FORMAT_CANONICAL, self.canonical_digest))
        if self.legacy_digest is not None:
            accepted.append((MEMO_FORMAT_LEGACY, self.legacy_digest))

        return tuple(accepted)


def _canonical_payload(purpose, *, proposed_by, proposal_type, proposal_id, title, text_html, start_at, end_at):
    if purpose == PURPOSE_CREATE:
        return create_memo_payload(
            proposed_by=proposed_by,
            proposal_type=proposal_type,
            title=title,
            text_html=text_html,
        )
    if purpose == PURPOSE_UPDATE:
        return update_memo_payload(
            proposal_id=proposal_id,
            new_title=title,
            new_text_html=text_html,
        )

    return submit_memo_payload(proposal_id=proposal_id, start_at=start_at, end_at=end_at)


def _legacy_preimage(purpose, text_html, legacy_text_html):
    if legacy_text_html is not None:
        return legacy_text_html
    if purpose == PURPOSE_SUBMIT:
        raise MemoPayloadError(
            'legacy_text_html must be supplied for a SUBMIT expectation: the legacy submit '
            'memo hashes the current proposal text, not the voting window.'
        )

    return text_html


def build_memo_expectation(purpose, *, proposed_by=None, proposal_type=None,
                           proposal_id=None, title=None, text_html=None,
                           start_at=None, end_at=None,
                           legacy_text_html=None, accept_legacy=None) -> MemoExpectation:
    """Build both digests a transition's payment may carry.

    A field that cannot be represented degrades that half to ``None`` with the reason kept
    for logging, rather than raising: one hand-written admin row must not abort a whole
    sweep.  ``accept_legacy`` defaults to ``PROPOSAL_LEGACY_MEMO_ACCEPTED``; passing
    ``False`` drops the legacy digest for one expectation.
    """
    if purpose not in MEMO_PURPOSES:
        raise ValueError('Unknown memo purpose: {!r}'.format(purpose))

    if accept_legacy is None:
        accept_legacy = settings.PROPOSAL_LEGACY_MEMO_ACCEPTED

    canonical_payload = None
    canonical_digest = None
    canonical_error = None
    try:
        canonical_payload = _canonical_payload(
            purpose,
            proposed_by=proposed_by,
            proposal_type=proposal_type,
            proposal_id=proposal_id,
            title=title,
            text_html=text_html,
            start_at=start_at,
            end_at=end_at,
        )
        canonical_digest = memo_digest(canonical_payload)
    except MemoPayloadError as exc:
        canonical_payload = None
        canonical_error = str(exc)

    legacy_digest = None
    legacy_error = None
    if accept_legacy:
        try:
            legacy_digest = legacy_memo_digest(_legacy_preimage(purpose, text_html, legacy_text_html))
        except MemoPayloadError as exc:
            legacy_error = str(exc)

    return MemoExpectation(
        purpose=purpose,
        canonical_digest=canonical_digest,
        canonical_payload=canonical_payload,
        legacy_digest=legacy_digest,
        canonical_error=canonical_error,
        legacy_error=legacy_error,
    )


def match_memo(expectation: MemoExpectation, memo_bytes) -> Optional[str]:
    """The strongest accepted format the memo satisfies, or ``None``."""
    if not isinstance(memo_bytes, (bytes, bytearray)):
        return None

    candidate = bytes(memo_bytes)
    for memo_format, digest in expectation.accepted():
        if hmac.compare_digest(candidate, digest):
            return memo_format

    return None
