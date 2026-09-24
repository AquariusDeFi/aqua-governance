from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from stellar_sdk import Server

from aqua_governance.governance.tasks import closed_proposals_with_unclaimed_votes, sync_closed_proposal_claim_state


class Command(BaseCommand):
    help = (
        'Sync claimable balance ids, amounts and claimed flags of the votes of closed proposals, like the '
        'task_sync_closed_proposal_claims Beat task but without a time limit, one proposal at a time, newest '
        'first. Frozen weights and results are not changed.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--proposal-id', type=int, action='append', dest='proposal_ids',
            help='Only sync this proposal if the Beat task would select it. Repeatable.',
        )

    def handle(self, *args, **options):
        proposals = closed_proposals_with_unclaimed_votes(timezone.now())
        requested_ids = options['proposal_ids']
        if requested_ids:
            proposals = proposals.filter(id__in=requested_ids)
        proposals = list(proposals)

        horizon_server = Server(settings.HORIZON_URL)
        for proposal in proposals:
            try:
                unresolved_groups = sync_closed_proposal_claim_state(proposal, horizon_server)
            except Exception as exc:  # noqa: B902
                self.stdout.write(f'proposal {proposal.pk}: error: {exc!r}')
                continue
            self.stdout.write(f'proposal {proposal.pk}: synced (unresolved groups: {unresolved_groups})')

        selected_ids = {proposal.pk for proposal in proposals}
        for proposal_id in requested_ids or ():
            if proposal_id not in selected_ids:
                self.stdout.write(f'proposal {proposal_id}: not selected')
