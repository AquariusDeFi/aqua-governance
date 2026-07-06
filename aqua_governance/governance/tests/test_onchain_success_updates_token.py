from unittest.mock import Mock, patch

from django.test import TestCase
from django.utils import timezone
from stellar_sdk.soroban_rpc import GetTransactionStatus

from aqua_governance.governance.models import AssetToken, Proposal
from aqua_governance.governance.tasks import (
    task_execute_onchain_action_send,
    task_poll_submitted_onchain_executions,
)
from aqua_governance.governance.tests._factories import make_asset_proposal


class OnchainExecutionTaskTests(TestCase):
    CODE = 'AQUA'
    ISSUER = 'GBNZILSTVQZ4R7IKQDGHYGY2QXL5QOFJYQMXPKWRRM5PAV7Y4M67AQUA'

    def _make_pending_proposal(self, proposal_type=Proposal.PROPOSAL_TYPE_ADD_ASSET):
        return make_asset_proposal(
            proposal_type=proposal_type,
            asset_code=self.CODE,
            asset_issuer=self.ISSUER,
            title='Pending asset proposal',
            draft=False,
            action=Proposal.NONE,
            proposal_status=Proposal.VOTED,
            onchain_execution_status=Proposal.ONCHAIN_EXECUTION_PENDING,
            onchain_execution_tx_hash=None,
        )

    def _make_submitted_proposal(self, proposal_type=Proposal.PROPOSAL_TYPE_ADD_ASSET):
        return make_asset_proposal(
            proposal_type=proposal_type,
            asset_code=self.CODE,
            asset_issuer=self.ISSUER,
            title='Submitted asset proposal',
            draft=False,
            action=Proposal.NONE,
            proposal_status=Proposal.VOTED,
            onchain_execution_status=Proposal.ONCHAIN_EXECUTION_SUBMITTED,
            onchain_execution_tx_hash='deadbeef' * 8,
        )

    def _success_result(self):
        result = Mock()
        result.status = GetTransactionStatus.SUCCESS
        return result

    def _failed_result(self):
        result = Mock()
        result.status = GetTransactionStatus.FAILED
        result.result_xdr = 'AAAA'
        return result

    def test_send_task_uses_linked_asset_token_for_onchain_args(self):
        proposal = self._make_pending_proposal()
        expected_contract_id = proposal.asset_token_id
        seen = {}

        def fake_execute_onchain_action(proposal_for_execution):
            seen['args'] = proposal_for_execution.onchain_action_args
            return 'cafebabe' * 8

        with patch(
            'aqua_governance.governance.tasks.execute_onchain_action',
            side_effect=fake_execute_onchain_action,
        ):
            task_execute_onchain_action_send(proposal.id)

        proposal.refresh_from_db()
        token = AssetToken.objects.get(pk=expected_contract_id)
        self.assertEqual(seen['args'], [expected_contract_id])
        self.assertEqual(proposal.onchain_execution_status, Proposal.ONCHAIN_EXECUTION_SUBMITTED)
        self.assertEqual(proposal.onchain_execution_tx_hash, 'cafebabe' * 8)
        self.assertEqual(token.contract_sync_tx_hash, 'cafebabe' * 8)
        self.assertIsNotNone(proposal.onchain_execution_started_at)
        self.assertIsNotNone(proposal.onchain_execution_submitted_at)

    def test_send_failure_marks_proposal_and_token_failed_without_reverting_whitelist(self):
        proposal = self._make_pending_proposal()
        AssetToken.objects.filter(pk=proposal.asset_token_id).update(
            whitelisted=True,
            whitelisted_since=timezone.now(),
        )

        with patch(
            'aqua_governance.governance.tasks.execute_onchain_action',
            side_effect=RuntimeError('simulated send failure'),
        ):
            task_execute_onchain_action_send(proposal.id)

        proposal.refresh_from_db()
        token = AssetToken.objects.get(pk=proposal.asset_token_id)
        self.assertEqual(proposal.onchain_execution_status, Proposal.ONCHAIN_EXECUTION_FAILED)
        self.assertIsNone(proposal.onchain_execution_tx_hash)
        self.assertIsNone(proposal.onchain_execution_submitted_at)
        self.assertEqual(token.contract_sync_status, AssetToken.CONTRACT_SYNC_FAILED)
        self.assertEqual(token.contract_sync_error, 'Onchain send failed')
        self.assertTrue(token.whitelisted)

    def test_poll_success_marks_token_synced_without_changing_whitelist_state(self):
        proposal = self._make_submitted_proposal()
        AssetToken.objects.filter(pk=proposal.asset_token_id).update(
            whitelisted=True,
            whitelisted_since=timezone.now(),
            contract_sync_status=AssetToken.CONTRACT_SYNC_PENDING,
            contract_sync_tx_hash=proposal.onchain_execution_tx_hash,
        )

        with patch(
            'aqua_governance.governance.tasks.get_soroban_transaction',
            return_value=self._success_result(),
        ):
            task_poll_submitted_onchain_executions()

        proposal.refresh_from_db()
        token = AssetToken.objects.get(pk=proposal.asset_token_id)
        self.assertEqual(proposal.onchain_execution_status, Proposal.ONCHAIN_EXECUTION_SUCCESS)
        self.assertEqual(token.contract_sync_status, AssetToken.CONTRACT_SYNC_SYNCED)
        self.assertEqual(token.contract_sync_tx_hash, proposal.onchain_execution_tx_hash)
        self.assertTrue(token.whitelisted)
        self.assertIsNotNone(token.contract_sync_updated_at)

    def test_poll_failed_marks_token_failed_without_reverting_whitelist(self):
        proposal = self._make_submitted_proposal(Proposal.PROPOSAL_TYPE_REMOVE_ASSET)
        AssetToken.objects.filter(pk=proposal.asset_token_id).update(
            whitelisted=True,
            whitelisted_since=timezone.now(),
            contract_sync_status=AssetToken.CONTRACT_SYNC_PENDING,
        )

        with patch(
            'aqua_governance.governance.tasks.get_soroban_transaction',
            return_value=self._failed_result(),
        ):
            task_poll_submitted_onchain_executions()

        proposal.refresh_from_db()
        token = AssetToken.objects.get(pk=proposal.asset_token_id)
        self.assertEqual(proposal.onchain_execution_status, Proposal.ONCHAIN_EXECUTION_FAILED)
        self.assertEqual(proposal.onchain_execution_poll_count, 1)
        self.assertEqual(token.contract_sync_status, AssetToken.CONTRACT_SYNC_FAILED)
        self.assertTrue(token.whitelisted)
        self.assertIn(proposal.onchain_execution_tx_hash, token.contract_sync_error)

    def test_poll_success_skips_token_sync_when_proposal_was_changed_concurrently(self):
        proposal = self._make_submitted_proposal()
        AssetToken.objects.filter(pk=proposal.asset_token_id).update(
            contract_sync_status=AssetToken.CONTRACT_SYNC_PENDING,
        )

        def race_then_success(*args, **kwargs):
            Proposal.objects.filter(id=proposal.id).update(
                onchain_execution_status=Proposal.ONCHAIN_EXECUTION_REQUIRES_REVIEW,
            )
            return self._success_result()

        with patch(
            'aqua_governance.governance.tasks.get_soroban_transaction',
            side_effect=race_then_success,
        ):
            task_poll_submitted_onchain_executions()

        proposal.refresh_from_db()
        token = AssetToken.objects.get(pk=proposal.asset_token_id)
        self.assertEqual(
            proposal.onchain_execution_status,
            Proposal.ONCHAIN_EXECUTION_REQUIRES_REVIEW,
        )
        self.assertEqual(token.contract_sync_status, AssetToken.CONTRACT_SYNC_PENDING)
