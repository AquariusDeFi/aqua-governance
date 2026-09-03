import base64
import hashlib
from datetime import datetime, timedelta
from datetime import timezone as datetime_timezone
from decimal import Decimal
from types import SimpleNamespace

from django.test import SimpleTestCase, override_settings

from stellar_sdk import HashMemo, MuxedAccount

from aqua_governance.governance.tests._factories import ATTACKER_KEYPAIR, DEFAULT_PROPOSED_BY
from aqua_governance.utils.memo import (
    MEMO_FORMAT_CANONICAL,
    MEMO_FORMAT_LEGACY,
    MEMO_PREFIX,
    PURPOSE_CREATE,
    PURPOSE_SUBMIT,
    PURPOSE_UPDATE,
    MemoExpectation,
    MemoPayloadError,
    build_memo_expectation,
    create_memo_payload,
    field_digest,
    iso_utc_seconds,
    legacy_memo_digest,
    match_memo,
    memo_digest,
    submit_memo_payload,
    update_memo_payload,
)


UTC = datetime_timezone.utc

PROPOSED_BY = 'GA5WUJ54Z23KILLCUOUNAKTPBVZWKMQVO4O6EQ5GHLAERIMLLHNCSKYH'

TITLE = 'Test proposal'
TEXT = '<p>Hello</p>'
NEW_TITLE = 'New title'
NEW_TEXT = '<p>Updated</p>'
UNICODE_TITLE = 'Проверка 🚀'
UNICODE_TEXT = '<p>тест &amp; “quotes”</p>'

TITLE_DIGEST = '1c6b4a6130bbe90b01411cf29743a638cf8520f556dcbdc570ff655bfafd2c0a'
TEXT_DIGEST = 'd0a26d23e9d8e0538fd47e7bc502d26cf6c320e8daaec7c8521d4769530f5900'
NEW_TITLE_DIGEST = '522156b9b0af7eb99063569c92036931a3c9f027728ac6de8a70bcd0a1d3721c'
NEW_TEXT_DIGEST = '70eabcfdbd5707888bb57642a544db5a1ba8d37906f7cb4c76942fe2ada372ba'
UNICODE_TITLE_DIGEST = '636b7a815cb765df75702f76349ebe3a6e8e21bcf9f6736e2d9ec6be58d349bf'
UNICODE_TEXT_DIGEST = 'b231442f666dfd7c700381d98eb1a11710da0fbf41194ba3c198bb7c72a45f09'

C1_PAYLOAD = (
    'AQUA-GOV|v1|CREATE|GA5WUJ54Z23KILLCUOUNAKTPBVZWKMQVO4O6EQ5GHLAERIMLLHNCSKYH|GENERAL'
    '|1c6b4a6130bbe90b01411cf29743a638cf8520f556dcbdc570ff655bfafd2c0a'
    '|d0a26d23e9d8e0538fd47e7bc502d26cf6c320e8daaec7c8521d4769530f5900'
)
C1_MEMO_HEX = '1a5813d673d5c97e2c8c44f41a71d98f7e5d06a76f14f02dc5b5b2b82bbd4f6e'
C1_MEMO_BASE64 = 'GlgT1nPVyX4sjET0GnHZj35dBqdvFPAtxbWyuCu9T24='

C2_PAYLOAD = (
    'AQUA-GOV|v1|CREATE|GA5WUJ54Z23KILLCUOUNAKTPBVZWKMQVO4O6EQ5GHLAERIMLLHNCSKYH|ADD_ASSET'
    '|1c6b4a6130bbe90b01411cf29743a638cf8520f556dcbdc570ff655bfafd2c0a'
    '|d0a26d23e9d8e0538fd47e7bc502d26cf6c320e8daaec7c8521d4769530f5900'
)
C2_MEMO_HEX = 'a5dadfa0c640e1a250936d5a95055415094ca3587083e3822f068a430c0c01c7'
C2_MEMO_BASE64 = 'pdrfoMZA4aJQk21alQVUFQlMo1hwg+OCLwaKQwwMAcc='

C3_PAYLOAD = (
    'AQUA-GOV|v1|CREATE|GA5WUJ54Z23KILLCUOUNAKTPBVZWKMQVO4O6EQ5GHLAERIMLLHNCSKYH|GENERAL'
    '|636b7a815cb765df75702f76349ebe3a6e8e21bcf9f6736e2d9ec6be58d349bf'
    '|b231442f666dfd7c700381d98eb1a11710da0fbf41194ba3c198bb7c72a45f09'
)
C3_MEMO_HEX = '93b7b60f7ce551fbbdada926a1cc523eddc191586ebb41883bd944de9c66fd27'
C3_MEMO_BASE64 = 'k7e2D3zlUfu9rakmocxSPt3BkVhuu0GIO9lE3pxm/Sc='

U1_PAYLOAD = (
    'AQUA-GOV|v1|UPDATE|42'
    '|522156b9b0af7eb99063569c92036931a3c9f027728ac6de8a70bcd0a1d3721c'
    '|70eabcfdbd5707888bb57642a544db5a1ba8d37906f7cb4c76942fe2ada372ba'
)
U1_MEMO_HEX = '268b7ee34dad9b091c1b49c8f7aaa40bddea7d797d04ea24908dfce0f2ad6677'
U1_MEMO_BASE64 = 'Jot+402tmwkcG0nI96qkC93qfXl9BOokkI384PKtZnc='

S1_PAYLOAD = 'AQUA-GOV|v1|SUBMIT|42|2026-09-07T00:00:00Z|2026-09-13T23:59:59Z'
S1_MEMO_HEX = 'ebed4212138ebbac4f2b99be9d7fbc5a7dac4d4baae80d3d248d8b62fca103c1'
S1_MEMO_BASE64 = '6+1CEhOOu6xPK5m+nX+8Wn2sTUuq6A09JI2LYvyhA8E='

LEGACY_MEMO_HEX = TEXT_DIGEST
LEGACY_MEMO_BASE64 = '0KJtI+nY4FOP1H57xQLSbPbDIOjarsfIUh1HaVMPWQA='

SUBMIT_START_AT = datetime(2026, 9, 7, 0, 0, 0, tzinfo=UTC)
SUBMIT_END_AT = datetime(2026, 9, 13, 23, 59, 59, tzinfo=UTC)

GENERAL = 'GENERAL'
ADD_ASSET = 'ADD_ASSET'


def _memo_base64(digest):
    return base64.b64encode(digest).decode()


class GoldenVectorTests(SimpleTestCase):
    """Byte-exact vectors the JavaScript twin has to reproduce."""

    def test_default_proposed_by_anchors_every_vector(self):
        self.assertEqual(DEFAULT_PROPOSED_BY, PROPOSED_BY)

    def test_field_digests(self):
        cases = (
            (TITLE, TITLE_DIGEST),
            (TEXT, TEXT_DIGEST),
            (NEW_TITLE, NEW_TITLE_DIGEST),
            (NEW_TEXT, NEW_TEXT_DIGEST),
            (UNICODE_TITLE, UNICODE_TITLE_DIGEST),
            (UNICODE_TEXT, UNICODE_TEXT_DIGEST),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(field_digest(value), expected)

    def test_create_vector(self):
        payload = create_memo_payload(
            proposed_by=PROPOSED_BY,
            proposal_type=GENERAL,
            title=TITLE,
            text_html=TEXT,
        )

        self.assertEqual(payload, C1_PAYLOAD)
        self.assertEqual(memo_digest(payload).hex(), C1_MEMO_HEX)
        self.assertEqual(_memo_base64(memo_digest(payload)), C1_MEMO_BASE64)

    def test_asset_create_vector(self):
        payload = create_memo_payload(
            proposed_by=PROPOSED_BY,
            proposal_type=ADD_ASSET,
            title=TITLE,
            text_html=TEXT,
        )

        self.assertEqual(payload, C2_PAYLOAD)
        self.assertEqual(memo_digest(payload).hex(), C2_MEMO_HEX)
        self.assertEqual(_memo_base64(memo_digest(payload)), C2_MEMO_BASE64)

    def test_non_ascii_create_vector(self):
        payload = create_memo_payload(
            proposed_by=PROPOSED_BY,
            proposal_type=GENERAL,
            title=UNICODE_TITLE,
            text_html=UNICODE_TEXT,
        )

        self.assertEqual(payload, C3_PAYLOAD)
        self.assertEqual(memo_digest(payload).hex(), C3_MEMO_HEX)
        self.assertEqual(_memo_base64(memo_digest(payload)), C3_MEMO_BASE64)

    def test_update_vector(self):
        payload = update_memo_payload(proposal_id=42, new_title=NEW_TITLE, new_text_html=NEW_TEXT)

        self.assertEqual(payload, U1_PAYLOAD)
        self.assertEqual(memo_digest(payload).hex(), U1_MEMO_HEX)
        self.assertEqual(_memo_base64(memo_digest(payload)), U1_MEMO_BASE64)

    def test_submit_vector(self):
        payload = submit_memo_payload(proposal_id=42, start_at=SUBMIT_START_AT, end_at=SUBMIT_END_AT)

        self.assertEqual(payload, S1_PAYLOAD)
        self.assertEqual(memo_digest(payload).hex(), S1_MEMO_HEX)
        self.assertEqual(_memo_base64(memo_digest(payload)), S1_MEMO_BASE64)

    def test_legacy_vector(self):
        digest = legacy_memo_digest(TEXT)

        self.assertEqual(digest.hex(), LEGACY_MEMO_HEX)
        self.assertEqual(_memo_base64(digest), LEGACY_MEMO_BASE64)

    def test_legacy_digest_reproduces_the_pre_v1_horizon_comparison(self):
        """The bridge to every payment made before v1: the memo string Horizon serves."""
        text_hash = hashlib.sha256(TEXT.encode('utf-8')).hexdigest()
        horizon_memo = base64.b64encode(HashMemo(text_hash).memo_hash).decode()

        self.assertEqual(_memo_base64(legacy_memo_digest(TEXT)), horizon_memo)

    def test_canonical_digest_round_trips_through_hash_memo(self):
        digest = memo_digest(C1_PAYLOAD)

        self.assertEqual(HashMemo(digest).memo_hash, digest)
        self.assertEqual(base64.b64encode(HashMemo(digest).memo_hash).decode(), C1_MEMO_BASE64)

    def test_every_payload_carries_the_domain_prefix(self):
        for payload in (C1_PAYLOAD, C2_PAYLOAD, C3_PAYLOAD, U1_PAYLOAD, S1_PAYLOAD):
            with self.subTest(payload=payload):
                self.assertTrue(payload.startswith(MEMO_PREFIX + '|'))

    def test_proposal_type_is_the_only_difference_between_the_two_create_vectors(self):
        general = C1_PAYLOAD.split('|')
        add_asset = C2_PAYLOAD.split('|')

        self.assertEqual(
            [element for index, element in enumerate(general) if index != 4],
            [element for index, element in enumerate(add_asset) if index != 4],
        )
        self.assertNotEqual(memo_digest(C1_PAYLOAD), memo_digest(C2_PAYLOAD))


class FieldDigestTests(SimpleTestCase):
    def test_inner_hashing_makes_the_framing_injective(self):
        """A separator inside a field cannot be moved between fields."""
        shifted_title = create_memo_payload(
            proposed_by=PROPOSED_BY,
            proposal_type=GENERAL,
            title='A|B',
            text_html='X',
        )
        shifted_text = create_memo_payload(
            proposed_by=PROPOSED_BY,
            proposal_type=GENERAL,
            title='A',
            text_html='B|X',
        )

        self.assertNotEqual(shifted_title, shifted_text)
        self.assertNotEqual(memo_digest(shifted_title), memo_digest(shifted_text))

    def test_a_literal_element_may_not_carry_the_separator(self):
        """The one element that is not inner-hashed has to refuse it itself."""
        with self.assertRaises(MemoPayloadError):
            create_memo_payload(
                proposed_by=PROPOSED_BY,
                proposal_type='GENERAL|{0}'.format('0' * 64),
                title=TITLE,
                text_html=TEXT,
            )

    def test_digest_is_lowercase_hex_of_fixed_width(self):
        digest = field_digest(UNICODE_TITLE)

        self.assertEqual(len(digest), 64)
        self.assertEqual(digest, digest.lower())

    def test_rejects_none(self):
        with self.assertRaises(MemoPayloadError):
            field_digest(None)

    def test_rejects_non_string(self):
        for value in (7, {'html': 'x'}, b'<p>Hello</p>'):
            with self.subTest(value=value):
                with self.assertRaises(MemoPayloadError):
                    field_digest(value)

    def test_rejects_lone_surrogate(self):
        with self.assertRaises(MemoPayloadError):
            field_digest('\ud800')


class LegacyMemoDigestTests(SimpleTestCase):
    """`legacy_memo_digest` is total in the same way the canonical builders are."""

    def test_rejects_non_string_text(self):
        for value in (None, 7, {'html': '<p>Hello</p>'}):
            with self.subTest(value=value):
                with self.assertRaises(MemoPayloadError):
                    legacy_memo_digest(value)

    def test_rejects_lone_surrogate_text(self):
        with self.assertRaises(MemoPayloadError):
            legacy_memo_digest('<p>\ud800</p>')

    def test_surrogate_text_degrades_the_expectation_instead_of_raising(self):
        expectation = build_memo_expectation(
            PURPOSE_UPDATE,
            proposal_id=42,
            title=NEW_TITLE,
            text_html='<p>\ud800</p>',
        )

        self.assertIsNone(expectation.canonical_digest)
        self.assertIsNone(expectation.legacy_digest)
        self.assertEqual(expectation.accepted(), ())
        self.assertIsNotNone(expectation.canonical_error)
        self.assertIsNotNone(expectation.legacy_error)


class IsoUtcSecondsTests(SimpleTestCase):
    def test_equal_instants_in_other_zones_normalise_to_the_same_string(self):
        moscow = datetime(2026, 9, 7, 3, 0, 0, tzinfo=datetime_timezone(timedelta(hours=3)))

        self.assertEqual(iso_utc_seconds(moscow), '2026-09-07T00:00:00Z')

    def test_equal_instants_in_other_zones_produce_the_same_submit_digest(self):
        moscow_start = SUBMIT_START_AT.astimezone(datetime_timezone(timedelta(hours=3)))
        moscow_end = SUBMIT_END_AT.astimezone(datetime_timezone(timedelta(hours=3)))

        payload = submit_memo_payload(proposal_id=42, start_at=moscow_start, end_at=moscow_end)

        self.assertEqual(payload, S1_PAYLOAD)
        self.assertEqual(memo_digest(payload).hex(), S1_MEMO_HEX)

    def test_rejects_naive_datetime(self):
        with self.assertRaises(MemoPayloadError):
            iso_utc_seconds(datetime(2026, 9, 7, 0, 0, 0))

    def test_rejects_sub_second_precision(self):
        with self.assertRaises(MemoPayloadError):
            iso_utc_seconds(datetime(2026, 9, 7, 0, 0, 0, 1, tzinfo=UTC))

    def test_rejects_non_datetime(self):
        for value in (None, '2026-09-07T00:00:00Z', 1757203200):
            with self.subTest(value=value):
                with self.assertRaises(MemoPayloadError):
                    iso_utc_seconds(value)

    def test_submit_payload_rejects_a_naive_window(self):
        with self.assertRaises(MemoPayloadError):
            submit_memo_payload(
                proposal_id=42,
                start_at=datetime(2026, 9, 7, 0, 0, 0),
                end_at=SUBMIT_END_AT,
            )


class ProposalIdElementTests(SimpleTestCase):
    def test_an_int_and_its_own_decimal_spelling_agree(self):
        for value in (42, '42'):
            with self.subTest(value=value):
                payload = update_memo_payload(
                    proposal_id=value,
                    new_title=NEW_TITLE,
                    new_text_html=NEW_TEXT,
                )
                self.assertEqual(payload, U1_PAYLOAD)

    def test_rejects_a_spelling_a_js_helper_would_render_differently(self):
        # '042' and 42.0 are integral, so a lenient coercion would accept them and hash
        # '42' while another implementation of the grammar hashes what it was given.
        for value in ('042', ' 42', '+42', Decimal(42), 42.0):
            with self.subTest(value=value):
                with self.assertRaises(MemoPayloadError):
                    update_memo_payload(proposal_id=value, new_title=NEW_TITLE, new_text_html=NEW_TEXT)

    def test_rejects_missing_and_non_integral_ids(self):
        for value in (None, True, 42.5, Decimal('42.5'), 'forty-two', ''):
            with self.subTest(value=value):
                with self.assertRaises(MemoPayloadError):
                    update_memo_payload(proposal_id=value, new_title=NEW_TITLE, new_text_html=NEW_TEXT)


class AccountElementTests(SimpleTestCase):
    def test_muxed_proposed_by_folds_to_the_underlying_account(self):
        muxed = MuxedAccount(PROPOSED_BY, 1234).universal_account_id

        payload = create_memo_payload(
            proposed_by=muxed,
            proposal_type=GENERAL,
            title=TITLE,
            text_html=TEXT,
        )

        self.assertEqual(payload, C1_PAYLOAD)

    def test_another_account_changes_the_digest(self):
        payload = create_memo_payload(
            proposed_by=ATTACKER_KEYPAIR.public_key,
            proposal_type=GENERAL,
            title=TITLE,
            text_html=TEXT,
        )

        self.assertNotEqual(memo_digest(payload).hex(), C1_MEMO_HEX)

    def test_rejects_an_unnormalisable_account(self):
        for value in (None, '', 'not-an-account', 7):
            with self.subTest(value=value):
                with self.assertRaises(MemoPayloadError):
                    create_memo_payload(
                        proposed_by=value,
                        proposal_type=GENERAL,
                        title=TITLE,
                        text_html=TEXT,
                    )


class BuildMemoExpectationTests(SimpleTestCase):
    def test_create_expectation_carries_both_formats(self):
        expectation = build_memo_expectation(
            PURPOSE_CREATE,
            proposed_by=PROPOSED_BY,
            proposal_type=GENERAL,
            title=TITLE,
            text_html=TEXT,
        )

        self.assertEqual(expectation.purpose, PURPOSE_CREATE)
        self.assertEqual(expectation.canonical_payload, C1_PAYLOAD)
        self.assertEqual(expectation.canonical_digest.hex(), C1_MEMO_HEX)
        self.assertEqual(expectation.legacy_digest.hex(), LEGACY_MEMO_HEX)
        self.assertIsNone(expectation.canonical_error)
        self.assertIsNone(expectation.legacy_error)

    def test_accepted_offers_canonical_before_legacy(self):
        expectation = build_memo_expectation(
            PURPOSE_CREATE,
            proposed_by=PROPOSED_BY,
            proposal_type=GENERAL,
            title=TITLE,
            text_html=TEXT,
        )

        self.assertEqual(
            expectation.accepted(),
            (
                (MEMO_FORMAT_CANONICAL, bytes.fromhex(C1_MEMO_HEX)),
                (MEMO_FORMAT_LEGACY, bytes.fromhex(LEGACY_MEMO_HEX)),
            ),
        )

    def test_update_expectation_hashes_the_staged_values(self):
        expectation = build_memo_expectation(
            PURPOSE_UPDATE,
            proposal_id=42,
            title=NEW_TITLE,
            text_html=NEW_TEXT,
        )

        self.assertEqual(expectation.canonical_payload, U1_PAYLOAD)
        self.assertEqual(expectation.canonical_digest.hex(), U1_MEMO_HEX)
        self.assertEqual(expectation.legacy_digest, legacy_memo_digest(NEW_TEXT))

    def test_submit_expectation_requires_the_legacy_text_explicitly(self):
        without_text = build_memo_expectation(
            PURPOSE_SUBMIT,
            proposal_id=42,
            start_at=SUBMIT_START_AT,
            end_at=SUBMIT_END_AT,
        )

        self.assertEqual(without_text.canonical_digest.hex(), S1_MEMO_HEX)
        self.assertIsNone(without_text.legacy_digest)
        self.assertIn('legacy_text_html', without_text.legacy_error)

    def test_submit_expectation_hashes_the_current_text_not_the_window(self):
        expectation = build_memo_expectation(
            PURPOSE_SUBMIT,
            proposal_id=42,
            start_at=SUBMIT_START_AT,
            end_at=SUBMIT_END_AT,
            legacy_text_html=TEXT,
        )

        self.assertEqual(expectation.canonical_digest.hex(), S1_MEMO_HEX)
        self.assertEqual(expectation.legacy_digest.hex(), LEGACY_MEMO_HEX)

    def test_accept_legacy_false_drops_the_legacy_digest(self):
        expectation = build_memo_expectation(
            PURPOSE_UPDATE,
            proposal_id=42,
            title=NEW_TITLE,
            text_html=NEW_TEXT,
            accept_legacy=False,
        )

        self.assertIsNone(expectation.legacy_digest)
        self.assertEqual(
            expectation.accepted(),
            ((MEMO_FORMAT_CANONICAL, bytes.fromhex(U1_MEMO_HEX)),),
        )

    @override_settings(PROPOSAL_LEGACY_MEMO_ACCEPTED=False)
    def test_accept_legacy_defaults_to_the_setting_when_off(self):
        expectation = build_memo_expectation(
            PURPOSE_UPDATE,
            proposal_id=42,
            title=NEW_TITLE,
            text_html=NEW_TEXT,
        )

        self.assertIsNone(expectation.legacy_digest)

    @override_settings(PROPOSAL_LEGACY_MEMO_ACCEPTED=True)
    def test_accept_legacy_defaults_to_the_setting_when_on(self):
        expectation = build_memo_expectation(
            PURPOSE_UPDATE,
            proposal_id=42,
            title=NEW_TITLE,
            text_html=NEW_TEXT,
        )

        self.assertEqual(expectation.legacy_digest, legacy_memo_digest(NEW_TEXT))

    def test_degrades_to_legacy_only_when_the_canonical_build_fails(self):
        expectation = build_memo_expectation(
            PURPOSE_UPDATE,
            proposal_id=None,
            title=NEW_TITLE,
            text_html=NEW_TEXT,
        )

        self.assertIsNone(expectation.canonical_digest)
        self.assertIsNone(expectation.canonical_payload)
        self.assertIn('proposal_id', expectation.canonical_error)
        self.assertEqual(
            expectation.accepted(),
            ((MEMO_FORMAT_LEGACY, legacy_memo_digest(NEW_TEXT)),),
        )

    def test_accepts_nothing_when_legacy_is_off_and_the_canonical_build_fails(self):
        expectation = build_memo_expectation(
            PURPOSE_SUBMIT,
            proposal_id=42,
            start_at=SUBMIT_START_AT,
            end_at=datetime(2026, 9, 13, 23, 59, 59, 500000, tzinfo=UTC),
            legacy_text_html=TEXT,
            accept_legacy=False,
        )

        self.assertIsNone(expectation.canonical_digest)
        self.assertIsNone(expectation.legacy_digest)
        self.assertEqual(expectation.accepted(), ())

    def test_unknown_purpose_is_a_programming_error(self):
        with self.assertRaises(ValueError) as raised:
            build_memo_expectation('DELETE', proposal_id=42, title=NEW_TITLE, text_html=NEW_TEXT)

        self.assertNotIsInstance(raised.exception, MemoPayloadError)


class MatchMemoTests(SimpleTestCase):
    def _create_expectation(self, **overrides):
        kwargs = {
            'proposed_by': PROPOSED_BY,
            'proposal_type': GENERAL,
            'title': TITLE,
            'text_html': TEXT,
        }
        kwargs.update(overrides)
        return build_memo_expectation(PURPOSE_CREATE, **kwargs)

    def test_reports_canonical_for_a_canonical_memo(self):
        expectation = self._create_expectation()

        self.assertEqual(match_memo(expectation, bytes.fromhex(C1_MEMO_HEX)), MEMO_FORMAT_CANONICAL)

    def test_reports_legacy_for_a_legacy_memo(self):
        expectation = self._create_expectation()

        self.assertEqual(match_memo(expectation, bytes.fromhex(LEGACY_MEMO_HEX)), MEMO_FORMAT_LEGACY)

    def test_reports_canonical_when_both_digests_match(self):
        """Canonical-first ordering is what keeps the reported format honest."""
        digest = bytes.fromhex(C1_MEMO_HEX)
        expectation = MemoExpectation(
            purpose=PURPOSE_CREATE,
            canonical_digest=digest,
            canonical_payload=C1_PAYLOAD,
            legacy_digest=digest,
        )

        self.assertEqual(match_memo(expectation, digest), MEMO_FORMAT_CANONICAL)

    def test_a_legacy_preimage_forged_as_a_canonical_payload_is_reported_as_legacy(self):
        """The prefix buys rotation, not domain separation - so the format reported must be the weak one."""
        forged_text = create_memo_payload(
            proposed_by=PROPOSED_BY,
            proposal_type=GENERAL,
            title=TITLE,
            text_html=TEXT,
        )
        expectation = self._create_expectation(text_html=forged_text)

        matched = match_memo(expectation, memo_digest(forged_text))

        self.assertEqual(matched, MEMO_FORMAT_LEGACY)
        self.assertNotEqual(matched, MEMO_FORMAT_CANONICAL)
        self.assertEqual(legacy_memo_digest(forged_text), memo_digest(forged_text))

    def test_accepts_a_bytearray_memo(self):
        expectation = self._create_expectation()

        self.assertEqual(
            match_memo(expectation, bytearray(bytes.fromhex(C1_MEMO_HEX))),
            MEMO_FORMAT_CANONICAL,
        )

    def test_returns_none_for_an_unrelated_memo(self):
        expectation = self._create_expectation()

        self.assertIsNone(match_memo(expectation, bytes(32)))

    def test_returns_none_for_a_non_bytes_memo(self):
        expectation = self._create_expectation()

        for value in (None, C1_MEMO_HEX, 42):
            with self.subTest(value=value):
                self.assertIsNone(match_memo(expectation, value))

    def test_returns_none_for_a_truncated_memo(self):
        expectation = self._create_expectation()

        self.assertIsNone(match_memo(expectation, bytes.fromhex(C1_MEMO_HEX)[:31]))

    def test_returns_none_when_nothing_is_accepted(self):
        expectation = MemoExpectation(
            purpose=PURPOSE_CREATE,
            canonical_digest=None,
            canonical_payload=None,
            legacy_digest=None,
            canonical_error='title must be a string, got NoneType.',
            legacy_error='text_html must be a string, got NoneType.',
        )

        self.assertEqual(expectation.accepted(), ())
        self.assertIsNone(match_memo(expectation, bytes.fromhex(C1_MEMO_HEX)))


class CreatePreimageAssetFieldsTests(SimpleTestCase):
    """The asset triple stays out of the CREATE preimage: the backend rewrites it after the client hashes."""

    @staticmethod
    def _proposal_stub(**overrides):
        fields = {
            'proposed_by': PROPOSED_BY,
            'proposal_type': ADD_ASSET,
            'title': TITLE,
            'asset_code': 'AQUA',
            'asset_issuer': 'GBNZILSTVQZ4R7IKQDGHYGY2QXL5QOFJYQMXPKWRRM5PAV7Y4M67AQUA',
            'asset_contract_address': None,
        }
        fields.update(overrides)
        return SimpleNamespace(**fields)

    def _digest_for(self, proposal):
        return memo_digest(create_memo_payload(
            proposed_by=proposal.proposed_by,
            proposal_type=proposal.proposal_type,
            title=proposal.title,
            text_html=TEXT,
        ))

    def test_asset_triple_does_not_change_the_create_digest(self):
        staged = self._proposal_stub()
        promoted = self._proposal_stub(
            asset_code='aqua',
            asset_issuer='GA5WUJ54Z23KILLCUOUNAKTPBVZWKMQVO4O6EQ5GHLAERIMLLHNCSKYH',
            asset_contract_address='CBQHNAXSI55GX2GN6D67GK7BHVPSLJUGZQEU7WJ5LKR5PNUCGLIMAO4K',
        )

        self.assertEqual(self._digest_for(staged), self._digest_for(promoted))

    def test_proposal_type_still_separates_an_asset_create_from_a_general_one(self):
        asset = self._proposal_stub()
        general = self._proposal_stub(proposal_type=GENERAL)

        self.assertNotEqual(self._digest_for(asset), self._digest_for(general))

    def test_the_builders_take_no_asset_arguments(self):
        with self.assertRaises(TypeError):
            create_memo_payload(
                proposed_by=PROPOSED_BY,
                proposal_type=ADD_ASSET,
                title=TITLE,
                text_html=TEXT,
                asset_code='AQUA',
            )

        with self.assertRaises(TypeError):
            build_memo_expectation(
                PURPOSE_CREATE,
                proposed_by=PROPOSED_BY,
                proposal_type=ADD_ASSET,
                title=TITLE,
                text_html=TEXT,
                asset_contract_address='CBQHNAXSI55GX2GN6D67GK7BHVPSLJUGZQEU7WJ5LKR5PNUCGLIMAO4K',
            )
