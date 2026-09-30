import hashlib
import json
from datetime import datetime
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.utils import timezone

from aqua_governance.governance.models import AssetToken, LogVote, Proposal


PROPOSAL_FIELDS = (
    'id', 'proposal_type', 'proposal_status', 'hide', 'draft', 'action', 'payment_status',
    'start_at', 'end_at', 'asset_token_id', 'asset_contract_address', 'asset_code', 'asset_issuer',
    'vote_for_issuer', 'vote_against_issuer', 'abstain_issuer',
    'vote_for_result', 'vote_against_result', 'vote_abstain_result',
    'aqua_circulating_supply', 'ice_circulating_supply', 'percent_for_quorum',
    'onchain_execution_status', 'onchain_execution_tx_hash', 'onchain_execution_started_at',
    'onchain_execution_submitted_at', 'onchain_execution_poll_count',
)
VOTE_RESULTS = {
    LogVote.VOTE_FOR: 'vote_for_result',
    LogVote.VOTE_AGAINST: 'vote_against_result',
    LogVote.VOTE_ABSTAIN: 'vote_abstain_result',
}
UNRESOLVED_STATUSES = (
    Proposal.ONCHAIN_EXECUTION_PENDING, Proposal.ONCHAIN_EXECUTION_IN_PROGRESS,
    Proposal.ONCHAIN_EXECUTION_SUBMITTED, Proposal.ONCHAIN_EXECUTION_FAILED,
    Proposal.ONCHAIN_EXECUTION_REQUIRES_REVIEW,
)


class SnapshotEncoder(DjangoJSONEncoder):
    def default(self, value):
        if isinstance(value, datetime):
            return value.isoformat()
        return super().default(value)


def _canonical_json(value):
    return json.dumps(value, cls=SnapshotEncoder, sort_keys=True, separators=(',', ':'))


def _fields(instance, fields=None):
    names = fields or [field.attname for field in instance._meta.concrete_fields]
    return {name: getattr(instance, name) for name in names}


class Command(BaseCommand):
    help = (
        'Preview or atomically repair one stored late vote. Data only: retains raw weights, freezes execution '
        'for review, and does not send transactions. Pause and drain all writers before preview/apply.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--proposal-id', type=int, required=True)
        parser.add_argument('--vote-id', type=int, required=True)
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--expected-snapshot')
        parser.add_argument('--writers-paused', action='store_true')

    def handle(self, *args, **options):
        if options['apply'] and (not options['expected_snapshot'] or not options['writers_paused']):
            raise CommandError('--apply requires --expected-snapshot and --writers-paused after draining all writers.')
        with transaction.atomic():
            try:
                proposal = Proposal.objects.select_for_update().get(pk=options['proposal_id'])
            except Proposal.DoesNotExist as exc:
                raise CommandError('Proposal does not exist.') from exc
            if not proposal.asset_token_id:
                raise CommandError('Proposal must have a linked asset token.')
            token = AssetToken.objects.select_for_update().get(pk=proposal.asset_token_id)
            votes = list(LogVote.objects.select_for_update().filter(proposal=proposal).order_by('pk'))
            related = list(
                Proposal.objects.select_for_update().filter(asset_token_id=token.pk)
                .exclude(pk=proposal.pk).order_by('pk'),
            )
            target = next((vote for vote in votes if vote.pk == options['vote_id']), None)
            self._validate(proposal, token, votes, target, related)
            totals = self._totals(votes, target)
            reason = (
                f'Late vote repair: proposal={proposal.pk}, vote={target.pk}, '
                f'original_created_at={target.created_at.isoformat()}, end_at={proposal.end_at.isoformat()}. '
                'Frozen totals corrected; asset registry state and execution require manual review.'
            )
            changes = {
                'vote': {'id': target.pk, 'hide': True},
                'proposal': {**totals, 'onchain_execution_status': Proposal.ONCHAIN_EXECUTION_REQUIRES_REVIEW},
                'asset_token': {
                    'contract_sync_status': AssetToken.CONTRACT_SYNC_REQUIRES_REVIEW,
                    'contract_sync_error': reason,
                    'contract_sync_updated_at': 'apply_time_utc',
                },
            }
            snapshot = {
                'before': {
                    'proposal': _fields(proposal, PROPOSAL_FIELDS),
                    'asset_token': _fields(token),
                    'votes': [_fields(vote) for vote in votes],
                    'related_proposals': [_fields(item, PROPOSAL_FIELDS) for item in related],
                },
                'changes': changes,
                'totals': totals,
                'decision': self._decision(proposal, totals),
            }
            digest = hashlib.sha256(_canonical_json(snapshot).encode('utf-8')).hexdigest()
            if options['apply'] and options['expected_snapshot'] != digest:
                raise CommandError('Expected snapshot does not match the locked current snapshot; preview again.')
            applied_at = None
            if options['apply']:
                applied_at = timezone.now()
                LogVote.objects.filter(pk=target.pk).update(hide=True)
                Proposal.objects.filter(pk=proposal.pk).update(**changes['proposal'])
                AssetToken.objects.filter(pk=token.pk).update(
                    **{**changes['asset_token'], 'contract_sync_updated_at': applied_at},
                )
            result = {
                **snapshot, 'snapshot_sha256': digest, 'applied': options['apply'], 'applied_at': applied_at,
                'checkpoint': 'Database repair only; execution and contract synchronization require manual review.',
                'precondition': 'All indexing, finalization and execution writers must remain paused and drained.',
            }
        self.stdout.write(_canonical_json(result))

    def _validate(self, proposal, token, votes, target, related):
        if (
            not proposal.is_asset_proposal or proposal.proposal_status != Proposal.VOTED
            or proposal.end_at is None or proposal.end_at >= timezone.now()
            or proposal.hide or proposal.draft or proposal.action != Proposal.NONE
        ):
            raise CommandError('Repair requires a visible, ended VOTED asset proposal with no pending action.')
        if (
            proposal.asset_contract_address != token.pk or proposal.asset_code != token.classic_code
            or proposal.asset_issuer != token.classic_issuer
        ):
            raise CommandError('Proposal asset identity does not match the linked token.')
        if (
            proposal.onchain_execution_status != Proposal.ONCHAIN_EXECUTION_SKIPPED
            or proposal.onchain_execution_tx_hash is not None or proposal.onchain_execution_started_at is not None
            or proposal.onchain_execution_submitted_at is not None or proposal.onchain_execution_poll_count != 0
        ):
            raise CommandError('Repair requires SKIPPED execution with null hashes/timestamps and no poll attempts.')
        if (
            token.contract_sync_tx_hash is not None
            or token.contract_sync_status == AssetToken.CONTRACT_SYNC_REQUIRES_REVIEW
            or any(
                item.onchain_execution_tx_hash is not None or item.onchain_execution_started_at is not None
                or item.onchain_execution_submitted_at is not None or item.onchain_execution_poll_count != 0
                or (
                    item.onchain_execution_status in UNRESOLVED_STATUSES
                    and not (
                        item.onchain_execution_status == Proposal.ONCHAIN_EXECUTION_PENDING
                        and item.proposal_status != Proposal.VOTED
                    )
                )
                for item in related
            )
        ):
            raise CommandError('Linked token has unresolved execution or contract synchronization evidence.')
        if target is None or target.hide or target.created_at is None or target.created_at <= proposal.end_at:
            raise CommandError('Selected vote must be a visible row with a stored original creation date after end_at.')
        if not target.claimable_balance_id:
            raise CommandError('Selected vote must have a claimable balance ID.')
        if LogVote.objects.select_for_update().filter(
            claimable_balance_id=target.claimable_balance_id, hide=True,
        ).exists():
            raise CommandError('A hidden shadow row already exists for the selected balance.')
        for vote in votes:
            if vote.hide or vote.pk == target.pk:
                continue
            if vote.created_at is None or vote.created_at > proposal.end_at:
                raise CommandError('Additional visible late or undated votes require separate review.')

    def _totals(self, votes, target):
        totals = {field: Decimal('0.0000000') for field in VOTE_RESULTS.values()}
        supported_assets = (settings.GOVERNANCE_ICE_ASSET_CODE, settings.GDICE_ASSET_CODE)
        for vote in votes:
            if vote.hide or vote.pk == target.pk or vote.asset_code not in supported_assets:
                continue
            if vote.vote_choice not in VOTE_RESULTS:
                raise CommandError('Countable vote has an unknown choice.')
            amount = vote.voted_amount if vote.voted_amount is not None else vote.amount
            if amount is None or not amount.is_finite() or amount < 0:
                raise CommandError('Countable vote has a missing or invalid weight.')
            totals[VOTE_RESULTS[vote.vote_choice]] += amount
        return totals

    def _decision(self, proposal, totals):
        supply = proposal.ice_circulating_supply
        if supply is None or not supply.is_finite() or supply < 0:
            raise CommandError('Preserved ICE supply must be a finite, nonnegative value.')
        total_votes = sum(totals.values())
        required_votes = supply * proposal.percent_for_quorum / Decimal('100')
        majority_for = totals['vote_for_result'] > totals['vote_against_result']
        quorum_met = total_votes >= required_votes
        return {
            'majority_for': majority_for, 'total_votes': total_votes, 'required_votes': required_votes,
            'quorum_met': quorum_met, 'approved': majority_for and quorum_met,
        }
