import logging
from typing import Optional

from django.conf import settings
from stellar_sdk import Keypair, SorobanServer, TransactionBuilder, scval
from stellar_sdk.exceptions import PrepareTransactionException
from stellar_sdk.soroban_rpc import SendTransactionStatus

from aqua_governance.governance.models import Proposal
from aqua_governance.governance.asset_payload import normalize_asset_addresses


logger = logging.getLogger(__name__)


def execute_asset_registry_action(proposal: Proposal, args: list[str], allowed: bool) -> Optional[str]:
    contract_id = _get_required_setting("ONCHAIN_ASSET_REGISTRY_CONTRACT_ID")
    manager_secret = _get_required_setting("ONCHAIN_ASSET_REGISTRY_MANAGER_SECRET")
    rpc_url = _get_required_setting("SOROBAN_RPC_URL")

    manager_keypair = Keypair.from_secret(manager_secret)
    manager_address = manager_keypair.public_key
    assets = normalize_asset_addresses(args)
    meta_hash = _build_empty_meta_hash()

    server = SorobanServer(rpc_url)
    actions = [
        scval.to_struct({
            "asset": scval.to_address(asset_address),
            "allowed": scval.to_bool(allowed),
        })
        for asset_address in assets
    ]
    parameters = [
        scval.to_address(manager_address),
        scval.to_uint64(proposal.id),
        scval.to_vec(actions),
        scval.to_bytes(meta_hash),
    ]
    tx_hash = _send_execute_proposal_transaction(
        server=server,
        manager_address=manager_address,
        manager_keypair=manager_keypair,
        contract_id=contract_id,
        parameters=parameters,
        proposal_id=proposal.id,
    )
    return tx_hash


def _send_execute_proposal_transaction(
    server: SorobanServer,
    manager_address: str,
    manager_keypair: Keypair,
    contract_id: str,
    parameters: list,
    proposal_id: int,
) -> str:
    send_result = None

    for attempt in range(2):
        prepared_transaction = _prepare_execute_proposal_transaction(
            server=server,
            manager_address=manager_address,
            manager_keypair=manager_keypair,
            contract_id=contract_id,
            parameters=parameters,
        )
        send_result = server.send_transaction(prepared_transaction)
        if send_result.status != SendTransactionStatus.ERROR:
            return send_result.hash

        if attempt == 0:
            logger.warning(
                "Soroban send failed for proposal %s on first attempt; retrying with refreshed sequence. "
                "hash=%s error_xdr=%s",
                proposal_id,
                send_result.hash,
                send_result.error_result_xdr,
            )

    raise RuntimeError(
        f"Soroban send failed after retry. hash={send_result.hash} error_xdr={send_result.error_result_xdr}",
    )


def _prepare_execute_proposal_transaction(
    server: SorobanServer,
    manager_address: str,
    manager_keypair: Keypair,
    contract_id: str,
    parameters: list,
):
    source_account = server.load_account(manager_address)
    transaction = (
        TransactionBuilder(
            source_account,
            settings.NETWORK_PASSPHRASE,
            base_fee=settings.ONCHAIN_SOROBAN_BASE_FEE,
        )
        .set_timeout(settings.ONCHAIN_SOROBAN_TIMEOUT)
        .append_invoke_contract_function_op(
            contract_id=contract_id,
            function_name="execute_proposal",
            parameters=parameters,
        )
        .build()
    )

    try:
        prepared_transaction = server.prepare_transaction(transaction)
    except PrepareTransactionException as exc:
        simulation_error = getattr(exc.simulate_transaction_response, "error", None)
        raise RuntimeError(
            f"Failed to prepare onchain transaction. simulation_error={simulation_error}",
        ) from exc

    prepared_transaction.sign(manager_keypair)
    return prepared_transaction


def _build_empty_meta_hash() -> bytes:
    # Contract expects BytesN<32>; use all-zero payload until a canonical meta hash format is defined.
    return bytes(32)


def _get_required_setting(name: str) -> str:
    value = getattr(settings, name, "")
    if not value:
        raise RuntimeError(f"Required setting `{name}` is not configured.")
    return value
