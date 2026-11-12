from django.db import migrations, models
from django.db.models import Q
from django.utils import timezone


def mark_completed_proposals_pooled(apps, schema_editor):
    # Proposals whose voting has ended stay on the pooled rule; every other row keeps the
    # separate side default.  A VOTING proposal past its end_at has ended as well: only
    # _finish_due_voting_proposals has not marked it VOTED yet.
    Proposal = apps.get_model('governance', 'Proposal')
    now = timezone.now()
    Proposal.objects.filter(
        Q(proposal_status='VOTED') | Q(proposal_status='VOTING', end_at__lte=now),
    ).update(separate_side_quorum=False)


class Migration(migrations.Migration):

    dependencies = [
        ('governance', '0032_proposal_payment_not_found_since'),
    ]

    operations = [
        migrations.AddField(
            model_name='proposal',
            name='separate_side_quorum',
            field=models.BooleanField(default=True),
        ),
        migrations.RunPython(
            mark_completed_proposals_pooled,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
