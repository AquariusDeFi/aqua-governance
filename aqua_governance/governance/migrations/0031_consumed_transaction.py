from django.db import migrations, models
import django.db.models.deletion

from aqua_governance.governance.consumed_transaction_backfill import (
    backfill_consumed_transactions,
    normalize_proposal_hash_case,
)


class Migration(migrations.Migration):
    """Create the ConsumedTransaction ledger and reconstruct it from the source hash columns.

    Schema and data ship together, matching 0028 and 0029: there is then no committed window
    in which the table exists but is empty, and a failing backfill rolls the schema back too,
    leaving a state that can be fixed and retried instead of an orphaned empty table.

    Both RunPython bodies live in `governance.consumed_transaction_backfill` so that the
    mandatory post-deploy re-run executes exactly the same code as the migration.
    """

    dependencies = [
        ('governance', '0030_alter_proposal_percent_for_quorum'),
    ]

    operations = [
        migrations.CreateModel(
            name='ConsumedTransaction',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('transaction_hash', models.CharField(max_length=64, unique=True)),
                ('purpose', models.CharField(choices=[('CREATE', 'Proposal creation payment'), ('UPDATE', 'Proposal update payment'), ('SUBMIT', 'Proposal submit payment'), ('LEGACY', 'Backfilled: consumed before the ledger existed')], max_length=16)),
                ('payer', models.CharField(blank=True, max_length=56, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('proposal', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='consumed_transactions', to='governance.proposal')),
            ],
        ),
        migrations.AddField(
            model_name='proposal',
            name='payment_check_rejected_hash',
            field=models.CharField(blank=True, max_length=64, null=True),
        ),
        migrations.RunPython(
            normalize_proposal_hash_case,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.RunPython(
            backfill_consumed_transactions,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
