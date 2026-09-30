"""Reconstruction of the ConsumedTransaction ledger from the two source hash columns.

`0031_consumed_transaction` and `manage.py backfill_consumed_transactions` both call in
here, so the migration and the mandatory post-deploy re-run execute the same code.  Every
entry point takes an `apps` registry rather than importing models, so the migration can
hand over its historical models and the management command the live ones.

The reconstruction rests on one lemma: every hash ever successfully consumed is right now
in `Proposal.transaction_hash` or `HistoryProposal.transaction_hash`, because all three
promotion paths end by writing the consumed hash into `Proposal.transaction_hash` and the
displaced one into history.  The lemma stops holding the moment a transition type is added
that does not write into `Proposal.transaction_hash`; from v1 onward the ledger, not this
reconstruction, is the source of truth.
"""
import logging
from dataclasses import asdict, dataclass

from django.db.models.functions import Lower, Trim

from aqua_governance.governance.proposal_constants import CONSUMED_TRANSACTION_PURPOSE_LEGACY, PROPOSAL_ACTION_TO_CREATE


logger = logging.getLogger(__name__)

BACKFILL_PURPOSE = CONSUMED_TRANSACTION_PURPOSE_LEGACY
IN_FLIGHT_CREATE_ACTION = PROPOSAL_ACTION_TO_CREATE

HASH_COLUMNS = (
    ('Proposal', 'transaction_hash'),
    ('Proposal', 'new_transaction_hash'),
    ('HistoryProposal', 'transaction_hash'),
)


@dataclass(frozen=True)
class BackfillReport:
    """What the backfill saw and what it wrote, as one structured record."""

    source_hashes: int = 0
    unique_hashes: int = 0
    rows_created: int = 0
    rows_pre_existing: int = 0
    in_flight_skipped: int = 0
    dry_run: bool = False


def _canonical_hash(value):
    return (value or '').strip().lower()


def _group_by_canonical_hash(model, field_name):
    """Every non-empty value of one hash column, grouped by its canonical spelling.

    The whole column is held in memory, which is what the collision check needs and what
    the table size affords: a few hundred proposals, not a table where this is a decision.
    """
    groups = {}
    for row_id, value in model.objects.order_by('id').values_list('id', field_name):
        canonical = _canonical_hash(value)
        if not canonical:
            continue
        groups.setdefault(canonical, []).append((row_id, value))
    return groups


def _describe_collisions(collisions):
    return '; '.join(
        '{0!r} is held by rows {1}'.format(canonical, [row_id for row_id, _ in sorted(members)])
        for canonical, members in sorted(collisions.items())
    )


def find_hash_case_collisions(apps):
    """Every column where two rows differ only in letter case, keyed by ``Model.field``.

    Read-only, so an operator can compute the condition that aborts the migration days
    before the maintenance window rather than discovering it with celery beat paused.
    """
    collisions = {}
    for model_name, field_name in HASH_COLUMNS:
        model = apps.get_model('governance', model_name)
        colliding = {
            canonical: members
            for canonical, members in _group_by_canonical_hash(model, field_name).items()
            if len(members) > 1
        }
        if colliding:
            collisions['{0}.{1}'.format(model_name, field_name)] = colliding
    return collisions


def _normalize_hash_column(model, field_name):
    """Lowercase one hash column, refusing to guess when two rows differ only in case.

    A unique index is case-sensitive, so a legacy uppercase row plus a later lowercase
    claim would not collide and the staging pre-check meant to be a defence would silently
    pass.  A case-only duplicate must be resolved by a human who knows which row is real:
    collapsing it here would silently destroy one of the two claims the fix depends on.
    """
    groups = _group_by_canonical_hash(model, field_name)

    collisions = {canonical: rows for canonical, rows in groups.items() if len(rows) > 1}
    if collisions:
        raise RuntimeError(
            'Cannot normalise {0}.{1}: {2} hash(es) differ only in letter case and must be '
            'resolved by hand before the ConsumedTransaction ledger can be trusted. {3}'.format(
                model.__name__, field_name, len(collisions), _describe_collisions(collisions),
            )
        )

    stale = [members[0][0] for canonical, members in groups.items() if members[0][1] != canonical]
    if stale:
        model.objects.filter(id__in=stale).update(**{field_name: Lower(Trim(field_name))})


def normalize_proposal_hash_case(apps, schema_editor=None):
    """Lowercase every stored hash so that later exact-match comparisons are safe."""
    for model_name, field_name in HASH_COLUMNS:
        _normalize_hash_column(apps.get_model('governance', model_name), field_name)


def _iter_hashes(queryset, proposal_id_field):
    rows = queryset.order_by('id').values_list('transaction_hash', proposal_id_field)
    for value, proposal_id in rows:
        canonical = _canonical_hash(value)
        if canonical:
            yield canonical, proposal_id


def backfill_consumed_transactions(apps, schema_editor=None, *, dry_run=False):
    """Burn every hash a transition has already spent, and report what happened.

    Idempotent: conflicting inserts are ignored, so a re-run never duplicates a row and
    never downgrades a real CREATE/UPDATE/SUBMIT claim to LEGACY.
    """
    Proposal = apps.get_model('governance', 'Proposal')
    HistoryProposal = apps.get_model('governance', 'HistoryProposal')
    ConsumedTransaction = apps.get_model('governance', 'ConsumedTransaction')

    # Proposal.transaction_hash holds the *pending* payment while action='TO_CREATE'
    # (_check_create_transaction reads it, not new_transaction_hash), so burning it would
    # make the in-flight creation permanently unconfirmable and the payment unrecoverable
    # through the API.  Proposal.new_transaction_hash is a staged claim nothing has
    # confirmed yet, so it is never a source column here either.
    in_flight = Proposal.objects.filter(action=IN_FLIGHT_CREATE_ACTION)
    sources = (
        (Proposal.objects.exclude(action=IN_FLIGHT_CREATE_ACTION), 'id'),
        (HistoryProposal.objects.all(), 'proposal_id'),
    )

    source_hashes = 0
    # A hash held by a live proposal and by a history row is one claim, attributed to the
    # live proposal, because Proposal is read first and the first attribution wins.
    claims = {}
    for queryset, proposal_id_field in sources:
        for canonical, proposal_id in _iter_hashes(queryset, proposal_id_field):
            source_hashes += 1
            claims.setdefault(canonical, proposal_id)

    # Counted before the insert, so the report says how many rows this run adds whether or
    # not it writes them: a dry run that reported every hash as new would tell the operator
    # the mandatory post-deploy re-run is unnecessary.
    rows_pre_existing = ConsumedTransaction.objects.filter(transaction_hash__in=sorted(claims)).count()
    if claims and not dry_run:
        ConsumedTransaction.objects.bulk_create(
            [
                ConsumedTransaction(
                    transaction_hash=canonical,
                    proposal_id=proposal_id,
                    purpose=BACKFILL_PURPOSE,
                    payer=None,
                )
                for canonical, proposal_id in claims.items()
            ],
            ignore_conflicts=True,
        )

    report = BackfillReport(
        source_hashes=source_hashes,
        unique_hashes=len(claims),
        rows_created=len(claims) - rows_pre_existing,
        rows_pre_existing=rows_pre_existing,
        in_flight_skipped=in_flight.count(),
        dry_run=dry_run,
    )

    logger.info(
        'ConsumedTransaction backfill: %s source hashes became %s new rows; %s already '
        'present, %s in-flight TO_CREATE row(s) deliberately left unburned.',
        report.source_hashes,
        report.rows_created,
        report.rows_pre_existing,
        report.in_flight_skipped,
        extra={'consumed_transaction_backfill': asdict(report)},
    )

    return report
