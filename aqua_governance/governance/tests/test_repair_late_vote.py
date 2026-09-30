import json
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db.models.query import QuerySet
from django.test import TestCase
from django.utils import timezone

from aqua_governance.governance.models import AssetToken, LogVote, Proposal
from aqua_governance.governance.tests._factories import make_asset_proposal


class RepairLateVoteTests(TestCase):
    def setUp(self):
        self.end = timezone.now() - timedelta(days=1)
        self.proposal = make_asset_proposal()
        Proposal.objects.filter(pk=self.proposal.pk).update(
            proposal_status=Proposal.VOTED, end_at=self.end,
            onchain_execution_status=Proposal.ONCHAIN_EXECUTION_SKIPPED,
            vote_for_result=100, vote_against_result=101, vote_abstain_result=7,
            ice_circulating_supply=500,
        )
        self.proposal.refresh_from_db()
        self.vote('for', LogVote.VOTE_FOR, voted_amount=100, amount=50, claimed=True)
        self.vote('against', LogVote.VOTE_AGAINST, voted_amount=None, amount=90, claimed=True)
        self.vote('abstain', LogVote.VOTE_ABSTAIN, voted_amount=7)
        self.vote('zero', LogVote.VOTE_FOR, voted_amount=0, amount=999)
        self.late = self.vote('late', LogVote.VOTE_AGAINST, created_at=self.end + timedelta(seconds=1), voted_amount=11)

    def vote(self, balance_id, choice, **overrides):
        fields = {
            'proposal': self.proposal, 'claimable_balance_id': balance_id,
            'vote_choice': choice, 'asset_code': settings.GDICE_ASSET_CODE,
            'created_at': self.end, 'amount': 1, 'original_amount': 120, 'voted_amount': 1,
        }
        fields.update(overrides)
        return LogVote.objects.create(**fields)

    def command(self, **options):
        output = StringIO()
        call_command('repair_late_vote', proposal_id=self.proposal.pk, vote_id=self.late.pk, stdout=output, **options)
        return json.loads(output.getvalue())

    def snapshot(self):
        return self.command()['snapshot_sha256']

    def apply(self, digest=None):
        return self.command(apply=True, expected_snapshot=digest or self.snapshot(), writers_paused=True)

    def test_dry_run_is_complete_deterministic_and_does_not_write(self):
        before = list(LogVote.objects.values())
        first = self.command()
        self.assertEqual(first, self.command())
        self.assertEqual(list(LogVote.objects.values()), before)
        self.assertEqual(len(first['before']['votes']), 5)
        self.assertEqual(first['totals']['vote_for_result'], '100.0000000')
        self.assertEqual(first['totals']['vote_against_result'], '90.0000000')
        self.assertEqual(first['decision'], {
            'majority_for': True, 'total_votes': '197.0000000', 'required_votes': '100.0000000',
            'quorum_met': True, 'approved': True,
        })
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.onchain_execution_status, Proposal.ONCHAIN_EXECUTION_SKIPPED)

    def test_apply_repairs_only_totals_and_review_state_without_side_effects(self):
        before = list(LogVote.objects.order_by('pk').values())
        token_before = AssetToken.objects.get(pk=self.proposal.asset_token_id)
        with patch('aqua_governance.governance.models.requests.get') as network, patch(
            'aqua_governance.governance.task_logic.proposal_finalization._enqueue_onchain_send_task',
        ) as enqueue, patch.object(Proposal, 'save') as save_proposal, patch.object(AssetToken, 'save') as save_token:
            result = self.apply()
        self.assertTrue(result['applied'])
        network.assert_not_called()
        enqueue.assert_not_called()
        save_proposal.assert_not_called()
        save_token.assert_not_called()
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.vote_against_result, Decimal('90'))
        self.assertEqual(self.proposal.vote_abstain_result, Decimal('7'))
        self.assertEqual(self.proposal.ice_circulating_supply, Decimal('500'))
        self.assertEqual(self.proposal.proposal_status, Proposal.VOTED)
        self.assertEqual(self.proposal.onchain_execution_status, Proposal.ONCHAIN_EXECUTION_REQUIRES_REVIEW)
        token = AssetToken.objects.get(pk=token_before.pk)
        self.assertEqual(token.whitelisted, token_before.whitelisted)
        self.assertEqual(token.last_execution_at, token_before.last_execution_at)
        self.assertEqual(token.contract_sync_status, AssetToken.CONTRACT_SYNC_REQUIRES_REVIEW)
        for row in before:
            if row['id'] == self.late.pk:
                row['hide'] = True
        self.assertEqual(list(LogVote.objects.order_by('pk').values()), before)

    def test_apply_requires_digest_and_paused_writers_acknowledgement(self):
        for options in [{'apply': True}, {'apply': True, 'expected_snapshot': self.snapshot()}]:
            with self.subTest(options=options), self.assertRaises(CommandError):
                self.command(**options)

    def test_snapshot_drift_in_any_vote_or_token_aborts(self):
        digest = self.snapshot()
        LogVote.objects.filter(claimable_balance_id='zero').update(original_amount=121)
        with self.assertRaisesMessage(CommandError, 'snapshot'):
            self.apply(digest)
        digest = self.snapshot()
        AssetToken.objects.filter(pk=self.proposal.asset_token_id).update(whitelisted=True)
        with self.assertRaisesMessage(CommandError, 'snapshot'):
            self.apply(digest)
        self.late.refresh_from_db()
        self.assertFalse(self.late.hide)

    def test_repeated_apply_is_rejected(self):
        digest = self.snapshot()
        self.apply(digest)
        with self.assertRaises(CommandError):
            self.apply(digest)

    def test_inflight_or_completed_execution_is_rejected(self):
        for status in [Proposal.ONCHAIN_EXECUTION_PENDING, Proposal.ONCHAIN_EXECUTION_SUBMITTED,
                       Proposal.ONCHAIN_EXECUTION_SUCCESS, Proposal.ONCHAIN_EXECUTION_REQUIRES_REVIEW]:
            with self.subTest(status=status):
                Proposal.objects.filter(pk=self.proposal.pk).update(onchain_execution_status=status)
                with self.assertRaises(CommandError):
                    self.command()

    def test_execution_markers_and_token_sync_hash_are_rejected(self):
        for field, value in [('onchain_execution_tx_hash', 'tx'), ('onchain_execution_started_at', self.end),
                             ('onchain_execution_submitted_at', self.end), ('onchain_execution_poll_count', 1)]:
            with self.subTest(field=field):
                Proposal.objects.filter(pk=self.proposal.pk).update(**{field: value})
                with self.assertRaises(CommandError):
                    self.command()
                Proposal.objects.filter(pk=self.proposal.pk).update(**{field: 0 if field.endswith('count') else None})
        AssetToken.objects.filter(pk=self.proposal.asset_token_id).update(contract_sync_tx_hash='tx')
        with self.assertRaises(CommandError):
            self.command()

    def test_hidden_shadow_extra_late_and_unknown_rows_are_rejected(self):
        for fields in [
            {'claimable_balance_id': self.late.claimable_balance_id, 'hide': True},
            {'claimable_balance_id': 'extra', 'created_at': self.end + timedelta(seconds=2)},
            {'claimable_balance_id': 'unknown', 'created_at': None},
        ]:
            with self.subTest(fields=fields):
                vote = self.vote(fields.pop('claimable_balance_id'), LogVote.VOTE_FOR, **fields)
                with self.assertRaises(CommandError):
                    self.command()
                vote.delete()

    def test_nonlate_or_hidden_target_is_rejected(self):
        for fields in [{'created_at': self.end}, {'created_at': None}, {'hide': True}]:
            with self.subTest(fields=fields):
                LogVote.objects.filter(pk=self.late.pk).update(**fields)
                with self.assertRaises(CommandError):
                    self.command()

    def test_asset_link_mismatch_is_rejected(self):
        Proposal.objects.filter(pk=self.proposal.pk).update(asset_code='WRONG')
        with self.assertRaises(CommandError):
            self.command()

    def test_unfinished_related_asset_execution_is_rejected(self):
        other = make_asset_proposal()
        Proposal.objects.filter(pk=other.pk).update(
            proposal_status=Proposal.VOTED, onchain_execution_status=Proposal.ONCHAIN_EXECUTION_PENDING,
        )
        with self.assertRaises(CommandError):
            self.command()

    def test_pending_token_without_send_evidence_can_be_held_after_skipped_proposal(self):
        AssetToken.objects.filter(pk=self.proposal.asset_token_id).update(
            whitelisted=True, contract_sync_status=AssetToken.CONTRACT_SYNC_PENDING,
        )
        make_asset_proposal()
        make_asset_proposal(draft=True)
        result = self.apply()
        self.assertTrue(result['applied'])
        token = AssetToken.objects.get(pk=self.proposal.asset_token_id)
        self.assertTrue(token.whitelisted)
        self.assertEqual(token.contract_sync_status, AssetToken.CONTRACT_SYNC_REQUIRES_REVIEW)

    def test_decision_uses_preserved_supply_and_abstention_quorum(self):
        Proposal.objects.filter(pk=self.proposal.pk).update(ice_circulating_supply=1000)
        decision = self.command()['decision']
        self.assertTrue(decision['majority_for'])
        self.assertFalse(decision['quorum_met'])
        self.assertFalse(decision['approved'])
        LogVote.objects.filter(claimable_balance_id='abstain').update(voted_amount=10)
        decision = self.command()['decision']
        self.assertEqual(decision['total_votes'], '200.0000000')
        self.assertTrue(decision['approved'])

    def test_atomic_rollback_when_token_update_fails(self):
        digest = self.snapshot()
        original_update = QuerySet.update

        def fail_token_update(queryset, **fields):
            if queryset.model is AssetToken:
                raise RuntimeError('injected token failure')
            return original_update(queryset, **fields)

        with patch.object(QuerySet, 'update', fail_token_update), self.assertRaises(RuntimeError):
            self.apply(digest)
        self.late.refresh_from_db()
        self.proposal.refresh_from_db()
        self.assertFalse(self.late.hide)
        self.assertEqual(self.proposal.vote_against_result, Decimal('101'))
