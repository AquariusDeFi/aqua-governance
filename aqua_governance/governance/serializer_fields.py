import json

from rest_framework import serializers

from django_quill.quill import Quill

from aqua_governance.utils.payments import TRANSACTION_HASH_RE


# The model column is an unbounded TextField, so this is a denial-of-service bound rather
# than a column match: every pending proposal's text is hashed into a memo expectation on
# every sweep tick.
MAX_QUILL_HTML_LENGTH = 65536


class QuillField(serializers.Field):
    """HTML in, HTML out, stored as the ``{"delta": "", "html": ...}`` payload Quill parses.

    The input guards are load-bearing rather than defensive.  A value that is not a string,
    or that carries a surrogate code point, round-trips through PostgreSQL - the column
    stores ``json.dumps(..., ensure_ascii=True)`` - and only detonates later, on
    ``.encode('utf-8')`` at memo-build time, which v1 moves ahead of the Horizon call, on an
    unauthenticated path, for every pending proposal on every sweep tick.
    """

    def __init__(self, **kwargs):
        self.max_length = kwargs.pop('max_length', MAX_QUILL_HTML_LENGTH)
        super().__init__(**kwargs)

    def get_attribute(self, instance):
        return instance.text.html

    def to_representation(self, value):
        return value

    def to_internal_value(self, data):
        if not isinstance(data, str):
            raise serializers.ValidationError('This field must be a string.')
        if self.max_length is not None and len(data) > self.max_length:
            raise serializers.ValidationError(
                'This field must not exceed {0} characters.'.format(self.max_length),
            )
        try:
            data.encode('utf-8')
        except UnicodeEncodeError:
            raise serializers.ValidationError('This field must not contain surrogate characters.')
        return Quill(json.dumps({'delta': '', 'html': data}))


class TransactionHashField(serializers.RegexField):
    """A Stellar transaction hash: 64 hexadecimal characters, normalised to lowercase.

    Uppercase hex names the same transaction, so it is accepted - but accepting it without
    normalising would defeat the consumed-transaction ledger, which keys on the string.
    """

    def __init__(self, **kwargs):
        kwargs.setdefault('max_length', 64)
        kwargs.setdefault('trim_whitespace', True)
        super().__init__(TRANSACTION_HASH_RE.pattern, **kwargs)
        self.error_messages['invalid'] = 'Enter a valid 64-character hexadecimal transaction hash.'

    def to_internal_value(self, data):
        return super().to_internal_value(data).lower()
