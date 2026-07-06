from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
from stellar_sdk import Asset


ASSET_PROPOSAL_TYPES = ('ADD_ASSET', 'REMOVE_ASSET')


def _normalize(value):
    value = (value or '').strip()
    return value or None


def _derive_contract_address(asset_code, asset_issuer):
    return Asset(asset_code, asset_issuer).contract_id(settings.NETWORK_PASSPHRASE)


def backfill_asset_tokens(apps, schema_editor):
    Proposal = apps.get_model('governance', 'Proposal')
    AssetToken = apps.get_model('governance', 'AssetToken')

    proposals = Proposal.objects.filter(proposal_type__in=ASSET_PROPOSAL_TYPES).order_by('id')
    for proposal in proposals:
        asset_code = _normalize(proposal.asset_code)
        asset_issuer = _normalize(proposal.asset_issuer)
        explicit_contract = _normalize(proposal.asset_contract_address)

        if bool(asset_code) != bool(asset_issuer):
            raise ValueError(f'Proposal {proposal.id} has incomplete classic asset identifier.')

        derived_contract = None
        if asset_code and asset_issuer:
            derived_contract = _derive_contract_address(asset_code, asset_issuer)

        if explicit_contract and derived_contract and explicit_contract != derived_contract:
            raise ValueError(
                f'Proposal {proposal.id} asset_contract_address does not match asset_code + asset_issuer.'
            )

        contract_address = derived_contract or explicit_contract
        if not contract_address:
            raise ValueError(f'Proposal {proposal.id} has no usable asset identifier.')

        token, _ = AssetToken.objects.get_or_create(
            contract_address=contract_address,
            defaults={
                'classic_code': asset_code,
                'classic_issuer': asset_issuer,
            },
        )

        token_update_fields = []
        if asset_code and not token.classic_code:
            token.classic_code = asset_code
            token_update_fields.append('classic_code')
        if asset_issuer and not token.classic_issuer:
            token.classic_issuer = asset_issuer
            token_update_fields.append('classic_issuer')
        if token_update_fields:
            token.save(update_fields=token_update_fields)

        proposal_update_fields = []
        if not explicit_contract:
            proposal.asset_contract_address = contract_address
            proposal_update_fields.append('asset_contract_address')
        if proposal.asset_token_id != contract_address:
            proposal.asset_token_id = contract_address
            proposal_update_fields.append('asset_token')
        if proposal_update_fields:
            proposal.save(update_fields=proposal_update_fields)

    successful_history = Proposal.objects.filter(
        proposal_type__in=ASSET_PROPOSAL_TYPES,
        proposal_status='VOTED',
        onchain_execution_status='SUCCESS',
        asset_token__isnull=False,
    ).order_by('end_at', 'id')

    for proposal in successful_history:
        token = AssetToken.objects.get(pk=proposal.asset_token_id)
        execution_at = proposal.end_at
        update_fields = ['whitelisted', 'last_execution_at']

        if proposal.proposal_type == 'ADD_ASSET':
            token.whitelisted = True
            token.whitelisted_since = execution_at
            token.last_execution_at = execution_at
            update_fields.append('whitelisted_since')
        elif proposal.proposal_type == 'REMOVE_ASSET':
            token.whitelisted = False
            token.unwhitelisted_since = execution_at
            token.last_execution_at = execution_at
            update_fields.append('unwhitelisted_since')
        else:
            continue

        token.save(update_fields=update_fields)


class Migration(migrations.Migration):

    dependencies = [
        ('governance', '0027_proposal_manage_asset_proposals_permission'),
    ]

    operations = [
        migrations.CreateModel(
            name='AssetToken',
            fields=[
                ('contract_address', models.CharField(max_length=128, primary_key=True, serialize=False)),
                ('classic_code', models.CharField(blank=True, max_length=64, null=True)),
                ('classic_issuer', models.CharField(blank=True, max_length=56, null=True)),
                ('whitelisted', models.BooleanField(default=False)),
                ('whitelisted_since', models.DateTimeField(blank=True, null=True)),
                ('unwhitelisted_since', models.DateTimeField(blank=True, null=True)),
                ('last_execution_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('contract_sync_status', models.CharField(choices=[('SYNCED', 'Contract is up to date with DB state'), ('PENDING', 'Waiting for contract update'), ('FAILED', 'Contract update failed'), ('REQUIRES_REVIEW', 'Contract update requires manual review')], db_index=True, default='SYNCED', max_length=16)),
                ('contract_sync_tx_hash', models.CharField(blank=True, max_length=128, null=True)),
                ('contract_sync_updated_at', models.DateTimeField(blank=True, null=True)),
                ('contract_sync_error', models.TextField(blank=True, null=True)),
            ],
            options={
                'indexes': [
                    models.Index(fields=['last_execution_at'], name='gov_assettoken_last_exec_at'),
                ],
            },
        ),
        migrations.AddField(
            model_name='proposal',
            name='asset_token',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='proposals',
                to='governance.assettoken',
            ),
        ),
        migrations.AddIndex(
            model_name='proposal',
            index=models.Index(fields=['asset_token', 'hide', 'draft'], name='gov_proposal_at_hidedraft'),
        ),
        migrations.RunPython(backfill_asset_tokens, reverse_code=migrations.RunPython.noop),
    ]
