import logging
import sys
from collections import Counter
from decimal import Decimal
from typing import Any, Optional

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from celery.exceptions import SoftTimeLimitExceeded
from dateutil.parser import parse as date_parse
from requests.exceptions import RequestException
from stellar_sdk import Server
from stellar_sdk.exceptions import BaseRequestError, NotFoundError

from aqua_governance.governance.claimable_trace import find_origin_claimable_balance_id, find_stored_ancestor_balance_id
from aqua_governance.governance.exceptions import ClaimableBalanceParsingError, GenerateGrouKeyException
from aqua_governance.governance.models import LogVote, Proposal
from aqua_governance.governance.parser import generate_vote_key, is_supported_vote_asset, parse_vote
from aqua_governance.governance.task_logic.unlock_rules import (
    extract_abs_before_values,
    get_expected_unlock_timestamp,
    has_valid_unlock_date,
)
from aqua_governance.utils.requests import load_all_records


logger = logging.getLogger()

GROUP_UPDATE_NEW_VOTE = "new_vote"
GROUP_UPDATE_MELTING = "melting"
GROUP_UPDATE_UNCHANGED = "unchanged"
GROUP_UPDATE_AMBIGUOUS = "ambiguous"


class IncompleteVoteSnapshot(RuntimeError):
    pass


def update_proposal_votes_snapshot(
    proposal: Proposal,
    horizon_server: Server,
    freezing_amount: bool = False,
    strict: bool = True,
) -> int:
    """
    Re-index a proposal's votes from Horizon.

    In strict mode, a vote group with unresolved original metadata aborts the whole snapshot with
    IncompleteVoteSnapshot. Otherwise such a group is left untouched (no new, updated or claimed votes)
    and the rest of the proposal is saved. Returns the number of groups left untouched.
    """
    with transaction.atomic():
        expected_unlock_timestamp = get_expected_unlock_timestamp(proposal)
        request_builders = _build_request_builders(proposal, horizon_server)

        all_votes = proposal.logvote_set.filter(hide=False)
        raw_vote_groups = _build_raw_vote_groups(
            proposal=proposal,
            request_builders=request_builders,
            expected_unlock_timestamp=expected_unlock_timestamp,
        )
        new_log_vote: list[LogVote] = []
        update_log_vote: list[LogVote] = []
        processed_vote_ids: set[int] = set()
        incomplete_balance_ids: set[str] = set()
        origin_cache: dict[str, Optional[str]] = {}
        unresolved_groups = 0

        logger.info("Proposal %s has %s vote groups", proposal.id, len(raw_vote_groups))

        for vote_key, raw_vote_group in raw_vote_groups.items():
            votes = list(all_votes.filter(key=vote_key))
            group_update_type = classify_vote_group_update(votes, raw_vote_group)
            logger.info(
                "Proposal %s vote_key %s classified as %s (active_votes=%s, current_group_size=%s)",
                proposal.id,
                vote_key,
                group_update_type,
                len(votes),
                len(raw_vote_group),
            )
            group_incomplete_balance_ids: set[str] = set()
            group_new_votes, group_updated_votes, group_processed_vote_ids = reconcile_vote_group(
                vote_key=vote_key,
                raw_vote_group=raw_vote_group,
                existing_votes=votes,
                all_votes=all_votes,
                proposal=proposal,
                freezing_amount=freezing_amount,
                horizon_server=horizon_server,
                origin_cache=origin_cache,
                incomplete_balance_ids=group_incomplete_balance_ids,
            )
            if group_incomplete_balance_ids and not strict:
                logger.warning(
                    "Proposal %s vote_key %s left untouched: unresolved original vote metadata %s",
                    proposal.id,
                    vote_key,
                    sorted(group_incomplete_balance_ids),
                )
                unresolved_groups += 1
                processed_vote_ids.update(vote.id for vote in votes)
                continue
            incomplete_balance_ids.update(group_incomplete_balance_ids)
            new_log_vote.extend(group_new_votes)
            update_log_vote.extend(group_updated_votes)
            processed_vote_ids.update(group_processed_vote_ids)

        if incomplete_balance_ids:
            raise IncompleteVoteSnapshot(
                f'Proposal {proposal.pk} has unresolved original vote metadata: {sorted(incomplete_balance_ids)}',
            )

        stale_vote_ids = [
            vote.id for vote in all_votes
            if vote.id is not None and vote.id not in processed_vote_ids and not vote.claimed
        ]
        if stale_vote_ids:
            LogVote.objects.filter(id__in=stale_vote_ids).update(claimed=True)

        LogVote.objects.bulk_create(new_log_vote)
        update_fields = ["group_index", "claimable_balance_id", "amount", "transaction_link", "claimed"]
        if freezing_amount:
            # Without freezing, voted_amount would be rewritten from the value read at the start of this
            # transaction and could revert a freeze committed in the meantime.
            update_fields.append("voted_amount")
        LogVote.objects.bulk_update(update_log_vote, update_fields)
    return unresolved_groups


def _build_request_builders(proposal: Proposal, horizon_server: Server):
    request_builders = (
        (
            horizon_server.claimable_balances().for_claimant(proposal.vote_for_issuer).order(desc=False),
            LogVote.VOTE_FOR,
        ),
        (
            horizon_server.claimable_balances().for_claimant(proposal.vote_against_issuer).order(desc=False),
            LogVote.VOTE_AGAINST,
        ),
    )
    if proposal.abstain_issuer:
        request_builders = request_builders + (
            (
                horizon_server.claimable_balances().for_claimant(proposal.abstain_issuer).order(desc=False),
                LogVote.VOTE_ABSTAIN,
            ),
        )
    return request_builders


def _build_raw_vote_groups(
    proposal: Proposal,
    request_builders,
    expected_unlock_timestamp: int,
) -> dict[str, list[tuple[str, dict[str, Any]]]]:
    raw_vote_groups: dict[str, list[tuple[str, dict[str, Any]]]] = {}

    for request_builder, vote_choice in request_builders:
        for claimable_balance in load_all_records(request_builder):
            if not is_supported_vote_asset(claimable_balance['asset']):
                continue
            if not has_valid_unlock_date(claimable_balance, expected_unlock_timestamp):
                logger.info(
                    "Skip claimable claimable_balance %s for proposal %s due to invalid abs_before values: %s",
                    claimable_balance.get("id"),
                    proposal.id,
                    extract_abs_before_values(claimable_balance),
                )
                continue
            try:
                vote_key = generate_vote_key(claimable_balance, proposal, vote_choice)
                raw_vote_groups.setdefault(vote_key, []).append((vote_choice, claimable_balance))
            except GenerateGrouKeyException:
                logger.warning("Error generating vote_key", exc_info=sys.exc_info())

    return raw_vote_groups


def _is_self_sponsored_claimable_balance(claimable_balance: dict[str, Any]) -> bool:
    sponsor = claimable_balance.get("sponsor")
    if not sponsor:
        return False

    for claimant in claimable_balance.get("claimants", []):
        if claimant.get("destination") == sponsor:
            return True

    return False


def classify_vote_group_update(
    existing_votes: list[LogVote],
    raw_vote_group: list[tuple[str, dict[str, Any]]],
) -> str:
    existing_balance_ids = {
        vote.claimable_balance_id for vote in existing_votes if vote.claimable_balance_id
    }
    raw_votes_by_id = {
        claimable_balance.get("id"): claimable_balance
        for _, claimable_balance in raw_vote_group
        if claimable_balance.get("id")
    }
    current_balance_ids = set(raw_votes_by_id.keys())
    new_balance_ids = current_balance_ids - existing_balance_ids

    if not existing_votes and raw_vote_group:
        return GROUP_UPDATE_NEW_VOTE

    if not new_balance_ids and len(raw_vote_group) == len(existing_votes):
        return GROUP_UPDATE_UNCHANGED

    has_self_sponsored_new_vote = any(
        _is_self_sponsored_claimable_balance(raw_votes_by_id[balance_id]) for balance_id in new_balance_ids
    )
    has_only_service_sponsored_new_balances = bool(new_balance_ids) and all(
        not _is_self_sponsored_claimable_balance(raw_votes_by_id[balance_id]) for balance_id in new_balance_ids
    )

    if len(raw_vote_group) > len(existing_votes) and has_self_sponsored_new_vote:
        return GROUP_UPDATE_NEW_VOTE

    if len(raw_vote_group) <= len(existing_votes) and has_only_service_sponsored_new_balances:
        return GROUP_UPDATE_MELTING

    return GROUP_UPDATE_AMBIGUOUS


def reconcile_vote_group(
    vote_key: str,
    raw_vote_group: list[tuple[str, dict[str, Any]]],
    existing_votes: list[LogVote],
    all_votes,
    proposal: Proposal,
    freezing_amount: bool,
    horizon_server: Optional[Server] = None,
    origin_cache: Optional[dict[str, Optional[str]]] = None,
    incomplete_balance_ids: Optional[set[str]] = None,
) -> tuple[list[LogVote], list[LogVote], set[int]]:
    new_log_vote: list[LogVote] = []
    update_log_vote: list[LogVote] = []
    processed_vote_ids: set[int] = set()
    sorted_raw_vote_group = sorted(raw_vote_group, key=lambda item: Decimal(item[1]['amount']), reverse=True)
    if origin_cache is None:
        origin_cache = {}

    raw_items: list[dict[str, Any]] = []
    for raw_index, (vote_choice, raw_vote) in enumerate(sorted_raw_vote_group):
        raw_items.append(
            {
                "index": raw_index,
                "vote_choice": vote_choice,
                "vote": raw_vote,
                "balance_id": raw_vote.get("id"),
                "self_sponsored": _is_self_sponsored_claimable_balance(raw_vote),
            }
        )

    existing_by_balance_id = {
        vote.claimable_balance_id: vote
        for vote in existing_votes
        if vote.claimable_balance_id is not None
    }
    lineage_matches = _match_service_replacements_by_lineage(
        horizon_server=horizon_server,
        raw_items=raw_items,
        existing_by_balance_id=existing_by_balance_id,
    )
    if proposal.end_at is not None:
        eligible_raw_items = []
        for raw_item in raw_items:
            existing_vote = existing_by_balance_id.get(raw_item['balance_id'])
            if existing_vote is None:
                existing_vote = lineage_matches.get(raw_item['index'])
            if existing_vote is not None:
                if existing_vote.created_at is None or existing_vote.created_at > proposal.end_at:
                    if existing_vote.created_at is None and incomplete_balance_ids is not None:
                        incomplete_balance_ids.add(raw_item['balance_id'])
                    _mark_votes_as_processed([existing_vote], processed_vote_ids)
                    continue
            else:
                metadata = _load_original_metadata(
                    horizon_server, raw_item['balance_id'], not raw_item['self_sponsored'], origin_cache,
                )
                if metadata is None:
                    if incomplete_balance_ids is not None:
                        incomplete_balance_ids.add(raw_item['balance_id'])
                    if not raw_item['self_sponsored']:
                        # An unresolved replacement may belong to any old row in
                        # this group. Retry without claiming or changing them.
                        _mark_votes_as_processed(existing_votes, processed_vote_ids)
                        return [], [], processed_vote_ids
                    continue
                if metadata[0] > proposal.end_at:
                    continue
                raw_item['original_metadata'] = metadata
            eligible_raw_items.append(raw_item)
        raw_items = eligible_raw_items
        existing_votes = [
            vote for vote in existing_votes
            if vote.created_at is not None and vote.created_at <= proposal.end_at
        ]
        existing_by_balance_id = {vote.claimable_balance_id: vote for vote in existing_votes}
    matched_existing_ids: set[int] = set()
    matched_raw_indexes: set[int] = set()

    def _apply_update(existing_vote: LogVote, raw_item: dict[str, Any]) -> None:
        update_vote = _make_updated_vote(
            existing_vote,
            raw_item["index"],
            raw_item["vote"],
            freezing_amount,
        )
        if update_vote:
            update_log_vote.append(update_vote)
            if update_vote.id is not None:
                processed_vote_ids.add(update_vote.id)
        else:
            logger.warning("Error updating vote for %s, %s", vote_key, raw_item["index"])

    for raw_item in raw_items:
        balance_id = raw_item["balance_id"]
        existing_vote = existing_by_balance_id.get(balance_id)
        if existing_vote is None:
            continue
        if existing_vote.id is None or existing_vote.id in matched_existing_ids:
            continue
        _apply_update(existing_vote, raw_item)
        matched_existing_ids.add(existing_vote.id)
        matched_raw_indexes.add(raw_item["index"])

    for raw_item in raw_items:
        existing_vote = lineage_matches.get(raw_item["index"])
        if existing_vote is None or raw_item["index"] in matched_raw_indexes:
            continue
        if existing_vote.id is None or existing_vote.id in matched_existing_ids:
            continue
        _apply_update(existing_vote, raw_item)
        matched_existing_ids.add(existing_vote.id)
        matched_raw_indexes.add(raw_item["index"])

    remaining_existing = [
        vote
        for vote in existing_votes
        if vote.id is not None and vote.id not in matched_existing_ids
    ]
    remaining_raw = [raw_item for raw_item in raw_items if raw_item["index"] not in matched_raw_indexes]
    service_raw = [raw_item for raw_item in remaining_raw if not raw_item["self_sponsored"]]
    if service_raw and remaining_existing and horizon_server is not None:
        origin_matches = _match_unresolved_service_replacements_by_origin(
            horizon_server=horizon_server,
            vote_key=vote_key,
            proposal_id=proposal.id,
            remaining_existing=remaining_existing,
            unresolved_service_raw=service_raw,
            origin_cache=origin_cache,
        )
        for existing_vote, raw_item in origin_matches:
            _apply_update(existing_vote, raw_item)
            if existing_vote.id is not None:
                matched_existing_ids.add(existing_vote.id)
            matched_raw_indexes.add(raw_item["index"])

    remaining_existing = [
        vote
        for vote in existing_votes
        if vote.id is not None and vote.id not in matched_existing_ids
    ]
    remaining_raw = [raw_item for raw_item in raw_items if raw_item["index"] not in matched_raw_indexes]
    service_raw = [raw_item for raw_item in remaining_raw if not raw_item["self_sponsored"]]
    if service_raw and remaining_existing:
        remaining_existing.sort(key=lambda vote: vote.group_index)
        service_raw.sort(key=lambda raw_item: raw_item["index"])
        pair_count = min(len(remaining_existing), len(service_raw))

        for index in range(pair_count):
            existing_vote = remaining_existing[index]
            raw_item = service_raw[index]
            _apply_update(existing_vote, raw_item)
            if existing_vote.id is not None:
                matched_existing_ids.add(existing_vote.id)
            matched_raw_indexes.add(raw_item["index"])

    remaining_existing = [
        vote
        for vote in existing_votes
        if vote.id is not None and vote.id not in matched_existing_ids
    ]
    remaining_raw = [raw_item for raw_item in raw_items if raw_item["index"] not in matched_raw_indexes]
    unresolved_service_raw = [raw_item for raw_item in remaining_raw if not raw_item["self_sponsored"]]
    if unresolved_service_raw and remaining_existing:
        logger.warning(
            "Proposal %s vote_key %s unresolved service-sponsored replacements (%s items). "
            "Keep existing votes and skip destructive reconciliation for this group.",
            proposal.id,
            vote_key,
            len(unresolved_service_raw),
        )
        _mark_votes_as_processed(existing_votes, processed_vote_ids)
        return new_log_vote, update_log_vote, processed_vote_ids

    for raw_item in remaining_raw:
        try:
            new_vote = _make_new_vote(
                vote_key=vote_key,
                vote_group_index=raw_item["index"],
                claimable_balance=raw_item["vote"],
                proposal=proposal,
                vote_choice=raw_item["vote_choice"],
                freezing_amount=freezing_amount,
                horizon_server=horizon_server,
                origin_cache=origin_cache,
                restore_from_origin=not raw_item["self_sponsored"],
                original_metadata=raw_item.get('original_metadata'),
            )
            if new_vote is None:
                logger.warning("Error create vote for %s, %s", vote_key, raw_item["index"])
                continue

            old_vote = all_votes.filter(claimable_balance_id=new_vote.claimable_balance_id).first()
            if old_vote is not None:
                update_vote = _make_updated_vote(old_vote, raw_item["index"], raw_item["vote"], freezing_amount)
                if update_vote and update_vote.id is not None:
                    update_log_vote.append(update_vote)
                    processed_vote_ids.add(update_vote.id)
                else:
                    logger.warning("Error updating vote for %s, %s", vote_key, raw_item["index"])
                continue

            new_log_vote.append(new_vote)
        except ClaimableBalanceParsingError:
            logger.warning('Balance info skipped.', exc_info=sys.exc_info())

    return new_log_vote, update_log_vote, processed_vote_ids


def _match_service_replacements_by_lineage(
    horizon_server: Optional[Server],
    raw_items: list[dict[str, Any]],
    existing_by_balance_id: dict[str, LogVote],
) -> dict[int, LogVote]:
    """
    Map service-sponsored replacements to the stored vote whose balance their replacement chain
    passes through, one to one. Replacements that share a stored vote, or whose chain does not reach
    one, are left to origin matching.
    """
    if horizon_server is None or not existing_by_balance_id:
        return {}

    stored_balance_ids = set(existing_by_balance_id)
    current_balance_ids = {raw_item['balance_id'] for raw_item in raw_items}
    ancestors: dict[int, str] = {}
    for raw_item in raw_items:
        balance_id = raw_item['balance_id']
        if raw_item['self_sponsored'] or not balance_id or balance_id in existing_by_balance_id:
            continue
        try:
            ancestor_balance_id = find_stored_ancestor_balance_id(horizon_server, balance_id, stored_balance_ids)
        except SoftTimeLimitExceeded:
            raise
        except Exception:  # noqa: B902
            logger.warning('Lineage trace failed for balance %s; fall back to origin matching.', balance_id,
                           exc_info=True)
            continue
        if ancestor_balance_id is not None and ancestor_balance_id not in current_balance_ids:
            ancestors[raw_item['index']] = ancestor_balance_id

    ancestor_counts = Counter(ancestors.values())
    return {
        raw_index: existing_by_balance_id[ancestor_balance_id]
        for raw_index, ancestor_balance_id in ancestors.items()
        if ancestor_counts[ancestor_balance_id] == 1
    }


def _resolve_origin_balance_id(
    horizon_server: Server,
    balance_id: Optional[str],
    origin_cache: dict[str, Optional[str]],
) -> Optional[str]:
    if not balance_id:
        return None
    if balance_id in origin_cache:
        return origin_cache[balance_id]
    try:
        origin_balance_id = find_origin_claimable_balance_id(horizon_server, balance_id)
    except SoftTimeLimitExceeded:
        raise
    except Exception:
        origin_balance_id = None
    origin_cache[balance_id] = origin_balance_id
    return origin_balance_id


def _match_unresolved_service_replacements_by_origin(
    horizon_server: Server,
    vote_key: str,
    proposal_id: int,
    remaining_existing: list[LogVote],
    unresolved_service_raw: list[dict[str, Any]],
    origin_cache: dict[str, Optional[str]],
) -> list[tuple[LogVote, dict[str, Any]]]:
    existing_by_origin: dict[str, list[LogVote]] = {}
    for existing_vote in remaining_existing:
        origin_balance_id = _resolve_origin_balance_id(
            horizon_server=horizon_server,
            balance_id=existing_vote.claimable_balance_id,
            origin_cache=origin_cache,
        )
        if origin_balance_id is None:
            continue
        existing_by_origin.setdefault(origin_balance_id, []).append(existing_vote)

    raw_by_origin: dict[str, list[dict[str, Any]]] = {}
    for raw_item in unresolved_service_raw:
        origin_balance_id = _resolve_origin_balance_id(
            horizon_server=horizon_server,
            balance_id=raw_item.get("balance_id"),
            origin_cache=origin_cache,
        )
        if origin_balance_id is None:
            continue
        raw_by_origin.setdefault(origin_balance_id, []).append(raw_item)

    matched_pairs: list[tuple[LogVote, dict[str, Any]]] = []
    for origin_balance_id, raw_items in raw_by_origin.items():
        existing_items = existing_by_origin.get(origin_balance_id, [])
        if len(raw_items) == 1 and len(existing_items) == 1:
            matched_pairs.append((existing_items[0], raw_items[0]))
            continue
        if raw_items and existing_items:
            logger.warning(
                "Proposal %s vote_key %s ambiguous origin-based match for origin_balance_id=%s "
                "(existing=%s raw=%s)",
                proposal_id,
                vote_key,
                origin_balance_id,
                len(existing_items),
                len(raw_items),
            )

    if matched_pairs:
        logger.info(
            "Proposal %s vote_key %s origin-based matched replacements=%s",
            proposal_id,
            vote_key,
            len(matched_pairs),
        )

    return matched_pairs


def _mark_votes_as_processed(votes: list[LogVote], processed_vote_ids: set[int]) -> None:
    for vote in votes:
        if vote.id is not None:
            processed_vote_ids.add(vote.id)


def _load_original_metadata(horizon_server, balance_id, restore_from_origin, origin_cache):
    """Require one dated create operation on the original balance, never a fallback."""
    if horizon_server is None or not balance_id:
        return None
    metadata_balance_id = balance_id
    if restore_from_origin:
        metadata_balance_id = _resolve_origin_balance_id(horizon_server, balance_id, origin_cache)
        if metadata_balance_id is None:
            return None
    try:
        response = (
            horizon_server.operations().for_claimable_balance(metadata_balance_id).order(desc=False).limit(200).call()
        )
        create_ops = [
            record for record in response['_embedded']['records']
            if record.get('type') == 'create_claimable_balance'
        ]
        if len(create_ops) != 1:
            return None
        created_at = date_parse(create_ops[0]['created_at'])
        original_amount = Decimal(create_ops[0]['amount'])
        if timezone.is_naive(created_at) or not original_amount.is_finite() or original_amount < 0:
            return None
        return created_at, str(original_amount)
    except (BaseRequestError, RequestException, KeyError, TypeError, ValueError, ArithmeticError):
        logger.warning('Unresolved original vote metadata for balance %s; retry later.', balance_id, exc_info=True)
        return None


def _make_new_vote(
    vote_key: str,
    vote_group_index: int,
    claimable_balance: dict,
    proposal: Proposal,
    vote_choice: str,
    freezing_amount: bool,
    horizon_server: Optional[Server] = None,
    origin_cache: Optional[dict[str, Optional[str]]] = None,
    restore_from_origin: bool = False,
    original_metadata=None,
):
    balance_id = claimable_balance['id']
    original_amount = None
    created_at = None
    metadata_balance_id = balance_id
    server = horizon_server if horizon_server is not None else Server(settings.HORIZON_URL)

    if proposal.end_at is not None:
        metadata = original_metadata or _load_original_metadata(
            server, balance_id, restore_from_origin, origin_cache if origin_cache is not None else {},
        )
        if metadata is None or metadata[0] > proposal.end_at:
            return None
        return parse_vote(
            vote_key=vote_key,
            vote_group_index=vote_group_index,
            claimable_balance=claimable_balance,
            proposal=proposal,
            vote_choice=vote_choice,
            created_at=metadata[0],
            original_amount=metadata[1],
            vote_id=None,
            freezing_amount=freezing_amount,
        )

    if restore_from_origin and horizon_server is not None:
        if origin_cache is None:
            origin_cache = {}
        origin_balance_id = _resolve_origin_balance_id(
            horizon_server=horizon_server,
            balance_id=balance_id,
            origin_cache=origin_cache,
        )
        if origin_balance_id:
            metadata_balance_id = origin_balance_id

    try:
        ops = server.operations().for_claimable_balance(metadata_balance_id).order(desc=False).limit(50).call()
        for record in ops["_embedded"]["records"]:
            if record['type'] == 'create_claimable_balance':
                created_at = str(date_parse(record["created_at"]))
                original_amount = str(record["amount"])
    except NotFoundError:
        if metadata_balance_id == balance_id:
            created_at = claimable_balance['last_modified_time']
    except SoftTimeLimitExceeded:
        raise
    except Exception:
        logger.warning(
            "Error loading create_claimable_balance metadata for balance %s (metadata source %s)",
            balance_id,
            metadata_balance_id,
            exc_info=sys.exc_info(),
        )

    # Fallback to current balance metadata if origin lookup has no create op.
    if (created_at is None or original_amount is None) and metadata_balance_id != balance_id:
        try:
            ops = server.operations().for_claimable_balance(balance_id).order(desc=False).limit(50).call()
            for record in ops["_embedded"]["records"]:
                if record['type'] == 'create_claimable_balance':
                    created_at = created_at or str(date_parse(record["created_at"]))
                    original_amount = original_amount or str(record["amount"])
        except NotFoundError:
            created_at = created_at or claimable_balance['last_modified_time']
        except SoftTimeLimitExceeded:
            raise
        except Exception:
            logger.warning(
                "Error loading fallback create_claimable_balance metadata for balance %s",
                balance_id,
                exc_info=sys.exc_info(),
            )

    if created_at is None:
        created_at = str(proposal.created_at)

    if original_amount is None:
        original_amount = claimable_balance['amount']

    return parse_vote(
        vote_key=vote_key,
        vote_group_index=vote_group_index,
        claimable_balance=claimable_balance,
        proposal=proposal,
        vote_choice=vote_choice,
        created_at=created_at,
        original_amount=original_amount,
        vote_id=None,
        freezing_amount=freezing_amount,
    )


def _make_updated_vote(vote: LogVote, vote_group_index: int, claimable_balance: dict, freezing_amount: bool):
    created_at = str(vote.created_at)
    original_amount = str(vote.original_amount)

    return parse_vote(
        vote_key=vote.key,
        vote_group_index=vote_group_index,
        claimable_balance=claimable_balance,
        proposal=vote.proposal,
        vote_choice=vote.vote_choice,
        created_at=created_at,
        original_amount=original_amount,
        vote_id=vote.id,
        freezing_amount=freezing_amount,
        original_voted_amount=vote.voted_amount
    )
