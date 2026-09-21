from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, TestCase

from celery.schedules import crontab

from aqua_governance.governance.models import Proposal
from aqua_governance.governance.tasks import task_update_proposal_results, task_update_votes
from aqua_governance.governance.tests._factories import _create_proposal
from aqua_governance.taskapp import setup_periodic_tasks


TASKS = 'aqua_governance.governance.tasks'


class VoteUpdateSchedulingTests(SimpleTestCase):
    def test_queued_bulk_updates_return_without_querying_or_marking_review(self):
        for args, kwargs in (((), {}), ((None,), {}), ((), {'proposal_id': None, 'freezing_amount': True})):
            with self.subTest(args=args, kwargs=kwargs), patch(
                f'{TASKS}.Proposal.objects.filter', side_effect=AssertionError('unexpected database access'),
            ), patch(
                f'{TASKS}.Server', side_effect=AssertionError('unexpected Horizon access'),
            ), patch(
                f'{TASKS}.update_proposal_votes_snapshot', side_effect=AssertionError('unexpected snapshot update'),
            ), patch(
                f'{TASKS}._hold_incomplete_vote_snapshot', side_effect=AssertionError('unexpected review update'),
            ):
                self.assertIs(task_update_votes(*args, **kwargs), True)

    def test_schedule_retains_active_and_closing_tasks_without_bulk_reindex(self):
        configured_app = SimpleNamespace(conf=SimpleNamespace(beat_schedule={}))
        with patch('aqua_governance.taskapp.app', configured_app):
            setup_periodic_tasks(sender=configured_app)

        scheduled = {entry['task']: entry for entry in configured_app.conf.beat_schedule.values()}
        self.assertNotIn(task_update_votes.name, scheduled)
        self.assertEqual(
            scheduled[f'{TASKS}.task_update_active_proposals']['schedule'], crontab(minute='*/5'),
        )
        self.assertEqual(
            scheduled[f'{TASKS}.task_sync_proposal_statuses_by_time']['schedule'], crontab(minute='*/1'),
        )


class ExplicitVoteUpdateTests(TestCase):
    def test_explicit_voted_proposal_still_freezes_and_finalizes(self):
        proposal = _create_proposal(proposal_type=Proposal.PROPOSAL_TYPE_GENERAL)
        Proposal.objects.filter(pk=proposal.pk).update(proposal_status=Proposal.VOTED)
        proposal.refresh_from_db()
        server = Mock()

        with patch(f'{TASKS}.Server', return_value=server), patch(
            f'{TASKS}.update_proposal_votes_snapshot',
        ) as snapshot, patch(f'{TASKS}.update_proposal_final_results') as finalize:
            task_update_proposal_results(proposal.pk, freezing_amount=True)

        snapshot.assert_called_once_with(proposal=proposal, horizon_server=server, freezing_amount=True)
        finalize.assert_called_once_with(proposal.pk)
