"""Re-run the ConsumedTransaction backfill that `0031_consumed_transaction` applies.

The deploy runbook requires one run after the code deploy, because between `migrate` and
the deploy the old code is still promoting transitions - mostly on the web tier, which the
runbook does not quiesce - and those promotions write consumed hashes into
`Proposal.transaction_hash` with no ledger row behind them.  The command exists so that run
is a documented invocation rather than an `importlib` one-liner improvised against a module
whose name starts with a digit.
"""
from django.apps import apps
from django.core.management.base import BaseCommand

from aqua_governance.governance.consumed_transaction_backfill import (
    backfill_consumed_transactions,
    find_hash_case_collisions,
)


class Command(BaseCommand):
    help = 'Burn every proposal hash a transition has already spent into ConsumedTransaction.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Report what would be burned without writing any ledger row.',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']

        if dry_run:
            # The migration's normaliser is a hard stop, so the dry run is where an
            # operator gets to discover a case-only duplicate - days before the
            # maintenance window rather than with celery beat already paused.
            self._report_hash_case_collisions()

        report = backfill_consumed_transactions(apps, dry_run=dry_run)

        if dry_run:
            self.stdout.write('Dry run: no ledger row was written.')

        self.stdout.write('Source hashes read:      {0}'.format(report.source_hashes))
        self.stdout.write('{0} {1}'.format(
            'Ledger rows to create:  ' if dry_run else 'Ledger rows created:    ',
            report.rows_created,
        ))
        self.stdout.write('Rows already present:    {0}'.format(report.rows_pre_existing))
        self.stdout.write('In-flight TO_CREATE:     {0} (deliberately left unburned)'.format(
            report.in_flight_skipped,
        ))

    def _report_hash_case_collisions(self):
        collisions = find_hash_case_collisions(apps)
        if not collisions:
            self.stdout.write('Hash case check:         no column holds two rows that differ only in case.')
            return

        for column, groups in sorted(collisions.items()):
            for canonical, members in sorted(groups.items()):
                self.stdout.write(self.style.ERROR(
                    '{0} holds {1} rows differing only in case for {2}: {3}. '
                    'Migration 0031 will abort until a human resolves them.'.format(
                        column, len(members), canonical, sorted(row_id for row_id, _ in members),
                    ),
                ))
