from django.db import connection
from django.db.migrations.executor import MigrationExecutor


class RestoresMigrationLeaf:
    """Puts the governance schema back at the migration leaf once the test is done.

    A migration test leaves the test database wherever it migrated to, and neither existing
    module restored it.  That is benign only while every node above the target is an
    AlterField; once a node creates a table, anything that runs afterwards finds the table
    missing.  The runner's habit of ordering TestCase before TransactionTestCase is not a
    guarantee, and it does not survive running a single module by name.
    """

    migration_leaf_app = 'governance'

    def setUp(self):
        super().setUp()
        self.addCleanup(self.restore_migration_leaf)

    def restore_migration_leaf(self):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes(self.migration_leaf_app))
