import json
from datetime import datetime
from datetime import timezone as datetime_timezone

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

from django_quill.quill import Quill

from aqua_governance.governance.consumed_transaction_backfill import backfill_consumed_transactions
from aqua_governance.governance.tests._factories import (
    DEFAULT_PROPOSED_BY,
    QUATERNARY_ACCOUNT,
    SECONDARY_ACCOUNT,
    TERTIARY_ACCOUNT,
    distinct_hash,
)
from aqua_governance.governance.tests._migrations import RestoresMigrationLeaf


UTC = datetime_timezone.utc
CREATED_AT = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)
MIXED_CASE_HASH = 'AbCd' * 16
LOWERCASE_HASH = 'abcd' * 16


class ConsumedTransactionBackfillMigrationTests(RestoresMigrationLeaf, TransactionTestCase):
    migrate_from = [('governance', '0030_alter_proposal_percent_for_quorum')]
    migrate_to = [('governance', '0031_consumed_transaction')]

    def setUp(self):
        super().setUp()
        self.executor = MigrationExecutor(connection)
        self.executor.migrate(self.migrate_from)
        self.apps_0030 = self.executor.loader.project_state(self.migrate_from).apps

    def _create_proposal(self, **overrides):
        Proposal = self.apps_0030.get_model('governance', 'Proposal')
        defaults = {
            'proposed_by': DEFAULT_PROPOSED_BY,
            'title': 'Backfill proposal',
            'text': Quill(json.dumps({'delta': {'ops': []}, 'html': '<p>x</p>'})),
            # The auto-keypair logic lives in Proposal.save(), which the historical model
            # does not have, so the issuers have to be supplied explicitly.
            'vote_for_issuer': TERTIARY_ACCOUNT,
            'vote_against_issuer': QUATERNARY_ACCOUNT,
            'action': 'NONE',
            'proposal_type': 'GENERAL',
        }
        defaults.update(overrides)
        return Proposal.objects.create(**defaults)

    def _create_history_proposal(self, **overrides):
        HistoryProposal = self.apps_0030.get_model('governance', 'HistoryProposal')
        defaults = {
            'version': 1,
            'title': 'Backfill history',
            'text': Quill(json.dumps({'delta': {'ops': []}, 'html': '<p>old</p>'})),
            'created_at': CREATED_AT,
        }
        defaults.update(overrides)
        return HistoryProposal.objects.create(**defaults)

    def _migrate_forward(self):
        self.executor = MigrationExecutor(connection)
        self.executor.migrate(self.migrate_to)
        return self.executor.loader.project_state(self.migrate_to).apps

    def _migrate_backward(self):
        self.executor = MigrationExecutor(connection)
        self.executor.migrate(self.migrate_from)
        return self.executor.loader.project_state(self.migrate_from).apps

    def _claims(self, apps):
        ConsumedTransaction = apps.get_model('governance', 'ConsumedTransaction')
        return {
            row.transaction_hash: row
            for row in ConsumedTransaction.objects.all()
        }

    def test_forward_burns_confirmed_hashes_and_never_the_staged_ones(self):
        settled = self._create_proposal(
            title='Settled',
            action='NONE',
            transaction_hash=distinct_hash(1),
            new_transaction_hash=distinct_hash(2),
        )
        pending_update = self._create_proposal(
            title='Pending update',
            action='TO_UPDATE',
            transaction_hash=distinct_hash(3),
            new_transaction_hash=distinct_hash(4),
        )
        pending_submit = self._create_proposal(
            title='Pending submit',
            action='TO_SUBMIT',
            transaction_hash=distinct_hash(5),
            new_transaction_hash=distinct_hash(6),
        )

        claims = self._claims(self._migrate_forward())

        self.assertEqual(
            sorted(claims),
            sorted([distinct_hash(1), distinct_hash(3), distinct_hash(5)]),
        )
        self.assertEqual(claims[distinct_hash(1)].proposal_id, settled.id)
        self.assertEqual(claims[distinct_hash(3)].proposal_id, pending_update.id)
        self.assertEqual(claims[distinct_hash(5)].proposal_id, pending_submit.id)
        self.assertEqual(
            {row.purpose for row in claims.values()},
            {'LEGACY'},
        )
        self.assertEqual({row.payer for row in claims.values()}, {None})

    def test_forward_skips_an_in_flight_create_entirely(self):
        in_flight = self._create_proposal(
            title='In flight create',
            action='TO_CREATE',
            transaction_hash=distinct_hash(1),
        )

        apps_0031 = self._migrate_forward()

        # Burning this hash would make the creation permanently unconfirmable and the
        # payment unrecoverable through the API.
        self.assertEqual(self._claims(apps_0031), {})
        Proposal = apps_0031.get_model('governance', 'Proposal')
        self.assertEqual(Proposal.objects.get(id=in_flight.id).transaction_hash, distinct_hash(1))

    def test_forward_backfills_history_and_attributes_it_to_its_proposal(self):
        proposal = self._create_proposal(title='With history', transaction_hash=distinct_hash(1))
        self._create_history_proposal(proposal=proposal, transaction_hash=distinct_hash(2))
        self._create_history_proposal(proposal=None, transaction_hash=distinct_hash(3))

        claims = self._claims(self._migrate_forward())

        self.assertEqual(claims[distinct_hash(2)].proposal_id, proposal.id)
        self.assertIsNone(claims[distinct_hash(3)].proposal_id)
        self.assertEqual(claims[distinct_hash(3)].purpose, 'LEGACY')

    def test_forward_ignores_null_empty_and_whitespace_hashes(self):
        self._create_proposal(title='Null hash', transaction_hash=None)
        self._create_proposal(title='Empty hash', transaction_hash='')
        self._create_proposal(title='Blank hash', transaction_hash='   ')
        self._create_history_proposal(transaction_hash=None)
        self._create_history_proposal(transaction_hash='')

        self.assertEqual(self._claims(self._migrate_forward()), {})

    def test_forward_lowercases_the_source_columns_and_the_ledger(self):
        proposal = self._create_proposal(
            title='Mixed case',
            action='TO_UPDATE',
            transaction_hash=MIXED_CASE_HASH,
            new_transaction_hash=distinct_hash(2).upper(),
        )
        history = self._create_history_proposal(proposal=proposal, transaction_hash=distinct_hash(3))

        apps_0031 = self._migrate_forward()
        Proposal = apps_0031.get_model('governance', 'Proposal')
        HistoryProposal = apps_0031.get_model('governance', 'HistoryProposal')

        self.assertIn(LOWERCASE_HASH, self._claims(apps_0031))
        migrated = Proposal.objects.get(id=proposal.id)
        self.assertEqual(migrated.transaction_hash, LOWERCASE_HASH)
        self.assertEqual(migrated.new_transaction_hash, distinct_hash(2))
        self.assertEqual(
            HistoryProposal.objects.get(id=history.id).transaction_hash,
            distinct_hash(3),
        )

    def test_forward_hard_stops_on_a_case_only_duplicate(self):
        first = self._create_proposal(title='Upper', transaction_hash=MIXED_CASE_HASH)
        second = self._create_proposal(title='Lower', transaction_hash=LOWERCASE_HASH)

        with self.assertRaises(RuntimeError) as caught:
            self._migrate_forward()

        message = str(caught.exception)
        self.assertIn('differ only in letter case', message)
        self.assertIn(str(first.id), message)
        self.assertIn(str(second.id), message)

        # The whole migration rolled back, so the ledger table does not exist yet.
        with connection.cursor() as cursor:
            cursor.execute("SELECT to_regclass('governance_consumedtransaction')")
            self.assertIsNone(cursor.fetchone()[0])

        # Let the leaf-restoring cleanup through: a human resolves the duplicate, we
        # simulate that by dropping the loser.
        Proposal = self.apps_0030.get_model('governance', 'Proposal')
        Proposal.objects.filter(id=second.id).delete()

    def test_forward_collapses_a_cross_table_duplicate_onto_the_live_proposal(self):
        proposal = self._create_proposal(title='Live', transaction_hash=distinct_hash(1))
        other = self._create_proposal(title='Other', transaction_hash=distinct_hash(2))
        self._create_history_proposal(proposal=other, transaction_hash=distinct_hash(1))

        apps_0031 = self._migrate_forward()

        ConsumedTransaction = apps_0031.get_model('governance', 'ConsumedTransaction')
        self.assertEqual(ConsumedTransaction.objects.filter(transaction_hash=distinct_hash(1)).count(), 1)
        self.assertEqual(
            ConsumedTransaction.objects.get(transaction_hash=distinct_hash(1)).proposal_id,
            proposal.id,
        )

    def test_rerunning_the_backfill_adds_nothing_and_keeps_a_real_claim(self):
        self._create_proposal(title='Settled', transaction_hash=distinct_hash(1))
        self._create_proposal(title='Second', transaction_hash=distinct_hash(2))

        apps_0031 = self._migrate_forward()
        ConsumedTransaction = apps_0031.get_model('governance', 'ConsumedTransaction')
        ConsumedTransaction.objects.filter(transaction_hash=distinct_hash(1)).update(
            purpose='SUBMIT',
            payer=SECONDARY_ACCOUNT,
        )

        report = backfill_consumed_transactions(apps_0031)

        self.assertEqual(report.rows_created, 0)
        self.assertEqual(report.rows_pre_existing, 2)
        self.assertEqual(ConsumedTransaction.objects.count(), 2)
        real_claim = ConsumedTransaction.objects.get(transaction_hash=distinct_hash(1))
        self.assertEqual(real_claim.purpose, 'SUBMIT')
        self.assertEqual(real_claim.payer, SECONDARY_ACCOUNT)

    def test_a_dry_run_reports_without_writing(self):
        self._create_proposal(title='Settled', transaction_hash=distinct_hash(1))
        self._create_proposal(title='In flight', action='TO_CREATE', transaction_hash=distinct_hash(2))

        apps_0031 = self._migrate_forward()
        ConsumedTransaction = apps_0031.get_model('governance', 'ConsumedTransaction')
        ConsumedTransaction.objects.all().delete()

        report = backfill_consumed_transactions(apps_0031, dry_run=True)

        self.assertTrue(report.dry_run)
        self.assertEqual(report.unique_hashes, 1)
        self.assertEqual(report.in_flight_skipped, 1)
        self.assertEqual(ConsumedTransaction.objects.count(), 0)
        # `rows_created` is what a real run would create, not what this one wrote: the
        # operator reads it before deciding whether the mandatory re-run is needed.
        self.assertEqual(report.rows_created, 1)
        self.assertEqual(report.rows_pre_existing, 0)

    def test_a_dry_run_counts_a_hash_the_ledger_already_holds_as_pre_existing(self):
        self._create_proposal(title='Settled', transaction_hash=distinct_hash(1))
        self._create_proposal(title='Also settled', transaction_hash=distinct_hash(2))

        apps_0031 = self._migrate_forward()
        ConsumedTransaction = apps_0031.get_model('governance', 'ConsumedTransaction')
        ConsumedTransaction.objects.exclude(transaction_hash=distinct_hash(1)).delete()

        report = backfill_consumed_transactions(apps_0031, dry_run=True)

        self.assertEqual(report.rows_pre_existing, 1)
        self.assertEqual(report.rows_created, 1)
        self.assertEqual(ConsumedTransaction.objects.count(), 1)

    def test_reverse_leaves_the_source_tables_untouched(self):
        proposal = self._create_proposal(
            title='Settled',
            action='TO_SUBMIT',
            transaction_hash=distinct_hash(1),
            new_transaction_hash=distinct_hash(2),
        )
        history = self._create_history_proposal(proposal=proposal, transaction_hash=distinct_hash(3))

        self._migrate_forward()
        apps_0030 = self._migrate_backward()

        Proposal = apps_0030.get_model('governance', 'Proposal')
        HistoryProposal = apps_0030.get_model('governance', 'HistoryProposal')
        reversed_proposal = Proposal.objects.get(id=proposal.id)
        self.assertEqual(reversed_proposal.transaction_hash, distinct_hash(1))
        self.assertEqual(reversed_proposal.new_transaction_hash, distinct_hash(2))
        self.assertEqual(reversed_proposal.action, 'TO_SUBMIT')
        self.assertEqual(
            HistoryProposal.objects.get(id=history.id).transaction_hash,
            distinct_hash(3),
        )
        with connection.cursor() as cursor:
            cursor.execute("SELECT to_regclass('governance_consumedtransaction')")
            self.assertIsNone(cursor.fetchone()[0])
