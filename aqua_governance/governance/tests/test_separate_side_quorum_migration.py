import json
from datetime import datetime, timedelta
from datetime import timezone as datetime_timezone
from unittest.mock import patch

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase

from django_quill.quill import Quill

from aqua_governance.governance.tests._factories import (
    DEFAULT_PROPOSED_BY,
    QUATERNARY_ACCOUNT,
    TERTIARY_ACCOUNT,
    make_general_proposal,
)
from aqua_governance.governance.tests._migrations import RestoresMigrationLeaf


UTC = datetime_timezone.utc
FIXED_NOW = datetime(2026, 11, 12, 12, 0, 0, tzinfo=UTC)
MIGRATION_NOW_PATCH = 'aqua_governance.governance.migrations.0033_proposal_separate_side_quorum.timezone.now'


class SeparateSideQuorumMigrationTests(RestoresMigrationLeaf, TransactionTestCase):
    migrate_from = [('governance', '0032_proposal_payment_not_found_since')]
    migrate_to = [('governance', '0033_proposal_separate_side_quorum')]

    def setUp(self):
        super().setUp()
        self.executor = MigrationExecutor(connection)
        self.executor.migrate(self.migrate_from)
        self.apps_0032 = self.executor.loader.project_state(self.migrate_from).apps

    def _create_proposal(self, **overrides):
        fields = {
            'proposed_by': DEFAULT_PROPOSED_BY,
            'title': 'Quorum rule proposal',
            'text': Quill(json.dumps({'delta': {'ops': []}, 'html': '<p>x</p>'})),
            # The historical model has no Proposal.save(), which generates the issuers.
            'vote_for_issuer': TERTIARY_ACCOUNT,
            'vote_against_issuer': QUATERNARY_ACCOUNT,
            'action': 'NONE',
            'proposal_type': 'GENERAL',
        }
        fields.update(overrides)
        return self.apps_0032.get_model('governance', 'Proposal').objects.create(**fields)

    def _migrate_forward(self):
        with patch(MIGRATION_NOW_PATCH, return_value=FIXED_NOW):
            self.executor = MigrationExecutor(connection)
            self.executor.migrate(self.migrate_to)
        return self.executor.loader.project_state(self.migrate_to).apps

    def test_forward_marks_only_completed_proposals_pooled(self):
        # Without an end_at, so the status alone marks it completed.
        voted = self._create_proposal(proposal_status='VOTED')
        voting_ended = self._create_proposal(proposal_status='VOTING', end_at=FIXED_NOW - timedelta(hours=1))
        voting_ends_now = self._create_proposal(proposal_status='VOTING', end_at=FIXED_NOW)
        voting_open = self._create_proposal(proposal_status='VOTING', end_at=FIXED_NOW + timedelta(hours=1))
        discussion = self._create_proposal(proposal_status='DISCUSSION')
        queued = self._create_proposal(proposal_status='QUEUED', end_at=FIXED_NOW + timedelta(days=7))
        # With a past end_at, so the status alone keeps it on the separate side rule.
        expired = self._create_proposal(proposal_status='EXPIRED', end_at=FIXED_NOW - timedelta(hours=1))

        proposals = self._migrate_forward().get_model('governance', 'Proposal').objects

        expected = {
            voted.id: False,
            voting_ended.id: False,
            voting_ends_now.id: False,
            voting_open.id: True,
            discussion.id: True,
            queued.id: True,
            expired.id: True,
        }
        self.assertEqual(
            dict(proposals.filter(id__in=expected).values_list('id', 'separate_side_quorum')),
            expected,
        )


class NewProposalQuorumRuleTests(TestCase):
    def test_proposal_created_after_migration_uses_separate_side_quorum(self):
        proposal = make_general_proposal()
        proposal.refresh_from_db()

        self.assertIs(proposal.separate_side_quorum, True)
