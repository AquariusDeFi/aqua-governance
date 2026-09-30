from django.conf import settings
from stellar_sdk import Address, Asset, StrKey


def normalize_asset_addresses(args: list[str]) -> list[str]:
    if not isinstance(args, list):
        raise ValueError("onchain_action_args must be an array of asset addresses.")
    if not args:
        raise ValueError("onchain_action_args must contain at least one asset address.")

    normalized_assets: list[str] = []
    for idx, raw_value in enumerate(args):
        value = str(raw_value).strip()
        if not value:
            raise ValueError(f"onchain_action_args[{idx}] contains an empty asset address.")

        try:
            Address(value)
        except Exception as exc:
            raise ValueError(
                f"onchain_action_args[{idx}] is not a valid Soroban address: {value}",
            ) from exc

        normalized_assets.append(value)

    return normalized_assets


def validate_asset_payload(
    *,
    asset_code,
    asset_issuer,
    asset_contract_address,
    require_onchain_verification: bool,
) -> list[str]:
    normalized_asset_code = str(asset_code).strip() if asset_code is not None else ''
    normalized_asset_issuer = str(asset_issuer).strip() if asset_issuer is not None else ''
    normalized_contract_address = str(asset_contract_address).strip() if asset_contract_address is not None else ''

    has_asset_code = bool(normalized_asset_code)
    has_asset_issuer = bool(normalized_asset_issuer)
    has_classic_asset = has_asset_code and has_asset_issuer
    has_contract_asset = bool(normalized_contract_address)

    if has_asset_code != has_asset_issuer:
        raise ValueError('Provide both asset_code and asset_issuer together.')

    if not has_classic_asset and not has_contract_asset:
        raise ValueError('Provide asset_code + asset_issuer, or asset_contract_address.')

    derived_addresses: list[str] = []
    derived_contract_address = None

    if has_classic_asset:
        if not StrKey.is_valid_ed25519_public_key(normalized_asset_issuer):
            raise ValueError('asset_issuer must be a valid Stellar public key.')

        try:
            derived_contract_address = Asset(
                normalized_asset_code,
                normalized_asset_issuer,
            ).contract_id(settings.NETWORK_PASSPHRASE)
        except Exception as exc:
            raise ValueError('asset_code or asset_issuer is invalid.') from exc

        derived_addresses.append(derived_contract_address)

    if has_contract_asset:
        normalized_addresses = normalize_asset_addresses([normalized_contract_address])
        normalized_contract_address = normalized_addresses[0]

        if derived_contract_address and derived_contract_address != normalized_contract_address:
            raise ValueError('asset_contract_address does not match asset_code + asset_issuer.')

        derived_addresses = [normalized_contract_address]

    return normalize_asset_addresses(derived_addresses)


def derive_onchain_action_args(*, asset_code, asset_issuer, asset_contract_address) -> list[str]:
    return validate_asset_payload(
        asset_code=asset_code,
        asset_issuer=asset_issuer,
        asset_contract_address=asset_contract_address,
        require_onchain_verification=False,
    )
