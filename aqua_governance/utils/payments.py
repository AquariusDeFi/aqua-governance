"""Stellar payment verification for proposal transitions.

``verify_payment`` is the authoritative check.  It asks Horizon for a settled transaction
and confirms, together, that a single payment operation carries the AQUA asset to the
issuer for the exact expected amount, that the operation's own payer is the account the
transition is bound to, and that the memo commits to the transition being applied.

``inspect_envelope`` inspects an unsigned envelope handed over in a request body.  ``FINE``
from that function is a hint, never authorization: the envelope carries no signatures when
it is checked, and Horizon serves the signed envelope of any past transaction, so demanding
signatures would not make it one either.
"""
import base64
import logging
import os
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Tuple

from django.conf import settings

from stellar_sdk import Server, Payment, HashMemo, TransactionEnvelope, MuxedAccount
from stellar_sdk.exceptions import BaseRequestError, NotFoundError

from aqua_governance.governance import payment_statuses
from aqua_governance.utils.memo import MEMO_FORMAT_LEGACY, match_memo
from aqua_governance.utils.requests import load_all_records


logger = logging.getLogger(__name__)

TRANSACTION_HASH_RE = re.compile(r'\A[0-9a-fA-F]{64}\Z')

REASON_OK = 'ok'
REASON_BYPASS = 'bypass'
REASON_BAD_HASH_FORMAT = 'bad_hash_format'
REASON_HORIZON_UNAVAILABLE = 'horizon_unavailable'
REASON_HORIZON_EMPTY_OPERATIONS = 'horizon_empty_operations'
REASON_TRANSACTION_NOT_FOUND = 'transaction_not_found'
REASON_TRANSACTION_FAILED = 'transaction_failed'
REASON_NO_MATCHING_PAYMENT = 'no_matching_payment'
REASON_PAYER_MISMATCH = 'payer_mismatch'
REASON_MEMO_MISSING = 'memo_missing'
REASON_MEMO_TYPE_MISMATCH = 'memo_type_mismatch'
REASON_MEMO_MISMATCH = 'memo_mismatch'
REASON_MEMO_UNREPRESENTABLE = 'memo_unrepresentable'

# Closed vocabulary.  ``reason`` drives logs and metrics and is never serialised to a
# client, so a new member is a deliberate contract change rather than an implementation
# detail.
PAYMENT_CHECK_REASONS = (
    REASON_OK,
    REASON_BYPASS,
    REASON_BAD_HASH_FORMAT,
    REASON_HORIZON_UNAVAILABLE,
    REASON_HORIZON_EMPTY_OPERATIONS,
    REASON_TRANSACTION_NOT_FOUND,
    REASON_TRANSACTION_FAILED,
    REASON_NO_MATCHING_PAYMENT,
    REASON_PAYER_MISMATCH,
    REASON_MEMO_MISSING,
    REASON_MEMO_TYPE_MISMATCH,
    REASON_MEMO_MISMATCH,
    REASON_MEMO_UNREPRESENTABLE,
)

MEMO_TYPE_HASH = 'hash'
MEMO_HASH_LENGTH = 32


class InvalidTransactionHash(ValueError):
    """A transaction hash is not 64 hexadecimal characters."""


class HorizonUnavailableError(Exception):
    """Horizon could not answer.  The verdict is retryable and must never be terminal."""


class TransactionNotFoundError(Exception):
    """Horizon has no record of this transaction."""


@dataclass(frozen=True)
class PaymentCheckResult:
    """The verdict on one payment, plus everything the caller needs to act on it."""

    payment_status: str
    reason: str
    transaction_hash: Optional[str] = None
    resolved_hashes: Tuple[str, ...] = ()
    payer: Optional[str] = None
    amount: Optional[Decimal] = None
    memo_format: Optional[str] = None


def normalize_account_id(value):
    """Fold an account id to its ``G…`` form; a muxed ``M…`` resolves to its underlying account.

    Raises ``ValueError`` for anything that is not an account id, a non-string included, so
    a caller comparing payers never sees an ``AttributeError`` from the SDK.
    """
    if not isinstance(value, str):
        raise ValueError('Account id must be a string, got {}.'.format(type(value).__name__))

    return MuxedAccount.from_account(value).account_id


def is_dev_payment_bypass_enabled():
    if not settings.DEBUG:
        return False

    return os.getenv('PROPOSAL_PAYMENT_BYPASS', '').lower() in {'1', 'true', 'yes', 'on'}


def is_valid_transaction_hash(value):
    """True for a 64-character hexadecimal transaction hash, in either case."""
    return isinstance(value, str) and TRANSACTION_HASH_RE.match(value) is not None


def normalize_transaction_hash(value):
    """Lowercase a transaction hash, or raise :class:`InvalidTransactionHash`.

    Lowercasing is load-bearing rather than cosmetic: uniqueness on a PostgreSQL
    ``varchar`` is case-sensitive, so an unnormalised hash could spend one payment twice
    by flipping the case of a single hex digit.
    """
    if not is_valid_transaction_hash(value):
        raise InvalidTransactionHash('Not a transaction hash: {!r}'.format(value))

    return value.lower()


def _to_decimal(value):
    """Exact decimal for an amount Horizon renders as a string.

    Raises ``ValueError`` for anything that is not a finite number, so a garbled amount is
    a rejected payment rather than an amount that silently compares unequal forever.
    """
    try:
        amount = Decimal(str(value))
    except (ArithmeticError, ValueError) as exc:
        raise ValueError('Not a decimal amount: {!r}'.format(value)) from exc

    if not amount.is_finite():
        raise ValueError('Not a finite amount: {!r}'.format(value))

    return amount


def _resolve_payment_amount(payment_amount):
    """The expected amount, resolved at call time so an override reaches the comparison."""
    if payment_amount is None:
        return _to_decimal(settings.PROPOSAL_COST)

    return _to_decimal(payment_amount)


def _fetch_transaction(horizon_server, transaction_hash):
    try:
        return horizon_server.transactions().transaction(transaction_hash).call()
    except NotFoundError as exc:
        raise TransactionNotFoundError(transaction_hash) from exc
    except (BaseRequestError, ValueError, KeyError, TypeError) as exc:
        raise HorizonUnavailableError(str(exc)) from exc


def _resolved_hashes(requested, transaction_info):
    """Every hash under which Horizon resolves this payment.

    A fee-bumped payment answers to both its outer and its inner hash and reports the same
    operation for either, so single-use enforcement has to burn the whole set at once.
    """
    candidates = {requested}
    for value in (
        transaction_info.get('hash'),
        (transaction_info.get('inner_transaction') or {}).get('hash'),
        (transaction_info.get('fee_bump_transaction') or {}).get('hash'),
    ):
        if isinstance(value, str) and TRANSACTION_HASH_RE.match(value):
            candidates.add(value.lower())

    return tuple(sorted(candidates))


def _find_matching_payment_operation(horizon_server, transaction_hash, *,
                                     expected_payer, payment_amount):
    """Locate the AQUA burn that pays for this transition.

    Returns ``(operation, reason)`` with reason in ``{'ok', 'no_matching_payment',
    'payer_mismatch', 'horizon_empty_operations'}``.  The amount must be met by a single
    operation; amounts across operations are never summed.
    """
    saw_payment_to_issuer = False
    saw_any_record = False
    try:
        for operation in load_all_records(
                horizon_server.operations().for_transaction(transaction_hash)):
            saw_any_record = True
            if operation.get('type') != 'payment':
                continue
            if operation.get('transaction_successful') is False:
                continue
            if operation.get('asset_code') != settings.AQUA_ASSET_CODE:
                continue
            if operation.get('asset_issuer') != settings.AQUA_ASSET_ISSUER:
                continue
            if operation.get('to') != settings.AQUA_ASSET_ISSUER:
                continue
            try:
                amount = _to_decimal(operation.get('amount'))
            except ValueError:
                continue
            if amount != payment_amount:
                continue

            saw_payment_to_issuer = True
            try:
                payer = normalize_account_id(operation.get('from'))
            except ValueError:
                continue
            if payer == expected_payer:
                return operation, REASON_OK
    except NotFoundError as exc:
        raise TransactionNotFoundError(transaction_hash) from exc
    except (BaseRequestError, ValueError, KeyError, TypeError) as exc:
        raise HorizonUnavailableError(str(exc)) from exc

    if not saw_any_record:
        return None, REASON_HORIZON_EMPTY_OPERATIONS

    return None, REASON_PAYER_MISMATCH if saw_payment_to_issuer else REASON_NO_MATCHING_PAYMENT


def _match_transaction_memo(transaction_info, memo_expectation):
    """Returns ``(reason, memo_format)``; ``memo_format`` is ``None`` unless the memo matched."""
    memo = transaction_info.get('memo')
    if not memo:
        return REASON_MEMO_MISSING, None

    if transaction_info.get('memo_type') != MEMO_TYPE_HASH:
        return REASON_MEMO_TYPE_MISMATCH, None

    try:
        memo_bytes = base64.b64decode(memo, validate=True)
    except (ValueError, TypeError):
        return REASON_MEMO_TYPE_MISMATCH, None

    if len(memo_bytes) != MEMO_HASH_LENGTH:
        return REASON_MEMO_TYPE_MISMATCH, None

    if not memo_expectation.accepted():
        return REASON_MEMO_UNREPRESENTABLE, None

    memo_format = match_memo(memo_expectation, memo_bytes)
    if memo_format is None:
        return REASON_MEMO_MISMATCH, None

    return REASON_OK, memo_format


def verify_payment(*, transaction_hash, expected_payer, memo_expectation,
                   payment_amount=None, horizon_server=None, log_context=None):
    """Confirm on Horizon that a payment authorizes one proposal transition.

    ``expected_payer`` has no default: a call site that forgets the payer binding fails on
    its first request rather than silently accepting anyone's payment.  The payment is
    checked before the memo, so a transaction that is wrong in both ways reports the
    payment verdict, as it always has.
    """
    if is_dev_payment_bypass_enabled():
        return PaymentCheckResult(payment_status=payment_statuses.FINE, reason=REASON_BYPASS)

    log_extra = dict(log_context or {})

    try:
        canonical_hash = normalize_transaction_hash(transaction_hash)
    except InvalidTransactionHash:
        logger.error(
            'Payment verification was asked about a malformed transaction hash.',
            extra=dict(log_extra, transaction_hash=transaction_hash),
        )
        return PaymentCheckResult(
            payment_status=payment_statuses.INVALID_PAYMENT,
            reason=REASON_BAD_HASH_FORMAT,
        )

    log_extra['transaction_hash'] = canonical_hash

    try:
        expected_payer = normalize_account_id(expected_payer)
    except ValueError:
        logger.error(
            'Payment verification was asked to bind a payment to an unusable account.',
            extra=dict(log_extra, expected_payer=expected_payer),
        )
        return PaymentCheckResult(
            payment_status=payment_statuses.INVALID_PAYMENT,
            reason=REASON_PAYER_MISMATCH,
            transaction_hash=canonical_hash,
        )

    payment_amount = _resolve_payment_amount(payment_amount)

    if horizon_server is None:
        horizon_server = Server(settings.HORIZON_URL)

    try:
        transaction_info = _fetch_transaction(horizon_server, canonical_hash)
    except TransactionNotFoundError:
        return PaymentCheckResult(
            payment_status=payment_statuses.HORIZON_ERROR,
            reason=REASON_TRANSACTION_NOT_FOUND,
            transaction_hash=canonical_hash,
        )
    except HorizonUnavailableError as exc:
        logger.warning(
            'Horizon could not be reached for a transaction lookup.',
            extra=dict(log_extra, error=str(exc)),
        )
        return PaymentCheckResult(
            payment_status=payment_statuses.HORIZON_ERROR,
            reason=REASON_HORIZON_UNAVAILABLE,
            transaction_hash=canonical_hash,
        )

    if not transaction_info.get('successful'):
        return PaymentCheckResult(
            payment_status=payment_statuses.FAILED_TRANSACTION,
            reason=REASON_TRANSACTION_FAILED,
            transaction_hash=canonical_hash,
        )

    try:
        operation, reason = _find_matching_payment_operation(
            horizon_server,
            canonical_hash,
            expected_payer=expected_payer,
            payment_amount=payment_amount,
        )
    except TransactionNotFoundError:
        return PaymentCheckResult(
            payment_status=payment_statuses.HORIZON_ERROR,
            reason=REASON_TRANSACTION_NOT_FOUND,
            transaction_hash=canonical_hash,
        )
    except HorizonUnavailableError as exc:
        logger.warning(
            'Horizon could not be reached for an operation scan.',
            extra=dict(log_extra, error=str(exc)),
        )
        return PaymentCheckResult(
            payment_status=payment_statuses.HORIZON_ERROR,
            reason=REASON_HORIZON_UNAVAILABLE,
            transaction_hash=canonical_hash,
        )

    if operation is None:
        if reason == REASON_HORIZON_EMPTY_OPERATIONS:
            # A transaction Horizon reports as successful always carries an operation, so
            # an empty page is an infrastructure answer and not a payment verdict.
            logger.warning(
                'Horizon returned no operations for a successful transaction.',
                extra=log_extra,
            )
            return PaymentCheckResult(
                payment_status=payment_statuses.HORIZON_ERROR,
                reason=REASON_HORIZON_EMPTY_OPERATIONS,
                transaction_hash=canonical_hash,
            )

        logger.warning(
            'No payment operation authorizes this transition.',
            extra=dict(log_extra, reason=reason, expected_payer=expected_payer),
        )
        return PaymentCheckResult(
            payment_status=payment_statuses.INVALID_PAYMENT,
            reason=reason,
            transaction_hash=canonical_hash,
        )

    memo_reason, memo_format = _match_transaction_memo(transaction_info, memo_expectation)
    if memo_reason != REASON_OK:
        logger.warning(
            'Payment memo does not commit to this transition.',
            extra=dict(log_extra, reason=memo_reason, purpose=memo_expectation.purpose),
        )
        return PaymentCheckResult(
            payment_status=payment_statuses.BAD_MEMO,
            reason=memo_reason,
            transaction_hash=canonical_hash,
        )

    if memo_format == MEMO_FORMAT_LEGACY:
        # The v3 go/no-go instrument, and the only place the beat sweep - which produces
        # no HTTP response - is observable.
        logger.warning(
            'Payment accepted under the legacy memo format.',
            extra=dict(log_extra, purpose=memo_expectation.purpose),
        )

    return PaymentCheckResult(
        payment_status=payment_statuses.FINE,
        reason=REASON_OK,
        transaction_hash=canonical_hash,
        resolved_hashes=_resolved_hashes(canonical_hash, transaction_info),
        payer=normalize_account_id(operation.get('from')),
        amount=payment_amount,
        memo_format=memo_format,
    )


def _envelope_payment_operation(transaction_envelope, *, expected_payer, payment_amount):
    """The AQUA burn inside an envelope that would pay ``expected_payer``'s way."""
    transaction = transaction_envelope.transaction
    for operation in transaction.operations:
        if not isinstance(operation, Payment):
            continue
        if operation.asset.code != settings.AQUA_ASSET_CODE:
            continue
        if operation.asset.issuer != settings.AQUA_ASSET_ISSUER:
            continue
        if operation.destination.account_id != settings.AQUA_ASSET_ISSUER:
            continue
        try:
            amount = _to_decimal(operation.amount)
        except ValueError:
            continue
        if amount != payment_amount:
            continue

        source = operation.source or transaction.source
        if getattr(source, 'account_id', None) == expected_payer:
            return operation

    return None


def inspect_envelope(*, envelope_xdr, expected_payer, memo_expectation, payment_amount=None):
    """Advisory read of an unsigned envelope, for a client that has not signed yet.

    ``FINE`` from this function is a hint, never authorization - only ``verify_payment``
    against a settled Horizon record authorizes a transition.
    """
    if is_dev_payment_bypass_enabled():
        return payment_statuses.FINE

    try:
        transaction_envelope = TransactionEnvelope.from_xdr(envelope_xdr, settings.NETWORK_PASSPHRASE)
    except (AttributeError, EOFError, TypeError, ValueError):
        # Horizon is not involved in decoding an envelope, so a bad envelope is an invalid
        # payment and not a retryable outage.  Fee-bump envelopes land here too.
        return payment_statuses.INVALID_PAYMENT

    try:
        expected_payer = normalize_account_id(expected_payer)
    except ValueError:
        return payment_statuses.INVALID_PAYMENT

    operation = _envelope_payment_operation(
        transaction_envelope,
        expected_payer=expected_payer,
        payment_amount=_resolve_payment_amount(payment_amount),
    )
    if operation is None:
        return payment_statuses.INVALID_PAYMENT

    memo = transaction_envelope.transaction.memo
    if not isinstance(memo, HashMemo):
        return payment_statuses.BAD_MEMO
    if match_memo(memo_expectation, memo.memo_hash) is None:
        return payment_statuses.BAD_MEMO

    return payment_statuses.FINE
