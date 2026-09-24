from __future__ import annotations

import logging
from typing import Any, Optional

from stellar_sdk import Server

logger = logging.getLogger(__name__)


def find_origin_claimable_balance_id(
    horizon_server: Server,
    start_balance_id: str,
    *,
    max_depth: int = 120,
    per_call_limit: int = 200,
) -> Optional[str]:
    """
    Trace claimable-balance replacement chain backwards until first create operation
    where sponsor is among claimant destinations.

    Returns origin balance id on success, otherwise None.
    """
    if not start_balance_id:
        logger.warning("Origin trace skipped: empty start_balance_id")
        return None
    if max_depth < 1 or per_call_limit < 1:
        logger.warning(
            "Origin trace skipped for %s: invalid limits (max_depth=%s per_call_limit=%s)",
            start_balance_id,
            max_depth,
            per_call_limit,
        )
        return None

    logger.info(
        "Origin trace started for balance_id=%s (max_depth=%s per_call_limit=%s)",
        start_balance_id,
        max_depth,
        per_call_limit,
    )

    current_balance_id = start_balance_id
    for depth in range(max_depth):
        logger.debug(
            "Origin trace depth=%s balance_id=%s: loading claimable-balance operations",
            depth,
            current_balance_id,
        )
        balance_ops = (
            horizon_server.operations()
            .for_claimable_balance(current_balance_id)
            .limit(per_call_limit)
            .order(False)
            .call()
        )
        records = _extract_records(balance_ops)
        logger.debug(
            "Origin trace depth=%s balance_id=%s: loaded operations=%s",
            depth,
            current_balance_id,
            len(records),
        )
        if not records:
            logger.warning(
                "Origin trace stopped for start=%s at depth=%s: no operations for balance_id=%s",
                start_balance_id,
                depth,
                current_balance_id,
            )
            return None

        create_op = _extract_single_create_op(records)
        if create_op is None:
            logger.warning(
                "Origin trace stopped for start=%s at depth=%s: create operation not found or ambiguous "
                "(balance_id=%s operations=%s)",
                start_balance_id,
                depth,
                current_balance_id,
                len(records),
            )
            return None

        if _sponsor_in_claimant_destinations(create_op):
            logger.info(
                "Origin trace found origin for start=%s: origin=%s depth=%s",
                start_balance_id,
                current_balance_id,
                depth,
            )
            return current_balance_id

        transaction_hash = create_op.get("transaction_hash")
        create_operation_id = create_op.get("id")
        if transaction_hash is None or create_operation_id is None:
            logger.warning(
                "Origin trace stopped for start=%s at depth=%s: create operation missing tx metadata "
                "(balance_id=%s tx_hash=%s op_id=%s)",
                start_balance_id,
                depth,
                current_balance_id,
                transaction_hash,
                create_operation_id,
            )
            return None

        logger.debug(
            "Origin trace depth=%s balance_id=%s: loading tx operations tx_hash=%s",
            depth,
            current_balance_id,
            transaction_hash,
        )
        tx_ops = (
            horizon_server.operations()
            .for_transaction(str(transaction_hash))
            .limit(per_call_limit)
            .order(False)
            .call()
        )
        tx_records = _extract_records(tx_ops)
        logger.debug(
            "Origin trace depth=%s balance_id=%s: loaded tx operations=%s",
            depth,
            current_balance_id,
            len(tx_records),
        )
        previous_balance_id = _extract_previous_balance_id(tx_records, str(create_operation_id))
        if not previous_balance_id:
            logger.warning(
                "Origin trace stopped for start=%s at depth=%s: previous balance id not found "
                "(balance_id=%s tx_hash=%s create_op_id=%s)",
                start_balance_id,
                depth,
                current_balance_id,
                transaction_hash,
                create_operation_id,
            )
            return None

        logger.debug(
            "Origin trace depth=%s start=%s: step back %s -> %s",
            depth,
            start_balance_id,
            current_balance_id,
            previous_balance_id,
        )
        current_balance_id = previous_balance_id

    logger.warning(
        "Origin trace stopped for start=%s: max_depth=%s reached",
        start_balance_id,
        max_depth,
    )
    return None


def find_stored_ancestor_balance_id(
    horizon_server: Server,
    start_balance_id: str,
    stored_balance_ids: set[str],
    *,
    max_depth: int = 120,
    per_call_limit: int = 200,
) -> Optional[str]:
    """
    Trace claimable-balance replacement chain backwards from start_balance_id until the first
    previous balance id that is in stored_balance_ids.

    Returns that balance id, or None when the chain reaches the voter's own create operation,
    cannot be followed, or exceeds max_depth first.
    """
    current_balance_id = start_balance_id
    for _ in range(max_depth):
        previous_balance_id = _load_previous_balance_id(horizon_server, current_balance_id, per_call_limit)
        if previous_balance_id is None:
            return None
        if previous_balance_id in stored_balance_ids:
            return previous_balance_id
        current_balance_id = previous_balance_id
    return None


def _load_previous_balance_id(horizon_server: Server, balance_id: str, per_call_limit: int) -> Optional[str]:
    balance_ops = (
        horizon_server.operations()
        .for_claimable_balance(balance_id)
        .limit(per_call_limit)
        .order(False)
        .call()
    )
    create_op = _extract_single_create_op(_extract_records(balance_ops))
    if create_op is None or _sponsor_in_claimant_destinations(create_op):
        return None

    transaction_hash = create_op.get("transaction_hash")
    create_operation_id = create_op.get("id")
    if transaction_hash is None or create_operation_id is None:
        return None

    tx_ops = (
        horizon_server.operations()
        .for_transaction(str(transaction_hash))
        .limit(per_call_limit)
        .order(False)
        .call()
    )
    return _extract_previous_balance_id(_extract_records(tx_ops), str(create_operation_id))


def _extract_records(response: dict[str, Any]) -> list[dict[str, Any]]:
    return list(response.get("_embedded", {}).get("records", []))


def _extract_single_create_op(records: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    create_ops = [operation for operation in records if operation.get("type") == "create_claimable_balance"]
    if len(create_ops) != 1:
        return None
    return create_ops[0]


def _extract_previous_balance_id(
    tx_records: list[dict[str, Any]],
    create_operation_id: str,
) -> Optional[str]:
    create_index = None
    for index, operation in enumerate(tx_records):
        if str(operation.get("id")) == create_operation_id:
            create_index = index
            break

    if create_index is None or create_index == 0:
        return None

    previous_operation = tx_records[create_index - 1]
    if previous_operation.get("type") != "clawback_claimable_balance":
        return None

    previous_balance_id = previous_operation.get("balance_id")
    if previous_balance_id is None:
        return None
    return str(previous_balance_id)


def _sponsor_in_claimant_destinations(create_operation: dict[str, Any]) -> bool:
    sponsor = create_operation.get("sponsor")
    if not sponsor:
        return False

    for claimant in create_operation.get("claimants", []):
        if claimant.get("destination") == sponsor:
            return True

    return False
