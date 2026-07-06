import json
from datetime import timedelta

from django.conf import settings
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone
from django_quill.quill import Quill
from stellar_sdk import Asset

from aqua_governance.governance.tests._factories import (
    DEFAULT_CODE,
    DEFAULT_ISSUER,
    DEFAULT_PROPOSED_BY,
    TERTIARY_ACCOUNT,
    QUATERNARY_ACCOUNT,
)


class AssetSingleShotMigrationTests(TransactionTestCase):
    migrate_from = [('governance', '0027_proposal_manage_asset_proposals_permission')]
    migrate_to = [('governance', '0028_asset_token_and_proposal_fk')]

    def setUp(self):
        super().setUp()
        self.executor = MigrationExecutor(connection)
        self.executor.migrate(self.migrate_from)
        self.apps_0027 = self.executor.loader.project_state(self.migrate_from).apps

    def test_forward_backfills_asset_tokens_and_proposal_fk(self):
        Proposal = self.apps_0027.get_model('governance', 'Proposal')
        now = timezone.now()
        text = Quill(json.dumps({'delta': '', 'html': '<p>x</p>'}))
        common = {
            'proposed_by': DEFAULT_PROPOSED_BY,
            'text': text,
            'vote_for_issuer': TERTIARY_ACCOUNT,
            'vote_against_issuer': QUATERNARY_ACCOUNT,
            'draft': False,
            'proposal_status': 'VOTED',
            'onchain_execution_status': 'SUCCESS',
            'asset_code': DEFAULT_CODE,
            'asset_issuer': DEFAULT_ISSUER,
            'asset_issuer_information': 'issuer-info',
            'asset_token_description': 'description',
            'asset_holder_distribution': 'holders',
            'asset_liquidity': 'liquidity',
            'asset_trading_volume': 'volume',
            'asset_audit_info': 'audit',
            'asset_stellar_flags': 'flags',
            'asset_related_projects': 'projects',
            'asset_community_references': 'references',
            'asset_aquarius_traction': 'traction',
            'asset_issuer_commitments': 'commitments',
        }
        add = Proposal.objects.create(
            title='Add token',
            proposal_type='ADD_ASSET',
            end_at=now,
            **common,
        )
        remove = Proposal.objects.create(
            title='Remove token',
            proposal_type='REMOVE_ASSET',
            end_at=now + timedelta(days=1),
            **common,
        )

        self.executor = MigrationExecutor(connection)
        self.executor.migrate(self.migrate_to)
        apps_0028 = self.executor.loader.project_state(self.migrate_to).apps

        AssetToken = apps_0028.get_model('governance', 'AssetToken')
        ProposalAfterMigrate = apps_0028.get_model('governance', 'Proposal')
        expected_contract = Asset(DEFAULT_CODE, DEFAULT_ISSUER).contract_id(
            settings.NETWORK_PASSPHRASE,
        )

        self.assertEqual(AssetToken.objects.count(), 1)
        token = AssetToken.objects.get()
        self.assertEqual(token.contract_address, expected_contract)
        self.assertEqual(token.classic_code, DEFAULT_CODE)
        self.assertEqual(token.classic_issuer, DEFAULT_ISSUER)
        self.assertFalse(token.whitelisted)
        self.assertEqual(token.whitelisted_since, add.end_at)
        self.assertEqual(token.unwhitelisted_since, remove.end_at)
        self.assertEqual(token.last_execution_at, remove.end_at)
        self.assertEqual(token.contract_sync_status, 'SYNCED')

        migrated_add = ProposalAfterMigrate.objects.get(id=add.id)
        migrated_remove = ProposalAfterMigrate.objects.get(id=remove.id)
        for proposal in (migrated_add, migrated_remove):
            self.assertEqual(proposal.asset_code, DEFAULT_CODE)
            self.assertEqual(proposal.asset_issuer, DEFAULT_ISSUER)
            self.assertEqual(proposal.asset_contract_address, expected_contract)
            self.assertEqual(proposal.asset_token_id, expected_contract)
            self.assertEqual(proposal.asset_token_description, 'description')
            self.assertEqual(proposal.asset_issuer_commitments, 'commitments')
