import json
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from django_quill.quill import Quill
from stellar_sdk import Network

from aqua_governance.governance.asset_payload import derive_onchain_action_args
from aqua_governance.governance.models import AssetToken, Proposal


BOOTSTRAP_PROPOSER = "GA5WUJ54Z23KILLCUOUNAKTPBVZWKMQVO4O6EQ5GHLAERIMLLHNCSKYH"
BOOTSTRAP_TITLE_PREFIX = "[TESTNET BOOTSTRAP] Add Soroban asset contract"
BOOTSTRAP_TEXT = Quill(
    json.dumps(
        {
            "delta": {"ops": [{"insert": "Synthetic testnet bootstrap record.\n"}]},
            "html": "<p>Synthetic testnet bootstrap record.</p>",
        }
    )
)


class Command(BaseCommand):
    help = "Idempotently bootstrap an approved testnet Soroban asset contract."

    def add_arguments(self, parser):
        parser.add_argument("contract_address")

    def handle(self, *args, **options):
        if settings.NETWORK_PASSPHRASE != Network.TESTNET_NETWORK_PASSPHRASE:
            raise CommandError("This command may only run on Stellar testnet.")

        try:
            contract_address = derive_onchain_action_args(
                asset_code=None,
                asset_issuer=None,
                asset_contract_address=options["contract_address"],
            )[0]
        except ValueError as exc:
            raise CommandError(f"Invalid bootstrap contract address: {exc}") from exc

        bootstrap_title = f"{BOOTSTRAP_TITLE_PREFIX}: {contract_address}"

        with transaction.atomic():
            now = timezone.now()
            token, token_created = AssetToken.objects.get_or_create(
                contract_address=contract_address,
                defaults={
                    "whitelisted": True,
                    "whitelisted_since": now,
                    "last_execution_at": now,
                    "contract_sync_status": AssetToken.CONTRACT_SYNC_SYNCED,
                    "contract_sync_updated_at": now,
                },
            )
            token_updates = {}
            intended_token_values = {
                "whitelisted": True,
                "unwhitelisted_since": None,
                "contract_sync_status": AssetToken.CONTRACT_SYNC_SYNCED,
                "contract_sync_tx_hash": None,
                "contract_sync_error": None,
            }
            for field_name, intended_value in intended_token_values.items():
                if getattr(token, field_name) != intended_value:
                    token_updates[field_name] = intended_value
            if token.whitelisted_since is None:
                token_updates["whitelisted_since"] = now
            if token.last_execution_at is None:
                token_updates["last_execution_at"] = now
            if token.contract_sync_updated_at is None:
                token_updates["contract_sync_updated_at"] = now
            if token_updates:
                AssetToken.objects.filter(pk=contract_address).update(**token_updates)

            proposal = Proposal.objects.filter(
                proposed_by=BOOTSTRAP_PROPOSER,
                title=bootstrap_title,
                proposal_type=Proposal.PROPOSAL_TYPE_ADD_ASSET,
                asset_contract_address=contract_address,
            ).first()
            proposal_created = proposal is None
            if proposal_created:
                proposal = Proposal(
                    proposed_by=BOOTSTRAP_PROPOSER,
                    title=bootstrap_title,
                    text=BOOTSTRAP_TEXT,
                    vote_for_issuer=BOOTSTRAP_PROPOSER,
                    vote_against_issuer=BOOTSTRAP_PROPOSER,
                    abstain_issuer=BOOTSTRAP_PROPOSER,
                    start_at=now,
                    end_at=now,
                    hide=False,
                    draft=False,
                    status=Proposal.FINE,
                    proposal_status=Proposal.VOTED,
                    payment_status=Proposal.FINE,
                    vote_for_result=Decimal("1"),
                    vote_against_result=Decimal("0"),
                    vote_abstain_result=Decimal("0"),
                    proposal_type=Proposal.PROPOSAL_TYPE_ADD_ASSET,
                    action=Proposal.NONE,
                    asset_code=None,
                    asset_issuer=None,
                    asset_contract_address=contract_address,
                    asset_token=token,
                    onchain_execution_status=Proposal.ONCHAIN_EXECUTION_SUCCESS,
                )
                Proposal.objects.bulk_create([proposal])
                proposal_updates = {}
            else:
                proposal_updates = {}
                intended_proposal_values = {
                    "vote_for_issuer": BOOTSTRAP_PROPOSER,
                    "vote_against_issuer": BOOTSTRAP_PROPOSER,
                    "abstain_issuer": BOOTSTRAP_PROPOSER,
                    "hide": False,
                    "draft": False,
                    "status": Proposal.FINE,
                    "proposal_status": Proposal.VOTED,
                    "payment_status": Proposal.FINE,
                    "vote_for_result": Decimal("1"),
                    "vote_against_result": Decimal("0"),
                    "vote_abstain_result": Decimal("0"),
                    "action": Proposal.NONE,
                    "asset_code": None,
                    "asset_issuer": None,
                    "asset_contract_address": contract_address,
                    "asset_token_id": contract_address,
                    "onchain_execution_status": Proposal.ONCHAIN_EXECUTION_SUCCESS,
                    "onchain_execution_tx_hash": None,
                    "onchain_execution_started_at": None,
                    "onchain_execution_submitted_at": None,
                    "onchain_execution_poll_count": 0,
                }
                for field_name, intended_value in intended_proposal_values.items():
                    if getattr(proposal, field_name) != intended_value:
                        proposal_updates[field_name] = intended_value
                if proposal.start_at is None:
                    proposal_updates["start_at"] = now
                if proposal.end_at is None:
                    proposal_updates["end_at"] = now
                if proposal_updates:
                    proposal_updates["last_updated_at"] = now
                    Proposal.objects.filter(pk=proposal.pk).update(**proposal_updates)

        if token_created or proposal_created:
            outcome = "created"
        elif token_updates or proposal_updates:
            outcome = "repaired"
        else:
            outcome = "already present"
        self.stdout.write(
            self.style.SUCCESS(
                f"Testnet bootstrap asset {contract_address}: {outcome}."
            )
        )
