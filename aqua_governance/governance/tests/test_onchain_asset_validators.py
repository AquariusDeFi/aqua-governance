from django.conf import settings
from django.test import SimpleTestCase, override_settings
from stellar_sdk import Asset

from aqua_governance.governance.asset_payload import validate_asset_payload
from aqua_governance.governance.tests._factories import DEFAULT_ISSUER


@override_settings(NETWORK_PASSPHRASE='Test SDF Network ; September 2015')
class ValidateAssetPayloadTests(SimpleTestCase):
    def test_classic_asset_does_not_require_deployed_sac(self):
        expected_contract_address = Asset(
            'AQUA',
            'GBNZILSTVQZ4R7IKQDGHYGY2QXL5QOFJYQMXPKWRRM5PAV7Y4M67AQUA',
        ).contract_id(settings.NETWORK_PASSPHRASE)

        result = validate_asset_payload(
            asset_code='AQUA',
            asset_issuer='GBNZILSTVQZ4R7IKQDGHYGY2QXL5QOFJYQMXPKWRRM5PAV7Y4M67AQUA',
            asset_contract_address=None,
            require_onchain_verification=True,
        )

        self.assertEqual(result, [expected_contract_address])

    def test_classic_asset_accepts_matching_contract_address_without_rpc_check(self):
        expected_contract_address = Asset(
            'AQUA',
            'GBNZILSTVQZ4R7IKQDGHYGY2QXL5QOFJYQMXPKWRRM5PAV7Y4M67AQUA',
        ).contract_id(settings.NETWORK_PASSPHRASE)

        result = validate_asset_payload(
            asset_code='AQUA',
            asset_issuer='GBNZILSTVQZ4R7IKQDGHYGY2QXL5QOFJYQMXPKWRRM5PAV7Y4M67AQUA',
            asset_contract_address=expected_contract_address,
            require_onchain_verification=True,
        )

        self.assertEqual(result, [expected_contract_address])

    def test_explicit_contract_address_is_accepted_without_rpc_check(self):
        result = validate_asset_payload(
            asset_code=None,
            asset_issuer=None,
            asset_contract_address='CAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD2KM',
            require_onchain_verification=True,
        )

        self.assertEqual(
            result,
            ['CAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD2KM'],
        )

    def test_rejects_partial_classic_asset_identifier(self):
        with self.assertRaisesRegex(ValueError, 'Provide both asset_code and asset_issuer together.'):
            validate_asset_payload(
                asset_code='AQUA',
                asset_issuer=None,
                asset_contract_address=None,
                require_onchain_verification=True,
            )

    def test_rejects_invalid_asset_issuer(self):
        with self.assertRaisesRegex(ValueError, 'asset_issuer must be a valid Stellar public key.'):
            validate_asset_payload(
                asset_code='AQUA',
                asset_issuer='not-a-stellar-key',
                asset_contract_address=None,
                require_onchain_verification=True,
            )

    def test_rejects_mismatched_contract_address(self):
        wrong_contract_address = Asset('WRONG', DEFAULT_ISSUER).contract_id('Test SDF Network ; September 2015')

        with self.assertRaisesRegex(
            ValueError,
            'asset_contract_address does not match asset_code \+ asset_issuer.',
        ):
            validate_asset_payload(
                asset_code='AQUA',
                asset_issuer=DEFAULT_ISSUER,
                asset_contract_address=wrong_contract_address,
                require_onchain_verification=True,
            )
