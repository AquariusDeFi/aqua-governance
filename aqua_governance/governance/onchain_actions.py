from aqua_governance.governance.asset_payload import derive_onchain_action_args


def derive_proposal_onchain_action_args(*, asset_code, asset_issuer, asset_contract_address) -> list[str]:
    return derive_onchain_action_args(
        asset_code=asset_code,
        asset_issuer=asset_issuer,
        asset_contract_address=asset_contract_address,
    )
