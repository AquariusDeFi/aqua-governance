"""Operator remedy for a proposal the payment check has terminally rejected.

A terminal verdict records `payment_check_rejected_hash`, which takes the row out of the
sweep for good, and `ProposalAdmin.readonly_fields` makes `action`, `payment_status` and
the rejection marker uneditable even for a superuser.  Without this command an over-payer
the exact-amount rule rejected has no remedy short of a database shell.

It is deliberately narrow: it re-arms the payment check and nothing else.  It does not
promote a transition, does not touch a hash column, and does not release a burned hash -
a hash in `ConsumedTransaction` stays burned, so re-arming a row cannot replay a payment.
"""
import logging

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from aqua_governance.governance.models import Proposal


logger = logging.getLogger(__name__)

ACTIONS = [action for action, _label in Proposal.PROPOSAL_ACTION_CHOICES]


class Command(BaseCommand):
    help = 'Clear a terminal payment rejection so the sweep re-checks the proposal.'

    def add_arguments(self, parser):
        parser.add_argument('proposal_id', type=int)
        parser.add_argument(
            '--action',
            required=True,
            choices=ACTIONS,
            help='The action to re-arm. NONE disarms the row instead.',
        )
        parser.add_argument(
            '--unhide',
            action='store_true',
            help='Also clear hide, which a rejected creation sets and which keeps the row out of the sweep.',
        )

    def handle(self, *args, **options):
        proposal_id = options['proposal_id']

        with transaction.atomic():
            try:
                proposal = Proposal.objects.select_for_update().get(id=proposal_id)
            except Proposal.DoesNotExist:
                raise CommandError('No proposal with id {0}.'.format(proposal_id))

            before = {
                'action': proposal.action,
                'payment_status': proposal.payment_status,
                'payment_check_rejected_hash': proposal.payment_check_rejected_hash,
                'hide': proposal.hide,
            }
            fields = {
                'action': options['action'],
                'payment_status': Proposal.FINE,
                'payment_check_rejected_hash': None,
            }
            if options['unhide']:
                fields['hide'] = False

            Proposal.objects.filter(id=proposal_id).update(**fields)

            # The only durable record of a by-hand write to payment state: stdout lives
            # and dies with the operator's terminal.
            logger.error(
                'Proposal payment check re-armed by hand: proposal=%s before=%s after=%s',
                proposal_id,
                before,
                fields,
                extra={
                    'proposal_id': proposal_id,
                    'rearm_before': before,
                    'rearm_after': fields,
                },
            )

        self.stdout.write('Proposal {0} re-armed.'.format(proposal_id))
        for name, value in sorted(fields.items()):
            self.stdout.write('  {0}: {1!r} -> {2!r}'.format(name, before[name], value))
