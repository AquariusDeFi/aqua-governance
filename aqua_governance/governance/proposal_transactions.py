"""Promotion of a paid proposal transition.

Authorization for a transition is the on-chain AQUA payment and nothing else: the payer
must equal ``proposed_by``, the memo must commit to the transition being applied, and no
hash the payment resolves under may have been spent before.  This module is where that
verdict turns into state, under invariants that are load-bearing rather than stylistic:

* **I-1** no network I/O inside an ``atomic()`` block - Horizon latency inside the single
  app-wide advisory lock would serialise every submit;
* **I-2** the claim and the state change share one atomic scope, so a payment is burned iff
  a transition really happened and any rollback releases it;
* **I-3** a claim happens only on ``FINE`` and only in the branch that actually promotes;
* **I-4'** every terminal write is a conditional UPDATE filtered on the expected action, and
  reports ``skipped`` on zero rows rather than stamping a loser's verdict on a promoted row;
* **I-5** every promotion re-reads the row under ``select_for_update()`` and re-checks that
  ``action`` is still the one it was dispatched for;
* **I-6** no promotion path touches the database before the payment verdict is known.

The values written are the ones that were verified - carried on the resolved transition -
never the staged ``new_*`` columns re-read after the Horizon round trip.
"""
import json
import logging
import re

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, OperationalError, transaction
from django.utils import timezone

from django_quill.quill import Quill

from aqua_governance.governance import payment_statuses
from aqua_governance.governance.asset_tokens import (
    find_active_asset_proposal_conflict,
    serialize_asset_proposal_conflict,
)
from aqua_governance.governance.consumed_transactions import claim_transaction_hashes
from aqua_governance.governance.db_locks import acquire_proposal_transition_lock
from aqua_governance.governance.exceptions import TransactionAlreadyConsumedError
from aqua_governance.governance.proposal_constants import (
    CONSUMED_TRANSACTION_PURPOSE_CREATE,
    CONSUMED_TRANSACTION_PURPOSE_SUBMIT,
    CONSUMED_TRANSACTION_PURPOSE_UPDATE,
)
from aqua_governance.governance.proposal_queue import validate_weekly_queue_slot
from aqua_governance.governance.proposal_queue_slots import find_queue_slot_conflict, sync_proposal_queue_slot
from aqua_governance.governance.transitions import CreateTransition, SubmitTransition, UpdateTransition
from aqua_governance.utils.payments import PaymentCheckResult, verify_payment


logger = logging.getLogger(__name__)


try:
    import sentry_sdk
except ImportError:  # pragma: no cover — test dependencies may not include sentry-sdk
    sentry_sdk = None


# Deliberately stricter than ``payments.TRANSACTION_HASH_RE``, which accepts either case:
# by the time a hash reaches a promotion it has been through ``TransactionHashField`` or
# migration 0031, so anything else is a row no ledger claim could match.
CANONICAL_TRANSACTION_HASH_RE = re.compile(r'^[0-9a-f]{64}$')

# PostgreSQL SQLSTATE for a deadlock the server broke by killing one of the waiters.
DEADLOCK_PGCODE = '40P01'


def _alert_operator(message, extra=None):
    """Send an operator-facing alert via Sentry and the application logger.

    Intended for payment / slot-conflict paths where the human operator must
    investigate.  Tests can mock this helper instead of reaching into
    sentry_sdk internals.
    """
    extra = extra or {}
    logger.error(message, extra=extra)
    if sentry_sdk is not None:
        with sentry_sdk.push_scope() as scope:
            for key, value in extra.items():
                scope.set_extra(key, value)
            sentry_sdk.capture_message(message, level='error')


def check_proposal_status(**kwargs):
    """Thin named wrapper so existing tests can keep patching this symbol."""
    return verify_payment(**kwargs)


def _coerce_payment_result(value):
    """Tolerate a bare status string from a test mock."""
    if isinstance(value, str):
        return PaymentCheckResult(payment_status=value, reason='mocked')
    return value


def _verify(proposal, *, transaction_hash, memo_expectation, payment_amount):
    return _coerce_payment_result(check_proposal_status(
        transaction_hash=transaction_hash,
        expected_payer=proposal.proposed_by,
        memo_expectation=memo_expectation,
        payment_amount=payment_amount,
        log_context={'proposal_id': proposal.id},
    ))


def _invalid_transaction_hash_reason(value):
    if not value:
        return 'missing_transaction_hash'
    if not CANONICAL_TRANSACTION_HASH_RE.match(value):
        return 'malformed_transaction_hash'
    return None


def _rejection_marker(transaction_hash):
    """The value that dedups terminal alerts for one attempt.

    A hash Horizon could never resolve is still recorded, truncated to the column width, so
    a row that can only ever fail pages once instead of once a minute forever.  The empty
    string stands for "there was no hash at all"; it is a legal ``CharField`` value and,
    unlike NULL, compares equal to itself.
    """
    if isinstance(transaction_hash, str) and transaction_hash.strip():
        return transaction_hash.strip().lower()[:64]
    return ''


def _persist_terminal_verdict(proposal, *, expected_action, status, transaction_hash, extra=None):
    """I-4': write a terminal verdict only while the row still awaits this action.

    ``True`` means the write landed and this hash had not been reported for this proposal
    before, which is exactly the condition for alerting once.  ``False`` means another
    worker already moved the row on - report ``skipped``, persist nothing, alert nobody.
    """
    updated = type(proposal).objects.filter(
        id=proposal.id,
        action=expected_action,
    ).exclude(
        payment_check_rejected_hash=_rejection_marker(transaction_hash),
    ).update(
        payment_status=status,
        payment_check_rejected_hash=_rejection_marker(transaction_hash),
        **(extra or {}),
    )
    return bool(updated)


def _persist_retryable_verdict(proposal, *, expected_action, status):
    """A non-terminal HORIZON_ERROR: same action guard, but no rejection is recorded."""
    return bool(type(proposal).objects.filter(
        id=proposal.id,
        action=expected_action,
    ).update(payment_status=status))


def _terminal_create_fields(proposal, expected_action):
    """A rejected creation is retired: the row leaves the sweep and the public lists."""
    if expected_action != proposal.TO_CREATE:
        return {}
    return {'draft': False, 'action': proposal.NONE, 'hide': True}


def _claim_hashes(result, transaction_hash):
    """Every hash the payment resolves under, or the requested one if Horizon was mocked."""
    return result.resolved_hashes or (transaction_hash,)


def _claim(proposal, transition, result, purpose):
    claim_transaction_hashes(
        transaction_hashes=_claim_hashes(result, transition.transaction_hash),
        proposal=proposal,
        purpose=purpose,
        payer=result.payer,
    )


def _quill_from_html(text_html):
    """Wrap verified HTML the way ``QuillField.to_internal_value`` does on the way in.

    Assigning the raw string instead would persist something ``Quill`` cannot parse back,
    because ``QuillField`` stores whatever string it is handed verbatim.
    """
    return Quill(json.dumps({'delta': '', 'html': text_html}))


def _is_deadlock(exc):
    return getattr(getattr(exc, '__cause__', None), 'pgcode', None) == DEADLOCK_PGCODE


def check_transaction(proposal):
    """Apply the pending transition if - and only if - a payment authorizes it."""
    action = proposal.action

    try:
        return _dispatch(proposal)
    except OperationalError as exc:
        if not _is_deadlock(exc):
            raise
        # The claim contends with the FK share locks another transaction's queue-slot
        # cleanup takes, so a deadlock is possible even with the claim ordered last.  The
        # losing side rolled back whole, so one retry is safe.
        logger.warning(
            'Retrying a proposal transition after a database deadlock.',
            extra={'proposal_id': proposal.id, 'action': action},
        )
        return _dispatch(proposal)


def _dispatch(proposal):
    if proposal.action == proposal.TO_UPDATE:
        return _check_update_transaction(proposal)

    elif proposal.action == proposal.TO_SUBMIT:
        return _check_submit_transaction(proposal)

    elif proposal.action == proposal.TO_CREATE:
        return _check_create_transaction(proposal)

    return None


def _skipped(proposal):
    """Nothing was written, so report what the row actually says.

    The persisted value belongs to whichever worker owns this transition now - the winner
    of the race, or an earlier terminal rejection of this same hash.  Reporting the instance
    as it was read at the start of the request would hand the owner's polling loop the
    advisory ``FINE`` written at staging and tell them a burned payment succeeded.
    """
    proposal.refresh_from_db()
    return {'outcome': 'skipped', 'payment_status': proposal.payment_status}


def _reject_unusable_transaction_hash(proposal, expected_action, transition, reason):
    """A hash Horizon can never resolve, rejected before any Horizon call is made."""
    status = payment_statuses.INVALID_PAYMENT
    transaction_hash = transition.transaction_hash
    if not _persist_terminal_verdict(
        proposal,
        expected_action=expected_action,
        status=status,
        transaction_hash=transaction_hash,
        extra=_terminal_create_fields(proposal, expected_action),
    ):
        return _skipped(proposal)

    proposal.refresh_from_db()
    _alert_operator(
        'A pending proposal transition carries a transaction hash Horizon can never resolve.',
        extra={
            'proposal_id': proposal.id,
            'action': expected_action,
            'payment_status': status,
            'proposal_status': proposal.proposal_status,
            'transaction_hash': transaction_hash,
            'reason': reason,
        },
    )
    return {'outcome': 'payment_rejected', 'payment_status': status}


def _reject_payment(proposal, expected_action, transition, result):
    """Persist and report a non-FINE verdict for a transition that was not applied."""
    status = result.payment_status
    if status == payment_statuses.HORIZON_ERROR:
        if _persist_retryable_verdict(proposal, expected_action=expected_action, status=status):
            proposal.refresh_from_db()
        return {'outcome': 'payment_not_confirmed', 'payment_status': status}

    if not _persist_terminal_verdict(
        proposal,
        expected_action=expected_action,
        status=status,
        transaction_hash=transition.transaction_hash,
        extra=_terminal_create_fields(proposal, expected_action),
    ):
        return _skipped(proposal)

    proposal.refresh_from_db()
    _alert_payment_rejected(proposal, expected_action, transition, result)
    return {'outcome': 'payment_rejected', 'payment_status': status}


def _alert_payment_rejected(proposal, expected_action, transition, result):
    _alert_operator(
        'A proposal transition was rejected because its payment does not authorize it.',
        extra={
            'proposal_id': proposal.id,
            'action': expected_action,
            'payment_status': result.payment_status,
            'proposal_status': proposal.proposal_status,
            'transaction_hash': transition.transaction_hash,
            'reason': result.reason,
            'payer': result.payer,
            'proposed_by': proposal.proposed_by,
        },
    )


def _alert_unpromotable_payment(proposal, expected_action, transition, result):
    """A verified payment that cannot be written, because its hash is taken on another row.

    ``Proposal.transaction_hash`` is unique and the staging pre-check that keeps two rows
    off one hash is a non-locking read, so a create and an update can still interleave past
    it.  The transition rolls back whole and the caller re-raises; the payment is on chain
    and now needs an operator, so it must not go out as a bare 500.
    """
    _alert_operator(
        'A confirmed proposal payment could not be applied: its transaction hash is taken.',
        extra={
            'proposal_id': proposal.id,
            'action': expected_action,
            'payment_status': result.payment_status,
            'transaction_hash': transition.transaction_hash,
            'payer': result.payer,
        },
    )


def _handle_transaction_reuse(proposal, expected_action, transition, exc):
    """The payment was already spent on some transition; this one applied nothing."""
    status = payment_statuses.INVALID_PAYMENT
    _log_transaction_reuse(proposal, expected_action, transition, exc)
    if not _persist_terminal_verdict(
        proposal,
        expected_action=expected_action,
        status=status,
        transaction_hash=transition.transaction_hash,
        extra=_terminal_create_fields(proposal, expected_action),
    ):
        return _skipped(proposal)

    proposal.refresh_from_db()
    _alert_operator(
        'A proposal transition was rejected because its payment was already spent.',
        extra={
            'proposal_id': proposal.id,
            'action': expected_action,
            'payment_status': status,
            'proposal_status': proposal.proposal_status,
            'transaction_hash': exc.transaction_hash,
        },
    )
    return {'outcome': 'transaction_already_consumed', 'payment_status': status}


def _stale_transition(proposal, expected_action, transition):
    """The staged copy changed under us between the Horizon answer and the row lock.

    Nothing is written: the verdict belongs to a transition this row no longer describes,
    and staging is unauthenticated, so persisting here would hand an attacker a way to
    stamp any pending proposal.  The reported status is ``HORIZON_ERROR`` because nothing
    has been disproved either - it is the one value that keeps both the browser poll and
    the beat sweep retrying, and reporting the persisted column instead would tell an owner
    whose staged copy was overwritten that their transition succeeded.
    """
    logger.warning(
        'A verified transition no longer matches the staged copy; nothing was applied.',
        extra={
            'proposal_id': proposal.id,
            'action': expected_action,
            'transaction_hash': transition.transaction_hash,
        },
    )
    return {'outcome': 'stale_transition', 'payment_status': payment_statuses.HORIZON_ERROR}


def _check_update_transaction(proposal):
    transition = UpdateTransition.resolve(proposal)
    reason = _invalid_transaction_hash_reason(transition.transaction_hash)
    if reason is not None:
        return _reject_unusable_transaction_hash(proposal, proposal.TO_UPDATE, transition, reason)

    result = _verify(
        proposal,
        transaction_hash=transition.transaction_hash,
        memo_expectation=transition.memo_expectation(),
        payment_amount=settings.PROPOSAL_CREATE_OR_UPDATE_COST,
    )
    status = result.payment_status
    if status != proposal.FINE:
        return _reject_payment(proposal, proposal.TO_UPDATE, transition, result)

    proposal_model = type(proposal)
    try:
        with transaction.atomic():
            locked_proposal = proposal_model.objects.select_for_update().get(id=proposal.id)
            if locked_proposal.action != proposal.TO_UPDATE:
                return _skipped(proposal)
            if not transition.matches_staged(locked_proposal):
                proposal.refresh_from_db()
                return _stale_transition(proposal, proposal.TO_UPDATE, transition)

            _apply_update_confirmation(locked_proposal, transition, status)
            _claim(locked_proposal, transition, result, CONSUMED_TRANSACTION_PURPOSE_UPDATE)
    except IntegrityError:
        _alert_unpromotable_payment(proposal, proposal.TO_UPDATE, transition, result)
        raise
    except TransactionAlreadyConsumedError as exc:
        outcome = _handle_transaction_reuse(proposal, proposal.TO_UPDATE, transition, exc)
        proposal.refresh_from_db()
        return outcome

    proposal.refresh_from_db()
    _log_payment_confirmed(proposal, CONSUMED_TRANSACTION_PURPOSE_UPDATE, transition, result)
    return {'outcome': 'updated', 'payment_status': status}


def _apply_update_confirmation(proposal, transition, status):
    _history_model(proposal).objects.create(
        version=proposal.version,
        title=proposal.title,
        text=proposal.text,
        transaction_hash=proposal.transaction_hash,
        envelope_xdr=proposal.envelope_xdr,
        proposal=proposal,
        created_at=proposal.last_updated_at,
    )
    proposal.payment_status = status
    proposal.payment_check_rejected_hash = None
    proposal.last_updated_at = timezone.now()
    proposal.text = _quill_from_html(transition.text_html)
    proposal.title = transition.title
    proposal.version = proposal.version + 1
    proposal.transaction_hash = transition.transaction_hash
    proposal.envelope_xdr = transition.envelope_xdr
    proposal.action = proposal.NONE
    proposal.new_title = None
    proposal.new_text = None
    proposal.new_transaction_hash = None
    proposal.new_envelope_xdr = None
    proposal.save()
    # A ``QuillField`` set to None persists the empty-Quill sentinel rather than NULL,
    # because the descriptor re-wraps it on the way out; clear the column explicitly so
    # operational queries can still use ``new_text IS NULL``.
    type(proposal).objects.filter(pk=proposal.pk).update(new_text=None)


def _check_submit_transaction(proposal):
    transition = SubmitTransition.resolve(proposal)
    reason = _invalid_transaction_hash_reason(transition.transaction_hash)
    if reason is not None:
        return _reject_unusable_transaction_hash(proposal, proposal.TO_SUBMIT, transition, reason)

    result = _verify(
        proposal,
        transaction_hash=transition.transaction_hash,
        memo_expectation=transition.memo_expectation(),
        payment_amount=settings.PROPOSAL_SUBMIT_COST,
    )
    status = result.payment_status
    if status != proposal.FINE:
        _log_submit_payment_not_confirmed(proposal, transition, status)
        return _reject_payment(proposal, proposal.TO_SUBMIT, transition, result)

    proposal_model = type(proposal)
    with transaction.atomic():
        acquire_proposal_transition_lock()
        locked_proposal = proposal_model.objects.select_for_update().get(id=proposal.id)
        if locked_proposal.action != proposal.TO_SUBMIT:
            return _skipped(proposal)

        if not transition.matches_staged(locked_proposal):
            proposal.refresh_from_db()
            return _stale_transition(proposal, proposal.TO_SUBMIT, transition)

        now = timezone.now()
        new_start_at = transition.start_at
        new_end_at = transition.end_at
        if new_start_at is None or new_end_at is None:
            proposal.refresh_from_db()
            return {
                'outcome': 'missing_submit_window',
                'payment_status': status,
            }

        try:
            validate_weekly_queue_slot(
                new_start_at,
                new_end_at,
                now=now,
                allow_current_week=True,
            )
        except ValidationError as exc:
            _mark_submit_retry_state(locked_proposal, status)
            _log_invalid_submit_window(locked_proposal, transition, status, exc)
            proposal.refresh_from_db()
            return {
                'outcome': 'invalid_submit_window',
                'payment_status': status,
                'errors': _validation_error_details(exc),
            }

        if new_end_at and new_end_at <= now:
            # An expired window still applies a transition, so it needs a scope of its own
            # to claim in: the outer block has four paths that return - and therefore
            # commit - without applying anything.
            try:
                with transaction.atomic():
                    _apply_submit_confirmation(
                        proposal,
                        locked_proposal,
                        transition,
                        status,
                        proposal.EXPIRED,
                        now,
                        create_queue_slot=False,
                    )
                    _claim(locked_proposal, transition, result, CONSUMED_TRANSACTION_PURPOSE_SUBMIT)
            except TransactionAlreadyConsumedError as exc:
                outcome = _handle_transaction_reuse(proposal, proposal.TO_SUBMIT, transition, exc)
                proposal.refresh_from_db()
                return outcome

            proposal.refresh_from_db()
            _log_payment_confirmed(proposal, CONSUMED_TRANSACTION_PURPOSE_SUBMIT, transition, result)
            return {
                'outcome': 'expired',
                'payment_status': status,
            }

        asset_conflict = find_active_asset_proposal_conflict(proposal=locked_proposal)
        if asset_conflict is not None:
            _mark_submit_retry_state(locked_proposal, status)
            proposal.refresh_from_db()
            return {
                'outcome': 'asset_proposal_conflict',
                'payment_status': status,
                'asset_contract_address': asset_conflict.canonical_asset_contract_address,
                'conflict': serialize_asset_proposal_conflict(asset_conflict),
            }

        conflict = find_queue_slot_conflict(
            start_at=new_start_at,
            end_at=new_end_at,
            exclude_proposal_id=locked_proposal.id,
        )
        if conflict is not None:
            _mark_submit_retry_state(locked_proposal, status)
            _log_submit_slot_conflict(locked_proposal, transition, status, conflict)
            proposal.refresh_from_db()
            return {
                'outcome': 'slot_conflict',
                'payment_status': status,
                'conflict': _serialize_queue_conflict(conflict),
            }

        proposal_status = _resolve_submit_proposal_status(proposal, transition, now)
        try:
            with transaction.atomic():
                _apply_submit_confirmation(proposal, locked_proposal, transition, status, proposal_status, now)
                _claim(locked_proposal, transition, result, CONSUMED_TRANSACTION_PURPOSE_SUBMIT)
        except TransactionAlreadyConsumedError as exc:
            outcome = _handle_transaction_reuse(proposal, proposal.TO_SUBMIT, transition, exc)
            proposal.refresh_from_db()
            return outcome
        except IntegrityError:
            locked_proposal.refresh_from_db()
            conflict = find_queue_slot_conflict(
                start_at=new_start_at,
                end_at=new_end_at,
                exclude_proposal_id=locked_proposal.id,
            )
            if conflict is not None:
                _mark_submit_retry_state(locked_proposal, status)
                _log_submit_slot_conflict(locked_proposal, transition, status, conflict)
                proposal.refresh_from_db()
                return {
                    'outcome': 'slot_conflict',
                    'payment_status': status,
                    'conflict': _serialize_queue_conflict(conflict),
                }
            _log_unexpected_submit_booking_integrity_error(locked_proposal, transition, status)
            raise

        proposal.refresh_from_db()
        _log_payment_confirmed(proposal, CONSUMED_TRANSACTION_PURPOSE_SUBMIT, transition, result)
        return {
            'outcome': 'booked',
            'payment_status': status,
            'proposal_status': proposal.proposal_status,
        }


def _check_create_transaction(proposal):
    transition = CreateTransition.from_proposal(proposal)
    reason = _invalid_transaction_hash_reason(transition.transaction_hash)
    if reason is not None:
        return _reject_unusable_transaction_hash(proposal, proposal.TO_CREATE, transition, reason)

    result = _verify(
        proposal,
        transaction_hash=transition.transaction_hash,
        memo_expectation=transition.memo_expectation(),
        payment_amount=settings.PROPOSAL_CREATE_OR_UPDATE_COST,
    )
    status = result.payment_status
    if status == proposal.HORIZON_ERROR and proposal.status == proposal.HORIZON_ERROR:
        return None

    if proposal.is_asset_proposal and status != proposal.HORIZON_ERROR:
        return _apply_asset_create_transaction(proposal, transition, result)

    if status != proposal.FINE:
        return _reject_payment(proposal, proposal.TO_CREATE, transition, result)

    proposal_model = type(proposal)
    try:
        with transaction.atomic():
            locked_proposal = proposal_model.objects.select_for_update().get(id=proposal.id)
            if locked_proposal.action != proposal.TO_CREATE:
                return _skipped(proposal)
            if not transition.matches_locked(locked_proposal):
                proposal.refresh_from_db()
                return _stale_transition(proposal, proposal.TO_CREATE, transition)

            locked_proposal.draft = False
            locked_proposal.action = proposal.NONE
            locked_proposal.payment_status = status
            locked_proposal.payment_check_rejected_hash = None
            locked_proposal.save()
            _claim(locked_proposal, transition, result, CONSUMED_TRANSACTION_PURPOSE_CREATE)
    except IntegrityError:
        _alert_unpromotable_payment(proposal, proposal.TO_CREATE, transition, result)
        raise
    except TransactionAlreadyConsumedError as exc:
        outcome = _handle_transaction_reuse(proposal, proposal.TO_CREATE, transition, exc)
        proposal.refresh_from_db()
        return outcome

    proposal.refresh_from_db()
    _log_payment_confirmed(proposal, CONSUMED_TRANSACTION_PURPOSE_CREATE, transition, result)
    return {'outcome': 'created', 'payment_status': status}


def _apply_asset_create_transaction(proposal, transition, result):
    status = result.payment_status
    proposal_model = type(proposal)
    rejection_marker = _rejection_marker(transition.transaction_hash)
    already_reported = False
    try:
        with transaction.atomic():
            acquire_proposal_transition_lock()
            locked_proposal = proposal_model.objects.select_for_update().get(id=proposal.id)
            if locked_proposal.action != proposal.TO_CREATE:
                return _skipped(proposal)
            if not transition.matches_locked(locked_proposal):
                proposal.refresh_from_db()
                return _stale_transition(proposal, proposal.TO_CREATE, transition)

            if status == proposal.FINE:
                asset_conflict = find_active_asset_proposal_conflict(proposal=locked_proposal)
                if asset_conflict is not None:
                    locked_proposal.payment_status = status
                    locked_proposal.save(update_fields=['payment_status'])
                    proposal.refresh_from_db()
                    return {
                        'outcome': 'asset_proposal_conflict',
                        'payment_status': status,
                        'asset_contract_address': asset_conflict.canonical_asset_contract_address,
                        'conflict': serialize_asset_proposal_conflict(asset_conflict),
                    }

            locked_proposal.draft = False
            locked_proposal.action = proposal.NONE
            locked_proposal.last_updated_at = timezone.now()
            if status != proposal.FINE:
                locked_proposal.hide = True
                # The same (proposal, hash) dedup the conditional UPDATE gives the other
                # three paths: this one writes under the row lock instead, so the gate has
                # to be read here rather than inferred from an updated row count.
                already_reported = locked_proposal.payment_check_rejected_hash == rejection_marker
                locked_proposal.payment_check_rejected_hash = rejection_marker
            else:
                locked_proposal.payment_check_rejected_hash = None
            locked_proposal.payment_status = status
            locked_proposal.save()
            if status == proposal.FINE:
                _claim(locked_proposal, transition, result, CONSUMED_TRANSACTION_PURPOSE_CREATE)
    except TransactionAlreadyConsumedError as exc:
        outcome = _handle_transaction_reuse(proposal, proposal.TO_CREATE, transition, exc)
        proposal.refresh_from_db()
        return outcome

    proposal.refresh_from_db()
    if status != proposal.FINE:
        if not already_reported:
            _alert_payment_rejected(proposal, proposal.TO_CREATE, transition, result)
        return {'outcome': 'payment_rejected', 'payment_status': status}

    _log_payment_confirmed(proposal, CONSUMED_TRANSACTION_PURPOSE_CREATE, transition, result)
    return {'outcome': 'created', 'payment_status': status}


def _create_submit_history(source_proposal, history_proposal):
    _history_model(source_proposal).objects.create(
        version=history_proposal.version,
        hide=True,
        title=history_proposal.title,
        text=history_proposal.text,
        transaction_hash=history_proposal.transaction_hash,
        envelope_xdr=history_proposal.envelope_xdr,
        proposal=history_proposal,
        created_at=history_proposal.last_updated_at,
    )


def _apply_submit_confirmation(
    source_proposal,
    proposal,
    transition,
    status,
    proposal_status,
    now,
    *,
    create_queue_slot=True,
):
    _create_submit_history(source_proposal, proposal)
    proposal.payment_status = status
    proposal.payment_check_rejected_hash = None
    proposal.start_at = transition.start_at
    proposal.end_at = transition.end_at
    proposal.proposal_status = proposal_status
    proposal.last_updated_at = now
    proposal.transaction_hash = transition.transaction_hash
    proposal.envelope_xdr = transition.envelope_xdr
    proposal.action = proposal.NONE
    proposal.new_start_at = None
    proposal.new_end_at = None
    proposal.new_envelope_xdr = None
    proposal.new_transaction_hash = None
    proposal.save()
    if create_queue_slot:
        sync_proposal_queue_slot(proposal)


def _mark_submit_retry_state(proposal, status):
    """Record a confirmed payment on a submit that could not be applied yet.

    A conditional UPDATE rather than a ``save()``: the caller's instance has been through a
    Horizon round trip and, on the booking-recovery path, through a rolled-back savepoint,
    so writing the whole row would revert whatever a concurrent request wrote meanwhile.
    """
    type(proposal).objects.filter(id=proposal.id, action=proposal.TO_SUBMIT).update(payment_status=status)
    proposal.payment_status = status


def _resolve_submit_proposal_status(source_proposal, transition, now):
    if transition.start_at and transition.start_at > now:
        return source_proposal.QUEUED
    return source_proposal.VOTING


def _serialize_queue_conflict(conflict):
    return {
        'proposal': conflict.proposal.id,
        'proposal_status': conflict.proposal.proposal_status,
        'start_at': conflict.slot.start_at if conflict.slot is not None else conflict.proposal.start_at,
        'end_at': conflict.slot.end_at if conflict.slot is not None else conflict.proposal.end_at,
    }


def _log_payment_confirmed(proposal, purpose, transition, result):
    logger.info(
        'A proposal transition was applied against a confirmed payment.',
        extra={
            'proposal_id': proposal.id,
            'purpose': purpose,
            'transaction_hash': transition.transaction_hash,
            'memo_format': result.memo_format,
            'payer': result.payer,
        },
    )


def _log_transaction_reuse(proposal, expected_action, transition, exc):
    logger.info(
        'A proposal transition presented a payment that was already spent.',
        extra={
            'proposal_id': proposal.id,
            'purpose': expected_action,
            'transaction_hash': transition.transaction_hash,
            'consumed_transaction_hash': exc.transaction_hash,
        },
    )


def _log_submit_payment_not_confirmed(proposal, transition, status):
    if status == proposal.HORIZON_ERROR:
        logger.info(
            'Submit payment could not be confirmed yet; queue slot not booked.',
            extra={
                'proposal_id': proposal.id,
                'action': proposal.action,
                'payment_status': status,
                'proposal_status': proposal.proposal_status,
                'transaction_hash': transition.transaction_hash,
                'selected_start_at': transition.start_at,
                'selected_end_at': transition.end_at,
            },
        )
        return

    # Non‑FINE definitive failures (BAD_MEMO, INVALID_PAYMENT, FAILED_TRANSACTION)
    # are currently surfaced to the proposer only.  A warning log at least lets
    # operators trace them.
    if status != proposal.FINE:
        logger.warning(
            'Submit payment finished with a non-recoverable status; queue slot not booked.',
            extra={
                'proposal_id': proposal.id,
                'action': proposal.action,
                'payment_status': status,
                'proposal_status': proposal.proposal_status,
                'transaction_hash': transition.transaction_hash,
                'selected_start_at': transition.start_at,
                'selected_end_at': transition.end_at,
            },
        )


def _log_submit_slot_conflict(proposal, transition, status, conflict):
    _alert_operator(
        'Confirmed submit payment could not book queue slot because it is already occupied.',
        extra={
            'proposal_id': proposal.id,
            'action': proposal.action,
            'payment_status': status,
            'proposal_status': proposal.proposal_status,
            'transaction_hash': transition.transaction_hash,
            'selected_start_at': transition.start_at,
            'selected_end_at': transition.end_at,
            'conflicting_proposal_id': conflict.proposal.id,
            'conflicting_proposal_status': conflict.proposal.proposal_status,
            'conflicting_slot_id': conflict.slot.proposal_id if conflict.slot is not None else None,
            'conflicting_start_at': conflict.slot.start_at if conflict.slot is not None else conflict.proposal.start_at,
            'conflicting_end_at': conflict.slot.end_at if conflict.slot is not None else conflict.proposal.end_at,
        },
    )


def _log_invalid_submit_window(proposal, transition, status, exc: ValidationError):
    _alert_operator(
        'Confirmed submit payment could not be applied because the selected queue slot is no longer valid.',
        extra={
            'proposal_id': proposal.id,
            'action': proposal.action,
            'payment_status': status,
            'proposal_status': proposal.proposal_status,
            'transaction_hash': transition.transaction_hash,
            'selected_start_at': transition.start_at,
            'selected_end_at': transition.end_at,
            'validation_errors': _validation_error_details(exc),
        },
    )


def _log_unexpected_submit_booking_integrity_error(proposal, transition, status):
    _alert_operator(
        'Confirmed submit payment hit an unexpected integrity error while booking the queue slot.',
        extra={
            'proposal_id': proposal.id,
            'action': proposal.action,
            'payment_status': status,
            'proposal_status': proposal.proposal_status,
            'transaction_hash': transition.transaction_hash,
            'selected_start_at': transition.start_at,
            'selected_end_at': transition.end_at,
        },
    )


def _validation_error_details(exc: ValidationError):
    return getattr(exc, 'message_dict', {'__all__': exc.messages})


def _history_model(proposal):
    return proposal._meta.apps.get_model('governance', 'HistoryProposal')
