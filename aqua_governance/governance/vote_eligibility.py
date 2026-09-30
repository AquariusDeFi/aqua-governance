from django.db.models import F, Q


def eligible_votes(queryset):
    """Public/countable votes, using the stored original creation date."""
    return queryset.filter(hide=False).filter(
        Q(proposal__end_at__isnull=True) | Q(created_at__lte=F('proposal__end_at')),
    )
