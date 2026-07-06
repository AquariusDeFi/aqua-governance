from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from django.db import IntegrityError, transaction
from django.db.models import F, Q

from aqua_governance.governance.models import Proposal, ProposalQueueSlot
from aqua_governance.governance.proposal_constants import (
    PROPOSAL_ACTION_NONE,
    QUEUE_OCCUPYING_PROPOSAL_STATUSES,
)
from aqua_governance.governance.proposal_queue import proposal_uses_queue_slot


@dataclass(frozen=True)
class QueueSlotConflict:
    proposal: Proposal
    slot: ProposalQueueSlot | None = None


def queue_occupancy_filters(*, prefix: str = '') -> dict[str, object]:
    return {
        f'{prefix}hide': False,
        f'{prefix}draft': False,
        f'{prefix}action': PROPOSAL_ACTION_NONE,
        f'{prefix}proposal_status__in': QUEUE_OCCUPYING_PROPOSAL_STATUSES,
    }


def true_queue_slot_occupancy_q() -> Q:
    return Q(
        start_at=F('proposal__start_at'),
        end_at=F('proposal__end_at'),
        **queue_occupancy_filters(prefix='proposal__'),
    )


def sync_proposal_queue_slot(proposal, *, create_missing: bool = True):
    slot_values = {
        'start_at': proposal.start_at,
        'end_at': proposal.end_at,
    }

    if proposal_uses_queue_slot(proposal):
        _delete_stale_queue_slots_for_start_at(
            proposal.start_at,
            exclude_proposal_id=proposal.id,
        )

        try:
            updated = _update_existing_queue_slot(proposal, slot_values)
        except IntegrityError:
            deleted_stale_rows = _delete_stale_queue_slots_for_start_at(
                proposal.start_at,
                exclude_proposal_id=proposal.id,
            )
            if not deleted_stale_rows:
                raise

            updated = _update_existing_queue_slot(proposal, slot_values)

        if updated or not create_missing:
            return

        _delete_stale_queue_slots_for_start_at(
            proposal.start_at,
            exclude_proposal_id=proposal.id,
        )

        try:
            with transaction.atomic():
                ProposalQueueSlot.objects.create(
                    proposal=proposal,
                    **slot_values,
                )
        except IntegrityError:
            deleted_stale_rows = _delete_stale_queue_slots_for_start_at(
                proposal.start_at,
                exclude_proposal_id=proposal.id,
            )
            if not deleted_stale_rows:
                raise

            with transaction.atomic():
                ProposalQueueSlot.objects.create(
                    proposal=proposal,
                    **slot_values,
                )
        return

    ProposalQueueSlot.objects.filter(proposal=proposal).delete()


def _delete_stale_queue_slots_for_start_at(
    start_at: datetime | None,
    *,
    exclude_proposal_id: int | None = None,
) -> int:
    if start_at is None:
        return 0

    stale_queryset = ProposalQueueSlot.objects.filter(start_at=start_at)
    if exclude_proposal_id is not None:
        stale_queryset = stale_queryset.exclude(proposal_id=exclude_proposal_id)

    stale_queryset = stale_queryset.exclude(true_queue_slot_occupancy_q())

    deleted_count, _ = stale_queryset.delete()
    return deleted_count


def _update_existing_queue_slot(proposal, slot_values: dict[str, datetime | None]) -> int:
    with transaction.atomic():
        return ProposalQueueSlot.objects.filter(proposal=proposal).update(**slot_values)


def is_queue_slot_available(
    start_at: datetime,
    end_at: datetime,
    *,
    exclude_proposal_id: int | None = None,
) -> bool:
    return find_queue_slot_conflict(
        start_at,
        end_at,
        exclude_proposal_id=exclude_proposal_id,
    ) is None


def find_queue_slot_conflict(
    start_at: datetime,
    end_at: datetime,
    *,
    exclude_proposal_id: int | None = None,
) -> QueueSlotConflict | None:
    # ProposalQueueSlot is the only queue-occupancy source of truth.
    # Legacy Proposal.start_at/end_at rows without a slot remain editable, but
    # they must not block booking because /api/proposal-queue/ cannot surface
    # them.
    queryset = ProposalQueueSlot.objects.filter(
        true_queue_slot_occupancy_q(),
        start_at__lt=end_at,
        end_at__gt=start_at,
    ).select_related('proposal')
    if exclude_proposal_id is not None:
        queryset = queryset.exclude(proposal_id=exclude_proposal_id)

    slot = queryset.order_by('start_at', 'proposal_id').first()
    if slot is not None:
        return QueueSlotConflict(proposal=slot.proposal, slot=slot)

    return None
