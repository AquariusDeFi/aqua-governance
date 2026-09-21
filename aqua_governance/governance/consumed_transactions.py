"""The claim side of the ConsumedTransaction ledger.

A payment authorises exactly one transition.  The mechanism is the unique index on
`ConsumedTransaction.transaction_hash`, chosen over any lock because it is the only one
that is cross-proposal (the replay being closed presents the same hash against a different
row, in a different code path), durable, per-hash for free, and correct at READ COMMITTED
with no retry loop.
"""
import logging

from django.db import IntegrityError, transaction

from aqua_governance.governance.exceptions import TransactionAlreadyConsumedError
from aqua_governance.governance.models import ConsumedTransaction
from aqua_governance.utils.payments import TRANSACTION_HASH_RE


logger = logging.getLogger(__name__)

STELLAR_ACCOUNT_ID_LENGTH = 56


def claim_transaction_hashes(*, transaction_hashes, proposal, purpose, payer=None):
    """Burn every hash in `transaction_hashes` so none can authorise another transition.

    MUST be the LAST statement of the database transaction that applies the transition,
    and must run only after every check has passed - a rollback of that transaction must
    release the hashes.  Nothing may be locked afterwards: the index tuple taken here is
    the last lock any of our transactions acquires, which is what keeps the lock order
    acyclic while the queue-slot step still touches other proposals' rows.

    Raises TransactionAlreadyConsumedError if any hash was already spent.  The caller's
    transaction stays usable after the raise, because the INSERT runs in a savepoint.
    """
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError('claim_transaction_hashes must run inside the applying transaction')

    canonical = []
    for value in transaction_hashes:
        value = (value or '').strip().lower()
        if not value:
            continue
        if not TRANSACTION_HASH_RE.match(value):
            logger.warning(
                'Claiming a transaction hash that is not 64 hexadecimal characters.',
                extra={'transaction_hash': value, 'proposal_id': getattr(proposal, 'id', None)},
            )
        if value not in canonical:
            canonical.append(value)

    if not canonical:
        raise ValueError('Cannot claim an empty transaction hash.')

    if payer and len(payer) != STELLAR_ACCOUNT_ID_LENGTH:
        # A muxed M... address is 69 characters and would raise DataError on a 56-char
        # column.  The payer is forensic metadata, so drop it rather than lose the claim.
        logger.warning(
            'Discarding a payer that is not a 56-character account id.',
            extra={'payer': payer, 'proposal_id': getattr(proposal, 'id', None)},
        )
        payer = None

    # A read cannot squat, so this is a pure fast path: it turns the common attack case
    # from a wait on the index tuple - held, on the submit path, while the caller also owns
    # the single app-wide advisory lock - into an immediate rejection.  The unique INSERT
    # below remains the authority.
    existing = ConsumedTransaction.objects.filter(transaction_hash__in=canonical).first()
    if existing is not None:
        raise TransactionAlreadyConsumedError(
            transaction_hash=existing.transaction_hash,
            existing=existing,
        )

    try:
        with transaction.atomic():
            return ConsumedTransaction.objects.bulk_create([
                ConsumedTransaction(
                    transaction_hash=value,
                    proposal=proposal,
                    purpose=purpose,
                    payer=payer,
                )
                for value in canonical
            ])
    except IntegrityError as exc:
        # The INSERT only raises once the conflicting transaction has committed, so the
        # re-read finds the winner; getattr covers the row vanishing in between.
        existing = ConsumedTransaction.objects.filter(transaction_hash__in=canonical).first()
        raise TransactionAlreadyConsumedError(
            transaction_hash=getattr(existing, 'transaction_hash', canonical[0]),
            existing=existing,
        ) from exc
