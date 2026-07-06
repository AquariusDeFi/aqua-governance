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
