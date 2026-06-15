from datetime import timedelta, timezone as datetime_timezone

import django.db.models.deletion
from django.db import migrations, models
from django.utils import timezone


ASSET_PROPOSAL_TYPES = ('ADD_ASSET', 'REMOVE_ASSET')
ELIGIBLE_STATUSES = ('DISCUSSION', 'VOTING')
UTC = datetime_timezone.utc
QUEUE_SLOT_DURATION = timedelta(days=7)
QUEUE_SLOT_END_INCLUSIVE_OFFSET = timedelta(seconds=1)


def _as_utc(value, *, proposal_id, field_name):
    if value is None or timezone.is_naive(value):
        raise ValueError(
            f'Proposal {proposal_id} has invalid {field_name}: expected a timezone-aware datetime.'
        )
    return value.astimezone(UTC)


def _is_utc_monday_start(value):
    return (
        value.weekday() == 0
        and value.hour == 0
        and value.minute == 0
        and value.second == 0
        and value.microsecond == 0
    )


def _has_exact_weekly_range(start_at, end_at):
    return (
        _is_utc_monday_start(start_at)
        and end_at == start_at + QUEUE_SLOT_DURATION - QUEUE_SLOT_END_INCLUSIVE_OFFSET
    )


def _has_legacy_monday_weekly_range(start_at, end_at):
    return start_at.weekday() == 0 and end_at == start_at + QUEUE_SLOT_DURATION


def _normalize_legacy_monday_weekly_range(start_at):
    normalized_start_at = start_at.replace(hour=0, minute=0, second=0, microsecond=0)
    normalized_end_at = normalized_start_at + QUEUE_SLOT_DURATION - QUEUE_SLOT_END_INCLUSIVE_OFFSET
    return normalized_start_at, normalized_end_at


def _validate_candidate_window(proposal, now):
    start_at = _as_utc(proposal.start_at, proposal_id=proposal.id, field_name='start_at')
    end_at = _as_utc(proposal.end_at, proposal_id=proposal.id, field_name='end_at')

    if end_at <= start_at:
        raise ValueError(f'Proposal {proposal.id} has invalid queue slot range: end_at must be after start_at.')

    if _has_exact_weekly_range(start_at, end_at):
        normalized_start_at = start_at
        normalized_end_at = end_at
    elif _has_legacy_monday_weekly_range(start_at, end_at):
        normalized_start_at, normalized_end_at = _normalize_legacy_monday_weekly_range(start_at)
    else:
        raise ValueError(
            f'Proposal {proposal.id} has invalid queue slot range: '
            'expected Monday 00:00:00 UTC -> Sunday 23:59:59 UTC, '
            'or a legacy Monday-based exact 7-day window that can be normalized.'
        )

    if proposal.proposal_status == 'DISCUSSION' and normalized_start_at <= now:
        raise ValueError(
            f'Proposal {proposal.id} is DISCUSSION but its scheduled slot already started.'
        )

    if proposal.proposal_status == 'VOTING' and not (normalized_start_at <= now <= normalized_end_at):
        raise ValueError(
            f'Proposal {proposal.id} is VOTING but is not active at migration time.'
        )

    return normalized_start_at, normalized_end_at


def backfill_proposal_queue_slots(apps, schema_editor):
    Proposal = apps.get_model('governance', 'Proposal')
    ProposalQueueSlot = apps.get_model('governance', 'ProposalQueueSlot')

    now = timezone.now().astimezone(UTC)
    candidates = list(
        Proposal.objects.filter(
            proposal_type__in=ASSET_PROPOSAL_TYPES,
            hide=False,
            draft=False,
            payment_status='FINE',
            proposal_status__in=ELIGIBLE_STATUSES,
            start_at__isnull=False,
            end_at__isnull=False,
            end_at__gte=now,
        ).order_by('start_at', 'id')
    )

    validated_candidates = []
    previous_proposal = None
    previous_start_at = None
    previous_end_at = None

    for proposal in candidates:
        start_at, end_at = _validate_candidate_window(proposal, now)

        if previous_proposal is not None and start_at <= previous_end_at:
            assert previous_start_at is not None
            assert previous_end_at is not None
            raise ValueError(
                'Eligible asset proposal queue slots overlap or duplicate: '
                f'proposal {previous_proposal.id} ({previous_start_at.isoformat()} -> {previous_end_at.isoformat()}) '
                f'conflicts with proposal {proposal.id} ({start_at.isoformat()} -> {end_at.isoformat()}).'
            )

        validated_candidates.append((proposal, start_at, end_at))
        previous_proposal = proposal
        previous_start_at = start_at
        previous_end_at = end_at

    ProposalQueueSlot.objects.bulk_create([
        ProposalQueueSlot(
            proposal_id=proposal.id,
            start_at=start_at,
            end_at=end_at,
            occupied_at=now,
        )
        for proposal, start_at, end_at in validated_candidates
    ])

    for proposal, start_at, end_at in validated_candidates:
        Proposal.objects.filter(id=proposal.id).update(
            start_at=start_at,
            end_at=end_at,
            proposal_status='QUEUED' if proposal.proposal_status == 'DISCUSSION' else proposal.proposal_status,
        )


class Migration(migrations.Migration):

    dependencies = [
        ('governance', '0028_asset_token_and_proposal_fk'),
    ]

    operations = [
        migrations.AlterField(
            model_name='proposal',
            name='proposal_status',
            field=models.CharField(
                choices=[
                    ('DISCUSSION', 'Proposal under discussion'),
                    ('QUEUED', 'Proposal queued for voting'),
                    ('VOTING', 'Proposal under voting'),
                    ('VOTED', 'Voted'),
                    ('EXPIRED', 'Expired'),
                ],
                default='DISCUSSION',
                max_length=64,
            ),
        ),
        migrations.CreateModel(
            name='ProposalQueueSlot',
            fields=[
                ('start_at', models.DateTimeField(unique=True)),
                ('end_at', models.DateTimeField()),
                ('occupied_at', models.DateTimeField(auto_now_add=True)),
                ('proposal', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, primary_key=True, related_name='queue_slot', serialize=False, to='governance.proposal')),
            ],
            options={
                'ordering': ['start_at', 'proposal_id'],
            },
        ),
        migrations.RunPython(
            backfill_proposal_queue_slots,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
