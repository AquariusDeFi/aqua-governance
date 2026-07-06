import json
from datetime import timedelta
from unittest.mock import Mock, patch

from django.contrib import admin
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser, Permission
from django.conf import settings
from django.test import RequestFactory, TestCase
from django.utils import timezone
from django_quill.quill import Quill
from rest_framework.test import APIClient
from stellar_sdk import Asset

from aqua_governance.governance.admin import AssetTokenAdmin, ProposalAdmin
from aqua_governance.governance.asset_tokens import (
    apply_asset_proposal_result_to_token,
    upsert_asset_token_from_proposal,
)
from aqua_governance.governance.models import AssetToken, Proposal
from aqua_governance.governance.tasks import _sync_asset_token_on_success
from aqua_governance.governance.tests._factories import patch_ice_circulating_supply


DEFAULT_PROPOSED_BY = 'GAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAWHF'
DEFAULT_CODE = 'AQUA'
DEFAULT_ISSUER = 'GBNZILSTVQZ4R7IKQDGHYGY2QXL5QOFJYQMXPKWRRM5PAV7Y4M67AQUA'


def quill_text(html='<p>x</p>'):
    return Quill(json.dumps({'delta': {'ops': []}, 'html': html}))


def patch_ice_supply():
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.json.return_value = {'ice_supply_amount': 0}
    return patch('aqua_governance.governance.models.requests.get', return_value=mock_response)


def asset_narratives():
    return {
        'asset_issuer_information': 'info',
        'asset_token_description': 'desc',
        'asset_holder_distribution': 'dist',
        'asset_liquidity': 'liq',
        'asset_trading_volume': 'vol',
        'asset_audit_info': 'audit',
        'asset_stellar_flags': 'flags',
        'asset_related_projects': 'projects',
        'asset_community_references': 'refs',
        'asset_aquarius_traction': 'traction',
        'asset_issuer_commitments': 'commitments',
    }


def create_proposal(**overrides):
    defaults = {
        'proposed_by': DEFAULT_PROPOSED_BY,
        'title': 'Test proposal',
        'text': quill_text(),
        'draft': False,
        'action': Proposal.NONE,
        'proposal_status': Proposal.DISCUSSION,
    }
    defaults.update(overrides)
    with patch_ice_supply():
        return Proposal.objects.create(**defaults)


class SimplifiedAssetTokenTests(TestCase):
    def setUp(self):
        super().setUp()
        self.ice_supply_patcher = patch_ice_circulating_supply()
        self.ice_supply_patcher.start()
        self.addCleanup(self.ice_supply_patcher.stop)

    def test_upsert_derives_contract_and_links_proposal(self):
        proposal = create_proposal(
            proposal_type=Proposal.PROPOSAL_TYPE_ADD_ASSET,
            asset_code=DEFAULT_CODE,
            asset_issuer=DEFAULT_ISSUER,
        )

        token = upsert_asset_token_from_proposal(proposal)
        derived = Asset(DEFAULT_CODE, DEFAULT_ISSUER).contract_id(settings.NETWORK_PASSPHRASE)

        proposal.refresh_from_db()
        self.assertEqual(token.contract_address, derived)
        self.assertEqual(proposal.asset_contract_address, derived)
        self.assertEqual(proposal.asset_token_id, derived)
        self.assertEqual(token.classic_code, DEFAULT_CODE)
        self.assertEqual(token.classic_issuer, DEFAULT_ISSUER)

    def test_general_create_rejects_asset_fields(self):
        with patch('aqua_governance.governance.serializers_v2.check_transaction_xdr', return_value=Proposal.FINE):
            response = APIClient().post('/api/proposal/', {
                'proposed_by': DEFAULT_PROPOSED_BY,
                'title': 'General proposal',
                'text': '<p>x</p>',
                'transaction_hash': 'a' * 64,
                'envelope_xdr': 'xdr',
                'discord_username': 'user',
                'asset_code': DEFAULT_CODE,
            }, format='json')

        self.assertEqual(response.status_code, 400)
        self.assertIn('asset_code', response.data)

    def test_asset_create_links_asset_token(self):
        payload = {
            'proposed_by': DEFAULT_PROPOSED_BY,
            'title': 'Asset proposal',
            'text': '<p>x</p>',
            'transaction_hash': 'b' * 64,
            'envelope_xdr': 'xdr',
            'discord_username': 'user',
            'proposal_type': Proposal.PROPOSAL_TYPE_ADD_ASSET,
            'asset_code': DEFAULT_CODE,
            'asset_issuer': DEFAULT_ISSUER,
            **asset_narratives(),
        }

        with patch('aqua_governance.governance.serializers_v2.check_transaction_xdr', return_value=Proposal.FINE):
            response = APIClient().post('/api/asset-proposal/', payload, format='json')

        self.assertEqual(response.status_code, 201)
        proposal = Proposal.objects.get(id=response.data['id'])
        derived = Asset(DEFAULT_CODE, DEFAULT_ISSUER).contract_id(settings.NETWORK_PASSPHRASE)
        self.assertEqual(proposal.proposal_type, Proposal.PROPOSAL_TYPE_ADD_ASSET)
        self.assertEqual(proposal.asset_contract_address, derived)
        self.assertEqual(proposal.asset_token_id, derived)
        self.assertTrue(AssetToken.objects.filter(contract_address=derived).exists())

    def test_asset_token_view_uses_stored_tokens_and_nulls_last(self):
        executed = AssetToken.objects.create(
            contract_address='C' + 'A' * 55,
            classic_code='EXE',
            classic_issuer=DEFAULT_ISSUER,
            whitelisted=True,
            last_execution_at=timezone.now(),
        )
        unexecuted = AssetToken.objects.create(
            contract_address='C' + 'B' * 55,
            classic_code='NEW',
            classic_issuer=DEFAULT_ISSUER,
        )
        create_proposal(proposal_type=Proposal.PROPOSAL_TYPE_ADD_ASSET, asset_token=executed)
        create_proposal(proposal_type=Proposal.PROPOSAL_TYPE_ADD_ASSET, asset_token=unexecuted)

        response = APIClient().get('/api/asset-tokens/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['results'][0]['asset_contract_address'], executed.contract_address)
        self.assertEqual(response.data['results'][1]['asset_contract_address'], unexecuted.contract_address)

    def test_asset_token_view_includes_hidden_linked_proposals_but_not_them_in_nested_list(self):
        token = AssetToken.objects.create(
            contract_address='C' + 'D' * 55,
            classic_code='HID',
            classic_issuer=DEFAULT_ISSUER,
        )
        create_proposal(
            proposal_type=Proposal.PROPOSAL_TYPE_ADD_ASSET,
            asset_token=token,
            hide=True,
        )

        response = APIClient().get('/api/asset-tokens/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['results'][0]['asset_contract_address'], token.contract_address)
        self.assertEqual(response.data['results'][0]['proposals'], [])

    def test_asset_token_view_excludes_no_linked_or_draft_only_tokens(self):
        AssetToken.objects.create(
            contract_address='C' + 'N' * 55,
            classic_code='NONE',
            classic_issuer=DEFAULT_ISSUER,
        )
        draft_token = AssetToken.objects.create(
            contract_address='C' + 'E' * 55,
            classic_code='DRA',
            classic_issuer=DEFAULT_ISSUER,
        )
        create_proposal(
            proposal_type=Proposal.PROPOSAL_TYPE_ADD_ASSET,
            asset_token=draft_token,
            draft=True,
        )

        response = APIClient().get('/api/asset-tokens/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['count'], 0)
        self.assertEqual(response.data['results'], [])

    def test_success_sync_whitelists_asset_token(self):
        token = AssetToken.objects.create(
            contract_address='C' + 'C' * 55,
            classic_code=DEFAULT_CODE,
            classic_issuer=DEFAULT_ISSUER,
        )
        proposal = create_proposal(
            proposal_type=Proposal.PROPOSAL_TYPE_ADD_ASSET,
            proposal_status=Proposal.VOTED,
            onchain_execution_status=Proposal.ONCHAIN_EXECUTION_SUBMITTED,
            onchain_execution_tx_hash='c' * 64,
            asset_token=token,
        )

        # Step 1 — DB-first: apply_asset_proposal_result_to_token whitelists + sets PENDING
        apply_asset_proposal_result_to_token(proposal)
        token.refresh_from_db()
        self.assertTrue(token.whitelisted)
        self.assertIsNotNone(token.whitelisted_since)
        self.assertIsNotNone(token.last_execution_at)
        self.assertEqual(token.contract_sync_status, AssetToken.CONTRACT_SYNC_PENDING)

        # Step 2 — on-chain confirmation: _sync_asset_token_on_success marks SYNCED + SUCCESS
        _sync_asset_token_on_success(proposal.id)
        proposal.refresh_from_db()
        token.refresh_from_db()
        self.assertEqual(proposal.onchain_execution_status, Proposal.ONCHAIN_EXECUTION_SUCCESS)
        self.assertEqual(token.contract_sync_status, AssetToken.CONTRACT_SYNC_SYNCED)
        self.assertIsNotNone(token.contract_sync_updated_at)
        # whitelist state is preserved by _sync_asset_token_on_success
        self.assertTrue(token.whitelisted)

    def test_apply_asset_proposal_result_add_whitelists_token(self):
        token = AssetToken.objects.create(
            contract_address='C' + 'E' * 55,
            classic_code=DEFAULT_CODE,
            classic_issuer=DEFAULT_ISSUER,
        )
        proposal = create_proposal(
            proposal_type=Proposal.PROPOSAL_TYPE_ADD_ASSET,
            proposal_status=Proposal.VOTED,
            asset_token=token,
        )

        result = apply_asset_proposal_result_to_token(proposal)

        token.refresh_from_db()
        self.assertIsNotNone(result)
        self.assertEqual(result.pk, token.pk)
        self.assertTrue(token.whitelisted)
        self.assertIsNotNone(token.whitelisted_since)
        self.assertIsNotNone(token.last_execution_at)
        self.assertEqual(token.contract_sync_status, AssetToken.CONTRACT_SYNC_PENDING)

    def test_apply_asset_proposal_result_remove_unwhitelists_token(self):
        whitelisted_since = timezone.now() - timedelta(days=30)
        token = AssetToken.objects.create(
            contract_address='C' + 'F' * 55,
            classic_code=DEFAULT_CODE,
            classic_issuer=DEFAULT_ISSUER,
            whitelisted=True,
            whitelisted_since=whitelisted_since,
        )
        proposal = create_proposal(
            proposal_type=Proposal.PROPOSAL_TYPE_REMOVE_ASSET,
            proposal_status=Proposal.VOTED,
            asset_token=token,
        )

        result = apply_asset_proposal_result_to_token(proposal)

        token.refresh_from_db()
        self.assertIsNotNone(result)
        self.assertEqual(result.pk, token.pk)
        self.assertFalse(token.whitelisted)
        self.assertIsNotNone(token.unwhitelisted_since)
        self.assertIsNotNone(token.last_execution_at)
        self.assertEqual(token.whitelisted_since, whitelisted_since)
        self.assertEqual(token.contract_sync_status, AssetToken.CONTRACT_SYNC_PENDING)

    def test_apply_asset_proposal_result_refreshes_whitelisted_since_on_re_add(self):
        """After add → remove → add cycles, whitelisted_since should reflect
        the current transition, not the first one."""
        # 1. Fresh token
        token = AssetToken.objects.create(
            contract_address='C' + 'G' * 55,
            classic_code=DEFAULT_CODE,
            classic_issuer=DEFAULT_ISSUER,
        )
        # ADD
        add1 = create_proposal(
            proposal_type=Proposal.PROPOSAL_TYPE_ADD_ASSET,
            proposal_status=Proposal.VOTED,
            asset_token=token,
        )
        apply_asset_proposal_result_to_token(add1)
        token.refresh_from_db()
        whitelisted_since_1 = token.whitelisted_since
        self.assertIsNotNone(whitelisted_since_1)
        self.assertIsNone(token.unwhitelisted_since)

        # 2. REMOVE — should preserve whitelisted_since as historical
        remove = create_proposal(
            proposal_type=Proposal.PROPOSAL_TYPE_REMOVE_ASSET,
            proposal_status=Proposal.VOTED,
            asset_token=token,
        )
        apply_asset_proposal_result_to_token(remove)
        token.refresh_from_db()
        unwhitelisted_since = token.unwhitelisted_since
        self.assertIsNotNone(unwhitelisted_since)
        self.assertEqual(token.whitelisted_since, whitelisted_since_1)  # preserved

        # 3. ADD again — whitelisted_since should be NEW (current transition)
        add2 = create_proposal(
            proposal_type=Proposal.PROPOSAL_TYPE_ADD_ASSET,
            proposal_status=Proposal.VOTED,
            asset_token=token,
        )
        apply_asset_proposal_result_to_token(add2)
        token.refresh_from_db()
        self.assertTrue(token.whitelisted)
        self.assertIsNotNone(token.whitelisted_since)
        self.assertGreater(token.whitelisted_since, whitelisted_since_1)  # fresh
        self.assertEqual(token.unwhitelisted_since, unwhitelisted_since)  # preserved

    def test_apply_asset_proposal_result_idempotent_on_retry(self):
        """Retry of the same ADD_ASSET proposal does not overwrite the timestamp."""
        token = AssetToken.objects.create(
            contract_address='C' + 'H' * 55,
            classic_code=DEFAULT_CODE,
            classic_issuer=DEFAULT_ISSUER,
        )
        proposal = create_proposal(
            proposal_type=Proposal.PROPOSAL_TYPE_ADD_ASSET,
            proposal_status=Proposal.VOTED,
            asset_token=token,
        )

        # First call — sets whitelisted_since
        apply_asset_proposal_result_to_token(proposal)
        token.refresh_from_db()
        first_since = token.whitelisted_since
        self.assertIsNotNone(first_since)

        # Retry (token already whitelisted) — timestamp unchanged
        apply_asset_proposal_result_to_token(proposal)
        token.refresh_from_db()
        self.assertEqual(token.whitelisted_since, first_since)
        self.assertTrue(token.whitelisted)

    def test_asset_create_without_proposal_type_returns_400(self):
        payload = {
            'proposed_by': DEFAULT_PROPOSED_BY,
            'title': 'Asset proposal',
            'text': '<p>x</p>',
            'transaction_hash': 'g' * 64,
            'envelope_xdr': 'xdr',
            'discord_username': 'user',
            'asset_code': DEFAULT_CODE,
            'asset_issuer': DEFAULT_ISSUER,
            **asset_narratives(),
        }
        with patch('aqua_governance.governance.serializers_v2.check_transaction_xdr', return_value=Proposal.FINE):
            response = APIClient().post('/api/asset-proposal/', payload, format='json')

        self.assertEqual(response.status_code, 400)
        self.assertIn('proposal_type', response.data)

    def test_general_create_rejects_asset_proposal_type(self):
        with patch('aqua_governance.governance.serializers_v2.check_transaction_xdr', return_value=Proposal.FINE):
            response = APIClient().post('/api/proposal/', {
                'proposed_by': DEFAULT_PROPOSED_BY,
                'title': 'General proposal',
                'text': '<p>x</p>',
                'transaction_hash': 'h' * 64,
                'envelope_xdr': 'xdr',
                'discord_username': 'user',
                'proposal_type': Proposal.PROPOSAL_TYPE_ADD_ASSET,
            }, format='json')

        self.assertEqual(response.status_code, 400)
        self.assertIn('proposal_type', response.data)

    def test_asset_create_rejects_mismatched_contract_address(self):
        wrong_address = Asset('BAD', DEFAULT_ISSUER).contract_id(settings.NETWORK_PASSPHRASE)
        payload = {
            'proposed_by': DEFAULT_PROPOSED_BY,
            'title': 'Asset proposal',
            'text': '<p>x</p>',
            'transaction_hash': 'i' * 64,
            'envelope_xdr': 'xdr',
            'discord_username': 'user',
            'proposal_type': Proposal.PROPOSAL_TYPE_ADD_ASSET,
            'asset_code': DEFAULT_CODE,
            'asset_issuer': DEFAULT_ISSUER,
            'asset_contract_address': wrong_address,
            **asset_narratives(),
        }
        with patch('aqua_governance.governance.serializers_v2.check_transaction_xdr', return_value=Proposal.FINE):
            response = APIClient().post('/api/asset-proposal/', payload, format='json')

        self.assertEqual(response.status_code, 400)
        # The error mapper prioritises the "asset_issuer" key when the message
        # contains that substring, so the mismatch surfaces on asset_issuer.
        self.assertIn('asset_issuer', response.data)

    def test_success_sync_unwhitelists_asset_token_on_remove(self):
        whitelisted_since = timezone.now() - timedelta(days=30)
        token = AssetToken.objects.create(
            contract_address='C' + 'D' * 55,
            classic_code=DEFAULT_CODE,
            classic_issuer=DEFAULT_ISSUER,
            whitelisted=True,
            whitelisted_since=whitelisted_since,
        )
        proposal = create_proposal(
            proposal_type=Proposal.PROPOSAL_TYPE_REMOVE_ASSET,
            proposal_status=Proposal.VOTED,
            onchain_execution_status=Proposal.ONCHAIN_EXECUTION_SUBMITTED,
            onchain_execution_tx_hash='d' * 64,
            asset_token=token,
        )

        # Step 1 — DB-first: apply_asset_proposal_result_to_token unwhitelists + sets PENDING
        apply_asset_proposal_result_to_token(proposal)
        token.refresh_from_db()
        self.assertFalse(token.whitelisted)
        self.assertIsNotNone(token.unwhitelisted_since)
        self.assertIsNotNone(token.last_execution_at)
        self.assertEqual(token.contract_sync_status, AssetToken.CONTRACT_SYNC_PENDING)

        # Step 2 — on-chain confirmation: _sync_asset_token_on_success marks SYNCED + SUCCESS
        _sync_asset_token_on_success(proposal.id)
        proposal.refresh_from_db()
        token.refresh_from_db()
        self.assertEqual(proposal.onchain_execution_status, Proposal.ONCHAIN_EXECUTION_SUCCESS)
        self.assertFalse(token.whitelisted)
        self.assertEqual(token.contract_sync_status, AssetToken.CONTRACT_SYNC_SYNCED)
        self.assertIsNotNone(token.contract_sync_updated_at)
        self.assertEqual(token.whitelisted_since, whitelisted_since)

    def test_admin_save_links_asset_token(self):
        proposal = Proposal(
            proposed_by=DEFAULT_PROPOSED_BY,
            title='Admin asset proposal',
            text=quill_text(),
            proposal_type=Proposal.PROPOSAL_TYPE_ADD_ASSET,
            asset_code=DEFAULT_CODE,
            asset_issuer=DEFAULT_ISSUER,
            draft=False,
            action=Proposal.NONE,
            payment_status=Proposal.FINE,
        )
        request = Mock()
        request.user.is_superuser = True

        with patch_ice_supply():
            ProposalAdmin(Proposal, admin.site).save_model(request, proposal, form=None, change=False)

        derived = Asset(DEFAULT_CODE, DEFAULT_ISSUER).contract_id(settings.NETWORK_PASSPHRASE)
        proposal.refresh_from_db()
        self.assertEqual(proposal.asset_token_id, derived)
        self.assertTrue(AssetToken.objects.filter(contract_address=derived).exists())


class AssetTokenAdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.site = AdminSite()
        cls.admin = AssetTokenAdmin(AssetToken, cls.site)
        cls.factory = RequestFactory()

    def _create_user(self, *, is_superuser=False, with_manage_perm=False, is_staff=True):
        user_model = get_user_model()
        user = user_model.objects.create_user(
            username=f'tokenadmin_{is_superuser}_{with_manage_perm}',
            password='password',
            is_staff=is_staff,
            is_superuser=is_superuser,
        )
        if with_manage_perm:
            perm = Permission.objects.get(codename='manage_asset_proposals')
            user.user_permissions.add(perm)
        return user

    def _request(self, user):
        request = self.factory.get('/admin/')
        request.user = user
        return request

    def test_asset_token_is_registered_in_admin(self):
        self.assertIn(AssetToken, admin.site._registry)

    def test_has_add_permission_superuser_and_manager(self):
        superuser = self._create_user(is_superuser=True)
        self.assertTrue(self.admin.has_add_permission(self._request(superuser)))

        manager = self._create_user(with_manage_perm=True)
        self.assertTrue(self.admin.has_add_permission(self._request(manager)))

        regular = self._create_user()
        self.assertFalse(self.admin.has_add_permission(self._request(regular)))

    def test_has_change_permission_always_false(self):
        token = AssetToken.objects.create(contract_address='C' + 'A' * 55)
        for user in [
            self._create_user(is_superuser=True),
            self._create_user(with_manage_perm=True),
            self._create_user(),
        ]:
            with self.subTest(user=user.username):
                self.assertFalse(self.admin.has_change_permission(self._request(user)))
                self.assertFalse(self.admin.has_change_permission(self._request(user), token))

    def test_has_delete_permission_always_false(self):
        token = AssetToken.objects.create(contract_address='C' + 'B' * 55)
        for user in [
            self._create_user(is_superuser=True),
            self._create_user(with_manage_perm=True),
            self._create_user(),
        ]:
            with self.subTest(user=user.username):
                self.assertFalse(self.admin.has_delete_permission(self._request(user)))
                self.assertFalse(self.admin.has_delete_permission(self._request(user), token))

    def test_has_view_permission_superuser(self):
        user = self._create_user(is_superuser=True)
        self.assertTrue(self.admin.has_view_permission(self._request(user)))
        self.assertTrue(self.admin.has_view_permission(self._request(user), None))

    def test_has_view_permission_asset_manager(self):
        user = self._create_user(with_manage_perm=True)
        self.assertTrue(self.admin.has_view_permission(self._request(user)))
        self.assertTrue(self.admin.has_view_permission(self._request(user), None))

    def test_has_view_permission_regular_user_denied(self):
        user = self._create_user()
        self.assertFalse(self.admin.has_view_permission(self._request(user)))

    def test_has_module_permission_asset_manager(self):
        user = self._create_user(with_manage_perm=True)
        self.assertTrue(self.admin.has_module_permission(self._request(user)))

    def test_has_module_permission_regular_user_denied(self):
        user = self._create_user()
        self.assertFalse(self.admin.has_module_permission(self._request(user)))

    def test_has_view_permission_unauthenticated_denied(self):
        request = self.factory.get('/admin/')
        request.user = AnonymousUser()
        self.assertFalse(self.admin.has_view_permission(request))

    def test_list_display_includes_expected_fields(self):
        expected = [
            'contract_address',
            'classic_code',
            'classic_issuer',
            'whitelisted',
            'whitelisted_since',
            'unwhitelisted_since',
            'last_execution_at',
            'contract_sync_status',
            'contract_sync_tx_hash',
            'contract_sync_updated_at',
            '_proposal_count',
            'created_at',
            'updated_at',
        ]
        self.assertEqual(self.admin.list_display, expected)

    def test_proposal_count_zero(self):
        token = AssetToken.objects.create(contract_address='C' + 'C' * 55)
        self.assertEqual(self.admin._proposal_count(token), 0)

    def test_proposal_count_with_linked_proposals(self):
        token = AssetToken.objects.create(contract_address='C' + 'D' * 55)
        create_proposal(proposal_type=Proposal.PROPOSAL_TYPE_ADD_ASSET, asset_token=token)
        create_proposal(proposal_type=Proposal.PROPOSAL_TYPE_REMOVE_ASSET, asset_token=token)
        self.assertEqual(self.admin._proposal_count(token), 2)

    def test_all_fields_are_readonly(self):
        model_field_names = {f.name for f in AssetToken._meta.get_fields() if f.concrete}
        admin_readonly = set(self.admin.readonly_fields)
        self.assertTrue(model_field_names.issubset(admin_readonly),
                        f'Missing readonly: {model_field_names - admin_readonly}')

    def test_get_fields_on_add(self):
        request = self._request(self._create_user(is_superuser=True))
        fields = self.admin.get_fields(request, obj=None)
        self.assertEqual(fields, ['contract_address', 'classic_code', 'classic_issuer'])

    def test_get_readonly_fields_excludes_editable_on_add(self):
        request = self._request(self._create_user(is_superuser=True))
        ro = self.admin.get_readonly_fields(request, obj=None)
        self.assertNotIn('contract_address', ro)
        self.assertNotIn('classic_code', ro)
        self.assertNotIn('classic_issuer', ro)
        self.assertIn('whitelisted', ro)
        self.assertIn('contract_sync_status', ro)

    def test_get_readonly_fields_all_readonly_when_change(self):
        token = AssetToken.objects.create(contract_address='C' + 'E' * 55)
        request = self._request(self._create_user(is_superuser=True))
        ro = self.admin.get_readonly_fields(request, obj=token)
        self.assertIn('contract_address', ro)
        self.assertIn('classic_code', ro)
        self.assertIn('classic_issuer', ro)

    def test_add_form_derives_contract_address_from_classic_pair(self):
        request = self._request(self._create_user(is_superuser=True))
        form_class = self.admin.get_form(request, obj=None)
        form = form_class(data={
            'classic_code': DEFAULT_CODE,
            'classic_issuer': DEFAULT_ISSUER,
        })
        self.assertTrue(form.is_valid(), msg=form.errors)
        derived = Asset(DEFAULT_CODE, DEFAULT_ISSUER).contract_id(settings.NETWORK_PASSPHRASE)
        self.assertEqual(form.cleaned_data['contract_address'], derived)

    def test_add_form_rejects_mismatched_contract_address(self):
        request = self._request(self._create_user(is_superuser=True))
        form_class = self.admin.get_form(request, obj=None)
        form = form_class(data={
            'classic_code': DEFAULT_CODE,
            'classic_issuer': DEFAULT_ISSUER,
            'contract_address': 'C' + 'X' * 55,
        })
        self.assertFalse(form.is_valid())
        self.assertIn('contract_address', form.errors)

    def test_add_form_accepts_contract_address_only(self):
        request = self._request(self._create_user(is_superuser=True))
        form_class = self.admin.get_form(request, obj=None)
        form = form_class(data={
            'contract_address': 'CA3Q2KZ2F4V5XUQ6P7YJSVSV2Y2NF7QPAX5LQZ5Z5Z5Z5Z5Z5Z5Z5Z5Z5',
        })
        self.assertTrue(form.is_valid(), msg=form.errors)
