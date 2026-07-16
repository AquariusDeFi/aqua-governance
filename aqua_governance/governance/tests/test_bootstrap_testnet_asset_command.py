from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from rest_framework.test import APIClient

from stellar_sdk import Network, StrKey

from aqua_governance.governance.management.commands.bootstrap_testnet_asset import (
    BOOTSTRAP_PROPOSER,
    BOOTSTRAP_TITLE_PREFIX,
)
from aqua_governance.governance.models import AssetToken, Proposal


CONTRACT_ADDRESS = "CCYPRFTJIIOLQUSQUPQKQJ36PELTK662G5NYAMUOFM6JM7HFOGL2WYQX"
SECOND_CONTRACT_ADDRESS = StrKey.encode_contract(bytes([1]) * 32)


@override_settings(NETWORK_PASSPHRASE=Network.TESTNET_NETWORK_PASSPHRASE)
class BootstrapTestnetAssetCommandTests(TestCase):
    def run_command(self, contract_address=CONTRACT_ADDRESS):
        stdout = StringIO()
        call_command("bootstrap_testnet_asset", contract_address, stdout=stdout)
        return stdout.getvalue()

    @override_settings(NETWORK_PASSPHRASE=Network.PUBLIC_NETWORK_PASSPHRASE)
    def test_mainnet_guard_makes_zero_writes(self):
        with self.assertRaisesMessage(CommandError, "only run on Stellar testnet"):
            self.run_command()

        self.assertEqual(AssetToken.objects.count(), 0)
        self.assertEqual(Proposal.objects.count(), 0)

    def test_missing_address_fails_before_writes(self):
        with self.assertRaises(CommandError):
            call_command("bootstrap_testnet_asset")

        self.assertEqual(AssetToken.objects.count(), 0)
        self.assertEqual(Proposal.objects.count(), 0)

    def test_invalid_address_fails_before_writes(self):
        with self.assertRaisesMessage(
            CommandError, "Invalid bootstrap contract address"
        ):
            self.run_command("not-a-contract-address")

        self.assertEqual(AssetToken.objects.count(), 0)
        self.assertEqual(Proposal.objects.count(), 0)

    def test_first_run_creates_successful_visible_asset(self):
        output = self.run_command()

        token = AssetToken.objects.get(pk=CONTRACT_ADDRESS)
        proposal = Proposal.objects.get(asset_token=token)
        self.assertIn("created", output)
        self.assertIsNone(token.classic_code)
        self.assertIsNone(token.classic_issuer)
        self.assertTrue(token.whitelisted)
        self.assertEqual(token.contract_sync_status, AssetToken.CONTRACT_SYNC_SYNCED)
        self.assertEqual(proposal.proposed_by, BOOTSTRAP_PROPOSER)
        self.assertEqual(
            proposal.title, f"{BOOTSTRAP_TITLE_PREFIX}: {CONTRACT_ADDRESS}"
        )
        self.assertEqual(proposal.proposal_type, Proposal.PROPOSAL_TYPE_ADD_ASSET)
        self.assertEqual(proposal.proposal_status, Proposal.VOTED)
        self.assertEqual(
            proposal.onchain_execution_status, Proposal.ONCHAIN_EXECUTION_SUCCESS
        )
        self.assertFalse(proposal.draft)
        self.assertFalse(proposal.hide)

        response = APIClient().get("/api/asset-tokens/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 1)
        self.assertEqual(
            response.json()["results"][0]["asset_contract_address"], CONTRACT_ADDRESS
        )
        self.assertEqual(
            response.json()["results"][0]["proposals"][0]["id"], proposal.id
        )

    def test_second_run_is_idempotent(self):
        self.run_command()
        token_count = AssetToken.objects.count()
        proposal = Proposal.objects.get()

        output = self.run_command()

        self.assertIn("already present", output)
        self.assertEqual(AssetToken.objects.count(), token_count)
        self.assertEqual(Proposal.objects.count(), 1)
        self.assertEqual(Proposal.objects.get().pk, proposal.pk)

    def test_different_addresses_create_isolated_proposals(self):
        self.run_command(CONTRACT_ADDRESS)
        first_proposal = Proposal.objects.get(asset_contract_address=CONTRACT_ADDRESS)

        self.run_command(SECOND_CONTRACT_ADDRESS)

        first_proposal.refresh_from_db()
        second_proposal = Proposal.objects.get(
            asset_contract_address=SECOND_CONTRACT_ADDRESS
        )
        self.assertEqual(AssetToken.objects.count(), 2)
        self.assertEqual(Proposal.objects.count(), 2)
        self.assertNotEqual(first_proposal.pk, second_proposal.pk)
        self.assertEqual(first_proposal.asset_token_id, CONTRACT_ADDRESS)
        self.assertEqual(second_proposal.asset_token_id, SECOND_CONTRACT_ADDRESS)
        self.assertEqual(
            first_proposal.title, f"{BOOTSTRAP_TITLE_PREFIX}: {CONTRACT_ADDRESS}"
        )
        self.assertEqual(
            second_proposal.title,
            f"{BOOTSTRAP_TITLE_PREFIX}: {SECOND_CONTRACT_ADDRESS}",
        )

    def test_partial_existing_rows_are_repaired_without_duplication(self):
        self.run_command()
        proposal = Proposal.objects.get()
        AssetToken.objects.filter(pk=CONTRACT_ADDRESS).update(
            whitelisted=False,
            whitelisted_since=None,
            contract_sync_status=AssetToken.CONTRACT_SYNC_FAILED,
        )
        Proposal.objects.filter(pk=proposal.pk).update(
            asset_token=None,
            draft=True,
            hide=True,
            proposal_status=Proposal.DISCUSSION,
            onchain_execution_status=Proposal.ONCHAIN_EXECUTION_FAILED,
        )

        output = self.run_command()

        self.assertIn("repaired", output)
        self.assertEqual(AssetToken.objects.count(), 1)
        self.assertEqual(Proposal.objects.count(), 1)
        token = AssetToken.objects.get()
        proposal.refresh_from_db()
        self.assertTrue(token.whitelisted)
        self.assertIsNotNone(token.whitelisted_since)
        self.assertEqual(token.contract_sync_status, AssetToken.CONTRACT_SYNC_SYNCED)
        self.assertEqual(proposal.asset_token_id, CONTRACT_ADDRESS)
        self.assertFalse(proposal.draft)
        self.assertFalse(proposal.hide)
        self.assertEqual(proposal.proposal_status, Proposal.VOTED)
        self.assertEqual(
            proposal.onchain_execution_status, Proposal.ONCHAIN_EXECUTION_SUCCESS
        )
