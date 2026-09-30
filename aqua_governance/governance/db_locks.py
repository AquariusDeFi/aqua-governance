from django.conf import settings
from django.db import connection


def _acquire_proposal_transition_lock(lock_id: int) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(%s)",
            [lock_id],
        )


def acquire_proposal_transition_lock() -> None:
    _acquire_proposal_transition_lock(settings.ASSET_PROPOSAL_TRANSITION_ADVISORY_LOCK_ID)


def _try_acquire_payment_sweep_lock() -> bool:
    """Take the session-level sweep lock, or report that another sweep already holds it.

    Session-level rather than transaction-scoped: the sweep must not hold a transaction
    open across its Horizon round-trips, so there is no transaction for an xact lock to
    live in.  The caller owns the matching release.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_try_advisory_lock(%s)",
            [settings.PROPOSAL_PAYMENT_SWEEP_ADVISORY_LOCK_ID],
        )
        return bool(cursor.fetchone()[0])


def _release_payment_sweep_lock() -> bool:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_unlock(%s)",
            [settings.PROPOSAL_PAYMENT_SWEEP_ADVISORY_LOCK_ID],
        )
        return bool(cursor.fetchone()[0])
