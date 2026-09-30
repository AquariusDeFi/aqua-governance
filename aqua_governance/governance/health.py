from django.db import DatabaseError
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe

from aqua_governance.governance.models import AssetToken, Proposal


@never_cache
@require_safe
def health(request):
    try:
        proposals = Proposal.objects.filter(
            onchain_execution_status=Proposal.ONCHAIN_EXECUTION_REQUIRES_REVIEW,
        ).count()
        asset_tokens = AssetToken.objects.filter(
            contract_sync_status=AssetToken.CONTRACT_SYNC_REQUIRES_REVIEW,
        ).count()
    except DatabaseError:
        return JsonResponse({'status': 'unavailable'}, status=503)

    requires_review = proposals > 0 or asset_tokens > 0
    return JsonResponse({
        'status': 'requires_review' if requires_review else 'ok',
        'requires_review': {'proposals': proposals, 'asset_tokens': asset_tokens},
    }, status=503 if requires_review else 200)
