import json
from datetime import datetime, timedelta
from datetime import timezone as datetime_timezone
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import RequestFactory, TestCase
from django.utils import timezone

from django_quill.quill import Quill

from aqua_governance.governance.admin import ProposalAdmin
from aqua_governance.governance.models import Proposal, ProposalQueueSlot
from aqua_governance.governance.proposal_queue import get_queue_week_start
from aqua_governance.governance.tests._factories import (
    DEFAULT_PROPOSED_BY,
    SECONDARY_ACCOUNT,
    make_asset_proposal,
    make_asset_proposal_raw,
    patch_ice_circulating_supply,
)


UTC = datetime_timezone.utc


class ProposalAdminPermissionTests(TestCase):
    def setUp(self):
        self.site = AdminSite()
        self.admin = ProposalAdmin(Proposal, self.site)
        self.factory = RequestFactory()

    def _create_user(self, *, is_superuser=False, with_manage_perm=False):
        user_model = get_user_model()
        user = user_model.objects.create_user(
            username=f'user_{is_superuser}_{with_manage_perm}',
            password='password',
            is_staff=True,
            is_superuser=is_superuser,
        )
        if with_manage_perm:
            perm = Permission.objects.get(codename='manage_asset_proposals')
            user.user_permissions.add(perm)
        return user

    def _make_proposal(self, proposal_type):
        if Proposal.is_asset_proposal_type(proposal_type):
            return make_asset_proposal(proposal_type=proposal_type, title='Test proposal')
        with patch_ice_circulating_supply():
            return Proposal.objects.create(
                proposed_by=DEFAULT_PROPOSED_BY,
                title='Test proposal',
                text=Quill(json.dumps({'delta': '', 'html': '<p>Test</p>'})),
                proposal_type=proposal_type,
            )

    def _quill_form_value(self, html='<p>Test</p>'):
        return json.dumps({'delta': '', 'html': html})

    def _split_datetime_form_value(self, value):
        return {
            'date': value.strftime('%Y-%m-%d'),
            'time': value.strftime('%H:%M:%S'),
        }

    def _queue_slot(self, *, weeks_ahead=1):
        start_at = get_queue_week_start(timezone.now()) + timedelta(weeks=weeks_ahead)
        end_at = start_at + timedelta(days=7, seconds=-1)
        return start_at, end_at

    def test_manager_queryset_is_limited_to_asset_proposals(self):
        general_proposal = self._make_proposal(Proposal.PROPOSAL_TYPE_GENERAL)
        asset_proposal = self._make_proposal(Proposal.PROPOSAL_TYPE_ADD_ASSET)

        request = self.factory.get('/admin/')
        request.user = self._create_user(with_manage_perm=True)

        queryset = self.admin.get_queryset(request)

        self.assertNotIn(general_proposal, queryset)
        self.assertIn(asset_proposal, queryset)

    def test_manager_cannot_delete_proposals(self):
        request = self.factory.get('/admin/')
        request.user = self._create_user(with_manage_perm=True)

        self.assertFalse(self.admin.has_delete_permission(request))

    def test_manager_cannot_change_general_proposals(self):
        general_proposal = self._make_proposal(Proposal.PROPOSAL_TYPE_GENERAL)

        request = self.factory.get('/admin/')
        request.user = self._create_user(with_manage_perm=True)

        self.assertFalse(self.admin.has_change_permission(request, general_proposal))

    def test_manager_proposal_type_choices_are_asset_only(self):
        request = self.factory.get('/admin/')
        request.user = self._create_user(with_manage_perm=True)

        form_class = self.admin.get_form(request)
        form = form_class()
        choice_values = [choice[0] for choice in form.fields['proposal_type'].choices]

        self.assertEqual(
            choice_values,
            [Proposal.PROPOSAL_TYPE_ADD_ASSET, Proposal.PROPOSAL_TYPE_REMOVE_ASSET],
        )

    def test_manager_cannot_submit_general_proposal_via_tampered_post(self):
        request = self.factory.post('/admin/')
        request.user = self._create_user(with_manage_perm=True)

        form_class = self.admin.get_form(request)
        form = form_class(data={
            'proposed_by': 'GAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAWHF',
            'title': 'Tampered proposal',
            'text': self._quill_form_value(),
            'proposal_type': Proposal.PROPOSAL_TYPE_GENERAL,
            'transaction_hash': 'a' * 64,
            'envelope_xdr': 'AAAA',
            'discord_username': 'manager',
        })

        self.assertFalse(form.is_valid())
        self.assertIn('proposal_type', form.errors)

    @patch('aqua_governance.governance.forms.acquire_proposal_transition_lock')
    def test_manager_can_create_asset_proposal_without_payment_xdr(self, mock_lock):
        request = self.factory.post('/admin/')
        request.user = self._create_user(with_manage_perm=True)

        form_class = self.admin.get_form(request)
        form = form_class(data={
            'proposed_by': 'GAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAWHF',
            'title': 'Asset proposal',
            'text': self._quill_form_value(),
            'proposal_type': Proposal.PROPOSAL_TYPE_ADD_ASSET,
            'proposal_status': Proposal.DISCUSSION,
            'discord_username': 'manager',
            'asset_code': 'AQUA',
            'asset_issuer': 'GBNZILSTVQZ4R7IKQDGHYGY2QXL5QOFJYQMXPKWRRM5PAV7Y4M67AQUA',
            'asset_issuer_information': 'info',
            'asset_token_description': 'desc',
            'asset_holder_distribution': 'distribution',
            'asset_liquidity': 'liquidity',
            'asset_trading_volume': 'volume',
            'asset_audit_info': 'audit',
            'asset_stellar_flags': 'flags',
            'asset_related_projects': 'projects',
            'asset_community_references': 'references',
            'asset_aquarius_traction': 'traction',
            'asset_issuer_commitments': 'commitments',
        })

        self.assertTrue(form.is_valid(), form.errors)
        proposal = form.save(commit=False)

        self.assertFalse(proposal.draft)
        self.assertEqual(proposal.action, Proposal.NONE)
        self.assertEqual(proposal.payment_status, Proposal.FINE)
        self.assertFalse(proposal.hide)
        mock_lock.assert_called_once_with()

    @patch('aqua_governance.governance.forms.timezone.now')
    @patch('aqua_governance.governance.forms.acquire_proposal_transition_lock')
    def test_admin_queued_rejects_current_week(self, mock_lock, mock_now):
        start_at = datetime(2026, 7, 6, 0, 0, 0, tzinfo=UTC)
        end_at = start_at + timedelta(days=7, seconds=-1)
        mock_now.return_value = datetime(2026, 7, 8, 12, 0, 0, tzinfo=UTC)
        request = self.factory.post('/admin/')
        request.user = self._create_user(with_manage_perm=True)

        form_class = self.admin.get_form(request)
        form = form_class(data={
            'proposed_by': 'GAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAWHF',
            'title': 'Queued asset proposal',
            'text': self._quill_form_value(),
            'proposal_type': Proposal.PROPOSAL_TYPE_ADD_ASSET,
            'proposal_status': Proposal.QUEUED,
            'start_at_0': self._split_datetime_form_value(start_at)['date'],
            'start_at_1': self._split_datetime_form_value(start_at)['time'],
            'end_at_0': self._split_datetime_form_value(end_at)['date'],
            'end_at_1': self._split_datetime_form_value(end_at)['time'],
            'discord_username': 'manager',
            'asset_code': 'AQUA',
            'asset_issuer': 'GBNZILSTVQZ4R7IKQDGHYGY2QXL5QOFJYQMXPKWRRM5PAV7Y4M67AQUA',
            'asset_issuer_information': 'info',
            'asset_token_description': 'desc',
            'asset_holder_distribution': 'distribution',
            'asset_liquidity': 'liquidity',
            'asset_trading_volume': 'volume',
            'asset_audit_info': 'audit',
            'asset_stellar_flags': 'flags',
            'asset_related_projects': 'projects',
            'asset_community_references': 'references',
            'asset_aquarius_traction': 'traction',
            'asset_issuer_commitments': 'commitments',
        })

        self.assertFalse(form.is_valid())
        self.assertIn('end_at', form.errors)
        mock_lock.assert_called_once_with()

    @patch('aqua_governance.governance.forms.acquire_proposal_transition_lock')
    def test_manager_can_create_active_asset_proposal_when_none_is_active(self, mock_lock):
        start_at, end_at = self._queue_slot(weeks_ahead=0)
        request = self.factory.post('/admin/')
        request.user = self._create_user(with_manage_perm=True)

        form_class = self.admin.get_form(request)
        form = form_class(data={
            'proposed_by': 'GAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAWHF',
            'title': 'Asset proposal',
            'text': self._quill_form_value(),
            'proposal_type': Proposal.PROPOSAL_TYPE_ADD_ASSET,
            'proposal_status': Proposal.VOTING,
            'start_at_0': self._split_datetime_form_value(start_at)['date'],
            'start_at_1': self._split_datetime_form_value(start_at)['time'],
            'end_at_0': self._split_datetime_form_value(end_at)['date'],
            'end_at_1': self._split_datetime_form_value(end_at)['time'],
            'discord_username': 'manager',
            'asset_code': 'AQUA',
            'asset_issuer': 'GBNZILSTVQZ4R7IKQDGHYGY2QXL5QOFJYQMXPKWRRM5PAV7Y4M67AQUA',
            'asset_issuer_information': 'info',
            'asset_token_description': 'desc',
            'asset_holder_distribution': 'distribution',
            'asset_liquidity': 'liquidity',
            'asset_trading_volume': 'volume',
            'asset_audit_info': 'audit',
            'asset_stellar_flags': 'flags',
            'asset_related_projects': 'projects',
            'asset_community_references': 'references',
            'asset_aquarius_traction': 'traction',
            'asset_issuer_commitments': 'commitments',
        })

        self.assertTrue(form.is_valid(), form.errors)
        mock_lock.assert_called_once_with()

    @patch('aqua_governance.governance.forms.acquire_proposal_transition_lock')
    def test_manager_cannot_create_voting_asset_proposal_with_non_weekly_slot(self, mock_lock):
        now = timezone.now()
        request = self.factory.post('/admin/')
        request.user = self._create_user(with_manage_perm=True)

        form_class = self.admin.get_form(request)
        form = form_class(data={
            'proposed_by': 'GAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAWHF',
            'title': 'Asset proposal',
            'text': self._quill_form_value(),
            'proposal_type': Proposal.PROPOSAL_TYPE_ADD_ASSET,
            'proposal_status': Proposal.VOTING,
            'start_at_0': self._split_datetime_form_value(now)['date'],
            'start_at_1': self._split_datetime_form_value(now)['time'],
            'end_at_0': self._split_datetime_form_value(now + timedelta(days=10))['date'],
            'end_at_1': self._split_datetime_form_value(now + timedelta(days=10))['time'],
            'discord_username': 'manager',
            'asset_code': 'AQUA',
            'asset_issuer': 'GBNZILSTVQZ4R7IKQDGHYGY2QXL5QOFJYQMXPKWRRM5PAV7Y4M67AQUA',
            'asset_issuer_information': 'info',
            'asset_token_description': 'desc',
            'asset_holder_distribution': 'distribution',
            'asset_liquidity': 'liquidity',
            'asset_trading_volume': 'volume',
            'asset_audit_info': 'audit',
            'asset_stellar_flags': 'flags',
            'asset_related_projects': 'projects',
            'asset_community_references': 'references',
            'asset_aquarius_traction': 'traction',
            'asset_issuer_commitments': 'commitments',
        })

        self.assertFalse(form.is_valid())
        self.assertIn('start_at', form.errors)
        mock_lock.assert_called_once_with()

    def test_admin_quorum_display_requires_positive_ice_supply(self):
        proposal = SimpleNamespace(
            vote_for_result=100,
            vote_against_result=0,
            vote_abstain_result=0,
            ice_circulating_supply=0,
            percent_for_quorum=20,
        )

        self.assertEqual(self.admin._list_display_quorum(proposal), 'Not enough votes')

    @patch('aqua_governance.governance.models.requests.get')
    def test_admin_save_model_creates_queue_slot_for_manual_queued_asset_proposal(self, mock_get):
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {'ice_supply_amount': 0}
        start_at, end_at = self._queue_slot(weeks_ahead=1)
        proposal = Proposal(
            proposed_by=DEFAULT_PROPOSED_BY,
            title='Manual queued asset proposal',
            text=Quill(json.dumps({'delta': '', 'html': '<p>Test</p>'})),
            proposal_type=Proposal.PROPOSAL_TYPE_ADD_ASSET,
            proposal_status=Proposal.QUEUED,
            payment_status=Proposal.FINE,
            draft=False,
            action=Proposal.NONE,
            start_at=start_at,
            end_at=end_at,
            asset_code='AQUA',
            asset_issuer='GBNZILSTVQZ4R7IKQDGHYGY2QXL5QOFJYQMXPKWRRM5PAV7Y4M67AQUA',
            asset_issuer_information='info',
            asset_token_description='desc',
            asset_holder_distribution='distribution',
            asset_liquidity='liquidity',
            asset_trading_volume='volume',
            asset_audit_info='audit',
            asset_stellar_flags='flags',
            asset_related_projects='projects',
            asset_community_references='references',
            asset_aquarius_traction='traction',
            asset_issuer_commitments='commitments',
        )
        request = self.factory.post('/admin/')
        request.user = self._create_user(is_superuser=True)

        self.admin.save_model(request, proposal, form=None, change=False)

        proposal.refresh_from_db()
        self.assertTrue(
            ProposalQueueSlot.objects.filter(
                proposal=proposal,
                start_at=start_at,
                end_at=end_at,
            ).exists()
        )

    @patch('aqua_governance.governance.models.requests.get')
    def test_admin_save_model_removes_queue_slot_when_proposal_leaves_queue_statuses(self, mock_get):
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {'ice_supply_amount': 0}
        start_at, end_at = self._queue_slot(weeks_ahead=1)
        proposal = make_asset_proposal(
            proposal_status=Proposal.QUEUED,
            start_at=start_at,
            end_at=end_at,
            title='Queued asset proposal',
        )
        ProposalQueueSlot.objects.create(proposal=proposal, start_at=start_at, end_at=end_at)
        proposal.proposal_status = Proposal.VOTED
        request = self.factory.post('/admin/')
        request.user = self._create_user(is_superuser=True)

        self.admin.save_model(request, proposal, form=None, change=True)

        self.assertFalse(ProposalQueueSlot.objects.filter(proposal=proposal).exists())

    @patch('aqua_governance.governance.forms.acquire_proposal_transition_lock')
    def test_manager_transition_to_discussion_clears_reserved_window_and_slot(self, mock_lock):
        start_at, end_at = self._queue_slot(weeks_ahead=1)
        proposal = make_asset_proposal(
            proposal_status=Proposal.QUEUED,
            start_at=start_at,
            end_at=end_at,
            title='Queued asset proposal',
        )
        ProposalQueueSlot.objects.create(proposal=proposal, start_at=start_at, end_at=end_at)

        request = self.factory.post('/admin/')
        request.user = self._create_user(with_manage_perm=True)
        form_class = self.admin.get_form(request, obj=proposal, change=True)
        form = form_class(
            instance=proposal,
            data={
                'proposed_by': proposal.proposed_by,
                'title': proposal.title,
                'text': self._quill_form_value(),
                'proposal_type': Proposal.PROPOSAL_TYPE_ADD_ASSET,
                'proposal_status': Proposal.DISCUSSION,
                'start_at_0': self._split_datetime_form_value(start_at)['date'],
                'start_at_1': self._split_datetime_form_value(start_at)['time'],
                'end_at_0': self._split_datetime_form_value(end_at)['date'],
                'end_at_1': self._split_datetime_form_value(end_at)['time'],
                'discord_username': 'manager',
                'asset_issuer_information': 'info',
                'asset_token_description': 'desc',
                'asset_holder_distribution': 'distribution',
                'asset_liquidity': 'liquidity',
                'asset_trading_volume': 'volume',
                'asset_audit_info': 'audit',
                'asset_stellar_flags': 'flags',
                'asset_related_projects': 'projects',
                'asset_community_references': 'references',
                'asset_aquarius_traction': 'traction',
                'asset_issuer_commitments': 'commitments',
            },
        )

        self.assertTrue(form.is_valid(), form.errors)

        updated_proposal = form.save(commit=False)
        self.admin.save_model(request, updated_proposal, form=form, change=True)

        proposal.refresh_from_db()
        self.assertEqual(proposal.proposal_status, Proposal.DISCUSSION)
        self.assertIsNone(proposal.start_at)
        self.assertIsNone(proposal.end_at)
        self.assertFalse(ProposalQueueSlot.objects.filter(proposal=proposal).exists())
        mock_lock.assert_called_once_with()

    @patch('aqua_governance.governance.forms.acquire_proposal_transition_lock')
    def test_manager_can_edit_legacy_queued_or_voting_asset_without_weekly_revalidation(self, mock_lock):
        now = timezone.now().replace(microsecond=0)
        scenarios = [
            {
                'status': Proposal.QUEUED,
                'start_at': now + timedelta(days=2),
                'end_at': now + timedelta(days=9),
                'title': 'Legacy queued asset proposal',
                'updated_title': 'Legacy queued asset proposal edited',
            },
            {
                'status': Proposal.VOTING,
                'start_at': now - timedelta(days=2),
                'end_at': now + timedelta(days=5),
                'title': 'Legacy voting asset proposal',
                'updated_title': 'Legacy voting asset proposal edited',
            },
        ]

        request = self.factory.post('/admin/')
        request.user = self._create_user(with_manage_perm=True)

        for scenario in scenarios:
            with self.subTest(status=scenario['status']):
                ProposalQueueSlot.objects.all().delete()
                Proposal.objects.all().delete()
                proposal = make_asset_proposal(
                    proposal_status=scenario['status'],
                    start_at=scenario['start_at'],
                    end_at=scenario['end_at'],
                    title=scenario['title'],
                )
                ProposalQueueSlot.objects.filter(proposal=proposal).delete()

                form_class = self.admin.get_form(request, obj=proposal, change=True)
                form = form_class(
                    instance=proposal,
                    data={
                        'proposed_by': proposal.proposed_by,
                        'title': scenario['updated_title'],
                        'text': self._quill_form_value(),
                        'proposal_type': Proposal.PROPOSAL_TYPE_ADD_ASSET,
                        'proposal_status': scenario['status'],
                        'start_at_0': self._split_datetime_form_value(scenario['start_at'])['date'],
                        'start_at_1': self._split_datetime_form_value(scenario['start_at'])['time'],
                        'end_at_0': self._split_datetime_form_value(scenario['end_at'])['date'],
                        'end_at_1': self._split_datetime_form_value(scenario['end_at'])['time'],
                        'discord_username': 'manager',
                        'asset_issuer_information': 'info',
                        'asset_token_description': 'desc',
                        'asset_holder_distribution': 'distribution',
                        'asset_liquidity': 'liquidity',
                        'asset_trading_volume': 'volume',
                        'asset_audit_info': 'audit',
                        'asset_stellar_flags': 'flags',
                        'asset_related_projects': 'projects',
                        'asset_community_references': 'references',
                        'asset_aquarius_traction': 'traction',
                        'asset_issuer_commitments': 'commitments',
                    },
                )

                self.assertTrue(form.is_valid(), form.errors)

                updated_proposal = form.save(commit=False)
                self.admin.save_model(request, updated_proposal, form=form, change=True)

                proposal.refresh_from_db()
                self.assertEqual(proposal.title, scenario['updated_title'])
                self.assertFalse(ProposalQueueSlot.objects.filter(proposal=proposal).exists())

        mock_lock.assert_not_called()

    @patch('aqua_governance.governance.forms.acquire_proposal_transition_lock')
    def test_manager_title_only_edit_on_slotless_weekly_legacy_asset_does_not_create_slot(self, mock_lock):
        current_week_start = get_queue_week_start(timezone.now())
        scenarios = [
            {
                'status': Proposal.QUEUED,
                'start_at': current_week_start + timedelta(weeks=1),
            },
            {
                'status': Proposal.VOTING,
                'start_at': current_week_start,
            },
        ]

        request = self.factory.post('/admin/')
        request.user = self._create_user(with_manage_perm=True)

        for scenario in scenarios:
            with self.subTest(status=scenario['status']):
                ProposalQueueSlot.objects.all().delete()
                Proposal.objects.all().delete()
                start_at = scenario['start_at']
                end_at = start_at + timedelta(days=7, seconds=-1)
                proposal = make_asset_proposal(
                    proposal_status=scenario['status'],
                    start_at=start_at,
                    end_at=end_at,
                    title=f'Legacy {scenario["status"].lower()} weekly proposal',
                )
                ProposalQueueSlot.objects.filter(proposal=proposal).delete()

                form_class = self.admin.get_form(request, obj=proposal, change=True)
                form = form_class(
                    instance=proposal,
                    data={
                        'proposed_by': proposal.proposed_by,
                        'title': f'{proposal.title} edited',
                        'text': self._quill_form_value(),
                        'proposal_type': Proposal.PROPOSAL_TYPE_ADD_ASSET,
                        'proposal_status': scenario['status'],
                        'start_at_0': self._split_datetime_form_value(start_at)['date'],
                        'start_at_1': self._split_datetime_form_value(start_at)['time'],
                        'end_at_0': self._split_datetime_form_value(end_at)['date'],
                        'end_at_1': self._split_datetime_form_value(end_at)['time'],
                        'discord_username': 'manager',
                        'asset_issuer_information': 'info',
                        'asset_token_description': 'desc',
                        'asset_holder_distribution': 'distribution',
                        'asset_liquidity': 'liquidity',
                        'asset_trading_volume': 'volume',
                        'asset_audit_info': 'audit',
                        'asset_stellar_flags': 'flags',
                        'asset_related_projects': 'projects',
                        'asset_community_references': 'references',
                        'asset_aquarius_traction': 'traction',
                        'asset_issuer_commitments': 'commitments',
                    },
                )

                self.assertTrue(form.is_valid(), form.errors)

                updated_proposal = form.save(commit=False)
                self.admin.save_model(request, updated_proposal, form=form, change=True)

                proposal.refresh_from_db()
                self.assertEqual(proposal.title, f'Legacy {scenario["status"].lower()} weekly proposal edited')
                self.assertFalse(ProposalQueueSlot.objects.filter(proposal=proposal).exists())

        mock_lock.assert_not_called()

    @patch('aqua_governance.governance.forms.acquire_proposal_transition_lock')
    def test_manager_schedule_change_on_slotless_weekly_asset_creates_validated_slot(self, mock_lock):
        old_start_at, old_end_at = self._queue_slot(weeks_ahead=1)
        new_start_at, new_end_at = self._queue_slot(weeks_ahead=2)
        proposal = make_asset_proposal(
            proposal_status=Proposal.QUEUED,
            start_at=old_start_at,
            end_at=old_end_at,
            title='Legacy queued asset proposal',
        )
        ProposalQueueSlot.objects.filter(proposal=proposal).delete()

        request = self.factory.post('/admin/')
        request.user = self._create_user(with_manage_perm=True)
        form_class = self.admin.get_form(request, obj=proposal, change=True)
        form = form_class(
            instance=proposal,
            data={
                'proposed_by': proposal.proposed_by,
                'title': 'Legacy queued asset proposal moved',
                'text': self._quill_form_value(),
                'proposal_type': Proposal.PROPOSAL_TYPE_ADD_ASSET,
                'proposal_status': Proposal.QUEUED,
                'start_at_0': self._split_datetime_form_value(new_start_at)['date'],
                'start_at_1': self._split_datetime_form_value(new_start_at)['time'],
                'end_at_0': self._split_datetime_form_value(new_end_at)['date'],
                'end_at_1': self._split_datetime_form_value(new_end_at)['time'],
                'discord_username': 'manager',
                'asset_issuer_information': 'info',
                'asset_token_description': 'desc',
                'asset_holder_distribution': 'distribution',
                'asset_liquidity': 'liquidity',
                'asset_trading_volume': 'volume',
                'asset_audit_info': 'audit',
                'asset_stellar_flags': 'flags',
                'asset_related_projects': 'projects',
                'asset_community_references': 'references',
                'asset_aquarius_traction': 'traction',
                'asset_issuer_commitments': 'commitments',
            },
        )

        self.assertTrue(form.is_valid(), form.errors)

        updated_proposal = form.save(commit=False)
        self.admin.save_model(request, updated_proposal, form=form, change=True)

        proposal.refresh_from_db()
        self.assertEqual(proposal.start_at, new_start_at)
        self.assertEqual(proposal.end_at, new_end_at)
        self.assertTrue(
            ProposalQueueSlot.objects.filter(
                proposal=proposal,
                start_at=new_start_at,
                end_at=new_end_at,
            ).exists()
        )
        mock_lock.assert_called_once_with()

    @patch('aqua_governance.governance.forms.acquire_proposal_transition_lock')
    def test_manager_can_queue_different_asset_proposal_when_same_asset_is_active(self, mock_lock):
        active_start_at, active_end_at = self._queue_slot(weeks_ahead=0)
        queued_start_at, queued_end_at = self._queue_slot(weeks_ahead=1)
        active_proposal = make_asset_proposal(
            proposal_status=Proposal.VOTING,
            start_at=active_start_at,
            end_at=active_end_at,
            title='Active AQUA asset proposal',
        )
        ProposalQueueSlot.objects.create(
            proposal=active_proposal,
            start_at=active_start_at,
            end_at=active_end_at,
        )
        request = self.factory.post('/admin/')
        request.user = self._create_user(with_manage_perm=True)

        form_class = self.admin.get_form(request)
        form = form_class(data={
            'proposed_by': 'GAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAWHF',
            'title': 'Asset proposal',
            'text': self._quill_form_value(),
            'proposal_type': Proposal.PROPOSAL_TYPE_ADD_ASSET,
            'proposal_status': Proposal.QUEUED,
            'start_at_0': self._split_datetime_form_value(queued_start_at)['date'],
            'start_at_1': self._split_datetime_form_value(queued_start_at)['time'],
            'end_at_0': self._split_datetime_form_value(queued_end_at)['date'],
            'end_at_1': self._split_datetime_form_value(queued_end_at)['time'],
            'discord_username': 'manager',
            'asset_code': 'USDC',
            'asset_issuer': SECONDARY_ACCOUNT,
            'asset_issuer_information': 'info',
            'asset_token_description': 'desc',
            'asset_holder_distribution': 'distribution',
            'asset_liquidity': 'liquidity',
            'asset_trading_volume': 'volume',
            'asset_audit_info': 'audit',
            'asset_stellar_flags': 'flags',
            'asset_related_projects': 'projects',
            'asset_community_references': 'references',
            'asset_aquarius_traction': 'traction',
            'asset_issuer_commitments': 'commitments',
        })

        self.assertTrue(form.is_valid(), form.errors)
        mock_lock.assert_called_once_with()

    @patch('aqua_governance.governance.forms.acquire_proposal_transition_lock')
    def test_manager_cannot_keep_queued_asset_when_same_asset_queued_or_voting_blocker_exists(self, mock_lock):
        request = self.factory.post('/admin/')
        request.user = self._create_user(with_manage_perm=True)

        for blocker_status, blocker_weeks_ahead in (
            (Proposal.QUEUED, 1),
            (Proposal.VOTING, 0),
        ):
            with self.subTest(blocker_status=blocker_status):
                ProposalQueueSlot.objects.all().delete()
                Proposal.objects.all().delete()

                blocker_start_at, blocker_end_at = self._queue_slot(weeks_ahead=blocker_weeks_ahead)
                blocker = make_asset_proposal(
                    proposal_status=blocker_status,
                    start_at=blocker_start_at,
                    end_at=blocker_end_at,
                    title=f'Blocking {blocker_status.lower()} asset proposal',
                )
                ProposalQueueSlot.objects.create(
                    proposal=blocker,
                    start_at=blocker_start_at,
                    end_at=blocker_end_at,
                )

                editable_start_at, editable_end_at = self._queue_slot(weeks_ahead=2)
                editable = make_asset_proposal(
                    proposal_status=Proposal.QUEUED,
                    start_at=editable_start_at,
                    end_at=editable_end_at,
                    title='Editable queued asset proposal',
                )

                form_class = self.admin.get_form(request, obj=editable, change=True)
                form = form_class(
                    instance=editable,
                    data={
                        'proposed_by': editable.proposed_by,
                        'title': 'Editable queued asset proposal updated',
                        'text': self._quill_form_value(),
                        'proposal_type': Proposal.PROPOSAL_TYPE_ADD_ASSET,
                        'proposal_status': Proposal.QUEUED,
                        'start_at_0': self._split_datetime_form_value(editable_start_at)['date'],
                        'start_at_1': self._split_datetime_form_value(editable_start_at)['time'],
                        'end_at_0': self._split_datetime_form_value(editable_end_at)['date'],
                        'end_at_1': self._split_datetime_form_value(editable_end_at)['time'],
                        'discord_username': 'manager',
                        'asset_issuer_information': 'info',
                        'asset_token_description': 'desc',
                        'asset_holder_distribution': 'distribution',
                        'asset_liquidity': 'liquidity',
                        'asset_trading_volume': 'volume',
                        'asset_audit_info': 'audit',
                        'asset_stellar_flags': 'flags',
                        'asset_related_projects': 'projects',
                        'asset_community_references': 'references',
                        'asset_aquarius_traction': 'traction',
                        'asset_issuer_commitments': 'commitments',
                    },
                )

                self.assertFalse(form.is_valid())
                self.assertIn('__all__', form.errors)
                self.assertIn(str(blocker.id), form.errors['__all__'][0])

        mock_lock.assert_not_called()

    def test_manager_add_form_does_not_prefill_voting_window(self):
        request = self.factory.get('/admin/')
        request.user = self._create_user(with_manage_perm=True)

        form_class = self.admin.get_form(request)
        form = form_class()

        self.assertNotIn('start_at', form.initial)
        self.assertNotIn('end_at', form.initial)

    @patch('aqua_governance.governance.forms.acquire_proposal_transition_lock')
    def test_manager_cannot_create_active_asset_proposal_with_overlapping_interval(self, mock_lock):
        start_at, end_at = self._queue_slot(weeks_ahead=0)
        active_proposal = self._make_proposal(Proposal.PROPOSAL_TYPE_ADD_ASSET)
        active_proposal.proposal_status = Proposal.VOTING
        active_proposal.start_at = start_at
        active_proposal.end_at = end_at
        active_proposal.save(update_fields=['proposal_status', 'start_at', 'end_at'])
        ProposalQueueSlot.objects.create(proposal=active_proposal, start_at=start_at, end_at=end_at)
        request = self.factory.post('/admin/')
        request.user = self._create_user(with_manage_perm=True)

        form_class = self.admin.get_form(request)
        form = form_class(data={
            'proposed_by': 'GAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAWHF',
            'title': 'Asset proposal',
            'text': self._quill_form_value(),
            'proposal_type': Proposal.PROPOSAL_TYPE_ADD_ASSET,
            'proposal_status': Proposal.VOTING,
            'start_at_0': self._split_datetime_form_value(start_at)['date'],
            'start_at_1': self._split_datetime_form_value(start_at)['time'],
            'end_at_0': self._split_datetime_form_value(end_at)['date'],
            'end_at_1': self._split_datetime_form_value(end_at)['time'],
            'discord_username': 'manager',
            'asset_code': 'AQUA',
            'asset_issuer': 'GBNZILSTVQZ4R7IKQDGHYGY2QXL5QOFJYQMXPKWRRM5PAV7Y4M67AQUA',
            'asset_issuer_information': 'info',
            'asset_token_description': 'desc',
            'asset_holder_distribution': 'distribution',
            'asset_liquidity': 'liquidity',
            'asset_trading_volume': 'volume',
            'asset_audit_info': 'audit',
            'asset_stellar_flags': 'flags',
            'asset_related_projects': 'projects',
            'asset_community_references': 'references',
            'asset_aquarius_traction': 'traction',
            'asset_issuer_commitments': 'commitments',
        })

        self.assertFalse(form.is_valid())
        self.assertIn('start_at', form.errors)
        self.assertIn('end_at', form.errors)
        mock_lock.assert_called_once_with()

    @patch('aqua_governance.governance.forms.acquire_proposal_transition_lock')
    def test_manager_cannot_create_active_asset_proposal_with_overlapping_general_interval(self, mock_lock):
        start_at, end_at = self._queue_slot(weeks_ahead=0)
        active_proposal = self._make_proposal(Proposal.PROPOSAL_TYPE_GENERAL)
        active_proposal.proposal_status = Proposal.VOTING
        active_proposal.start_at = start_at
        active_proposal.end_at = end_at
        active_proposal.save(update_fields=['proposal_status', 'start_at', 'end_at'])
        ProposalQueueSlot.objects.create(proposal=active_proposal, start_at=start_at, end_at=end_at)
        request = self.factory.post('/admin/')
        request.user = self._create_user(with_manage_perm=True)

        form_class = self.admin.get_form(request)
        form = form_class(data={
            'proposed_by': 'GAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAWHF',
            'title': 'Asset proposal',
            'text': self._quill_form_value(),
            'proposal_type': Proposal.PROPOSAL_TYPE_ADD_ASSET,
            'proposal_status': Proposal.VOTING,
            'start_at_0': self._split_datetime_form_value(start_at)['date'],
            'start_at_1': self._split_datetime_form_value(start_at)['time'],
            'end_at_0': self._split_datetime_form_value(end_at)['date'],
            'end_at_1': self._split_datetime_form_value(end_at)['time'],
            'discord_username': 'manager',
            'asset_code': 'AQUA',
            'asset_issuer': 'GBNZILSTVQZ4R7IKQDGHYGY2QXL5QOFJYQMXPKWRRM5PAV7Y4M67AQUA',
            'asset_issuer_information': 'info',
            'asset_token_description': 'desc',
            'asset_holder_distribution': 'distribution',
            'asset_liquidity': 'liquidity',
            'asset_trading_volume': 'volume',
            'asset_audit_info': 'audit',
            'asset_stellar_flags': 'flags',
            'asset_related_projects': 'projects',
            'asset_community_references': 'references',
            'asset_aquarius_traction': 'traction',
            'asset_issuer_commitments': 'commitments',
        })

        self.assertFalse(form.is_valid())
        self.assertIn('start_at', form.errors)
        self.assertIn('end_at', form.errors)
        mock_lock.assert_called_once_with()

    @patch('aqua_governance.governance.forms.acquire_proposal_transition_lock')
    def test_manager_cannot_edit_voted_asset_proposal_end_at_to_overlap_active_voting(self, mock_lock):
        """X5 regression: editing `end_at` on a VOTED asset proposal via admin must
        run the overlap check even though target_status stays VOTED.

        Setup:
          - Active VOTING proposal occupying days [+1, +10] from now.
          - VOTED asset proposal (already finished).

        Action: manager opens the VOTED proposal and rewrites end_at into the
        active window (days [+5, +15]), without touching proposal_status.

        Expectation: form validation rejects with overlap error. Previously
        (pre-X5) the gate `target_status in (DISCUSSION, VOTING)` skipped the
        check entirely on VOTED rows, allowing global "one voting at a time"
        invariant to be violated by an admin edit.
        """
        now = timezone.now()

        # Active VOTING proposal — the overlap target.
        active = self._make_proposal(Proposal.PROPOSAL_TYPE_ADD_ASSET)
        active.proposal_status = Proposal.VOTING
        active.start_at = now + timedelta(days=1)
        active.end_at = now + timedelta(days=10)
        active.title = 'Active VOTING'
        active.save(update_fields=['proposal_status', 'start_at', 'end_at', 'title'])
        ProposalQueueSlot.objects.create(
            proposal=active,
            start_at=active.start_at,
            end_at=active.end_at,
        )

        # VOTED asset proposal — the row the manager is editing.
        voted = self._make_proposal(Proposal.PROPOSAL_TYPE_ADD_ASSET)
        voted.proposal_status = Proposal.VOTED
        voted.start_at = now - timedelta(days=30)
        voted.end_at = now - timedelta(days=20)
        voted.title = 'Finished VOTED'
        voted.save(update_fields=['proposal_status', 'start_at', 'end_at', 'title'])

        request = self.factory.post('/admin/')
        request.user = self._create_user(with_manage_perm=True)

        # Manager edits end_at into the active VOTING window. Proposal status
        # left as VOTED (the legacy gate exempt VOTED rows from the overlap
        # check — that's the bug).
        new_start = now + timedelta(days=5)
        new_end = now + timedelta(days=15)
        form_class = self.admin.get_form(request, obj=voted, change=True)
        form = form_class(
            instance=voted,
            data={
                'proposed_by': voted.proposed_by,
                'title': voted.title,
                'text': self._quill_form_value(),
                'proposal_type': Proposal.PROPOSAL_TYPE_ADD_ASSET,
                'proposal_status': Proposal.VOTED,
                'start_at_0': self._split_datetime_form_value(new_start)['date'],
                'start_at_1': self._split_datetime_form_value(new_start)['time'],
                'end_at_0': self._split_datetime_form_value(new_end)['date'],
                'end_at_1': self._split_datetime_form_value(new_end)['time'],
                'discord_username': 'manager',
                # asset_code / asset_issuer / asset_contract_address are
                # `disabled` for existing pk — initial values are used regardless.
                'asset_issuer_information': 'info',
                'asset_token_description': 'desc',
                'asset_holder_distribution': 'distribution',
                'asset_liquidity': 'liquidity',
                'asset_trading_volume': 'volume',
                'asset_audit_info': 'audit',
                'asset_stellar_flags': 'flags',
                'asset_related_projects': 'projects',
                'asset_community_references': 'references',
                'asset_aquarius_traction': 'traction',
                'asset_issuer_commitments': 'commitments',
            },
        )

        self.assertFalse(form.is_valid(), 'Form must reject overlap on VOTED end_at edit')
        self.assertIn('start_at', form.errors)
        self.assertIn('end_at', form.errors)
        # Lock was acquired for the overlap check (X5: now fires regardless of status).
        mock_lock.assert_called_once_with()

    @patch('aqua_governance.governance.forms.acquire_proposal_transition_lock')
    def test_manager_can_edit_voted_asset_proposal_end_at_to_non_overlapping_window(self, mock_lock):
        """X5 follow-up: legitimate edits to VOTED asset proposal end_at must still pass.

        Manager moves end_at to a window that does NOT overlap any active
        VOTING proposal — overlap check runs (lock acquired) but the form is
        valid. Confirms the X5 fix didn't break legitimate VOTED edits.
        """
        now = timezone.now()

        voted = self._make_proposal(Proposal.PROPOSAL_TYPE_ADD_ASSET)
        voted.proposal_status = Proposal.VOTED
        voted.start_at = now - timedelta(days=30)
        voted.end_at = now - timedelta(days=20)
        voted.title = 'Finished VOTED'
        voted.save(update_fields=['proposal_status', 'start_at', 'end_at', 'title'])

        request = self.factory.post('/admin/')
        request.user = self._create_user(with_manage_perm=True)

        # Move end_at slightly further into the past — no overlap with any
        # active VOTING (there isn't one). VOTED row staying VOTED.
        new_end = now - timedelta(days=15)
        form_class = self.admin.get_form(request, obj=voted, change=True)
        form = form_class(
            instance=voted,
            data={
                'proposed_by': voted.proposed_by,
                'title': voted.title,
                'text': self._quill_form_value(),
                'proposal_type': Proposal.PROPOSAL_TYPE_ADD_ASSET,
                'proposal_status': Proposal.VOTED,
                'start_at_0': self._split_datetime_form_value(voted.start_at)['date'],
                'start_at_1': self._split_datetime_form_value(voted.start_at)['time'],
                'end_at_0': self._split_datetime_form_value(new_end)['date'],
                'end_at_1': self._split_datetime_form_value(new_end)['time'],
                'discord_username': 'manager',
                'asset_issuer_information': 'info',
                'asset_token_description': 'desc',
                'asset_holder_distribution': 'distribution',
                'asset_liquidity': 'liquidity',
                'asset_trading_volume': 'volume',
                'asset_audit_info': 'audit',
                'asset_stellar_flags': 'flags',
                'asset_related_projects': 'projects',
                'asset_community_references': 'references',
                'asset_aquarius_traction': 'traction',
                'asset_issuer_commitments': 'commitments',
            },
        )

        self.assertTrue(form.is_valid(), form.errors)
        # X5 still acquires the lock to run the overlap check even though the
        # final answer is "no conflict" — this is the new defense.
        mock_lock.assert_called_once_with()

    def test_manager_can_edit_manual_asset_admin_fields(self):
        asset_proposal = self._make_proposal(Proposal.PROPOSAL_TYPE_ADD_ASSET)

        request = self.factory.get('/admin/')
        request.user = self._create_user(with_manage_perm=True)

        readonly_fields = self.admin.get_readonly_fields(request, asset_proposal)

        self.assertNotIn('start_at', readonly_fields)
        self.assertNotIn('end_at', readonly_fields)
        self.assertNotIn('proposal_status', readonly_fields)
        self.assertNotIn('asset_issuer_information', readonly_fields)
        self.assertIn('onchain_execution_status', readonly_fields)

        form_class = self.admin.get_form(request, obj=asset_proposal, change=True)
        form = form_class(instance=asset_proposal)

        self.assertIn('asset_code', readonly_fields)
        self.assertIn('asset_issuer', readonly_fields)
        self.assertIn('asset_contract_address', readonly_fields)
        self.assertNotIn('asset_code', form.fields)
        self.assertNotIn('asset_issuer', form.fields)
        self.assertNotIn('asset_contract_address', form.fields)
        self.assertIn('asset_issuer_information', form.fields)
        self.assertFalse(form.fields['asset_issuer_information'].disabled)

    def test_manager_can_set_status_when_adding_asset_proposal(self):
        request = self.factory.get('/admin/')
        request.user = self._create_user(with_manage_perm=True)

        readonly_fields = self.admin.get_readonly_fields(request)

        self.assertNotIn('proposal_status', readonly_fields)

    def test_asset_payload_fieldset_is_hidden_for_existing_general_proposal(self):
        general_proposal = self._make_proposal(Proposal.PROPOSAL_TYPE_GENERAL)

        request = self.factory.get('/admin/')
        request.user = self._create_user(is_superuser=True)

        fieldset_names = [name for name, _ in self.admin.get_fieldsets(request, general_proposal)]

        self.assertNotIn('Asset payload', fieldset_names)
        self.assertNotIn('Onchain execution', fieldset_names)

    def test_admin_uses_default_single_fieldset_for_asset_proposal_and_add_form(self):
        asset_proposal = self._make_proposal(Proposal.PROPOSAL_TYPE_ADD_ASSET)

        request = self.factory.get('/admin/')
        request.user = self._create_user(is_superuser=True)

        asset_fieldset_names = [name for name, _ in self.admin.get_fieldsets(request, asset_proposal)]
        add_fieldset_names = [name for name, _ in self.admin.get_fieldsets(request)]

        self.assertEqual(asset_fieldset_names, [None])
        self.assertEqual(add_fieldset_names, [None])

    def test_add_form_exposes_asset_fields_without_custom_section_media_hook(self):
        request = self.factory.get('/admin/')
        request.user = self._create_user(is_superuser=True)

        form_class = self.admin.get_form(request)
        form = form_class()
        media = str(self.admin.media)

        self.assertIn('asset_code', form.fields)
        self.assertIn('asset_issuer', form.fields)
        self.assertIn('asset_contract_address', form.fields)
        self.assertNotIn('admin/proposal_asset_sections.js', media)

    def test_superuser_keeps_full_access(self):
        request = self.factory.get('/admin/')
        request.user = self._create_user(is_superuser=True)

        self.assertTrue(self.admin.has_add_permission(request))
        self.assertTrue(self.admin.has_change_permission(request))
        self.assertTrue(self.admin.has_view_permission(request))
