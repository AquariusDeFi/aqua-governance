from unittest.mock import patch

from django.db import DatabaseError, connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from rest_framework.test import APIClient

from aqua_governance.governance.models import AssetToken, Proposal
from aqua_governance.governance.tests._factories import _create_proposal


class GovernanceHealthTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def reviewed_proposal(self, **fields):
        proposal = _create_proposal(proposal_type=Proposal.PROPOSAL_TYPE_GENERAL, **fields)
        Proposal.objects.filter(pk=proposal.pk).update(
            onchain_execution_status=Proposal.ONCHAIN_EXECUTION_REQUIRES_REVIEW,
        )
        return proposal

    def test_no_review_required_is_healthy(self):
        response = self.client.get('/api/health/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            'status': 'ok',
            'requires_review': {'proposals': 0, 'asset_tokens': 0},
        })
        self.assertIn('no-store', response['Cache-Control'])

    def test_proposal_only_requires_review(self):
        self.reviewed_proposal()

        response = self.client.get('/api/health/')

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {
            'status': 'requires_review',
            'requires_review': {'proposals': 1, 'asset_tokens': 0},
        })
        self.assertIn('no-store', response['Cache-Control'])

    def test_token_without_proposal_requires_review(self):
        AssetToken.objects.create(
            contract_address='token-without-proposal',
            contract_sync_status=AssetToken.CONTRACT_SYNC_REQUIRES_REVIEW,
        )

        response = self.client.get('/api/health/')

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {
            'status': 'requires_review',
            'requires_review': {'proposals': 0, 'asset_tokens': 1},
        })

    def test_hidden_and_draft_reviews_are_counted_without_details(self):
        self.reviewed_proposal(hide=True, title='Private hidden proposal')
        self.reviewed_proposal(draft=True)
        AssetToken.objects.create(
            contract_address='private-token-address',
            contract_sync_status=AssetToken.CONTRACT_SYNC_REQUIRES_REVIEW,
            contract_sync_error='private token failure',
        )

        response = self.client.get('/api/health/')

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {
            'status': 'requires_review',
            'requires_review': {'proposals': 2, 'asset_tokens': 1},
        })

    def test_cleared_reviews_restore_healthy_status(self):
        proposal = self.reviewed_proposal()
        token = AssetToken.objects.create(
            contract_address='reviewed-token',
            contract_sync_status=AssetToken.CONTRACT_SYNC_REQUIRES_REVIEW,
        )
        self.assertEqual(self.client.get('/api/health/').status_code, 503)

        Proposal.objects.filter(pk=proposal.pk).update(onchain_execution_status=Proposal.ONCHAIN_EXECUTION_SUCCESS)
        AssetToken.objects.filter(pk=token.pk).update(contract_sync_status=AssetToken.CONTRACT_SYNC_SYNCED)

        response = self.client.get('/api/health/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['requires_review'], {'proposals': 0, 'asset_tokens': 0})

    def test_other_statuses_do_not_require_review(self):
        for status, _ in Proposal.ONCHAIN_EXECUTION_STATUS_CHOICES:
            if status != Proposal.ONCHAIN_EXECUTION_REQUIRES_REVIEW:
                proposal = _create_proposal(proposal_type=Proposal.PROPOSAL_TYPE_GENERAL)
                Proposal.objects.filter(pk=proposal.pk).update(onchain_execution_status=status)
        for status, _ in AssetToken.CONTRACT_SYNC_STATUS_CHOICES:
            if status != AssetToken.CONTRACT_SYNC_REQUIRES_REVIEW:
                AssetToken.objects.create(contract_address=status, contract_sync_status=status)

        response = self.client.get('/api/health/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['requires_review'], {'proposals': 0, 'asset_tokens': 0})

    def test_database_failures_are_unavailable_without_details(self):
        for model in (Proposal, AssetToken):
            with self.subTest(model=model.__name__), patch.object(
                model.objects, 'filter', side_effect=DatabaseError('private database details'),
            ):
                response = self.client.get('/api/health/')

            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json(), {'status': 'unavailable'})
            self.assertIn('no-store', response['Cache-Control'])

    def test_head_reports_the_same_review_status_without_body(self):
        self.assertEqual(self.client.head('/api/health/').status_code, 200)
        AssetToken.objects.create(
            contract_address='review-token',
            contract_sync_status=AssetToken.CONTRACT_SYNC_REQUIRES_REVIEW,
        )

        response = self.client.head('/api/health/')

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.content, b'')
        self.assertIn('no-store', response['Cache-Control'])

    def test_polling_is_read_only_and_does_not_contact_external_services(self):
        with patch('requests.sessions.Session.request', side_effect=AssertionError('unexpected network call')), patch(
            'celery.app.task.Task.apply_async', side_effect=AssertionError('unexpected queued task'),
        ), CaptureQueriesContext(connection) as queries:
            response = self.client.get('/api/health/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(queries), 2)
        self.assertTrue(all(query['sql'].lstrip().upper().startswith('SELECT ') for query in queries))

    def test_unsafe_methods_are_rejected_without_database_queries(self):
        for method in ('post', 'put', 'patch', 'delete'):
            with self.subTest(method=method), self.assertNumQueries(0):
                response = getattr(self.client, method)('/api/health/')

            self.assertEqual(response.status_code, 405)
            self.assertEqual(response['Allow'], 'GET, HEAD')
            self.assertIn('no-store', response['Cache-Control'])
