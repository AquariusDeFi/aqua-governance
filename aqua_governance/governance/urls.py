from django.urls import include, path

from rest_framework import routers

from aqua_governance.governance.views import AssetProposalViewSet, AssetTokenView, LogVoteView, ProposalsView, ProposalQueueViewSet, ProposalViewSet, TestProposalViewSet

api_router = routers.SimpleRouter()
api_router.register(r'proposals', ProposalsView, basename='proposals')  # TODO: remove it
api_router.register(r'proposal', ProposalViewSet, basename='proposal')
api_router.register(r'test/proposal', TestProposalViewSet, basename='proposaltest')  # todo: remove
api_router.register(r'votes-for-proposal', LogVoteView, basename='log_votes')
api_router.register(r'asset-tokens', AssetTokenView, basename='asset_tokens')
api_router.register(r'asset-proposal', AssetProposalViewSet, basename='asset_proposal')
api_router.register(r'proposal-queue', ProposalQueueViewSet, basename='proposal_queue')


urlpatterns = [
    path('', include(api_router.urls)),
]
