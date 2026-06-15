import json
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from django_quill.quill import Quill

from aqua_governance.governance.models import Proposal
from aqua_governance.governance.tasks import task_check_expired_proposals
from aqua_governance.governance.tests._factories import (
    DEFAULT_PROPOSED_BY,
    make_asset_proposal_raw,
    patch_ice_circulating_supply,
)


class AssetProposalExpiryTests(TestCase):
    def _create_proposal(self, proposal_type):
        if Proposal.is_asset_proposal_type(proposal_type):
            return make_asset_proposal_raw(
                proposal_type=proposal_type,
                title='Test proposal',
                draft=False,
                action=Proposal.NONE,
                proposal_status=Proposal.DISCUSSION,
            )
        data = {
            'proposed_by': DEFAULT_PROPOSED_BY,
            'title': 'Test proposal',
            'text': Quill(json.dumps({'delta': '', 'html': '<p>Test</p>'})),
            'proposal_type': proposal_type,
            'draft': False,
            'action': Proposal.NONE,
            'proposal_status': Proposal.DISCUSSION,
        }
        with patch_ice_circulating_supply():
            return Proposal.objects.create(**data)

    def test_stale_asset_proposal_expires_like_other_slotless_discussion_proposals(self):
        stale_time = timezone.now() - timedelta(days=31)
        asset_proposal = self._create_proposal(Proposal.PROPOSAL_TYPE_ADD_ASSET)
        general_proposal = self._create_proposal(Proposal.PROPOSAL_TYPE_GENERAL)
        Proposal.objects.filter(id__in=[asset_proposal.id, general_proposal.id]).update(last_updated_at=stale_time)

        task_check_expired_proposals()

        asset_proposal.refresh_from_db()
        general_proposal.refresh_from_db()
        self.assertEqual(asset_proposal.proposal_status, Proposal.EXPIRED)
        self.assertEqual(general_proposal.proposal_status, Proposal.EXPIRED)
